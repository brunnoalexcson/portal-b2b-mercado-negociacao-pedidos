from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, ValidationError
from psycopg import Error as PostgresError
from psycopg_pool import PoolTimeout
from starlette.responses import Response

from app.adaptadores.postgres import ErroPedido
from app.contratos import CORRELACAO_HTTP, Id
from app.jsonio import dumps, loads

rota = APIRouter(prefix="/api/v1/pedidos")
bearer = HTTPBearer(auto_error=False, bearerFormat="JWT")

ERROS = {
    "TOKEN_INVALIDO": (401, "Não autenticado", "Token de acesso ausente ou inválido."),
    "CONTEXTO_NAO_SELECIONADO": (403, "Acesso negado", "Selecione um contexto de empresa válido."),
    "ACESSO_NEGADO": (403, "Acesso negado", "Você não tem permissão para esta operação."),
    "PEDIDO_NAO_ENCONTRADO": (404, "Não encontrado", "Pedido não encontrado."),
    "RESULTADO_INDISPONIVEL": (409, "Conflito", "A negociação ainda não tem vencedor registrado em Pedidos."),
    "DADOS_DO_PEDIDO_PENDENTES": (409, "Conflito", "Ainda faltam dados para registrar o pedido."),
    "IDEMPOTENCIA_CONFLITO": (409, "Conflito", "A chave de idempotência já foi usada com outro corpo."),
    "VALIDACAO_INVALIDA": (422, "Entrada inválida", "Os dados da requisição são inválidos."),
    "DEPENDENCIA_INDISPONIVEL": (503, "Serviço indisponível", "Uma dependência necessária está indisponível."),
    "ERRO_INTERNO": (500, "Erro interno", "Não foi possível processar a requisição."),
}
Pagina = Annotated[int, Query(ge=1)]
Tamanho = Annotated[int, Query(ge=1, le=100)]
Status = Literal["CRIADO", "AGUARDANDO_FRETE", "FRETE_SELECIONADO", "VENDA_CONCLUIDA", "ENTREGUE", "CANCELADO"]


class PedidoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    negociacaoId: Id


def json_response(dados, status=200, headers=None):
    return Response(dumps(dados), status_code=status, media_type="application/json", headers=headers)


def problema(request, codigo):
    status, titulo, detalhe = ERROS[codigo]
    correlacao = getattr(request.state, "correlacao", str(uuid4()))
    headers = {"X-Correlation-Id": correlacao}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return Response(dumps({
        "type": "about:blank", "title": titulo, "status": status, "codigo": codigo,
        "detail": detalhe, "instance": request.url.path, "correlationId": correlacao,
        "timestamp": datetime.now(timezone.utc),
    }), status_code=status, media_type="application/problem+json", headers=headers)


def ator_autenticado(request: Request, credenciais=Depends(bearer)):
    return request.app.state.identidade.autenticar(credenciais.credentials if credenciais else None)


def operador(ator=Depends(ator_autenticado)):
    if not ator.admin:
        raise ErroPedido("ACESSO_NEGADO")
    return ator


@rota.post("", status_code=201, openapi_extra={
    "requestBody": {"required": True, "content": {"application/json": {"schema": PedidoBody.model_json_schema()}}},
})
async def criar(request: Request, ator=Depends(operador)):
    try:
        entrada = PedidoBody.model_validate(loads((await request.body()).decode("utf-8")))
    except (ValueError, ValidationError, RecursionError) as exc:
        raise ErroPedido("VALIDACAO_INVALIDA") from exc
    chave = request.headers.get("Idempotency-Key")
    if chave is not None and not CORRELACAO_HTTP.fullmatch(chave):
        raise ErroPedido("VALIDACAO_INVALIDA")
    status, pedido = await request.app.state.banco.criar_http(
        entrada.negociacaoId, ator, request.state.correlacao, chave,
    )
    headers = {"Location": f"/api/v1/pedidos/{pedido['pedidoId']}"} if status == 201 else None
    return json_response(pedido, status, headers)


@rota.get("")
async def listar(request: Request, ator=Depends(ator_autenticado), page: Pagina = 1, size: Tamanho = 10,
                 status: Status | None = None, negociacaoId: Id | None = None):
    return json_response(await request.app.state.banco.listar(ator, page, size, status, negociacaoId))


@rota.get("/{pedido_id}")
async def obter(pedido_id: UUID, request: Request, ator=Depends(ator_autenticado)):
    return json_response(await request.app.state.banco.consultar(pedido_id, ator))


@rota.get("/{pedido_id}/historico")
async def historico(pedido_id: UUID, request: Request, ator=Depends(ator_autenticado),
                    page: Pagina = 1, size: Tamanho = 10):
    return json_response(await request.app.state.banco.consultar(pedido_id, ator, page, size))


def configurar_http(app):
    @app.middleware("http")
    async def correlacionar(request, call_next):
        recebido = request.headers.get("X-Correlation-Id", "")
        request.state.correlacao = recebido if CORRELACAO_HTTP.fullmatch(recebido) else str(uuid4())
        resposta = await call_next(request)
        resposta.headers["X-Correlation-Id"] = request.state.correlacao
        return resposta

    async def erro_pedido(request, exc):
        return problema(request, exc.codigo)

    async def erro_validacao(request, _exc):
        return problema(request, "VALIDACAO_INVALIDA")

    async def erro_dependencia(request, _exc):
        return problema(request, "DEPENDENCIA_INDISPONIVEL")

    async def erro_interno(request, _exc):
        return problema(request, "ERRO_INTERNO")

    app.add_exception_handler(ErroPedido, erro_pedido)
    app.add_exception_handler(RequestValidationError, erro_validacao)
    app.add_exception_handler(PoolTimeout, erro_dependencia)
    app.add_exception_handler(PostgresError, erro_dependencia)
    app.add_exception_handler(Exception, erro_interno)
    app.include_router(rota)
