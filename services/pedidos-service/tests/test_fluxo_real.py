import asyncio
import os
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import psycopg
import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition

from app.jsonio import dumps, loads

pytestmark = pytest.mark.skipif(os.getenv("PEDIDOS_DEMO") != "1", reason="Requer compose.demo.yml")
ROTA = "/api/v1/pedidos"


def evento(topico, fonte, chave, payload, correlacao):
    return {
        "eventId": str(uuid4()), "eventType": topico, "eventVersion": "1.0",
        "timestamp": datetime.now(timezone.utc), "source": fonte,
        "correlationId": correlacao, "aggregateId": chave, "payload": payload,
    }


async def aguardar(verificar, descricao, timeout=20):
    fim = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < fim:
        valor = await verificar()
        if valor:
            return valor
        await asyncio.sleep(0.2)
    pytest.fail(f"Prazo excedido: {descricao}")


async def enviar(produtor, item):
    return await produtor.send_and_wait(item["eventType"], dumps(item).encode(), key=item["aggregateId"].encode())


async def token(client, **params):
    resposta = await client.post(os.environ["PEDIDOS_IDENTITY_URL"] + "/token", params=params)
    resposta.raise_for_status()
    return {"Authorization": f"Bearer {resposta.json()['access_token']}"}


async def offset_confirmado(probe, registro):
    tp = TopicPartition(registro.topic, registro.partition)
    offset = await probe.committed(tp)
    return offset is not None and offset >= registro.offset + 1


async def observar(observador, topico, chave):
    async with asyncio.timeout(20):
        while True:
            mensagem = await observador.getone()
            if mensagem.topic == topico and mensagem.key == chave.encode():
                return loads(mensagem.value)


async def contar(conn, sql, parametros):
    cursor = await conn.execute(sql, parametros)
    return (await cursor.fetchone())[0]


async def cenario():
    sufixo = uuid4().hex
    negociacao, produto, oferta = f"neg-{sufixo}", f"prod-{sufixo}", f"forn-{sufixo}"
    comprador, fornecedor = f"comp-{sufixo}", f"forn-emp-{sufixo}"
    correlacao = f"fluxo-{sufixo}"
    servidores = os.environ["KAFKA_BOOTSTRAP_SERVERS"]
    observador = AIOKafkaConsumer("pedido_criado", "pedidos.dlt", bootstrap_servers=servidores,
                                  group_id=f"verificar-{sufixo}", auto_offset_reset="earliest",
                                  enable_auto_commit=False)
    probe = AIOKafkaConsumer(bootstrap_servers=servidores, group_id=os.environ["KAFKA_GROUP_ID"],
                             enable_auto_commit=False)
    conexao = await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"], autocommit=True)

    async with conexao, httpx.AsyncClient(base_url=os.environ["PEDIDOS_API_URL"], timeout=5) as api:
        async def pronto():
            return (await api.get("/health/ready")).status_code == 200

        await aguardar(pronto, "prontidão pelo gateway")
        schema = (await api.get("/openapi.json")).json()
        post = schema["paths"][ROTA]["post"]
        assert post["security"] == [{"HTTPBearer": []}]
        assert post["requestBody"]["content"]["application/json"]["schema"]["required"] == ["negociacaoId"]
        admin = await token(api, usuario="teste-operador", admin="true")
        buyer = await token(api, usuario="teste-comprador", tenant=comprador)
        outro = await token(api, usuario="teste-outra-empresa", tenant=f"outro-{sufixo}")
        auth = dict(admin, **{"Idempotency-Key": f"tentativa-{sufixo}", "X-Correlation-Id": correlacao})
        corpo = {"negociacaoId": negociacao}
        antes = await api.post(ROTA, json=corpo, headers=auth)
        assert antes.status_code == 409
        assert antes.json()["codigo"] == "RESULTADO_INDISPONIVEL"
        assert antes.json()["correlationId"] == correlacao

        fechamento = evento("negociacao_fechada", "negociacao-service", negociacao, {
            "negociacaoId": negociacao, "status": "ENCERRADO", "compradorId": comprador,
            "fornecedorId": fornecedor, "produtoId": produto, "fornecimentoId": oferta,
            "quantidade": 1.25, "valorUnitario": 8, "destino": {"cidade": "Anápolis", "uf": "GO"},
        }, correlacao)

        async with AIOKafkaProducer(bootstrap_servers=servidores) as produtor, observador, probe:
            registro = await enviar(produtor, fechamento)
            await aguardar(lambda: offset_confirmado(probe, registro), "fechamento aguardando projeções")
            pendente = await api.post(ROTA, json=corpo, headers=auth)
            assert pendente.status_code == 409
            assert pendente.json()["codigo"] == "DADOS_DO_PEDIDO_PENDENTES"

            await enviar(produtor, evento("produto_cadastrado", "produtos-service", produto, {
                "produtoId": produto, "nome": "Milho em grão", "unidade": "TON", "tipoTransporte": "GRANEL",
            }, correlacao))
            await enviar(produtor, evento("fornecimento_criado", "fornecimentos-service", oferta, {
                "fornecimentoId": oferta, "fornecedorId": fornecedor, "produtoId": produto,
                "unidade": "TON", "origem": {"cidade": "Rio Verde", "uf": "GO"},
            }, correlacao))

            async def listar_criado():
                retorno = await api.get(ROTA, params=corpo, headers=buyer)
                assert retorno.status_code == 200, retorno.text
                return retorno.json()["items"]

            itens = await aguardar(listar_criado, "pedido visível para o comprador")
            assert len(itens) == 1
            pedido = itens[0]["pedidoId"]
            criado = await observar(observador, "pedido_criado", pedido)
            assert criado["aggregateId"] == pedido
            assert criado["correlationId"] == correlacao
            assert criado["payload"] == {
                "pedidoId": pedido, "negociacaoId": negociacao, "compradorId": comprador,
                "fornecedorId": fornecedor, "produtoId": produto, "fornecimentoId": oferta,
                "produtoNome": "Milho em grão", "quantidade": 1.25, "unidade": "TON",
                "pesoKg": 1250, "tipoTransporte": "GRANEL", "valorTotal": 10,
                "origem": {"cidade": "Rio Verde", "uf": "GO"}, "destino": {"cidade": "Anápolis", "uf": "GO"},
                "status": "CRIADO",
            }

            for _ in range(4):
                await enviar(produtor, fechamento)
            final = await enviar(produtor, dict(fechamento, eventId=str(uuid4())))
            await aguardar(lambda: offset_confirmado(probe, final), "confirmação das reentregas")

            post1, post2 = await asyncio.gather(*(
                api.post(ROTA, json=corpo, headers=auth) for _ in range(2)
            ))
            assert post1.status_code == post2.status_code == 200
            assert post1.json() == post2.json()
            assert post1.json()["pedidoId"] == pedido
            conflito = await api.post(ROTA, json={"negociacaoId": "outra"}, headers=auth)
            assert conflito.status_code == 409 and conflito.json()["codigo"] == "IDEMPOTENCIA_CONFLITO"

            detalhe = await api.get(f"{ROTA}/{pedido}", headers=buyer)
            assert detalhe.status_code == 200
            assert detalhe.json()["reservaSituacao"] == "PENDENTE"
            historico = await api.get(f"{ROTA}/{pedido}/historico", headers=buyer)
            assert historico.status_code == 200 and historico.json()["total"] == 1
            assert historico.json()["items"][0]["versao"] == 1
            assert (await api.get(f"{ROTA}/{pedido}", headers=outro)).status_code == 404
            assert (await api.get(ROTA, headers=outro)).json()["total"] == 0
            assert (await api.get(ROTA, params={"size": 101}, headers=buyer)).status_code == 422
            assert (await api.get(ROTA)).status_code == 401

            assert await contar(conexao, "SELECT count(*) FROM pedidos.pedido WHERE negociacao_id=%s",
                                (negociacao,)) == 1
            assert await contar(conexao, "SELECT count(*) FROM pedidos.outbox WHERE pedido_id=%s", (pedido,)) == 1
            assert await contar(conexao, """
                SELECT count(*) FROM pedidos.outbox
                WHERE pedido_id=%s AND event_id=%s AND confirmado_em IS NOT NULL
            """, (pedido, criado["eventId"])) == 1
            assert await contar(conexao, """
                SELECT count(*) FROM pedidos.audit_log WHERE pedido_id=%s AND correlation_id=%s
            """, (pedido, correlacao)) == 1
            restantes = await observador.getmany(timeout_ms=500)
            for particao, mensagens in restantes.items():
                if particao.topic == "pedido_criado":
                    assert all(loads(m.value)["eventId"] == criado["eventId"]
                               for m in mensagens if m.key == pedido.encode())

            deserta = f"deserta-{sufixo}"
            registro = await enviar(produtor, evento("negociacao_fechada", "negociacao-service", deserta,
                                                    {"negociacaoId": deserta, "status": "DESERTO"}, correlacao))
            await aguardar(lambda: offset_confirmado(probe, registro), "negociação deserta")
            assert await contar(conexao, "SELECT count(*) FROM pedidos.pedido WHERE negociacao_id=%s",
                                (deserta,)) == 0

            invalido = evento("negociacao_fechada", "negociacao-service", f"erro-{sufixo}", {}, correlacao)
            del invalido["payload"]
            registro = await enviar(produtor, invalido)
            dlt = await observar(observador, "pedidos.dlt", invalido["aggregateId"])
            assert dlt["payload"]["error"] == "EVENTO_INVALIDO"
            assert dlt["payload"]["originalEvent"]["eventId"] == invalido["eventId"]
            await aguardar(lambda: offset_confirmado(probe, registro), "confirmação do registro enviado à DLT")

            print(f"Pedido {pedido}, negociação {negociacao}, estado CRIADO, evento {criado['eventId']}.")


def test_fechamento_kafka_gera_um_pedido_consultavel_e_um_evento():
    asyncio.run(cenario())
