import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from app.contratos import CONSUMIDOS, instante_txt
from app.http import configurar_http
from app.portas import ChavesPublicas, Clock, EventBus, PedidosRepositorio
from app.servico import Metricas, Servico

log = logging.getLogger("pedidos")


def create_app(event_bus: EventBus, clock: Clock, repositorio: PedidosRepositorio, chaves_publicas: ChavesPublicas,
               emissor: str = "identity-service", audiencia: str = "b2b-platform") -> FastAPI:
    app = FastAPI(title="Pedidos — Portal B2B", version="2.2")
    metricas = Metricas()
    servico = Servico(repositorio, event_bus, clock, metricas)
    app.state.event_bus = event_bus
    app.state.repositorio = repositorio
    app.state.chaves_publicas = chaves_publicas
    app.state.emissor = emissor
    app.state.audiencia = audiencia
    app.state.metricas = metricas
    app.state.servico = servico
    configurar_http(app)
    for topico in CONSUMIDOS:
        event_bus.subscribe(topico, servico.handler(topico))
    return app


def reconciliar(app: FastAPI) -> int:
    return app.state.servico.reconciliar()


class RelogioSistema:
    def now(self):
        return datetime.now(timezone.utc)

    def sleep(self, segundos):
        time.sleep(segundos)


class LogJson(logging.Formatter):
    EXTRAS = ("correlationId", "eventId", "pedidoId", "negociacaoId", "codigo")

    def format(self, registro):
        linha = {"instante": instante_txt(datetime.fromtimestamp(registro.created, timezone.utc)),
                 "nivel": registro.levelname, "servico": "pedidos-service",
                 "correlationId": getattr(registro, "correlationId", None), "mensagem": registro.getMessage()}
        for campo in self.EXTRAS[1:]:
            if getattr(registro, campo, None) is not None:
                linha[campo] = getattr(registro, campo)
        return json.dumps(linha, ensure_ascii=False, default=str)


def configurar_logs():
    saida = logging.StreamHandler(sys.stdout)
    saida.setFormatter(LogJson())
    raiz = logging.getLogger()
    raiz.handlers[:] = [saida]
    raiz.setLevel(os.getenv("LOG_LEVEL", "INFO"))


async def laco_reconciliar(app):
    while True:
        await asyncio.sleep(60)
        try:
            criados = await asyncio.to_thread(reconciliar, app)
            if criados:
                log.info("Reconciliação aplicou %s itens", criados)
        except Exception as exc:
            log.warning("Reconciliação adiada: %s", type(exc).__name__)


def criar_app_real() -> FastAPI:
    """Ponto de entrada do Uvicorn (--factory): adaptadores reais a partir do ambiente."""
    from app.adaptadores.jwks import ChavesJwks
    from app.adaptadores.kafka import KafkaEventBus
    from app.adaptadores.postgres import PostgresRepositorio

    configurar_logs()
    repositorio = PostgresRepositorio(os.environ["DATABASE_URL"])
    bus = KafkaEventBus(repositorio, os.environ["KAFKA_BOOTSTRAP_SERVERS"],
                        os.getenv("KAFKA_GROUP_ID", "pedidos-service-v1"))
    app = create_app(bus, RelogioSistema(), repositorio, ChavesJwks(os.environ["JWKS_URL"]),
                     os.getenv("JWT_ISSUER", "identity-service"), os.getenv("JWT_AUDIENCE", "b2b-platform"))
    bus.metricas = app.state.metricas

    @asynccontextmanager
    async def ciclo(_app):
        repositorio.abrir()
        tarefas = [asyncio.create_task(bus.executar()), asyncio.create_task(laco_reconciliar(app))]
        try:
            yield
        finally:
            for tarefa in tarefas:
                tarefa.cancel()
            await asyncio.gather(*tarefas, return_exceptions=True)
            repositorio.fechar()

    app.router.lifespan_context = ciclo
    return app
