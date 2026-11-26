"""Fluxo real contra a stack de `compose.demo.yml`: Redpanda, PostgreSQL, outbox e duas réplicas atrás do gateway."""
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
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
FORNECEDOR_A = f"fornecedor-a-{SUFIXO}"
FORNECEDOR_B = f"fornecedor-b-{SUFIXO}"
PRODUTO = f"milho-{SUFIXO}"
OFERTA = f"oferta-x-{SUFIXO}"
OFERTA_A = f"oferta-a-{SUFIXO}"
OFERTA_B = f"oferta-b-{SUFIXO}"
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


def confirmacao_reserva(negociacao_id: str, pedido_id: str, event_id: str | None = None,
                        fornecimento_id: str = OFERTA) -> dict:
    return evento("pedido_atualizado", "pedidos-service", pedido_id, {
        "pedidoId": pedido_id,
        "negociacaoId": negociacao_id,
        "fornecimentoId": fornecimento_id,
        "status": "CRIADO",
        "statusAnterior": "CRIADO",
        "reservaSituacao": "CONFIRMADA",
        "versao": 2,
    }, event_id=event_id)


def recusa_reserva(negociacao_id: str, pedido_id: str, fornecimento_id: str = OFERTA) -> dict:
    return evento("pedido_atualizado", "pedidos-service", pedido_id, {
        "pedidoId": pedido_id,
        "negociacaoId": negociacao_id,
        "fornecimentoId": fornecimento_id,
        "status": "CANCELADO",
        "statusAnterior": "CRIADO",
        "reservaSituacao": "RECUSADA",
        "versao": 2,
    })


def leilao_iniciado(negociacao_id: str, duracao: timedelta = timedelta(hours=2)) -> dict:
    agora = datetime.now(timezone.utc)
    return evento("leilao_iniciado", "mercado-service", negociacao_id, {
        "negociacaoId": negociacao_id,
        "sugestaoId": str(uuid4()),
        "produtoId": PRODUTO,
        "unidade": "TON",
        "quantidadeTotal": D("20.00"),
        "precoInicial": D("2550.00"),
        "incrementoMinimo": D("25.50"),
        "incrementoMinimoPercentual": D("1.00"),
        "itens": [
            {
                "fornecimentoId": OFERTA_A,
                "fornecedorId": FORNECEDOR_A,
                "quantidade": D("5.00"),
                "precoUnitario": D("120.00"),
                "valorItem": D("600.00"),
                "participacao": D("0.2353"),
                "origem": {"cidade": "Rio Verde", "uf": "GO"},
            },
            {
                "fornecimentoId": OFERTA_B,
                "fornecedorId": FORNECEDOR_B,
                "quantidade": D("15.00"),
                "precoUnitario": D("130.00"),
                "valorItem": D("1950.00"),
                "participacao": D("0.7647"),
                "origem": {"cidade": "Jataí", "uf": "GO"},
            },
        ],
        "iniciaEm": formato_json(agora),
        "encerraEm": formato_json(agora + duracao),
    })


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


async def _ler(topico: str, filtro, limite: float = 30.0, esperar_extras: float = 0.0, minimo: int = 1):
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
            if len(encontrados) >= minimo:
                if esperar_extras <= 0:
                    return encontrados
                if prazo_extra is None:
                    prazo_extra = asyncio.get_running_loop().time() + esperar_extras
                elif asyncio.get_running_loop().time() >= prazo_extra:
                    return encontrados
        raise AssertionError(f"Esperadas >= {minimo} mensagens em {topico} em {limite}s, lidas {len(encontrados)}")
    finally:
        await consumidor.stop()


def ler(topico: str, filtro, limite: float = 30.0, esperar_extras: float = 0.0, minimo: int = 1):
    return asyncio.run(_ler(topico, filtro, limite, esperar_extras, minimo))


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


def test_contraproposta_idempotente_e_aceite_na_direta():
    negociacao_id = str(uuid4())
    pedido_id = str(uuid4())
    jwt_comprador = token(COMPRADOR, "BUYER")
    jwt_fornecedor = token(FORNECEDOR, "SUPPLIER")
    publicar(modo_direta(negociacao_id, compra_imediata=False))

    esperar(lambda: get_negociacao(negociacao_id, jwt_comprador).status_code == 200)
    publicar(confirmacao_reserva(negociacao_id, pedido_id))
    esperar(
        lambda: d if (d := loads(get_negociacao(negociacao_id, jwt_comprador).text))["reservaSituacao"] == "CONFIRMADA"
        else None,
    )

    chave_idem = f"proposta-{uuid4().hex[:12]}"
    cabecalhos_proposta = {
        "Authorization": f"Bearer {jwt_comprador}",
        "Content-Type": "application/json",
        "Idempotency-Key": chave_idem,
    }
    r_proposta = httpx.post(
        f"{API}/api/v1/negociacoes/{negociacao_id}/propostas",
        content=dumps({"precoUnitario": D("125.00")}),
        headers=cabecalhos_proposta,
        timeout=5,
    )
    assert r_proposta.status_code == 201
    proposta_criada = loads(r_proposta.text)
    assert (proposta_criada["autor"], proposta_criada["precoUnitario"], proposta_criada["sequencia"]) == (
        "BUYER", D("125.00"), 2,
    )

    r_aceite = httpx.post(
        f"{API}/api/v1/negociacoes/{negociacao_id}/aceitar",
        headers={"Authorization": f"Bearer {jwt_fornecedor}"},
        timeout=5,
    )
    assert r_aceite.status_code == 200
    detalhe_aceite = loads(r_aceite.text)
    assert (detalhe_aceite["status"], detalhe_aceite["precoAtual"], detalhe_aceite["acoesPermitidas"]) == (
        "ENCERRADO", D("125.00"), [],
    )

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
            "valorUnitario": D("125.00"),
            "valorTotal": D("1250.00"),
        }],
    }

    r_replay = httpx.post(
        f"{API}/api/v1/negociacoes/{negociacao_id}/propostas",
        content=dumps({"precoUnitario": D("125.00")}),
        headers=cabecalhos_proposta,
        timeout=5,
    )
    assert r_replay.status_code == 201
    assert loads(r_replay.text) == proposta_criada

    with psycopg.connect(DSN) as conexao:
        total_propostas = conexao.execute(
            "SELECT count(*) FROM negociacao.proposta WHERE negociacao_id = %s",
            (negociacao_id,),
        ).fetchone()[0]
        assert total_propostas == 2


def test_abertura_recalculo_de_lote_e_deserto_no_leilao():
    leilao_id = str(uuid4())
    jwt_comprador = token(COMPRADOR, "BUYER")
    publicar(leilao_iniciado(leilao_id))

    detalhe_inicial = esperar(
        lambda: loads(r.text) if (r := get_negociacao(leilao_id, jwt_comprador)).status_code == 200 else None,
    )
    assert detalhe_inicial["status"] == "AGUARDANDO_RESERVAS"
    assert detalhe_inicial["acoesPermitidas"] == []

    solicitados = ler(
        "pedido_temporario_solicitado", lambda v: v.get("aggregateId") == leilao_id, minimo=2,
    )
    assert len(solicitados) == 2
    (chave_a, msg_a), (chave_b, msg_b) = solicitados
    assert chave_a == chave_b == leilao_id
    assert msg_a["payload"] == {
        "negociacaoId": leilao_id,
        "tipo": "LEILAO",
        "fornecimentoId": OFERTA_A,
        "fornecedorId": FORNECEDOR_A,
        "produtoId": PRODUTO,
        "quantidade": D("5.00"),
        "precoUnitario": D("120.00"),
    }
    assert msg_b["payload"] == {
        "negociacaoId": leilao_id,
        "tipo": "LEILAO",
        "fornecimentoId": OFERTA_B,
        "fornecedorId": FORNECEDOR_B,
        "produtoId": PRODUTO,
        "quantidade": D("15.00"),
        "precoUnitario": D("130.00"),
    }

    publicar(
        confirmacao_reserva(leilao_id, str(uuid4()), fornecimento_id=OFERTA_A),
        recusa_reserva(leilao_id, str(uuid4()), fornecimento_id=OFERTA_B),
    )
    recalculado = esperar(
        lambda: d if (d := loads(get_negociacao(leilao_id, jwt_comprador).text))["status"] == "ABERTO" else None,
    )
    assert recalculado["quantidadeTotal"] == D("5.00")
    assert recalculado["precoInicial"] == D("600.00")
    assert recalculado["incrementoMinimo"] == D("6.00")
    assert recalculado["acoesPermitidas"] == ["DAR_LANCE"]
    assert recalculado["ofertas"][0]["fornecimentoId"] == OFERTA_A
    assert recalculado["ofertas"][0]["participacao"] == D("1.0000")
    assert recalculado["ofertas"][0]["situacao"] == "CONFIRMADA"
    assert recalculado["ofertas"][1]["fornecimentoId"] == OFERTA_B
    assert recalculado["ofertas"][1]["situacao"] == "RECUSADA"

    # Segundo leilão com as duas ofertas recusadas -> DESERTO e exatamente 1 negociacao_fechada.
    deserto_id = str(uuid4())
    publicar(leilao_iniciado(deserto_id))
    esperar(lambda: get_negociacao(deserto_id, jwt_comprador).status_code == 200)
    publicar(
        recusa_reserva(deserto_id, str(uuid4()), fornecimento_id=OFERTA_A),
        recusa_reserva(deserto_id, str(uuid4()), fornecimento_id=OFERTA_B),
    )

    deserto = esperar(
        lambda: d if (d := loads(get_negociacao(deserto_id, jwt_comprador).text))["status"] == "DESERTO" else None,
    )
    assert deserto["compradorId"] is None
    assert deserto["encerradoEm"] is not None
    assert deserto["acoesPermitidas"] == []

    fechamentos = ler(
        "negociacao_fechada", lambda v: v.get("aggregateId") == deserto_id, esperar_extras=1.5,
    )
    assert len(fechamentos) == 1
    chave_fechada, fechada = fechamentos[0]
    assert chave_fechada == deserto_id
    assert fechada["payload"] == {
        "negociacaoId": deserto_id,
        "tipo": "LEILAO",
        "status": "DESERTO",
        "compradorId": None,
        "demandaId": None,
        "produtoId": PRODUTO,
        "alocacoes": [],
    }


def test_lances_sigilo_eventos_e_persistencia_no_leilao():
    leilao_id = str(uuid4())
    comprador_b = f"comprador-b-{SUFIXO}"
    demanda_b = f"demanda-b-{SUFIXO}"
    jwt_comprador_a = token(COMPRADOR, "BUYER")
    jwt_comprador_b = token(comprador_b, "BUYER")
    jwt_fornecedor_a = token(FORNECEDOR_A, "SUPPLIER")

    publicar(leilao_iniciado(leilao_id))
    esperar(lambda: get_negociacao(leilao_id, jwt_comprador_a).status_code == 200)

    publicar(
        confirmacao_reserva(leilao_id, str(uuid4()), fornecimento_id=OFERTA_A),
        confirmacao_reserva(leilao_id, str(uuid4()), fornecimento_id=OFERTA_B),
    )
    esperar(
        lambda: d if (d := loads(get_negociacao(leilao_id, jwt_comprador_a).text))["status"] == "ABERTO" else None,
    )

    r_insuficiente = httpx.post(
        f"{API}/api/v1/negociacoes/{leilao_id}/lances",
        content=dumps({"valor": D("2549.99"), "demandaId": DEMANDA}),
        headers={"Authorization": f"Bearer {jwt_comprador_a}", "Content-Type": "application/json"},
        timeout=5,
    )
    assert r_insuficiente.status_code == 422
    assert loads(r_insuficiente.text)["codigo"] == "LANCE_INSUFICIENTE"

    r_lance_1 = httpx.post(
        f"{API}/api/v1/negociacoes/{leilao_id}/lances",
        content=dumps({"valor": D("2550.00"), "demandaId": DEMANDA}),
        headers={"Authorization": f"Bearer {jwt_comprador_a}", "Content-Type": "application/json"},
        timeout=5,
    )
    assert r_lance_1.status_code == 201
    lance_1 = loads(r_lance_1.text)
    assert (lance_1["compradorId"], lance_1["valor"], lance_1["sequencia"]) == (COMPRADOR, D("2550.00"), 1)
    assert "demandaId" not in lance_1

    r_lance_2 = httpx.post(
        f"{API}/api/v1/negociacoes/{leilao_id}/lances",
        content=dumps({"valor": D("2600.00"), "demandaId": demanda_b}),
        headers={"Authorization": f"Bearer {jwt_comprador_b}", "Content-Type": "application/json"},
        timeout=5,
    )
    assert r_lance_2.status_code == 201
    lance_2 = loads(r_lance_2.text)
    assert (lance_2["compradorId"], lance_2["valor"], lance_2["sequencia"]) == (comprador_b, D("2600.00"), 2)
    assert "demandaId" not in lance_2

    eventos_lance = ler(
        "lance_realizado", lambda v: v.get("aggregateId") == leilao_id, esperar_extras=1.5, minimo=2,
    )
    assert len(eventos_lance) == 2
    (chave_1, ev_1), (chave_2, ev_2) = eventos_lance
    assert chave_1 == chave_2 == leilao_id
    assert ev_1["payload"] == {
        "negociacaoId": leilao_id,
        "lanceId": lance_1["lanceId"],
        "compradorId": COMPRADOR,
        "valor": D("2550.00"),
        "sequencia": 1,
        "criadoEm": lance_1["criadoEm"],
    }
    assert ev_2["payload"] == {
        "negociacaoId": leilao_id,
        "lanceId": lance_2["lanceId"],
        "compradorId": comprador_b,
        "valor": D("2600.00"),
        "sequencia": 2,
        "criadoEm": lance_2["criadoEm"],
    }

    detalhe_perdedor = loads(get_negociacao(leilao_id, jwt_comprador_a).text)
    assert detalhe_perdedor["totalLances"] == 2
    assert detalhe_perdedor["melhorLance"] == {**lance_2, "compradorId": "OCULTO"}

    detalhe_vencedor = loads(get_negociacao(leilao_id, jwt_comprador_b).text)
    assert detalhe_vencedor["melhorLance"] == lance_2

    detalhe_fornecedor = loads(get_negociacao(leilao_id, jwt_fornecedor_a).text)
    assert detalhe_fornecedor["melhorLance"] == lance_2

    with psycopg.connect(DSN) as conexao:
        linhas = conexao.execute(
            "SELECT id::text, negociacao_id::text, comprador_id, valor, demanda_id, sequencia "
            "FROM negociacao.lance WHERE negociacao_id = %s ORDER BY sequencia",
            (leilao_id,),
        ).fetchall()
    assert linhas == [
        (lance_1["lanceId"], leilao_id, COMPRADOR, D("2550.00"), DEMANDA, 1),
        (lance_2["lanceId"], leilao_id, comprador_b, D("2600.00"), demanda_b, 2),
    ]


def test_encerramento_automatico_e_rateio_no_leilao():
    leilao_id = str(uuid4())
    pedido_a = str(uuid4())
    pedido_b = str(uuid4())
    comprador_b = f"comprador-b-{SUFIXO}"
    demanda_b = f"demanda-b-{SUFIXO}"
    jwt_comprador_a = token(COMPRADOR, "BUYER")
    jwt_comprador_b = token(comprador_b, "BUYER")

    publicar(leilao_iniciado(leilao_id, duracao=timedelta(seconds=20)))
    esperar(lambda: get_negociacao(leilao_id, jwt_comprador_a).status_code == 200)

    publicar(
        confirmacao_reserva(leilao_id, pedido_a, fornecimento_id=OFERTA_A),
        confirmacao_reserva(leilao_id, pedido_b, fornecimento_id=OFERTA_B),
    )
    esperar(
        lambda: d if (d := loads(get_negociacao(leilao_id, jwt_comprador_a).text))["status"] == "ABERTO" else None,
    )

    r_lance_1 = httpx.post(
        f"{API}/api/v1/negociacoes/{leilao_id}/lances",
        content=dumps({"valor": D("2550.00")}),
        headers={"Authorization": f"Bearer {jwt_comprador_a}", "Content-Type": "application/json"},
        timeout=5,
    )
    assert r_lance_1.status_code == 201

    r_lance_2 = httpx.post(
        f"{API}/api/v1/negociacoes/{leilao_id}/lances",
        content=dumps({"valor": D("2600.00"), "demandaId": demanda_b}),
        headers={"Authorization": f"Bearer {jwt_comprador_b}", "Content-Type": "application/json"},
        timeout=5,
    )
    assert r_lance_2.status_code == 201

    encerrado = esperar(
        lambda: d if (d := loads(get_negociacao(leilao_id, jwt_comprador_b).text))["status"] == "ENCERRADO" else None,
        limite=40.0,
    )
    assert encerrado["compradorId"] == comprador_b
    assert encerrado["encerradoEm"] is not None
    assert encerrado["acoesPermitidas"] == []
    assert loads(get_negociacao(leilao_id, jwt_comprador_a).text)["compradorId"] == "OCULTO"

    fechamentos = ler(
        "negociacao_fechada", lambda v: v.get("aggregateId") == leilao_id, esperar_extras=1.5,
    )
    assert len(fechamentos) == 1
    chave, fechada = fechamentos[0]
    assert chave == leilao_id
    assert fechada["payload"] == {
        "negociacaoId": leilao_id,
        "tipo": "LEILAO",
        "status": "ENCERRADO",
        "compradorId": comprador_b,
        "demandaId": demanda_b,
        "produtoId": PRODUTO,
        "alocacoes": [
            {
                "fornecimentoId": OFERTA_A,
                "fornecedorId": FORNECEDOR_A,
                "pedidoId": pedido_a,
                "quantidade": D("5.00"),
                "valorUnitario": D("122.35"),
                "valorTotal": D("611.76"),
            },
            {
                "fornecimentoId": OFERTA_B,
                "fornecedorId": FORNECEDOR_B,
                "pedidoId": pedido_b,
                "quantidade": D("15.00"),
                "valorUnitario": D("132.55"),
                "valorTotal": D("1988.24"),
            },
        ],
    }
    assert sum(a["valorTotal"] for a in fechada["payload"]["alocacoes"]) == D("2600.00")

