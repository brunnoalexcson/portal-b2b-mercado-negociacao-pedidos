import asyncio
from types import SimpleNamespace

from app.adaptadores.kafka import Kafka
from app.jsonio import loads
from tests.cenarios import validar_schema


class ProdutorFalso:
    def __init__(self):
        self.enviados = []

    async def send_and_wait(self, topico, valor, key):
        self.enviados.append((topico, key, loads(valor.decode("utf-8"))))


def publicar_na_dlt(valor, topico="pedido_atualizado", offset=7):
    produtor = ProdutorFalso()
    mensagem = SimpleNamespace(topic=topico, partition=1, offset=offset, value=valor)
    asyncio.run(Kafka(None, "broker:9092", "grupo")._dlt(produtor, mensagem))
    return produtor.enviados


def test_mensagem_que_nao_e_json_segue_o_schema_da_dlt():
    [(topico, chave, falha)] = publicar_na_dlt(b"{nao e json")

    assert (topico, chave) == ("negociacao.dlt", b"desconhecido")
    validar_schema("negociacao.dlt", falha)
    assert falha["aggregateId"] == "desconhecido"
    assert falha["payload"] == {
        "topicoOrigem": "pedido_atualizado", "originalEvent": "{nao e json", "error": "EVENTO_INVALIDO",
        "detalhe": "Mensagem não é JSON válido.", "stackTrace": "", "failedAtAttempt": 1,
    }


def test_reentrega_da_mesma_mensagem_mantem_o_event_id():
    [(_, _, primeira)] = publicar_na_dlt(b"x")
    [(_, _, segunda)] = publicar_na_dlt(b"x")
    [(_, _, outra)] = publicar_na_dlt(b"x", offset=8)

    validar_schema("negociacao.dlt", primeira)
    assert primeira["eventId"] == segunda["eventId"] != outra["eventId"]


def test_byte_que_nao_e_utf8_fica_recuperavel():
    [(_, _, falha)] = publicar_na_dlt(b"abc\xff")

    validar_schema("negociacao.dlt", falha)
    assert falha["payload"]["originalEvent"] == "abc\\xff"


def test_mensagem_sem_valor_vai_com_texto_vazio():
    [(_, _, falha)] = publicar_na_dlt(None)

    validar_schema("negociacao.dlt", falha)
    assert falha["payload"]["originalEvent"] == ""
