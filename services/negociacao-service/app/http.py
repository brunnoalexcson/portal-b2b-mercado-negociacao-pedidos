import logging
import re
import time
from datetime import timezone
from uuid import uuid4

from fastapi import Request
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from starlette.responses import Response

from app.jsonio import dumps
from app.portas import RepositorioIndisponivel

log = logging.getLogger(__name__)
SERVICO = "negociacao-service"
CORRELACAO_HTTP = re.compile(r"[A-Za-z0-9._-]{1,128}")
ERROS = {
    "ERRO_INTERNO": (500, "Erro interno", "Não foi possível processar a requisição."),
    "DEPENDENCIA_INDISPONIVEL": (503, "Serviço indisponível", "Uma dependência necessária está indisponível."),
}


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
    return Response(dumps({
        "type": "about:blank", "title": titulo, "status": status, "codigo": codigo,
        "detail": detalhe, "instance": request.url.path, "correlationId": correlacao,
        "timestamp": agora(request.app),
    }), status_code=status, media_type="application/problem+json", headers={"X-Correlation-Id": correlacao})


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

    async def dependencia(request, _exc):
        return problema(request, "DEPENDENCIA_INDISPONIVEL")

    async def interno(request, _exc):
        return problema(request, "ERRO_INTERNO")

    app.add_exception_handler(RepositorioIndisponivel, dependencia)
    app.add_exception_handler(Exception, interno)
