import logging
import re
import time
from datetime import timezone
from uuid import uuid4

import jwt
from fastapi import Request
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app import contratos
from app.dominio import Ator, ErroNegocio, Idempotencia
from app.jsonio import dumps, fingerprint, loads
from app.portas import RepositorioIndisponivel

log = logging.getLogger(__name__)
SERVICO = "negociacao-service"
CORRELACAO_HTTP = re.compile(r"[A-Za-z0-9._-]{1,128}")
UUID_URL = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
ERROS = {
    "TOKEN_INVALIDO": (401, "Não autenticado", "Token de acesso ausente ou inválido."),
    "CONTEXTO_NAO_SELECIONADO": (403, "Acesso negado", "Selecione um contexto de empresa válido."),
    "ACESSO_NEGADO": (403, "Acesso negado", "Você não tem permissão para esta operação."),
    "NEGOCIACAO_NAO_ENCONTRADA": (404, "Não encontrado", "Negociação não encontrada."),
    "NEGOCIACAO_ENCERRADA": (409, "Conflito", "A negociação não está aberta."),
    "FORA_DA_VEZ": (409, "Conflito", "Aguarde a resposta da outra parte."),
    "COMPRA_IMEDIATA": (409, "Conflito",
                        "A compra pelo preço anunciado fecha sozinha quando o estoque é reservado."),
    "RESERVA_PENDENTE": (409, "Conflito", "A reserva do estoque ainda não foi confirmada."),
    "MODO_INCOMPATIVEL": (409, "Conflito", "Operação não disponível para o modo desta negociação."),
    "IDEMPOTENCIA_CONFLITO": (409, "Conflito", "A chave de idempotência já foi usada com outro corpo."),
    "VALIDACAO_INVALIDA": (422, "Entrada inválida", "Os dados da requisição são inválidos."),
    "PROPOSTA_ACIMA_DA_OFERTA": (422, "Entrada inválida", "A proposta não pode ser maior que o preço da oferta."),
    "ERRO_INTERNO": (500, "Erro interno", "Não foi possível processar a requisição."),
    "DEPENDENCIA_INDISPONIVEL": (503, "Serviço indisponível", "Uma dependência necessária está indisponível."),
}
MODOS = ("LEILAO_DIRETO", "VENDA_DIRETA")
STATUS = ("AGUARDANDO_RESERVAS", "ABERTO", "ENCERRADO", "DESERTO", "RECUSADO", "EXPIRADO")
# só documentam no /docs o cabeçalho e o corpo das escritas; a validação continua em `escrever`
CHAVE_OPENAPI = {"parameters": [{"name": "Idempotency-Key", "in": "header", "required": False,
                                 "schema": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,128}$"}}]}
PROPOSTA_OPENAPI = {**CHAVE_OPENAPI, "requestBody": {"required": True, "content": {"application/json": {"schema": {
    "type": "object", "additionalProperties": False, "required": ["precoUnitario"],
    "properties": {"precoUnitario": {"type": "number", "exclusiveMinimum": 0, "maximum": 999999999999.99}}}}}}}
# o PyJWT só confere a assinatura: expiração, emissor e audiência são comparados aqui, com o relógio da porta
OPCOES_JWT = {"verify_exp": False, "verify_nbf": False, "verify_iat": False, "verify_aud": False,
              "verify_iss": False}


def agora(app):
    instante = app.state.clock.now()
    # relógio sem fuso vale como UTC
    return instante if instante.tzinfo else instante.replace(tzinfo=timezone.utc)


def json_response(dados, status=200):
    return Response(dumps(dados), status_code=status, media_type="application/json")


def problema(request, codigo):
    status, titulo, detalhe = ERROS[codigo]
    correlacao = getattr(request.state, "correlacao", None) or str(uuid4())
    # o cabeçalho vai aqui porque o handler de Exception responde por fora do middleware
    cabecalhos = {"X-Correlation-Id": correlacao}
    if status == 401:
        cabecalhos["WWW-Authenticate"] = "Bearer"
    return Response(dumps({
        "type": "about:blank", "title": titulo, "status": status, "codigo": codigo,
        "detail": detalhe, "instance": request.url.path, "correlationId": correlacao,
        "timestamp": agora(request.app),
    }), status_code=status, media_type="application/problem+json", headers=cabecalhos)


def autenticar(request) -> Ator:
    """§7.1, passos 1 a 3. Todas as rotas desta versão aceitam BUYER e SUPPLIER."""
    estado = request.app.state
    esquema, _, token = request.headers.get("Authorization", "").partition(" ")
    if esquema.lower() != "bearer" or not token.strip():
        raise ErroNegocio("TOKEN_INVALIDO")
    try:
        cabeca = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise ErroNegocio("TOKEN_INVALIDO") from exc
    kid = cabeca.get("kid")
    if cabeca.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
        raise ErroNegocio("TOKEN_INVALIDO")
    try:
        chave = estado.chaves_publicas.obter(kid)
    except Exception as exc:
        raise ErroNegocio("DEPENDENCIA_INDISPONIVEL") from exc
    if chave is None:
        raise ErroNegocio("TOKEN_INVALIDO")
    try:
        claims = jwt.decode(token, chave, algorithms=["RS256"], options=OPCOES_JWT)
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        raise ErroNegocio("TOKEN_INVALIDO") from exc
    audiencia, sub, expira = claims.get("aud"), claims.get("sub"), claims.get("exp")
    if (claims.get("iss") != estado.emissor
            or not (audiencia == estado.audiencia or (isinstance(audiencia, list) and estado.audiencia in audiencia))
            or not isinstance(sub, str) or not sub.strip()
            or not isinstance(expira, int) or isinstance(expira, bool)
            or expira <= int(agora(request.app).timestamp())):
        raise ErroNegocio("TOKEN_INVALIDO")
    empresa, escopo = claims.get("tenant_id"), claims.get("active_scope")
    if claims.get("view_mode") != "SINGLE" or not isinstance(empresa, str) or not empresa.strip() or escopo is None:
        raise ErroNegocio("CONTEXTO_NAO_SELECIONADO")
    if escopo not in ("BUYER", "SUPPLIER"):
        raise ErroNegocio("ACESSO_NEGADO")
    return Ator(sub, empresa, escopo)


def pagina_e_tamanho(request):
    """§7.2: `page` inteiro ≥ 1 (padrão 1) e `size` de 1 a 100 (padrão 10)."""
    lidos = []
    for nome, padrao, maximo in (("page", 1, 2_147_483_647), ("size", 10, 100)):
        valor = request.query_params.get(nome)
        if valor is None:
            lidos.append(padrao)
        elif valor.isascii() and valor.isdigit() and 1 <= int(valor) <= maximo:
            lidos.append(int(valor))
        else:
            raise ErroNegocio("VALIDACAO_INVALIDA")
    return lidos


def filtro(request, nome, valores):
    valor = request.query_params.get(nome)
    if valor is not None and valor not in valores:
        raise ErroNegocio("VALIDACAO_INVALIDA")
    return valor


def negociacao_id(valor):
    if not UUID_URL.fullmatch(valor):
        raise ErroNegocio("VALIDACAO_INVALIDA")
    return valor.lower()


def chave_idempotencia(request):
    """§7.1: vazia equivale a ausente; mesmo formato do X-Correlation-Id."""
    chave = request.headers.get("Idempotency-Key", "")
    if chave and not CORRELACAO_HTTP.fullmatch(chave):
        raise ErroNegocio("VALIDACAO_INVALIDA")
    return chave or None


def ler_corpo(bruto, validar):
    """Devolve o JSON recebido (`None` sem corpo) e o valor validado."""
    try:
        dados = loads(bruto.decode("utf-8")) if bruto else None
        return dados, validar(dados)
    # EventoInvalido, JSON malformado e UTF-8 inválido são todos ValueError
    except (ValueError, RecursionError) as exc:
        raise ErroNegocio("VALIDACAO_INVALIDA") from exc


async def escrever(request, negociacao_url, acao, validar, status):
    """§10, passos 2 a 5; o serviço segue do passo 6. O corpo é lido à mão para não ser validado antes do token."""
    bruto = await request.body()

    def processar():
        ator = autenticar(request)
        identificador = negociacao_id(negociacao_url)
        chave = chave_idempotencia(request)
        dados, valor = ler_corpo(bruto, validar)
        rota = request.url.path.rsplit("/", 1)[1]
        idempotencia = None if chave is None else Idempotencia(
            dumps([ator.empresa, ator.usuario, "POST", f"/api/v1/negociacoes/{identificador}/{rota}", chave]),
            fingerprint(dados or {}), status)
        servico = request.app.state.servico
        return json_response(acao(servico, ator, identificador, valor, request.state.correlacao, idempotencia),
                             status)

    # banco e JWKS são síncronos: fora do laço de eventos
    return await run_in_threadpool(processar)


def configurar_http(app):
    # registro por aplicação: duas apps no mesmo processo não somam métricas
    registro = CollectorRegistry()
    requisicoes = Counter("http_requests_total", "Requisições HTTP.", ["metodo", "rota", "status"],
                          registry=registro)
    duracao = Histogram("http_request_duration_seconds", "Duração das requisições HTTP.", ["metodo", "rota"],
                        registry=registro)

    @app.middleware("http")
    async def observar(request, call_next):
        recebido = request.headers.get("X-Correlation-Id", "")
        request.state.correlacao = recebido if CORRELACAO_HTTP.fullmatch(recebido) else str(uuid4())
        inicio = time.perf_counter()
        status = 500
        try:
            resposta = await call_next(request)
            status = resposta.status_code
        finally:
            rota = request.scope.get("route")
            # caminho sem rota fica com rótulo fixo para não abrir uma série por URL
            modelo = rota.path if rota else "(sem rota)"
            requisicoes.labels(request.method, modelo, str(status)).inc()
            duracao.labels(request.method, modelo).observe(time.perf_counter() - inicio)
            # linha de acesso aqui, e não no uvicorn, para sair com o correlationId
            # saúde é verificada a cada 2 s por réplica: só em DEBUG, para não encher o log
            nivel = logging.DEBUG if request.url.path.startswith("/health") else logging.INFO
            log.log(nivel, "%s %s %s", request.method, request.url.path, status,
                    extra={"correlationId": request.state.correlacao})
        resposta.headers["X-Correlation-Id"] = request.state.correlacao
        return resposta

    @app.get("/health")
    def saude(request: Request):
        return json_response({"status": "UP", "service": SERVICO, "timestamp": agora(request.app)})

    @app.get("/health/ready")
    def prontidao(request: Request):
        estado = request.app.state
        if estado.repositorio.disponivel() and estado.event_bus.disponivel():
            return json_response({"status": "READY", "service": SERVICO})
        return problema(request, "DEPENDENCIA_INDISPONIVEL")

    @app.get("/metrics")
    def metricas():
        return Response(generate_latest(registro),
                        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"})

    # rotas síncronas: o FastAPI as roda fora do laço de eventos, junto com o banco e o JWKS
    @app.get("/api/v1/negociacoes")
    def listar(request: Request):
        ator = autenticar(request)
        pagina, tamanho = pagina_e_tamanho(request)
        modo, status = filtro(request, "modo", MODOS), filtro(request, "status", STATUS)
        return json_response(request.app.state.servico.listar(ator, modo, status, pagina, tamanho))

    @app.get("/api/v1/negociacoes/{negociacaoId}")
    def detalhe(negociacaoId: str, request: Request):
        ator = autenticar(request)
        return json_response(request.app.state.servico.detalhe(ator, negociacao_id(negociacaoId)))

    @app.get("/api/v1/negociacoes/{negociacaoId}/propostas")
    def propostas(negociacaoId: str, request: Request):
        ator = autenticar(request)
        identificador = negociacao_id(negociacaoId)
        pagina, tamanho = pagina_e_tamanho(request)
        return json_response(request.app.state.servico.propostas(ator, identificador, pagina, tamanho))

    @app.post("/api/v1/negociacoes/{negociacaoId}/propostas", openapi_extra=PROPOSTA_OPENAPI)
    async def propor(negociacaoId: str, request: Request):
        def acao(servico, ator, identificador, preco, correlacao, idempotencia):
            return servico.propor(ator, identificador, preco, correlacao, idempotencia)
        return await escrever(request, negociacaoId, acao, contratos.corpo_proposta, 201)

    @app.post("/api/v1/negociacoes/{negociacaoId}/aceitar", openapi_extra=CHAVE_OPENAPI)
    async def aceitar(negociacaoId: str, request: Request):
        def acao(servico, ator, identificador, _vazio, correlacao, idempotencia):
            return servico.aceitar(ator, identificador, correlacao, idempotencia)
        return await escrever(request, negociacaoId, acao, contratos.corpo_vazio, 200)

    @app.post("/api/v1/negociacoes/{negociacaoId}/recusar", openapi_extra=CHAVE_OPENAPI)
    async def recusar(negociacaoId: str, request: Request):
        def acao(servico, ator, identificador, _vazio, correlacao, idempotencia):
            return servico.recusar(ator, identificador, correlacao, idempotencia)
        return await escrever(request, negociacaoId, acao, contratos.corpo_vazio, 200)

    async def negocio(request, exc):
        return problema(request, exc.codigo)

    async def dependencia(request, _exc):
        return problema(request, "DEPENDENCIA_INDISPONIVEL")

    async def interno(request, _exc):
        return problema(request, "ERRO_INTERNO")

    app.add_exception_handler(ErroNegocio, negocio)
    app.add_exception_handler(RepositorioIndisponivel, dependencia)
    app.add_exception_handler(Exception, interno)
