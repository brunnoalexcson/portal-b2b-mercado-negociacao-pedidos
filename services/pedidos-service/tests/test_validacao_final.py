"""Regressões da revisão final e verificações adicionais do primeiro fluxo."""
import asyncio
import os
from uuid import uuid4
from types import SimpleNamespace

import httpx
import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from app.contratos import validar_evento
from app.adaptadores.postgres import Postgres
from app.adaptadores.kafka import Kafka
from app.http import PedidoBody
from app.jsonio import dumps, loads
from tests.test_fluxo_real import aguardar, evento, observar, offset_confirmado, token


@pytest.mark.parametrize("bruto,esperado", [
    (b'{JSON quebrado', '{JSON quebrado'),
    (b'texto \xff quebrado', 'texto \ufffd quebrado'),
])
def test_dlt_preserva_texto_original_quando_json_nao_pode_ser_lido(bruto, esperado):
    class Transporte:
        async def send_and_wait(self, topico, valor, key):
            self.valor = loads(valor)
    async def cenario():
        transporte = Transporte()
        mensagem = SimpleNamespace(topic="produto_cadastrado", partition=0, offset=42, value=bruto)
        await Kafka(None, "", "")._dlt(transporte, mensagem, "produto", None)
        assert transporte.valor["payload"]["originalEvent"] == esperado
    asyncio.run(cenario())


def test_dlt_limita_texto_original_e_omite_credenciais_em_texto_quebrado():
    class Transporte:
        async def send_and_wait(self, topico, valor, key):
            self.valor = loads(valor)
    async def cenario():
        transporte = Transporte()
        kafka = Kafka(None, "", "")
        mensagem = SimpleNamespace(topic="produto_cadastrado", partition=0, offset=42,
                                   value=("á" * 40000).encode())
        await kafka._dlt(transporte, mensagem, "produto", None)
        original = transporte.valor["payload"]["originalEvent"]
        assert isinstance(original, str) and len(original.encode()) <= 64 * 1024
        assert original.startswith("áá")
        mensagem.value = b'{"password":"segredo-de-teste", quebrado'
        await kafka._dlt(transporte, mensagem, "produto", None)
        original = transporte.valor["payload"]["originalEvent"]
        assert isinstance(original, str) and "omitido" in original and "segredo-de-teste" not in original
    asyncio.run(cenario())


@pytest.mark.parametrize("bruto", [
    b'{"pass\\u0077ord":"chave-de-teste", quebrado',
    b'"Bearer chave-de-teste"',
])
def test_dlt_omite_credencial_em_chave_escapada_ou_texto_json(bruto):
    class Transporte:
        async def send_and_wait(self, topico, valor, key):
            self.valor = loads(valor)
    async def cenario():
        transporte = Transporte()
        mensagem = SimpleNamespace(topic="produto_cadastrado", partition=0, offset=42, value=bruto)
        await Kafka(None, "", "")._dlt(transporte, mensagem, "produto", None)
        original = transporte.valor["payload"]["originalEvent"]
        assert "chave-de-teste" not in dumps(original)
        assert "omitido" in dumps(original)
    asyncio.run(cenario())


@pytest.mark.parametrize("texto", ["neg\x00fim", "neg\ud800fim"])
def test_post_rejeita_texto_que_nao_pode_ser_persistido(texto):
    with pytest.raises(ValueError):
        PedidoBody.model_validate({"negociacaoId": texto})


@pytest.mark.parametrize("campo", ["nome", "correlationId", "extra"])
@pytest.mark.parametrize("texto", ["teste\x00fim", "teste\ud800fim"])
def test_kafka_rejeita_texto_invalido_antes_do_banco(campo, texto):
    item = evento("produto_cadastrado", "produtos-service", "produto-valido", {
        "produtoId": "produto-valido", "nome": "Milho", "unidade": "TON", "tipoTransporte": "GRANEL",
    }, "validacao-final")
    if campo == "correlationId":
        item[campo] = texto
    else:
        item["payload"][campo] = texto
    with pytest.raises(ValueError):
        validar_evento(item["eventType"], item["aggregateId"], item)


@pytest.mark.skipif(os.getenv("PEDIDOS_DEMO") != "1", reason="Requer compose.demo.yml")
def test_api_permissoes_e_validacao_de_entrada():
    async def cenario():
        async with httpx.AsyncClient(base_url=os.environ["PEDIDOS_API_URL"], timeout=5) as api:
            admin = await token(api, usuario="validacao-admin", admin="true")
            buyer = await token(api, usuario="validacao-buyer", tenant="validacao-empresa")
            sem_contexto = await token(api, usuario="validacao-sem-contexto")
            carrier = await token(api, usuario="validacao-carrier", tenant="validacao-empresa", escopo="CARRIER")
            rota = "/api/v1/pedidos"
            assert (await api.get(rota, headers=sem_contexto)).status_code == 403
            assert (await api.get(rota, headers=carrier)).status_code == 403
            assert (await api.post(rota, json={"negociacaoId": "inexistente"}, headers=buyer)).status_code == 403
            assert (await api.get(rota, headers={"Authorization": "Bearer invalido"})).status_code == 401
            assert (await api.get(rota + "/nao-e-uuid", headers=buyer)).status_code == 422
            assert (await api.get(rota, params={"page": 0}, headers=buyer)).status_code == 422
            assert (await api.get(rota, params={"status": "INVALIDO"}, headers=buyer)).status_code == 422
            for body in ({"negociacaoId": "neg\x00fim"}, {"negociacaoId": 12},
                         {"negociacaoId": "n", "compradorId": "injetado"}):
                resposta = await api.post(rota, json=body, headers=admin)
                assert resposta.status_code == 422, resposta.text
                assert resposta.json()["codigo"] == "VALIDACAO_INVALIDA"
                assert resposta.headers["content-type"].startswith("application/problem+json")
            resposta = await api.get(rota, headers=dict(buyer, **{"X-Correlation-Id": "validacao.final-1"}))
            assert resposta.headers["X-Correlation-Id"] == "validacao.final-1"
    asyncio.run(cenario())


@pytest.mark.skipif(os.getenv("PEDIDOS_DEMO") != "1", reason="Requer compose.demo.yml")
def test_post_cria_201_com_location_e_repeticao_preserva_resposta():
    async def cenario():
        sufixo = uuid4().hex
        negociacao, produto, oferta = f"http-{sufixo}", f"p-{sufixo}", f"f-{sufixo}"
        comprador, fornecedor = f"c-{sufixo}", f"v-{sufixo}"
        banco = Postgres(os.environ["DATABASE_URL"])
        await banco.pool.open()
        try:
            async def receber(item):
                envelope, payload = validar_evento(item["eventType"], item["aggregateId"], item)
                await banco.receber(envelope, payload, 0, 0)

            await receber(evento("negociacao_fechada", "negociacao-service", negociacao, {
                "negociacaoId": negociacao, "status": "ENCERRADO", "compradorId": comprador,
                "fornecedorId": fornecedor, "produtoId": produto, "fornecimentoId": oferta,
                "quantidade": 2, "valorUnitario": 10, "destino": {"cidade": "Goiânia", "uf": "GO"},
            }, sufixo))
            # Barreira de fixture: mantém o reconciliador afastado só deste resultado,
            # ainda sem projeções, para comprovar a criação pelo HTTP.
            async with banco.pool.connection() as conn:
                await conn.execute("UPDATE pedidos.resultado_negociacao SET reavaliar_em=now()+interval '1 hour' "
                                   "WHERE negociacao_id=%s", (negociacao,))
            await receber(evento("produto_cadastrado", "produtos-service", produto, {
                "produtoId": produto, "nome": "Milho", "unidade": "TON", "tipoTransporte": "GRANEL",
            }, sufixo))
            await receber(evento("fornecimento_criado", "fornecimentos-service", oferta, {
                "fornecimentoId": oferta, "fornecedorId": fornecedor, "produtoId": produto,
                "unidade": "TON", "origem": {"cidade": "Rio Verde", "uf": "GO"},
            }, sufixo))
            async with httpx.AsyncClient(base_url=os.environ["PEDIDOS_API_URL"], timeout=5) as api:
                admin = await token(api, usuario="criacao-http", admin="true")
                headers = dict(admin, **{"Idempotency-Key": sufixo})
                corpo = {"negociacaoId": negociacao}
                rota = "/api/v1/pedidos"
                respostas = await asyncio.gather(*(api.post(rota, json=corpo, headers=headers) for _ in range(2)))
                assert all(r.status_code == 201 for r in respostas)
                assert respostas[0].json() == respostas[1].json()
                pedido = respostas[0].json()["pedidoId"]
                assert all(r.headers["Location"] == f"{rota}/{pedido}" for r in respostas)
                nova = await api.post(rota, json=corpo, headers=dict(admin, **{"Idempotency-Key": f"nova-{sufixo}"}))
                assert nova.status_code == 200 and nova.json()["pedidoId"] == pedido
                for escopo, tenant in (("BUYER", comprador), ("SUPPLIER", fornecedor), ("TENANT_ADMIN", fornecedor)):
                    autorizado = await token(api, usuario="leitura-http", tenant=tenant, escopo=escopo)
                    assert (await api.get(f"{rota}/{pedido}", headers=autorizado)).status_code == 200
                outro = await token(api, usuario="outro-http", tenant=f"fora-{sufixo}")
                assert (await api.get(f"{rota}/{pedido}/historico", headers=outro)).status_code == 404
            async with banco.pool.connection() as conn:
                for tabela, campo, valor in (("pedido", "negociacao_id", negociacao),
                                             ("outbox", "pedido_id", pedido), ("pedido_historico", "pedido_id", pedido)):
                    linha = await (await conn.execute(f"SELECT count(*) AS n FROM pedidos.{tabela} WHERE {campo}=%s",
                                                      (valor,))).fetchone()
                    assert linha["n"] == 1
        finally:
            await banco.pool.close()
    asyncio.run(cenario())


@pytest.mark.skipif(os.getenv("PEDIDOS_DEMO") != "1", reason="Requer compose.demo.yml")
@pytest.mark.parametrize("nome", ["Milho\x00invalido", "Milho\ud800invalido"])
def test_mensagem_com_texto_invalido_vai_para_dlt_e_libera_a_particao(nome):
    async def cenario():
        sufixo = uuid4().hex
        chave = f"nul-{sufixo}"
        item = evento("produto_cadastrado", "produtos-service", chave, {
            "produtoId": chave, "nome": nome, "unidade": "TON", "tipoTransporte": "GRANEL",
        }, sufixo)
        servidores = os.environ["KAFKA_BOOTSTRAP_SERVERS"]
        observador = AIOKafkaConsumer("pedidos.dlt", bootstrap_servers=servidores,
                                     group_id=f"dlt-final-{sufixo}", auto_offset_reset="earliest",
                                     enable_auto_commit=False)
        probe = AIOKafkaConsumer(bootstrap_servers=servidores, group_id=os.environ["KAFKA_GROUP_ID"],
                                enable_auto_commit=False)
        async with AIOKafkaProducer(bootstrap_servers=servidores) as produtor, observador, probe:
            await produtor.send_and_wait(item["eventType"], dumps(item).encode("utf-8", "backslashreplace"),
                                         key=chave.encode(), partition=0)
            valido = evento("produto_cadastrado", "produtos-service", f"ok-{sufixo}", {
                "produtoId": f"ok-{sufixo}", "nome": "Milho", "unidade": "TON", "tipoTransporte": "GRANEL",
            }, sufixo)
            registro = await produtor.send_and_wait(valido["eventType"], dumps(valido).encode(),
                                                    key=valido["aggregateId"].encode(), partition=0)
            dlt = await observar(observador, "pedidos.dlt", chave)
            assert dlt["payload"]["error"] == "EVENTO_INVALIDO"
            assert dlt["payload"]["originalEvent"]["eventId"] == item["eventId"]
            await aguardar(lambda: offset_confirmado(probe, registro), "próxima mensagem na mesma partição")
    asyncio.run(cenario())
