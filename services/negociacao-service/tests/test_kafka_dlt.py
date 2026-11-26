import asyncio
import re
from types import SimpleNamespace

from app.adaptadores.kafka import Kafka
from app.jsonio import loads

# regras copiadas de docs/negociacao/contratos/negociacao.dlt.schema.json (a pasta docs não entra na imagem)
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
INSTANTE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
CORRELACAO = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


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
    assert set(falha) == {"eventId", "eventType", "eventVersion", "timestamp", "source", "correlationId",
                          "aggregateId", "payload"}
    assert UUID.match(falha["eventId"])
    assert (falha["eventType"], falha["eventVersion"], falha["source"]) == (
        "negociacao.dlt", "1.0", "negociacao-service")
    assert INSTANTE.match(falha["timestamp"])
    assert CORRELACAO.match(falha["correlationId"])
    assert falha["aggregateId"] == "desconhecido"
    assert falha["payload"] == {
        "topicoOrigem": "pedido_atualizado", "originalEvent": "{nao e json", "error": "EVENTO_INVALIDO",
        "detalhe": "Mensagem não é JSON válido.", "stackTrace": "", "failedAtAttempt": 1,
    }


def test_reentrega_da_mesma_mensagem_mantem_o_event_id():
    [(_, _, primeira)] = publicar_na_dlt(b"x")
    [(_, _, segunda)] = publicar_na_dlt(b"x")
    [(_, _, outra)] = publicar_na_dlt(b"x", offset=8)

    assert primeira["eventId"] == segunda["eventId"] != outra["eventId"]


def test_byte_que_nao_e_utf8_fica_recuperavel():
    [(_, _, falha)] = publicar_na_dlt(b"abc\xff")

    assert falha["payload"]["originalEvent"] == "abc\\xff"


def test_mensagem_sem_valor_vai_com_texto_vazio():
    [(_, _, falha)] = publicar_na_dlt(None)

    assert falha["payload"]["originalEvent"] == ""
