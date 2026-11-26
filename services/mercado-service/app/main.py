"""Pontos de entrada do §5: `create_app` (testes), `executar_prazos` e `criar_app_real` (execução)."""
import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import fastapi

from app.dominio import TOPICOS_CONSUMIDOS, Mercado
from app.http import ContextoHttp, Metricas, configurar_http
from app.jsonio import instante
from app.portas import ChavesPublicas, Clock, ConfigMercado, EventBus, MercadoRepositorio

log = logging.getLogger("app.main")
PORTA = 8105
JWKS_PADRAO = "https://identity.matheus-alves.dev/.well-known/jwks.json"
CAMPOS_LOG = ("produtoId", "demandaId", "negociacaoId", "eventId")


def create_app(event_bus: EventBus, clock: Clock, repositorio: MercadoRepositorio, chaves_publicas: ChavesPublicas,
               config: ConfigMercado = ConfigMercado(), emissor: str = "identity-service",
               audiencia: str = "b2b-platform") -> fastapi.FastAPI:
    metricas = Metricas()
    mercado = Mercado(repositorio, event_bus, clock, config, ao_consumir=metricas.consumo)
    app = fastapi.FastAPI(title="Mercado — Portal B2B")
    app.state.mercado = mercado
    configurar_http(app, ContextoHttp(mercado, repositorio, event_bus, clock, chaves_publicas, emissor, audiencia,
                                      metricas))
    for topico in TOPICOS_CONSUMIDOS:
        event_bus.subscribe(topico, mercado.handler(topico))
    return app


def executar_prazos(app: fastapi.FastAPI) -> int:
    """Fecha as janelas de adesão vencidas e marca as demandas com prazo vencido em `clock.now()` (RN-33)."""
    return app.state.mercado.executar_prazos()


# ---------------------------------------------------------------- execução real

class RelogioSistema:
    """`Clock` real: UTC do sistema."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, segundos: float) -> None:
        time.sleep(segundos)


class FormatoJson(logging.Formatter):
    """Logs em JSON no stdout com as chaves da biblioteca comum do kit (§5.1)."""

    def format(self, registro: logging.LogRecord) -> str:
        linha = {
            "timestamp": instante(datetime.fromtimestamp(registro.created, timezone.utc)),
            "level": registro.levelname, "service": "mercado-service",
            "correlationId": getattr(registro, "correlationId", None), "message": registro.getMessage(),
        }
        for campo in CAMPOS_LOG:
            valor = getattr(registro, campo, None)
            if valor is not None:
                linha[campo] = valor
        if registro.exc_info:
            linha["stackTrace"] = self.formatException(registro.exc_info)
        return json.dumps(linha, ensure_ascii=False, default=str)


def configurar_logs(nivel: str):
    saida = logging.StreamHandler(sys.stdout)
    saida.setFormatter(FormatoJson())
    raiz = logging.getLogger()
    raiz.handlers[:] = [saida]
    raiz.setLevel(nivel.upper())
    for nome in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(nome).handlers[:] = []
        logging.getLogger(nome).propagate = True


def ler_config(ambiente) -> ConfigMercado:
    """`ConfigMercado` a partir das variáveis do §5; valor inválido impede a subida."""
    padrao = ConfigMercado()
    try:
        excesso = Decimal(ambiente.get("EXCESSO_DEMANDA_MINIMO_PERCENTUAL", str(padrao.excesso_demanda_minimo_percentual)))
        incremento = Decimal(ambiente.get("LEILAO_INCREMENTO_MINIMO_PERCENTUAL",
                                          str(padrao.leilao_incremento_minimo_percentual)))
        janela = int(ambiente.get("LEILAO_JANELA_ADESAO_MINUTOS", padrao.leilao_janela_adesao_minutos))
        duracao = int(ambiente.get("LEILAO_DURACAO_HORAS", padrao.leilao_duracao_horas))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Configuração de negócio inválida: {exc}") from exc
    return ConfigMercado(excesso, janela, duracao, incremento)


def criar_app_real() -> fastapi.FastAPI:
    """`uvicorn app.main:criar_app_real --factory --host 0.0.0.0 --port 8105`."""
    from app.adaptadores.jwks import ChavesJwks
    from app.adaptadores.kafka import EventBusKafka
    from app.adaptadores.postgres import RepositorioPostgres

    configurar_logs(os.getenv("LOG_LEVEL", "INFO"))
    config = ler_config(os.environ)
    clock = RelogioSistema()
    repositorio = RepositorioPostgres(os.environ["DATABASE_URL"])
    bus = EventBusKafka(repositorio, os.environ["KAFKA_BOOTSTRAP_SERVERS"],
                        os.getenv("KAFKA_GROUP_ID", "mercado-service-v1"), clock)
    app = create_app(bus, clock, repositorio, ChavesJwks(os.getenv("JWKS_URL", JWKS_PADRAO)), config,
                     os.getenv("JWT_ISSUER", "identity-service"), os.getenv("JWT_AUDIENCE", "b2b-platform"))
    metricas = app.state.http.metricas
    bus.ao_dlt = lambda topico: metricas.consumo(topico, "dlt")

    async def agendador():
        while True:
            try:
                await asyncio.to_thread(executar_prazos, app)
            except Exception:
                log.exception("Falha no agendador de prazos")
            await asyncio.to_thread(clock.sleep, 1)

    @asynccontextmanager
    async def ciclo_de_vida(_app):
        repositorio.pool.open(wait=False)
        tarefas = [asyncio.create_task(bus.run()), asyncio.create_task(agendador())]
        log.info("mercado-service iniciado na porta %s", PORTA)
        try:
            yield
        finally:
            for tarefa in tarefas:
                tarefa.cancel()
            await asyncio.gather(*tarefas, return_exceptions=True)
            repositorio.pool.close()

    app.router.lifespan_context = ciclo_de_vida
    return app
