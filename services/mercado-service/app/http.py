"""HTTP: rotas do §7.2, autenticação (§7.1), erros RFC 7807 (§10), correlação, idempotência (RN-30) e métricas."""
import logging
import re
import time
from dataclasses import dataclass
from uuid import uuid4

import jwt
from fastapi import APIRouter, FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app import contratos
from app.contratos import MAX_QUANTIDADE, UUID_TEXTO, EventoInvalido
from app.dominio import ErroMercado, Idempotencia, Mercado
from app.jsonio import dumps, fingerprint, loads
from app.portas import ChavesPublicas, Clock, EventBus, MercadoRepositorio, RepositorioIndisponivel

log = logging.getLogger("app.http")

PREFIXO = "/api/v1/mercado"
ID_HTTP = re.compile(r"[A-Za-z0-9._-]{1,64}")
CHAVE_HTTP = re.compile(r"[A-Za-z0-9._-]{1,128}")
INTEIRO = re.compile(r"[0-9]{1,9}")   # page e size: até 9 dígitos (§7.2)
MAX_DURACAO_HORAS = 999999   # DG-70: sem máximo de negócio; o limite só evita estourar a data (≈ 114 anos)
COMPRADOR, FORNECEDOR = ("BUYER",), ("SUPPLIER",)
AMBOS = ("BUYER", "SUPPLIER")

ERROS = {
    "TOKEN_INVALIDO": (401, "Não autenticado", "Token de acesso ausente ou inválido."),
    "CONTEXTO_NAO_SELECIONADO": (403, "Acesso negado", "Selecione um contexto de empresa válido."),
    "ACESSO_NEGADO": (403, "Acesso negado", "Você não tem permissão para esta operação."),
    "CONFLITO_DE_INTERESSE": (403, "Acesso negado", "A empresa não pode negociar com uma oferta própria."),
    "OFERTA_DE_OUTRA_EMPRESA": (403, "Acesso negado", "A oferta pertence a outra empresa."),
    "PRODUTO_NAO_ENCONTRADO": (404, "Não encontrado", "Produto não encontrado."),
    "DEMANDA_NAO_ENCONTRADA": (404, "Não encontrado", "Demanda não encontrada."),
    "NEGOCIACAO_NAO_ENCONTRADA": (404, "Não encontrado", "Negociação não encontrada."),
    "SUGESTAO_NAO_ENCONTRADA": (404, "Não encontrado", "Sugestão de leilão não encontrada."),
    "ACEITE_NAO_ENCONTRADO": (404, "Não encontrado", "Aceite não encontrado."),
    "DEMANDA_ENCERRADA": (409, "Conflito", "A demanda não está aguardando oferta ou está com o prazo vencido."),
    "NEGOCIACAO_JA_ABERTA": (409, "Conflito", "Já existe uma negociação aberta para esta demanda e esta oferta."),
    "SUGESTAO_ENCERRADA": (409, "Conflito", "A sugestão de leilão não aceita mais mudanças."),
    "ACEITE_JA_REGISTRADO": (409, "Conflito", "Esta oferta já está no lote."),
    "FORNECEDOR_EM_LEILAO": (409, "Conflito", "A empresa já tem um leilão aberto deste produto."),
    "IDEMPOTENCIA_CONFLITO": (409, "Conflito", "A chave de idempotência já foi usada com outro corpo."),
    "VALIDACAO_INVALIDA": (422, "Entrada inválida", "Os dados da requisição são inválidos."),
    "OFERTA_INDISPONIVEL": (422, "Entrada inválida", "A oferta não existe ou está com preço ou quantidade zerados."),
    "PRODUTO_DIFERENTE": (422, "Entrada inválida", "A oferta ou a demanda é de outro produto."),
    "PRECO_ACIMA_DO_MAXIMO": (422, "Entrada inválida", "O preço da oferta é maior que o preço máximo da demanda."),
    "QUANTIDADE_ACIMA_DO_SALDO": (422, "Entrada inválida", "A quantidade é maior que o saldo negociável da demanda."),
    "QUANTIDADE_INDISPONIVEL": (422, "Entrada inválida", "A quantidade é maior que o disponível livre da oferta."),
    "ERRO_INTERNO": (500, "Erro interno", "Não foi possível processar a requisição."),
    "DEPENDENCIA_INDISPONIVEL": (503, "Serviço indisponível", "Uma dependência necessária está indisponível."),
}


@dataclass(frozen=True)
class Ator:
    empresa: str
    escopo: object
    sub: str


@dataclass
class ContextoHttp:
    mercado: Mercado
    repositorio: MercadoRepositorio
    event_bus: EventBus
    clock: Clock
    chaves: ChavesPublicas
    emissor: str
    audiencia: str
    metricas: "Metricas"


class Metricas:
    """Métricas do §5.1 num registro próprio da aplicação (sem estado de módulo)."""

    def __init__(self):
        self.registro = CollectorRegistry()
        self.requisicoes = Counter("mercado_http_requisicoes_total", "Requisições HTTP",
                                   ("rota", "metodo", "status"), registry=self.registro)
        self.latencia = Histogram("mercado_http_latencia_segundos", "Latência HTTP", ("rota", "metodo"),
                                  registry=self.registro)
        self.consumidos = Counter("mercado_eventos_consumidos_total", "Mensagens consumidas",
                                  ("topico", "resultado"), registry=self.registro)
        self.outbox = Gauge("mercado_outbox_pendentes", "Registros pendentes na outbox", registry=self.registro)
        self.sugestoes = Gauge("mercado_sugestoes_ativas", "Sugestões de leilão ativas", registry=self.registro)

    def consumo(self, topico: str, resultado: str):
        self.consumidos.labels(topico, resultado).inc()


# ---------------------------------------------------------------- respostas

def json_response(dados, status=200, headers=None) -> Response:
    return Response(dumps(dados), status_code=status, media_type="application/json", headers=headers)


def problema(contexto: ContextoHttp, request: Request, codigo: str) -> Response:
    status, titulo, detalhe = ERROS[codigo]
    correlacao = getattr(request.state, "correlacao", None) or str(uuid4())
    headers = {"X-Correlation-Id": correlacao}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return Response(dumps({
        "type": "about:blank", "title": titulo, "status": status, "codigo": codigo, "detail": detalhe,
        "instance": request.url.path, "correlationId": correlacao, "timestamp": contexto.mercado.agora(),
    }), status_code=status, media_type="application/problem+json", headers=headers)


# ---------------------------------------------------------------- autenticação (§7.1)

def autenticar(contexto: ContextoHttp, cabecalho: str | None) -> Ator:
    if not cabecalho or not cabecalho.startswith("Bearer ") or not cabecalho[7:].strip():
        raise ErroMercado("TOKEN_INVALIDO")
    token = cabecalho[7:].strip()
    try:
        cabecalho_token = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise ErroMercado("TOKEN_INVALIDO") from exc
    kid = cabecalho_token.get("kid")
    if cabecalho_token.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
        raise ErroMercado("TOKEN_INVALIDO")
    try:
        chave = contexto.chaves.obter(kid)
    except Exception as exc:
        raise ErroMercado("DEPENDENCIA_INDISPONIVEL") from exc
    if chave is None:
        raise ErroMercado("TOKEN_INVALIDO")
    try:
        claims = jwt.decode(token, chave, algorithms=["RS256"], options={"verify_exp": False},
                            issuer=contexto.emissor, audience=contexto.audiencia)
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        raise ErroMercado("TOKEN_INVALIDO") from exc
    sub, exp = claims.get("sub"), claims.get("exp")
    if not isinstance(sub, str) or not sub.strip():
        raise ErroMercado("TOKEN_INVALIDO")
    if type(exp) is not int or exp <= int(contexto.mercado.agora().timestamp()):
        raise ErroMercado("TOKEN_INVALIDO")
    tenant, escopo = claims.get("tenant_id"), claims.get("active_scope")
    if (claims.get("view_mode") != "SINGLE" or not isinstance(tenant, str) or not tenant.strip()
            or len(tenant) > 64 or escopo is None):
        raise ErroMercado("CONTEXTO_NAO_SELECIONADO")
    return Ator(tenant, escopo, sub)


# ---------------------------------------------------------------- validação de entrada (§7.2)

def _invalida():
    raise ErroMercado("VALIDACAO_INVALIDA")


def validar_id(valor: str) -> str:
    if not isinstance(valor, str) or not ID_HTTP.fullmatch(valor):
        _invalida()
    return valor


def validar_uuid(valor: str) -> str:
    if not UUID_TEXTO.fullmatch(valor):
        _invalida()
    return valor.lower()


def paginacao(request: Request) -> tuple[int, int]:
    resultado = []
    for nome, padrao, maximo in (("page", 1, None), ("size", 10, 100)):
        texto = request.query_params.get(nome)
        if texto is None:
            resultado.append(padrao)
            continue
        if not INTEIRO.fullmatch(texto):
            _invalida()
        valor = int(texto)
        if valor < 1 or (maximo is not None and valor > maximo):
            _invalida()
        resultado.append(valor)
    return resultado[0], resultado[1]


def filtro(request: Request, nome: str, valores: tuple[str, ...]) -> str | None:
    valor = request.query_params.get(nome)
    if valor is not None and valor not in valores:
        _invalida()
    return valor


def chave_idempotencia(request: Request) -> str | None:
    chave = request.headers.get("Idempotency-Key")
    if chave is None or chave == "":
        return None
    if not CHAVE_HTTP.fullmatch(chave):
        _invalida()
    return chave


def ler_corpo(bruto: bytes, obrigatorios: tuple[str, ...], opcionais: tuple[str, ...] = ()) -> dict:
    try:
        dados = loads(bruto.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        _invalida()
    if not isinstance(dados, dict):
        _invalida()
    if any(campo not in dados for campo in obrigatorios) or set(dados) - set(obrigatorios) - set(opcionais):
        _invalida()
    return dados


def quantidade(valor):
    try:
        return contratos.validar_numero(valor, "quantidade", positivo=True, maximo=MAX_QUANTIDADE)
    except EventoInvalido:
        _invalida()


def duracao_leilao(valor) -> int | None:
    """DG-70: inteiro de 1 a MAX_DURACAO_HORAS (`null` é inválido); o campo ausente usa a duração padrão."""
    if isinstance(valor, bool) or not isinstance(valor, int) or not 1 <= valor <= MAX_DURACAO_HORAS:
        _invalida()
    return valor


def idempotencia(ator: Ator, metodo: str, caminho: str, chave: str | None, corpo: dict) -> Idempotencia | None:
    if chave is None:
        return None
    escopo = dumps([ator.empresa, ator.sub, metodo, caminho, chave])
    return Idempotencia(escopo, fingerprint(corpo), 201)


# ---------------------------------------------------------------- execução de uma rota de negócio

async def executar(request: Request, rota: str, escopos: tuple[str, ...], acao, com_corpo=False) -> Response:
    request.state.rota = rota
    corpo = await request.body() if com_corpo else b""
    return await run_in_threadpool(_processar, request, escopos, acao, corpo)


def _processar(request: Request, escopos, acao, corpo: bytes) -> Response:
    contexto: ContextoHttp = request.app.state.http
    try:
        ator = autenticar(contexto, request.headers.get("Authorization"))
        if ator.escopo not in escopos:
            raise ErroMercado("ACESSO_NEGADO")
        return acao(contexto, ator, corpo)
    except ErroMercado as erro:
        return problema(contexto, request, erro.codigo)
    except RepositorioIndisponivel:
        log.warning("Banco indisponível", extra={"correlationId": request.state.correlacao})
        return problema(contexto, request, "DEPENDENCIA_INDISPONIVEL")
    except Exception:
        log.exception("Falha inesperada", extra={"correlationId": request.state.correlacao})
        return problema(contexto, request, "ERRO_INTERNO")


# ---------------------------------------------------------------- rotas

def _rotas() -> APIRouter:
    rota = APIRouter(prefix=PREFIXO)

    def corpo_openapi(obrigatorios, propriedades):
        return {"requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object", "additionalProperties": False, "required": list(obrigatorios),
            "properties": propriedades}}}}}

    id_schema = {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,64}$"}
    qtd_schema = {"type": "number", "exclusiveMinimum": 0, "maximum": 9999999999.99}

    def query_openapi(*parametros):
        # Só documenta os parâmetros de query no /docs; a validação continua no serviço (§7.2, §10).
        return {"parameters": [{"name": nome, "in": "query", "required": False, "description": descricao,
                                "schema": schema} for nome, schema, descricao in parametros]}

    pagina_openapi = (
        ("page", {"type": "integer", "minimum": 1, "maximum": 999999999, "default": 1}, "Página, a partir de 1."),
        ("size", {"type": "integer", "minimum": 1, "maximum": 100, "default": 10}, "Itens por página, até 100."))

    @rota.get("/produtos/{produtoId}/balanco")
    async def balanco(request: Request, produtoId: str):
        def acao(ctx, ator, _):
            return json_response(ctx.mercado.balanco(validar_id(produtoId)))
        return await executar(request, "/api/v1/mercado/produtos/{produtoId}/balanco", AMBOS, acao)

    @rota.get("/produtos/{produtoId}/ofertas", openapi_extra=query_openapi(
        ("demandaId", id_schema, "Só com escopo BUYER: demanda própria para calcular `apta` (RN-07)."),
        *pagina_openapi))
    async def ofertas(request: Request, produtoId: str):
        def acao(ctx, ator, _):
            demanda = request.query_params.get("demandaId")
            if demanda is not None and ator.escopo != "BUYER":
                _invalida()
            produto = validar_id(produtoId)
            if demanda is not None:
                validar_id(demanda)
            pagina, tamanho = paginacao(request)
            return json_response(ctx.mercado.vitrine(produto, ator.empresa, demanda, pagina, tamanho))
        return await executar(request, "/api/v1/mercado/produtos/{produtoId}/ofertas", AMBOS, acao)

    @rota.get("/demandas/{demandaId}")
    async def demanda(request: Request, demandaId: str):
        def acao(ctx, ator, _):
            return json_response(ctx.mercado.demanda(ator.empresa, validar_id(demandaId)))
        return await executar(request, "/api/v1/mercado/demandas/{demandaId}", COMPRADOR, acao)

    @rota.post("/negociacoes", status_code=201, openapi_extra=corpo_openapi(
        ("demandaId", "fornecimentoId", "quantidade"),
        {"demandaId": id_schema, "fornecimentoId": id_schema, "quantidade": qtd_schema,
         "compraImediata": {"type": "boolean"}}))
    async def abrir(request: Request):
        def acao(ctx, ator, bruto):
            chave = chave_idempotencia(request)
            dados = ler_corpo(bruto, ("demandaId", "fornecimentoId", "quantidade"), ("compraImediata",))
            demanda_id, oferta_id = validar_id(dados["demandaId"]), validar_id(dados["fornecimentoId"])
            qtd = quantidade(dados["quantidade"])
            compra = dados.get("compraImediata", False)
            if not isinstance(compra, bool):
                _invalida()
            resposta = ctx.mercado.abrir_direta(
                ator.empresa, demanda_id, oferta_id, qtd, compra, request.state.correlacao,
                idempotencia(ator, "POST", f"{PREFIXO}/negociacoes", chave, dados))
            return json_response(resposta, 201,
                                 {"Location": f"{PREFIXO}/negociacoes/{resposta['negociacaoId']}"})
        return await executar(request, "/api/v1/mercado/negociacoes", COMPRADOR, acao, com_corpo=True)

    @rota.get("/negociacoes", openapi_extra=query_openapi(
        ("modo", {"type": "string", "enum": ["VENDA_DIRETA", "LEILAO_DIRETO"]}, "Filtro opcional."),
        ("situacao", {"type": "string", "enum": ["ABERTA", "CONCLUIDA", "SEM_ACORDO"]}, "Filtro opcional."),
        *pagina_openapi))
    async def listar(request: Request):
        def acao(ctx, ator, _):
            modo = filtro(request, "modo", ("VENDA_DIRETA", "LEILAO_DIRETO"))
            situacao = filtro(request, "situacao", ("ABERTA", "CONCLUIDA", "SEM_ACORDO"))
            pagina, tamanho = paginacao(request)
            return json_response(ctx.mercado.negociacoes(ator.empresa, ator.escopo, modo, situacao,
                                                         pagina, tamanho))
        return await executar(request, "/api/v1/mercado/negociacoes", AMBOS, acao)

    @rota.get("/negociacoes/{negociacaoId}")
    async def detalhe(request: Request, negociacaoId: str):
        def acao(ctx, ator, _):
            return json_response(ctx.mercado.negociacao(ator.empresa, ator.escopo, validar_uuid(negociacaoId)))
        return await executar(request, "/api/v1/mercado/negociacoes/{negociacaoId}", AMBOS, acao)

    @rota.get("/sugestoes-leilao", openapi_extra=query_openapi(*pagina_openapi))
    async def sugestoes(request: Request):
        def acao(ctx, ator, _):
            pagina, tamanho = paginacao(request)
            return json_response(ctx.mercado.sugestoes(ator.empresa, pagina, tamanho))
        return await executar(request, "/api/v1/mercado/sugestoes-leilao", FORNECEDOR, acao)

    @rota.post("/sugestoes-leilao/{sugestaoId}/aceites", status_code=201, openapi_extra=corpo_openapi(
        ("fornecimentoId", "quantidade"), {"fornecimentoId": id_schema, "quantidade": qtd_schema,
                                           "duracaoLeilaoHoras": {"type": "integer", "minimum": 1,
                                                                  "maximum": MAX_DURACAO_HORAS}}))
    async def aceitar(request: Request, sugestaoId: str):
        def acao(ctx, ator, bruto):
            sugestao = validar_uuid(sugestaoId)
            chave = chave_idempotencia(request)
            dados = ler_corpo(bruto, ("fornecimentoId", "quantidade"), ("duracaoLeilaoHoras",))
            oferta_id, qtd = validar_id(dados["fornecimentoId"]), quantidade(dados["quantidade"])
            duracao = duracao_leilao(dados["duracaoLeilaoHoras"]) if "duracaoLeilaoHoras" in dados else None
            caminho = f"{PREFIXO}/sugestoes-leilao/{sugestao}/aceites"
            resposta = ctx.mercado.aceitar(ator.empresa, sugestao, oferta_id, qtd, request.state.correlacao,
                                           idempotencia(ator, "POST", caminho, chave, dados), duracao)
            return json_response(resposta, 201)
        return await executar(request, "/api/v1/mercado/sugestoes-leilao/{sugestaoId}/aceites", FORNECEDOR,
                              acao, com_corpo=True)

    @rota.put("/sugestoes-leilao/{sugestaoId}/aceites/{fornecimentoId}", openapi_extra=corpo_openapi(
        ("quantidade",), {"quantidade": qtd_schema}))
    async def alterar(request: Request, sugestaoId: str, fornecimentoId: str):
        def acao(ctx, ator, bruto):
            sugestao, oferta_id = validar_uuid(sugestaoId), validar_id(fornecimentoId)
            qtd = quantidade(ler_corpo(bruto, ("quantidade",))["quantidade"])
            return json_response(ctx.mercado.alterar_aceite(ator.empresa, sugestao, oferta_id, qtd,
                                                            request.state.correlacao))
        return await executar(request, "/api/v1/mercado/sugestoes-leilao/{sugestaoId}/aceites/{fornecimentoId}",
                              FORNECEDOR, acao, com_corpo=True)

    @rota.delete("/sugestoes-leilao/{sugestaoId}/aceites/{fornecimentoId}", status_code=204)
    async def remover(request: Request, sugestaoId: str, fornecimentoId: str):
        def acao(ctx, ator, _):
            sugestao, oferta_id = validar_uuid(sugestaoId), validar_id(fornecimentoId)
            ctx.mercado.remover_aceite(ator.empresa, sugestao, oferta_id, request.state.correlacao)
            return Response(status_code=204)
        return await executar(request, "/api/v1/mercado/sugestoes-leilao/{sugestaoId}/aceites/{fornecimentoId}",
                              FORNECEDOR, acao)

    return rota


# ---------------------------------------------------------------- saúde, métricas e montagem

def configurar_http(app: FastAPI, contexto: ContextoHttp):
    app.state.http = contexto

    @app.middleware("http")
    async def correlacionar(request: Request, call_next):
        recebido = request.headers.get("X-Correlation-Id", "")
        request.state.correlacao = recebido if CHAVE_HTTP.fullmatch(recebido) else str(uuid4())
        request.state.rota = "desconhecida"
        inicio = time.perf_counter()
        resposta = await call_next(request)
        resposta.headers["X-Correlation-Id"] = request.state.correlacao
        rota = request.state.rota
        contexto.metricas.requisicoes.labels(rota, request.method, str(resposta.status_code)).inc()
        contexto.metricas.latencia.labels(rota, request.method).observe(time.perf_counter() - inicio)
        return resposta

    @app.get("/health")
    async def health(request: Request):
        request.state.rota = "/health"
        return json_response({"status": "up"})

    def pronto() -> bool:
        try:
            return bool(contexto.repositorio.disponivel()) and bool(contexto.event_bus.disponivel())
        except Exception:
            return False

    @app.get("/health/ready")
    async def ready(request: Request):
        request.state.rota = "/health/ready"
        if await run_in_threadpool(pronto):
            return json_response({"status": "ready"})
        return problema(contexto, request, "DEPENDENCIA_INDISPONIVEL")

    def atualizar_medidores():
        pendentes = getattr(contexto.event_bus, "pendentes", None)
        try:
            contexto.metricas.outbox.set(pendentes() if callable(pendentes) else 0)
        except Exception:
            log.warning("Não foi possível medir a outbox")
        try:
            with contexto.repositorio.transacao():
                contexto.metricas.sugestoes.set(len(contexto.repositorio.listar_sugestoes_ativas()))
        except Exception:
            log.warning("Não foi possível medir as sugestões ativas")

    @app.get("/metrics")
    async def metrics(request: Request):
        request.state.rota = "/metrics"
        await run_in_threadpool(atualizar_medidores)
        return Response(generate_latest(contexto.metricas.registro), headers={"Content-Type": CONTENT_TYPE_LATEST})

    app.include_router(_rotas())
