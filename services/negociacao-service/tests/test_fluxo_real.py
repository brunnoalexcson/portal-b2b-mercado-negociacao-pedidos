"""Fluxo real contra a stack de `compose.demo.yml`: Redpanda, PostgreSQL, outbox e duas réplicas atrás do gateway."""
import asyncio
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import psycopg
import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from app.jsonio import dumps, formato_json, loads

pytestmark = pytest.mark.skipif(os.getenv("NEGOCIACAO_DEMO") != "1", reason="só no compose.demo.yml")

API = os.getenv("NEGOCIACAO_API_URL", "http://gateway:8080")
IDENTITY = os.getenv("NEGOCIACAO_IDENTITY_URL", "http://identity:8000")
KAFKA = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "broker:9092")
DSN = os.getenv("DATABASE_URL", "")
SUFIXO = uuid4().hex[:8]
D = Decimal

COMPRADOR = f"comprador-a-{SUFIXO}"
FORNECEDOR = f"fornecedor-x-{SUFIXO}"
PRODUTO = f"milho-{SUFIXO}"
OFERTA = f"oferta-x-{SUFIXO}"
DEMANDA = f"demanda-a-{SUFIXO}"


def evento(topico: str, fonte: str, chave: str, payload: dict, event_id: str | None = None) -> dict:
    return {
        "eventId": event_id or str(uuid4()),
        "eventType": topico,
        "eventVersion": "1.0",
        "timestamp": formato_json(datetime.now(timezone.utc)),
        "source": fonte,
        "correlationId": f"fluxo-{SUFIXO}",
        "aggregateId": chave,
        "payload": payload,
    }


def modo_direta(negociacao_id: str, compra_imediata: bool) -> dict:
    return evento("modo_negociacao_definido", "mercado-service", negociacao_id, {
        "negociacaoId": negociacao_id,
        "modo": "VENDA_DIRETA",
        "produtoId": PRODUTO,
        "unidade": "TON",
        "quantidadeTotal": D("10.00"),
        "valorReferencia": D("1300.00"),
        "demandas": [{
            "demandaId": DEMANDA,
            "demandaOrigemId": None,
            "compradorId": COMPRADOR,
            "quantidade": D("10.00"),
            "precoMaximo": D("135.00"),
            "destino": {"cidade": "Anápolis", "uf": "GO"},
            "prazoDesejado": None,
        }],
        "ofertas": [{
            "fornecimentoId": OFERTA,
            "fornecedorId": FORNECEDOR,
            "quantidade": D("10.00"),
            "precoUnitario": D("130.00"),
            "valorItem": D("1300.00"),
            "participacao": D("1.0000"),
            "origem": {"cidade": "Jataí", "uf": "GO"},
        }],
        "compraImediata": compra_imediata,
        "definidaEm": formato_json(datetime.now(timezone.utc)),
    })


def confirmacao_reserva(negociacao_id: str, pedido_id: str, event_id: str | None = None) -> dict:
    return evento("pedido_atualizado", "pedidos-service", pedido_id, {
        "pedidoId": pedido_id,
        "negociacaoId": negociacao_id,
        "fornecimentoId": OFERTA,
        "status": "CRIADO",
        "statusAnterior": "CRIADO",
        "reservaSituacao": "CONFIRMADA",
        "versao": 2,
    }, event_id=event_id)


async def _publicar(mensagens):
    produtor = AIOKafkaProducer(bootstrap_servers=KAFKA, acks="all")
    await produtor.start()
    try:
        for mensagem in mensagens:
            await produtor.send_and_wait(
                mensagem["eventType"], dumps(mensagem).encode(), key=mensagem["aggregateId"].encode(),
            )
    finally:
        await produtor.stop()


def publicar(*mensagens):
    asyncio.run(_publicar(mensagens))


async def _ler(topico: str, filtro, limite: float = 30.0, esperar_extras: float = 0.0):
    consumidor = AIOKafkaConsumer(topico, bootstrap_servers=KAFKA, group_id=None, auto_offset_reset="earliest")
    await consumidor.start()
    encontrados = []
    prazo_extra = None
    try:
        fim = asyncio.get_running_loop().time() + limite
        while asyncio.get_running_loop().time() < fim:
            lote = await consumidor.getmany(timeout_ms=500)
            for mensagens in lote.values():
                for mensagem in mensagens:
                    valor = loads(mensagem.value.decode("utf-8"))
                    if filtro(valor):
                        encontrados.append((mensagem.key.decode("utf-8"), valor))
            if encontrados:
                if esperar_extras <= 0:
                    return encontrados
                if prazo_extra is None:
                    prazo_extra = asyncio.get_running_loop().time() + esperar_extras
                elif asyncio.get_running_loop().time() >= prazo_extra:
                    return encontrados
        raise AssertionError(f"Nenhuma mensagem encontrada em {topico} em {limite}s")
    finally:
        await consumidor.stop()


def ler(topico: str, filtro, limite: float = 30.0, esperar_extras: float = 0.0):
    return asyncio.run(_ler(topico, filtro, limite, esperar_extras))


def token(empresa: str, escopo: str = "BUYER") -> str:
    resposta = httpx.post(f"{IDENTITY}/token", params={"empresa": empresa, "escopo": escopo}, timeout=5)
    resposta.raise_for_status()
    return resposta.json()["access_token"]


def get_negociacao(negociacao_id: str, jwt_token: str):
    return httpx.get(
        f"{API}/api/v1/negociacoes/{negociacao_id}",
        headers={"Authorization": f"Bearer {jwt_token}"},
        timeout=5,
    )


def esperar(condicao, limite: float = 30.0, intervalo: float = 0.5):
    fim = time.monotonic() + limite
    ultimo = None
    while time.monotonic() < fim:
        try:
            ultimo = condicao()
            if ultimo:
                return ultimo
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            ultimo = exc
        time.sleep(intervalo)
    raise AssertionError(f"Condição não atendida em {limite}s: {ultimo!r}")


@pytest.fixture(scope="module", autouse=True)
def aguardar_prontidao():
    esperar(lambda: httpx.get(f"{API}/health/ready", timeout=3).status_code == 200, limite=60)


def test_abertura_e_confirmacao_de_reserva_na_direta():
    negociacao_id = str(uuid4())
    jwt_comprador = token(COMPRADOR, "BUYER")
    publicar(modo_direta(negociacao_id, compra_imediata=False))

    detalhe = esperar(
        lambda: loads(r.text) if (r := get_negociacao(negociacao_id, jwt_comprador)).status_code == 200 else None,
    )
    assert (detalhe["status"], detalhe["reservaSituacao"], detalhe["compraImediata"]) == ("ABERTO", "PENDENTE", False)
    assert detalhe["acoesPermitidas"] == ["PROPOR", "RECUSAR"]

    [(chave, solicitado)] = ler(
        "pedido_temporario_solicitado", lambda v: v.get("aggregateId") == negociacao_id,
    )
    assert chave == negociacao_id
    assert solicitado["payload"] == {
        "negociacaoId": negociacao_id,
        "tipo": "VENDA_DIRETA",
        "fornecimentoId": OFERTA,
        "fornecedorId": FORNECEDOR,
        "produtoId": PRODUTO,
        "quantidade": D("10.00"),
        "precoUnitario": D("130.00"),
    }

    publicar(confirmacao_reserva(negociacao_id, str(uuid4())))
    confirmada = esperar(
        lambda: d if (d := loads(get_negociacao(negociacao_id, jwt_comprador).text))["reservaSituacao"] == "CONFIRMADA"
        else None,
    )
    assert confirmada["acoesPermitidas"] == ["PROPOR", "ACEITAR", "RECUSAR"]


def test_compra_imediata_com_cinco_entregas_repetidas():
    negociacao_id = str(uuid4())
    jwt_comprador = token(COMPRADOR, "BUYER")
    publicar(modo_direta(negociacao_id, compra_imediata=True))

    esperar(lambda: get_negociacao(negociacao_id, jwt_comprador).status_code == 200)

    pedido_id = str(uuid4())
    event_id = str(uuid4())
    confirmacao = confirmacao_reserva(negociacao_id, pedido_id, event_id=event_id)
    publicar(*([confirmacao] * 5))

    encerrada = esperar(
        lambda: d if (d := loads(get_negociacao(negociacao_id, jwt_comprador).text))["status"] == "ENCERRADO"
        else None,
    )
    assert (encerrada["reservaSituacao"], encerrada["acoesPermitidas"]) == ("CONFIRMADA", [])

    fechamentos = ler(
        "negociacao_fechada", lambda v: v.get("aggregateId") == negociacao_id, esperar_extras=1.5,
    )
    assert len(fechamentos) == 1
    chave, fechada = fechamentos[0]
    assert chave == negociacao_id
    assert fechada["payload"] == {
        "negociacaoId": negociacao_id,
        "tipo": "VENDA_DIRETA",
        "status": "ENCERRADO",
        "compradorId": COMPRADOR,
        "demandaId": None,
        "produtoId": PRODUTO,
        "alocacoes": [{
            "fornecimentoId": OFERTA,
            "fornecedorId": FORNECEDOR,
            "pedidoId": pedido_id,
            "quantidade": D("10.00"),
            "valorUnitario": D("130.00"),
            "valorTotal": D("1300.00"),
        }],
    }

    with psycopg.connect(DSN) as conexao:
        total_processados = conexao.execute(
            "SELECT count(*) FROM negociacao.processed_events WHERE source = 'pedidos-service' AND event_id = %s",
            (event_id,),
        ).fetchone()[0]
        assert total_processados == 1
