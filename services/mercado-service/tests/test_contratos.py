"""Validação dos eventos consumidos (§7.3, RN-32, §13.1) e chave/correlação da `mercado.dlt` (§7.4)."""
import copy
from decimal import Decimal

import pytest

from app.contratos import EventoInvalido, chave_dlt, evento_dlt, validar_evento
from tests.cenarios import DEM_A, OF_X, P, envelope, t

D = Decimal


def demanda_valida():
    return envelope("demanda_criada", {
        "demandaId": DEM_A, "compradorId": "30000000-0000-4000-8000-000000000001", "produtoId": P,
        "unidade": "TON", "quantidade": D("50.00"), "precoMaximo": D("9.00"),
        "destino": {"cidade": "Anápolis", "uf": "GO", "cep": "75000-000"}, "prazoDesejado": "2026-11-30T23:59:59Z",
        "tipo": "UNICA", "frequencia": None, "criadoEm": "2026-11-20T11:55:00.000Z",
    }, agregado=DEM_A, correlacao="teste-mrc-dem")


def com(mensagem, caminho, valor):
    mensagem = copy.deepcopy(mensagem)
    alvo = mensagem
    *partes, ultima = caminho.split(".")
    for parte in partes:
        alvo = alvo[parte]
    if valor is REMOVER:
        del alvo[ultima]
    else:
        alvo[ultima] = valor
    return mensagem


REMOVER = object()


def test_demanda_valida_e_normalizada():
    evento = validar_evento("demanda_criada", demanda_valida())
    assert evento.payload["quantidade"] == D("50.00")
    assert evento.payload["prazoDesejado"] == t("2026-11-30T23:59:59.000Z")
    assert evento.payload["destino"] == {"cidade": "Anápolis", "uf": "GO"}
    assert evento.correlation_id == "teste-mrc-dem"


def test_aceita_numero_inteiro_decimal_e_fuso_diferente_de_utc():
    mensagem = com(demanda_valida(), "payload.quantidade", 50)
    mensagem = com(mensagem, "payload.precoMaximo", D("9.5"))
    mensagem = com(mensagem, "payload.criadoEm", "2026-11-20T08:55:00-03:00")
    evento = validar_evento("demanda_criada", mensagem)
    assert evento.payload["quantidade"] == D("50.00")
    assert evento.payload["precoMaximo"] == D("9.50")
    assert evento.payload["instante"] == t("11:55")


def test_recusa_float_mesmo_com_valor_valido():
    # O leitor JSON do serviço entrega Decimal; float só chegaria por um caminho que já perdeu precisão (§4).
    mensagem = com(demanda_valida(), "payload.precoMaximo", 99999999999999.99)
    with pytest.raises(EventoInvalido):
        validar_evento("demanda_criada", mensagem)


@pytest.mark.parametrize("caminho,valor,detalhe", [
    ("payload", REMOVER, "Campo obrigatório ausente: payload."),
    ("payload.destino", REMOVER, "Campo obrigatório ausente: payload.destino."),
    ("payload.quantidade", "50.00", None),
    ("payload.quantidade", True, None),
    ("payload.quantidade", D("0.001"), None),
    ("payload.quantidade", D("0"), None),
    ("payload.quantidade", D("10000000000.00"), None),
    ("payload.quantidade", float("nan"), None),
    ("payload.precoMaximo", D("0"), None),
    ("payload.precoMaximo", D("100000000000000.00"), None),
    ("payload.demandaId", "   ", None),
    ("payload.demandaId", "x" * 65, None),
    ("payload.compradorId", 7, None),
    ("payload.unidade", "ton", None),
    ("payload.tipo", "AVULSA", None),
    ("payload.destino.uf", "go", None),
    ("payload.destino.cidade", "  ", None),
    ("payload.prazoDesejado", "2026-11-30T23:59:59", None),
    ("payload.criadoEm", "2026-11-20", None),
    ("eventId", "", None),
    ("eventId", 123, None),
    ("eventType", "demanda_cancelada", None),
    ("eventVersion", "2.0", None),
    ("source", "pedidos-service", None),
    ("correlationId", "com espaço", None),
    ("correlationId", "x" * 129, None),
    ("timestamp", "2026-11-20T12:00:00", None),
    ("aggregateId", "x" * 129, None),
    ("payload", [], None),
])
def test_mensagem_invalida(caminho, valor, detalhe):
    with pytest.raises(EventoInvalido) as erro:
        validar_evento("demanda_criada", com(demanda_valida(), caminho, valor))
    if detalhe:
        assert erro.value.detalhe == detalhe


def test_envelope_com_campo_extra_ou_mensagem_que_nao_e_objeto():
    with pytest.raises(EventoInvalido):
        validar_evento("demanda_criada", {**demanda_valida(), "extra": 1})
    with pytest.raises(EventoInvalido):
        validar_evento("demanda_criada", ["não", "é", "objeto"])


def test_campos_adicionais_do_payload_sao_ignorados():
    mensagem = envelope("fornecimento_criado", {
        "fornecimentoId": OF_X, "fornecedorId": "f", "produtoId": P, "unidade": "TON", "preco": D("8.00"),
        "quantidadeDisponivel": D("60.00"), "grupoId": "grupo-1", "criadoEm": "2026-11-20T11:40:00.000Z",
    }, agregado=OF_X)
    assert validar_evento("fornecimento_criado", mensagem).payload["origem"] is None


def test_negociacao_fechada_exige_uuid_e_alocacoes_validas():
    base = envelope("negociacao_fechada", {
        "negociacaoId": "31000000-0000-4000-8000-000000000002", "tipo": "LEILAO", "status": "ENCERRADO",
        "compradorId": None, "demandaId": None, "produtoId": P, "alocacoes": [{"fornecimentoId": OF_X,
                                                                              "quantidade": D("1.00")}],
    }, agregado="31000000-0000-4000-8000-000000000002")
    assert validar_evento("negociacao_fechada", base).payload["alocacoes"][0]["quantidade"] == D("1.00")
    for caminho, valor in (("payload.negociacaoId", "neg-1"), ("payload.status", "CANCELADO"),
                           ("payload.alocacoes", {}), ("payload.demandaId", "")):
        with pytest.raises(EventoInvalido):
            validar_evento("negociacao_fechada", com(base, caminho, valor))
    with pytest.raises(EventoInvalido):
        validar_evento("negociacao_fechada", com(base, "payload.alocacoes", [{"fornecimentoId": OF_X,
                                                                             "quantidade": D("0")}]))


def test_estoque_valida_campos_opcionais_do_schema():
    base = envelope("estoque_atualizado", {"fornecimentoId": OF_X, "quantidadeDisponivel": D("0"),
                                           "operacao": "ENCERRAMENTO", "atualizadoEm": "2026-11-20T12:00:00Z"},
                    agregado=OF_X)
    assert validar_evento("estoque_atualizado", base).payload["quantidadeDisponivel"] == D("0.00")
    with pytest.raises(EventoInvalido):
        validar_evento("estoque_atualizado", com(base, "payload.operacao", "VENDA"))
    with pytest.raises(EventoInvalido):
        validar_evento("estoque_atualizado", com(base, "payload.quantidadeDisponivel", D("-1")))


def test_chave_da_dlt():
    assert chave_dlt({"aggregateId": DEM_A, "eventId": "e"}) == DEM_A
    assert chave_dlt({"aggregateId": "x" * 129, "eventId": "e-1"}) == "e-1"
    assert chave_dlt({"aggregateId": 5, "eventId": 42}) == "42"
    assert chave_dlt({"eventId": "y" * 129}) == "desconhecido"
    assert chave_dlt({"eventId": ""}) == "desconhecido"
    assert chave_dlt({"eventId": None}) == "desconhecido"
    assert chave_dlt("texto qualquer") == "desconhecido"


def test_correlacao_da_dlt_invalida_vira_uuid_novo():
    _, mensagem = evento_dlt("demanda_criada", {"correlationId": "inválida!"}, "EVENTO_INVALIDO", "x", "", 1,
                             t("12:00"))
    assert len(mensagem["correlationId"]) == 36
    _, mensagem = evento_dlt("demanda_criada", {"correlationId": "ok-1"}, "EVENTO_INVALIDO", "x" * 600, "", 1,
                             t("12:00"))
    assert mensagem["correlationId"] == "ok-1"
    assert len(mensagem["payload"]["detalhe"]) == 500
