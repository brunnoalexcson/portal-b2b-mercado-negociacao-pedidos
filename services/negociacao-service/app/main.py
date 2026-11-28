import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from app.dominio import Negociacoes
from app.http import SERVICO, configurar_http
from app.jsonio import formato_json
from app.portas import ChavesPublicas, Clock, EventBus, NegociacaoRepositorio

log = logging.getLogger(__name__)


def create_app(event_bus: EventBus, clock: Clock, repositorio: NegociacaoRepositorio,
               chaves_publicas: ChavesPublicas, emissor: str = "identity-service",
               audiencia: str = "b2b-platform") -> FastAPI:
    app = FastAPI(title="Negociação e Leilão — Portal B2B")
    app.state.event_bus = event_bus
    app.state.clock = clock
    app.state.repositorio = repositorio
    app.state.chaves_publicas = chaves_publicas
    app.state.emissor = emissor
    app.state.audiencia = audiencia
    app.state.servico = Negociacoes(repositorio, event_bus, clock)
    for topico in ("leilao_iniciado", "modo_negociacao_definido", "pedido_atualizado"):
        event_bus.subscribe(topico, app.state.servico.handler(topico))
    configurar_http(app)
    return app


def executar_prazos(app: FastAPI) -> int:
    """RN-21: processa os prazos vencidos em `clock.now()` e devolve quantas negociações mudaram de estado."""
    return app.state.servico.executar_prazos()


class RelogioSistema:
    def now(self):
        return datetime.now(timezone.utc)

    def sleep(self, segundos):
        time.sleep(segundos)


class LogJson(logging.Formatter):
    def format(self, registro):
        dados = {
            "timestamp": formato_json(datetime.fromtimestamp(registro.created, timezone.utc)),
            "level": registro.levelname, "service": SERVICO,
            "correlationId": getattr(registro, "correlationId", None), "message": registro.getMessage(),
        }
        for campo in ("negociacaoId", "eventId"):
            if hasattr(registro, campo):
                dados[campo] = getattr(registro, campo)
        if registro.exc_info:
            dados["stackTrace"] = self.formatException(registro.exc_info)
        return json.dumps(dados, ensure_ascii=False)


async def agendar(app):
    while True:
        try:
            await asyncio.to_thread(executar_prazos, app)
        except Exception:
            log.exception("Falha ao processar os prazos")
        await asyncio.sleep(1)


def criar_app_real() -> FastAPI:
    # import aqui: create_app e os testes em memória não dependem de psycopg nem de aiokafka
    from app.adaptadores.jwks import ChavesJwks
    from app.adaptadores.kafka import Kafka
    from app.adaptadores.postgres import Postgres

    saida = logging.StreamHandler(sys.stdout)
    saida.setFormatter(LogJson())
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=[saida], force=True)
    for nome in ("uvicorn", "uvicorn.error"):
        logging.getLogger(nome).handlers.clear()
        logging.getLogger(nome).propagate = True
    # o acesso é registrado pelo middleware HTTP, com o correlationId
    logging.getLogger("uvicorn.access").disabled = True

    repositorio = Postgres(os.environ["DATABASE_URL"])
    event_bus = Kafka(repositorio, os.environ["KAFKA_BOOTSTRAP_SERVERS"],
                      os.getenv("KAFKA_GROUP_ID", "negociacao-service-v1"))
    app = create_app(
        event_bus, RelogioSistema(), repositorio,
        # sem JWKS_URL (o docker-compose.aws.yml do kit não injeta), usa o Identity na rede interna da AWS
        ChavesJwks(os.getenv("JWKS_URL", "http://usuarios-service:8101/.well-known/jwks.json")),
        os.getenv("JWT_ISSUER", "identity-service"), os.getenv("JWT_AUDIENCE", "b2b-platform"),
    )

    @asynccontextmanager
    async def ciclo(_app):
        tarefas = [asyncio.create_task(event_bus.run()), asyncio.create_task(agendar(app))]
        try:
            yield
        finally:
            for tarefa in tarefas:
                tarefa.cancel()
            await asyncio.gather(*tarefas, return_exceptions=True)
            repositorio.fechar()

    app.router.lifespan_context = ciclo
    return app
