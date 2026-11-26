"""Rotas, autenticação (§7.1), erros RFC 7807 (§10), correlação e idempotência."""
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from uuid import uuid4

import jwt
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app import contratos
from app.contratos import Invalido
from app.jsonio import dumps, loads
from app.portas import RepositorioIndisponivel
from app.servico import ErroHttp

log = logging.getLogger("pedidos")

ERROS = {
    "TOKEN_INVALIDO": (401, "Não autenticado", "Token de acesso ausente ou inválido."),
    "CONTEXTO_NAO_SELECIONADO": (403, "Acesso negado", "Selecione um contexto de empresa válido."),
    "ACESSO_NEGADO": (403, "Acesso negado", "Você não tem permissão para esta operação."),
    "PEDIDO_NAO_ENCONTRADO": (404, "Não encontrado", "Pedido não encontrado."),
    "TRANSICAO_INVALIDA": (409, "Conflito", "O estado atual do pedido não permite essa transição."),
    "IDEMPOTENCIA_CONFLITO": (409, "Conflito", "A chave de idempotência já foi usada com outro corpo."),
    "VALIDACAO_INVALIDA": (422, "Entrada inválida", "Os dados da requisição são inválidos."),
    "ERRO_INTERNO": (500, "Erro interno", "Não foi possível processar a requisição."),
    "DEPENDENCIA_INDISPONIVEL": (503, "Serviço indisponível", "Uma dependência necessária está indisponível."),
}
LEITURA = ("BUYER", "SUPPLIER", "TENANT_ADMIN")
Token = Annotated[HTTPAuthorizationCredentials | None,
                  Depends(HTTPBearer(auto_error=False, bearerFormat="JWT",
                                     description="JWT RS256 do Identity-Service (na demo: localhost:18100/tokens)"))]


def Texto(descricao):
    return Annotated[str | None, Query(description=descricao)]


OPCOES_JWT = {"verify_exp": False, "verify_nbf": False, "verify_iat": False, "verify_aud": False,
              "verify_iss": False}


@dataclass(frozen=True)
class Ator:
    usuario: str
    empresa: str | None
    escopo: str | None
    suporte: bool


def json_response(dados, status=200):
    return Response(dumps(dados), status_code=status, media_type="application/json; charset=utf-8")


def problema(request, codigo):
    status, titulo, detalhe = ERROS[codigo]
    correlacao = getattr(request.state, "correlacao", None) or str(uuid4())
    corpo = {"type": "about:blank", "title": titulo, "status": status, "codigo": codigo, "detail": detalhe,
             "instance": request.url.path, "correlationId": correlacao,
             "timestamp": contratos.instante_txt(request.app.state.servico.agora())}
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return Response(dumps(corpo), status_code=status, media_type="application/problem+json; charset=utf-8",
                    headers=headers)


def _numero(valor):
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def autenticar(request, escopos, suporte_permitido=True):
    """§7.1, passos 1 a 3."""
    estado = request.app.state
    cabecalho = request.headers.get("Authorization", "")
    esquema, _, token = cabecalho.partition(" ")
    token = token.strip()
    if esquema.lower() != "bearer" or not token:
        raise ErroHttp("TOKEN_INVALIDO")
    try:
        cabeca = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise ErroHttp("TOKEN_INVALIDO") from exc
    kid = cabeca.get("kid")
    if cabeca.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
        raise ErroHttp("TOKEN_INVALIDO")
    try:
        chave = estado.chaves_publicas.obter(kid)
    except Exception as exc:
        raise ErroHttp("DEPENDENCIA_INDISPONIVEL") from exc
    if chave is None:
        raise ErroHttp("TOKEN_INVALIDO")
    try:
        claims = jwt.decode(token, chave, algorithms=["RS256"], options=OPCOES_JWT)
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        raise ErroHttp("TOKEN_INVALIDO") from exc
    agora = int(estado.servico.agora().timestamp())
    audiencia = claims.get("aud")
    sub = claims.get("sub")
    if (claims.get("iss") != estado.emissor
            or not (audiencia == estado.audiencia or (isinstance(audiencia, list) and estado.audiencia in audiencia))
            or not isinstance(sub, str) or not sub.strip()
            or not _numero(claims.get("exp")) or claims["exp"] <= agora
            or ("nbf" in claims and (not _numero(claims["nbf"]) or claims["nbf"] > agora))):
        raise ErroHttp("TOKEN_INVALIDO")
    if claims.get("platform_role") == "PLATFORM_ADMIN":
        if not suporte_permitido:
            raise ErroHttp("ACESSO_NEGADO")
        return Ator(sub, None, None, True)
    empresa, escopo = claims.get("tenant_id"), claims.get("active_scope")
    if (not isinstance(empresa, str) or not empresa.strip() or escopo is None
            or ("view_mode" in claims and claims["view_mode"] != "SINGLE")):
        raise ErroHttp("CONTEXTO_NAO_SELECIONADO")
    if escopo not in escopos:
        raise ErroHttp("ACESSO_NEGADO")
    return Ator(sub, empresa, escopo, False)


def _query(request, permitidos):
    """§7.2: parâmetro desconhecido, repetido ou fora do formato → 422."""
    itens = request.query_params.multi_items()
    nomes = [nome for nome, _ in itens]
    if len(nomes) != len(set(nomes)) or set(nomes) - set(permitidos):
        raise ErroHttp("VALIDACAO_INVALIDA")
    valores = dict(itens)
    filtros = {"page": 1, "size": 10, "status": None, "negociacaoId": None}
    for nome, valor in valores.items():
        if nome in ("page", "size"):
            if not valor.isascii() or not valor.isdigit():
                raise ErroHttp("VALIDACAO_INVALIDA")
            numero = int(valor)
            if numero < 1 or (nome == "size" and numero > 100) or numero > 2_147_483_647:
                raise ErroHttp("VALIDACAO_INVALIDA")
            filtros[nome] = numero
        elif nome == "status":
            if valor not in contratos.STATUS:
                raise ErroHttp("VALIDACAO_INVALIDA")
            filtros[nome] = valor
        elif nome == "negociacaoId":
            if not valor.strip() or len(valor) > 64:
                raise ErroHttp("VALIDACAO_INVALIDA")
            filtros[nome] = valor
    return filtros


def _pedido_id(valor):
    if not contratos.UUID_RE.fullmatch(valor):
        raise ErroHttp("VALIDACAO_INVALIDA")
    return valor.lower()


def _listar(request):
    ator = autenticar(request, LEITURA)
    f = _query(request, ("page", "size", "status", "negociacaoId"))
    return request.app.state.servico.listar(ator, f["status"], f["negociacaoId"], f["page"], f["size"])


def _detalhe(request, pedido_id):
    ator = autenticar(request, LEITURA)
    _query(request, ())
    return request.app.state.servico.detalhe(ator, _pedido_id(pedido_id))


def _historico(request, pedido_id):
    ator = autenticar(request, LEITURA)
    f = _query(request, ("page", "size"))
    return request.app.state.servico.historico(ator, _pedido_id(pedido_id), f["page"], f["size"])


def _destino(request, pedido_id, corpo):
    ator = autenticar(request, ("BUYER",), suporte_permitido=False)
    _query(request, ())
    pedido_id = _pedido_id(pedido_id)
    chave = request.headers.get("Idempotency-Key", "")
    if chave and not contratos.CORRELACAO_RE.fullmatch(chave):
        raise ErroHttp("VALIDACAO_INVALIDA")
    try:
        dados, destino, data_desejada = contratos.validar_destino(corpo, loads)
    except Invalido as exc:
        raise ErroHttp("VALIDACAO_INVALIDA") from exc
    escopo = None
    if chave:  # RN-24
        escopo = "|".join((ator.usuario, ator.empresa, ator.escopo, "PUT",
                           f"/api/v1/pedidos/{pedido_id}/destino", chave))
    hash_corpo = hashlib.sha256(json.dumps(dados, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False).encode("utf-8")).hexdigest()
    status, resposta = request.app.state.servico.informar_destino(
        ator, pedido_id, destino, data_desejada, escopo, hash_corpo, request.state.correlacao)
    return json_response(resposta, status)


def configurar_http(app: FastAPI):
    @app.middleware("http")
    async def envolver(request: Request, call_next):
        recebido = request.headers.get("X-Correlation-Id", "")
        request.state.correlacao = recebido if contratos.CORRELACAO_RE.fullmatch(recebido) else str(uuid4())
        inicio = time.perf_counter()
        try:
            resposta = await call_next(request)
        except RepositorioIndisponivel:
            resposta = problema(request, "DEPENDENCIA_INDISPONIVEL")
        except Exception as exc:
            log.error("Erro inesperado na API: %s", type(exc).__name__,
                      extra={"correlationId": request.state.correlacao})
            resposta = problema(request, "ERRO_INTERNO")
        resposta.headers["X-Correlation-Id"] = request.state.correlacao
        rota = request.scope.get("route")
        nome = getattr(rota, "path", "desconhecida")
        metricas = request.app.state.metricas
        metricas.http.labels(nome, request.method, str(resposta.status_code)).inc()
        metricas.latencia.labels(nome, request.method).observe(time.perf_counter() - inicio)
        return resposta

    @app.exception_handler(ErroHttp)
    async def erro_http(request, exc):
        return problema(request, exc.codigo)

    # Os parâmetros abaixo só documentam a API no Swagger: são todos texto opcional, então o FastAPI nunca
    # responde 422 antes da autenticação (§10); a validação de verdade é a das funções _listar, _destino etc.
    @app.get("/health", tags=["Saúde"], summary="API-01 — Vivacidade")
    async def health():
        return json_response({"status": "up"})

    @app.get("/health/ready", tags=["Saúde"], summary="API-02 — Prontidão (banco e Kafka)")
    def ready(request: Request):
        estado = request.app.state

        def ok(porta):
            try:
                return bool(porta.disponivel())
            except Exception:
                return False

        pronto = ok(estado.repositorio) and ok(estado.event_bus)
        return json_response({"status": "ready" if pronto else "not_ready"}, 200 if pronto else 503)

    @app.get("/metrics", tags=["Saúde"], summary="API-03 — Métricas Prometheus")
    async def metrics(request: Request):
        return Response(generate_latest(request.app.state.metricas.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/v1/pedidos", tags=["Pedidos"], summary="API-04 — Listar pedidos visíveis",
             description="BUYER vê os pedidos em que é comprador; SUPPLIER, os de que é fornecedor; "
                         "TENANT_ADMIN, os dois; suporte (PLATFORM_ADMIN), todos. CARRIER → 403.")
    async def listar(request: Request, _token: Token = None, page: Texto("Página, a partir de 1") = None,
                     size: Texto("Itens por página, de 1 a 100") = None,
                     status: Texto("CRIADO, VENDA_CONCLUIDA, AGUARDANDO_FRETE, FRETE_SELECIONADO, ENTREGUE ou "
                                   "CANCELADO") = None,
                     negociacaoId: Texto("Filtra por negociação") = None):
        return json_response(await run_in_threadpool(_listar, request))

    @app.get("/api/v1/pedidos/{pedido_id}", tags=["Pedidos"], summary="API-05 — Detalhe do pedido")
    async def detalhe(pedido_id: str, request: Request, _token: Token = None):
        return json_response(await run_in_threadpool(_detalhe, request, pedido_id))

    @app.get("/api/v1/pedidos/{pedido_id}/historico", tags=["Pedidos"], summary="API-06 — Histórico de versões")
    async def historico(pedido_id: str, request: Request, _token: Token = None,
                        page: Texto("Página, a partir de 1") = None, size: Texto("Itens por página, de 1 a 100") = None):
        return json_response(await run_in_threadpool(_historico, request, pedido_id))

    @app.put("/api/v1/pedidos/{pedido_id}/destino", tags=["Pedidos"], summary="API-07 — Comprador informa o destino",
             description="Só BUYER comprador do pedido, em VENDA_CONCLUIDA. Repetir o mesmo destino responde 200 "
                         "sem versão nova; outro destino → 409. Data no passado → 422.",
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": {"type": "object", "additionalProperties": False, "required": ["cidade", "uf"],
                            "properties": {"cidade": {"type": "string", "maxLength": 255},
                                           "uf": {"type": "string", "example": "GO"},
                                           "dataEntregaDesejada": {"type": "string", "format": "date"}}},
                 "example": {"cidade": "Anápolis", "uf": "GO", "dataEntregaDesejada": "2026-12-20"}}}}})
    async def destino(pedido_id: str, request: Request, _token: Token = None,
                      idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key",
                                                                    description="Opcional (RN-24)")] = None,
                      x_correlation_id: Annotated[str | None, Header(alias="X-Correlation-Id")] = None):
        corpo = await request.body()
        return await run_in_threadpool(_destino, request, pedido_id, corpo)
