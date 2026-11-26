"""Fluxo real no `compose.demo.yml`: Kafka → PostgreSQL → outbox → Kafka e API pelo gateway com duas réplicas.

Roda só com `MERCADO_DEMO=1` (serviço `teste` do Compose)."""
import asyncio
import os
import time
from decimal import Decimal
from uuid import uuid4

import httpx
import psycopg
import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from app.jsonio import dumps, loads

pytestmark = pytest.mark.skipif(os.getenv("MERCADO_DEMO") != "1", reason="só no compose.demo.yml")

API = os.getenv("MERCADO_API_URL", "http://gateway:8080")
IDENTITY = os.getenv("MERCADO_IDENTITY_URL", "http://identity:8000")
KAFKA = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "broker:9092")
DSN = os.getenv("DATABASE_URL", "")
RODADA = uuid4().hex[:8]
D = Decimal


def ident(prefixo):
    return f"{prefixo}-{RODADA}"


COMPRADOR, FORN_X, FORN_Y = ident("comp-a"), ident("forn-x"), ident("forn-y")
PRODUTO, PRODUTO_LOTE = ident("prd"), ident("prd-lote")
OFERTA, OFERTA_LOTE = ident("of-x"), ident("of-y")
DEMANDA, DEMANDA_LOTE = ident("dem-a"), ident("dem-b")


def token(empresa, escopo):
    resposta = httpx.post(f"{IDENTITY}/token", params={"empresa": empresa, "escopo": escopo}, timeout=5)
    resposta.raise_for_status()
    return resposta.json()["access_token"]


def chamar(metodo, caminho, empresa, escopo, **kwargs):
    headers = {"Authorization": f"Bearer {token(empresa, escopo)}", **kwargs.pop("headers", {})}
    return httpx.request(metodo, f"{API}{caminho}", headers=headers, timeout=10, **kwargs)


def esperar(condicao, limite=60, intervalo=0.5):
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


def envelope(topico, fonte, agregado, payload, event_id=None):
    return {"eventId": event_id or str(uuid4()), "eventType": topico, "eventVersion": "1.0",
            "timestamp": "2026-11-20T12:00:00.000Z", "source": fonte, "correlationId": f"fluxo-{RODADA}",
            "aggregateId": agregado, "payload": payload}


async def _publicar(mensagens):
    produtor = AIOKafkaProducer(bootstrap_servers=KAFKA, acks="all")
    await produtor.start()
    try:
        for topico, chave, valor in mensagens:
            dados = valor if isinstance(valor, bytes) else dumps(valor).encode()
            await produtor.send_and_wait(topico, dados, key=chave.encode())
    finally:
        await produtor.stop()


def publicar(*mensagens):
    asyncio.run(_publicar(mensagens))


async def _ler(topico, filtro, limite):
    consumidor = AIOKafkaConsumer(topico, bootstrap_servers=KAFKA, group_id=None, auto_offset_reset="earliest")
    await consumidor.start()
    encontrados = []
    try:
        fim = asyncio.get_running_loop().time() + limite
        while asyncio.get_running_loop().time() < fim:
            lote = await consumidor.getmany(timeout_ms=1000)
            for mensagens in lote.values():
                for mensagem in mensagens:
                    valor = loads(mensagem.value.decode())
                    if filtro(valor):
                        encontrados.append((mensagem.key.decode(), valor))
            if encontrados:
                return encontrados
        raise AssertionError(f"Nada em {topico} em {limite}s")
    finally:
        await consumidor.stop()


def ler(topico, filtro, limite=30):
    return asyncio.run(_ler(topico, filtro, limite))


def produto(produto_id):
    return ("produto_cadastrado", produto_id, envelope("produto_cadastrado", "produtos-service", produto_id, {
        "produtoId": produto_id, "nome": "Milho em grão", "unidade": "TON",
        "cadastradoEm": "2026-11-20T11:00:00.000Z"}))


def oferta(oferta_id, fornecedor, produto_id, preco, quantidade):
    return ("fornecimento_criado", oferta_id, envelope("fornecimento_criado", "fornecimentos-service", oferta_id, {
        "fornecimentoId": oferta_id, "fornecedorId": fornecedor, "produtoId": produto_id, "unidade": "TON",
        "preco": D(preco), "quantidadeDisponivel": D(quantidade), "origem": {"cidade": "Rio Verde", "uf": "GO"},
        "criadoEm": "2026-11-20T11:40:00.000Z"}))


def demanda(demanda_id, produto_id, quantidade, event_id=None):
    return ("demanda_criada", demanda_id, envelope("demanda_criada", "demanda-service", demanda_id, {
        "demandaId": demanda_id, "compradorId": COMPRADOR, "produtoId": produto_id, "unidade": "TON",
        "quantidade": D(quantidade), "precoMaximo": D("9.00"), "destino": {"cidade": "Anápolis", "uf": "GO"},
        "prazoDesejado": None, "tipo": "UNICA", "criadoEm": "2026-11-20T11:55:00.000Z"}, event_id))


@pytest.fixture(scope="module", autouse=True)
def pronto():
    esperar(lambda: httpx.get(f"{API}/health/ready", timeout=3).status_code == 200, limite=120)


def balanco(produto_id):
    return loads(chamar("GET", f"/api/v1/mercado/produtos/{produto_id}/balanco", COMPRADOR, "BUYER").text)


def test_publico_e_autenticacao():
    assert httpx.get(f"{API}/health", timeout=3).json() == {"status": "up"}
    resposta = httpx.get(f"{API}/api/v1/mercado/produtos/{PRODUTO}/balanco", timeout=3)
    assert resposta.status_code == 401 and resposta.headers["www-authenticate"] == "Bearer"
    assert "mercado_http_requisicoes_total" in httpx.get(f"{API}/metrics", timeout=3).text


def test_direta_ponta_a_ponta_com_entrega_repetida():
    duplicada = demanda(DEMANDA, PRODUTO, "50.00", event_id=str(uuid4()))
    publicar(produto(PRODUTO), oferta(OFERTA, FORN_X, PRODUTO, "7.50", "40.00"), *([duplicada] * 5))
    esperar(lambda: balanco(PRODUTO).get("demandaTotal") == D("50.00"))
    with psycopg.connect(DSN) as conexao:
        assert conexao.execute("SELECT count(*) FROM mercado.processed_events WHERE event_id=%s",
                               (duplicada[2]["eventId"],)).fetchone()[0] == 1
        assert conexao.execute("SELECT count(*) FROM mercado.demanda WHERE demanda_id=%s",
                               (DEMANDA,)).fetchone()[0] == 1
    corpo = {"demandaId": DEMANDA, "fornecimentoId": OFERTA, "quantidade": 30.00}
    cabecalhos = {"X-Correlation-Id": f"direta-{RODADA}", "Idempotency-Key": f"k-{RODADA}"}
    resposta = chamar("POST", "/api/v1/mercado/negociacoes", COMPRADOR, "BUYER", json=corpo, headers=cabecalhos)
    assert resposta.status_code == 201, resposta.text
    repetida = chamar("POST", "/api/v1/mercado/negociacoes", COMPRADOR, "BUYER", json=corpo, headers=cabecalhos)
    assert (repetida.status_code, repetida.text) == (201, resposta.text)
    negociacao = resposta.json()["negociacaoId"]
    assert resposta.headers["location"] == f"/api/v1/mercado/negociacoes/{negociacao}"
    eventos = ler("modo_negociacao_definido", lambda v: v.get("aggregateId") == negociacao)
    assert len(eventos) == 1
    chave, evento = eventos[0]
    assert chave == negociacao and evento["correlationId"] == f"direta-{RODADA}"
    assert evento["payload"]["valorReferencia"] == D("225.00")
    print(f"\nDireta {negociacao}: evento {evento['eventId']} publicado pela outbox")

    publicar(("negociacao_fechada", negociacao, envelope("negociacao_fechada", "negociacao-service", negociacao, {
        "negociacaoId": negociacao, "tipo": "VENDA_DIRETA", "status": "ENCERRADO", "compradorId": COMPRADOR,
        "demandaId": None, "produtoId": PRODUTO,
        "alocacoes": [{"fornecimentoId": OFERTA, "quantidade": D("30.00")}]})))
    esperar(lambda: loads(chamar("GET", f"/api/v1/mercado/demandas/{DEMANDA}", COMPRADOR, "BUYER").text)
            ["saldo"] == D("20.00"))
    detalhe = chamar("GET", f"/api/v1/mercado/negociacoes/{negociacao}", FORN_X, "SUPPLIER").json()
    assert detalhe["situacao"] == "CONCLUIDA"


def test_lote_fecha_e_publica_modo_e_leilao_iniciado():
    publicar(produto(PRODUTO_LOTE), oferta(OFERTA_LOTE, FORN_Y, PRODUTO_LOTE, "5.00", "10.00"),
             demanda(DEMANDA_LOTE, PRODUTO_LOTE, "20.00"))
    sugestao = esperar(lambda: balanco(PRODUTO_LOTE).get("sugestaoId"))
    itens = chamar("GET", "/api/v1/mercado/sugestoes-leilao?size=100", FORN_Y, "SUPPLIER").json()["items"]
    assert sugestao in [item["sugestaoId"] for item in itens]
    resposta = chamar("POST", f"/api/v1/mercado/sugestoes-leilao/{sugestao}/aceites", FORN_Y, "SUPPLIER",
                      json={"fornecimentoId": OFERTA_LOTE, "quantidade": 10}, headers={"X-Correlation-Id":
                                                                                    f"lote-{RODADA}"})
    assert resposta.status_code == 201, resposta.text
    negociacao = resposta.json()["negociacaoId"]
    assert resposta.json()["situacao"] == "CONVERTIDA" and negociacao
    [(_, modo)] = ler("modo_negociacao_definido", lambda v: v.get("aggregateId") == negociacao)
    [(chave, leilao)] = ler("leilao_iniciado", lambda v: v.get("aggregateId") == negociacao)
    assert modo["payload"]["modo"] == "LEILAO_DIRETO" and chave == negociacao
    assert (leilao["payload"]["precoInicial"], leilao["payload"]["incrementoMinimo"]) == (D("50.00"), D("0.50"))
    assert leilao["correlationId"] == f"lote-{RODADA}"
    print(f"\nLeilão {negociacao}: modo {modo['eventId']} e leilao_iniciado {leilao['eventId']}")


def test_mensagens_invalidas_vao_para_a_dlt():
    marcador = f"nao-e-json-{RODADA}"
    sem_payload = envelope("demanda_criada", "demanda-service", ident("dlt"), {})
    del sem_payload["payload"]
    publicar(("demanda_criada", "x", marcador.encode()), ("demanda_criada", ident("dlt"), sem_payload))
    [(chave, bruto)] = ler("mercado.dlt", lambda v: v["payload"]["originalEvent"] == marcador)
    assert chave == "desconhecido" and bruto["payload"]["error"] == "EVENTO_INVALIDO"
    [(chave, invalida)] = ler("mercado.dlt", lambda v: v.get("aggregateId") == ident("dlt"))
    assert chave == ident("dlt")
    assert invalida["payload"]["detalhe"] == "Campo obrigatório ausente: payload."
    assert invalida["correlationId"] == f"fluxo-{RODADA}"
