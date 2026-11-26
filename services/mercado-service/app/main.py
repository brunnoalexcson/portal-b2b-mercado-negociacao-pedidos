"""Pontos de entrada do §5: `create_app` (testes) e `executar_prazos`."""
import fastapi

from app.dominio import TOPICOS_CONSUMIDOS, Mercado
from app.http import ContextoHttp, Metricas, configurar_http
from app.portas import ChavesPublicas, Clock, ConfigMercado, EventBus, MercadoRepositorio


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
