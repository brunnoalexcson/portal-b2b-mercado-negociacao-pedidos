import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg import Error as PostgresError
from psycopg_pool import PoolTimeout
from starlette.responses import Response

from app.adaptadores.jwks import Identidade
from app.adaptadores.kafka import Kafka
from app.adaptadores.postgres import Postgres
from app.http import configurar_http, json_response

log = logging.getLogger(__name__)


async def reconciliar(banco):
    while True:
        try:
            if not await banco.reavaliar():
                await asyncio.sleep(0.5)
        except (PostgresError, PoolTimeout) as exc:
            log.warning("Reavaliação de pedidos pendente: %s", type(exc).__name__)
            await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app):
    banco = Postgres(os.environ["DATABASE_URL"])
    kafka = Kafka(banco, os.environ["KAFKA_BOOTSTRAP_SERVERS"],
                  os.getenv("KAFKA_GROUP_ID", "pedidos-service-v1"))
    app.state.banco = banco
    app.state.kafka = kafka
    app.state.identidade = Identidade(
        os.environ["JWKS_URL"], os.getenv("JWT_ISSUER", "identity-service"),
        os.getenv("JWT_AUDIENCE", "b2b-platform"),
    )
    await banco.pool.open()
    tarefas = [asyncio.create_task(kafka.run()), asyncio.create_task(reconciliar(banco))]
    try:
        yield
    finally:
        for tarefa in tarefas:
            tarefa.cancel()
        await asyncio.gather(*tarefas, return_exceptions=True)
        await banco.pool.close()


app = FastAPI(title="Pedidos — Portal B2B", lifespan=lifespan)
configurar_http(app)


@app.get("/health")
async def health():
    return {"status": "up"}


@app.get("/health/ready")
async def readiness(request: Request):
    try:
        if request.app.state.kafka.pronto() and await request.app.state.banco.pronto():
            return json_response({"status": "ready"})
    except (PostgresError, PoolTimeout):
        pass
    return json_response({"status": "not_ready"}, 503)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})
