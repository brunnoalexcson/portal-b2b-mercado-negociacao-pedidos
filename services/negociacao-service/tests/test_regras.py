"""Testes unitários das funções puras de `app/dominio.py` e `app/contratos.py`."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.contratos import (
     MAX_QUANTIDADE, MAX_VALOR, EventoInvalido, chave_dlt, evento_dlt, validar_evento,
)
from app.dominio import Ator, acoes_permitidas, alocacao, detalhe_direta, participante, resumo, visivel
from tests.cenarios import COMP_X, COMP_Y, D1, FORN_A, FORN_B, L1, OF_B, P, PT_D, confirmar_reserva, evento_d1, t

D = Decimal
REMOVER = object()
T0 = t("12:00:00")
EXPIRA = T0 + timedelta(hours=48)


def _negociacao_direta(**alteracoes) -> dict:
    base = {
        "negociacaoId": D1,
        "modo": "VENDA_DIRETA",
        "status": "ABERTO",
        "produtoId": P,
        "dataInicio": T0,
        "dataFim": None,
        "quantidadeTotal": None,
        "precoInicial": None,
        "incrementoMinimo": None,
        "incrementoMinimoPercentual": None,
        "melhorLanceId": None,
        "totalLances": 0,
        "compradorId": COMP_X,
        "fornecimentoId": OF_B,
        "fornecedorId": FORN_B,
        "quantidade": D("10"),
        "precoOferta": D("130"),
        "precoAtual": D("125.5"),
        "vez": "BUYER",
        "compraImediata": False,
        "expiraEm": EXPIRA,
        "reservaSituacao": "PENDENTE",
        "pedidoId": PT_D,
        "totalPropostas": 1,
        "correlationId": "teste-neg-02",
        "criadoEm": T0,
        "encerradoEm": None,
    }
    base.update(alteracoes)
    return base


def _com(mensagem: dict, caminho: str, valor) -> dict:
    copia = copy.deepcopy(mensagem)
    alvo = copia
    *partes, ultimo = caminho.split(".")
    for parte in partes:
        alvo = alvo[int(parte)] if parte.isdigit() else alvo[parte]
    if valor is REMOVER:
        del alvo[ultimo]
    else:
        alvo[ultimo] = valor
    return copia


# ---------------------------------------------------------------- app/dominio.py

def test_participante_e_visivel():
    direta = _negociacao_direta()
    leilao = _negociacao_direta(negociacaoId=L1, modo="LEILAO_DIRETO")

    comprador = Ator("u1", COMP_X, "BUYER")
    fornecedor = Ator("u2", FORN_B, "SUPPLIER")
    outro_comprador = Ator("u3", COMP_Y, "BUYER")
    outro_fornecedor = Ator("u4", FORN_A, "SUPPLIER")
    escopo_invertido_comp = Ator("u5", COMP_X, "SUPPLIER")
    escopo_invertido_forn = Ator("u6", FORN_B, "BUYER")
    transportadora = Ator("u7", COMP_X, "CARRIER")

    assert participante(direta, comprador) is True
    assert participante(direta, fornecedor) is True
    assert participante(direta, outro_comprador) is False
    assert participante(direta, outro_fornecedor) is False
    assert participante(direta, escopo_invertido_comp) is False
    assert participante(direta, escopo_invertido_forn) is False
    assert participante(direta, transportadora) is False

    assert visivel(direta, comprador) is True
    assert visivel(direta, fornecedor) is True
    assert visivel(direta, outro_comprador) is False
    assert visivel(leilao, outro_comprador) is True
    assert visivel(leilao, outro_fornecedor) is True


def test_acoes_permitidas_casos_de_borda():
    comprador = Ator("u1", COMP_X, "BUYER")
    fornecedor = Ator("u2", FORN_B, "SUPPLIER")
    estranho = Ator("u3", COMP_Y, "BUYER")

    aberta_pendente = _negociacao_direta(vez="BUYER", reservaSituacao="PENDENTE")
    assert acoes_permitidas(aberta_pendente, comprador, T0) == ["PROPOR", "RECUSAR"]
    assert acoes_permitidas(aberta_pendente, fornecedor, T0) == ["RECUSAR"]
    assert acoes_permitidas(aberta_pendente, estranho, T0) == []

    aberta_confirmada = _negociacao_direta(vez="BUYER", reservaSituacao="CONFIRMADA")
    assert acoes_permitidas(aberta_confirmada, comprador, T0) == ["PROPOR", "ACEITAR", "RECUSAR"]
    assert acoes_permitidas(aberta_confirmada, fornecedor, T0) == ["RECUSAR"]

    vez_fornecedor = _negociacao_direta(vez="SUPPLIER", reservaSituacao="CONFIRMADA")
    assert acoes_permitidas(vez_fornecedor, fornecedor, T0) == ["PROPOR", "ACEITAR", "RECUSAR"]
    assert acoes_permitidas(vez_fornecedor, comprador, T0) == ["RECUSAR"]

    # Compra imediata, prazo exatamente igual a expiraEm e estados finais não permitem ações.
    assert acoes_permitidas(_negociacao_direta(compraImediata=True, reservaSituacao="CONFIRMADA"), comprador, T0) == []
    assert acoes_permitidas(aberta_confirmada, comprador, EXPIRA) == []
    assert acoes_permitidas(aberta_confirmada, comprador, EXPIRA + timedelta(seconds=1)) == []
    for status_final in ("ENCERRADO", "RECUSADO", "EXPIRADO"):
        assert acoes_permitidas(_negociacao_direta(status=status_final), comprador, T0) == []


def test_alocacao_quantiza_e_arredonda_half_up():
    neg = _negociacao_direta(quantidade=D("3.33"), pedidoId=PT_D)
    assert alocacao(neg, D("10.005")) == {
        "fornecimentoId": OF_B,
        "fornecedorId": FORN_B,
        "pedidoId": PT_D,
        "quantidade": D("3.33"),
        "valorUnitario": D("10.01"),
        "valorTotal": D("33.32"),
    }


def test_resumo_oculta_data_inicio_na_direta_e_mantem_no_leilao():
    direta = _negociacao_direta()
    assert resumo(direta) == {
        "negociacaoId": D1,
        "modo": "VENDA_DIRETA",
        "status": "ABERTO",
        "produtoId": P,
        "dataInicio": None,
        "dataFim": None,
        "expiraEm": EXPIRA,
        "criadoEm": T0,
    }

    fim = T0 + timedelta(hours=3)
    leilao = _negociacao_direta(negociacaoId=L1, modo="LEILAO_DIRETO", status="AGUARDANDO_RESERVAS",
                                dataInicio=T0, dataFim=fim, expiraEm=None)
    assert resumo(leilao) == {
        "negociacaoId": L1,
        "modo": "LEILAO_DIRETO",
        "status": "AGUARDANDO_RESERVAS",
        "produtoId": P,
        "dataInicio": T0,
        "dataFim": fim,
        "expiraEm": None,
        "criadoEm": T0,
    }


def test_detalhe_direta_formata_decimais_e_omite_campos_internos():
    ator = Ator("u1", COMP_X, "BUYER")
    neg = _negociacao_direta(quantidade=D("10"), precoOferta=D("130"), precoAtual=D("125.5"),
                             reservaSituacao="CONFIRMADA")
    detalhe = detalhe_direta(neg, ator, T0)

    assert detalhe["quantidade"] == D("10.00")
    assert detalhe["precoOferta"] == D("130.00")
    assert detalhe["precoAtual"] == D("125.50")
    assert detalhe["acoesPermitidas"] == ["PROPOR", "ACEITAR", "RECUSAR"]
    assert "pedidoId" not in detalhe
    assert "correlationId" not in detalhe
    assert "dataInicio" not in detalhe


# ---------------------------------------------------------------- app/contratos.py

def test_validar_evento_aceita_mensagens_validas_e_modo_leilao():
    ev_direta = validar_evento("modo_negociacao_definido", evento_d1())
    assert ev_direta.payload["quantidade"] == D("10.00")
    assert ev_direta.payload["precoUnitario"] == D("130.00")
    assert ev_direta.payload["compraImediata"] is False

    # Com modo LEILAO_DIRETO, os demais campos do payload não são exigidos (RN-02).
    msg_leilao = _com(evento_d1(), "payload", {"negociacaoId": L1, "modo": "LEILAO_DIRETO"})
    assert validar_evento("modo_negociacao_definido", msg_leilao).payload == {
        "negociacaoId": L1, "modo": "LEILAO_DIRETO",
    }

    # Instante com deslocamento diferente de Z e número inteiro também são aceitos.
    msg_fuso = _com(evento_d1(), "timestamp", "2026-11-20T09:00:00-03:00")
    msg_fuso = _com(msg_fuso, "payload.ofertas.0.quantidade", 10)
    assert validar_evento("modo_negociacao_definido", msg_fuso).payload["quantidade"] == D("10")

    ev_pedido = validar_evento("pedido_atualizado", confirmar_reserva())
    assert ev_pedido.payload["reservaSituacao"] == "CONFIRMADA"


@pytest.mark.parametrize("caminho,valor", [
    ("payload.ofertas.0.quantidade", "10.00"),
    ("payload.ofertas.0.quantidade", True),
    ("payload.ofertas.0.quantidade", False),
    ("payload.ofertas.0.quantidade", 10.0),
    ("payload.ofertas.0.quantidade", float("nan")),
    ("payload.ofertas.0.quantidade", D("10.001")),
    ("payload.ofertas.0.quantidade", D("0")),
    ("payload.ofertas.0.quantidade", D("-1.00")),
    ("payload.ofertas.0.quantidade", MAX_QUANTIDADE + D("0.01")),
    ("payload.ofertas.0.precoUnitario", "130.00"),
    ("payload.ofertas.0.precoUnitario", True),
    ("payload.ofertas.0.precoUnitario", 130.0),
    ("payload.ofertas.0.precoUnitario", D("130.999")),
    ("payload.ofertas.0.precoUnitario", MAX_VALOR + D("0.01")),
    ("payload.produtoId", "x" * 65),
    ("payload.produtoId", "   "),
    ("payload.demandas.0.compradorId", "x" * 65),
    ("payload.ofertas.0.fornecimentoId", "x" * 65),
    ("payload.ofertas.0.fornecedorId", "x" * 65),
    ("payload.negociacaoId", "nao-e-uuid"),
    ("payload.modo", "LEILAO_REVERSO"),
    ("payload.compraImediata", "false"),
    ("payload.compraImediata", 1),
    ("timestamp", "2026-11-20T12:00:00"),
    ("timestamp", "data-invalida"),
    ("timestamp", 12345),
    ("campoExtra", "fora-do-envelope"),
    ("eventId", "   "),
    ("eventType", "pedido_atualizado"),
    ("eventVersion", "2.0"),
    ("source", "pedidos-service"),
    ("correlationId", "invalido com espaco"),
    ("correlationId", "a" * 129),
    ("aggregateId", ""),
    ("aggregateId", "a" * 129),
    ("payload", []),
])
def test_validar_evento_recusa_casos_de_borda(caminho, valor):
    with pytest.raises(EventoInvalido):
        validar_evento("modo_negociacao_definido", _com(evento_d1(), caminho, valor))


def test_validar_evento_recusa_objeto_nao_dict_e_pedido_atualizado_com_id_longo():
    with pytest.raises(EventoInvalido):
        validar_evento("pedido_atualizado", ["lista"])
    for campo in ("pedidoId", "negociacaoId", "fornecimentoId"):
        with pytest.raises(EventoInvalido):
            validar_evento("pedido_atualizado", _com(confirmar_reserva(), f"payload.{campo}", "x" * 65))


def test_chave_dlt_e_evento_dlt():
    assert chave_dlt({"aggregateId": D1, "eventId": "e-1"}) == D1
    assert chave_dlt({"aggregateId": "", "eventId": "e-1"}) == "e-1"
    assert chave_dlt({"aggregateId": "a" * 129, "eventId": 99}) == "99"
    assert chave_dlt({"eventId": "a" * 129}) == "desconhecido"
    assert chave_dlt({"eventId": ""}) == "desconhecido"
    assert chave_dlt({"eventId": None}) == "desconhecido"
    assert chave_dlt("nao-dict") == "desconhecido"

    chave, env = evento_dlt(
        "pedido_atualizado", {"aggregateId": PT_D, "correlationId": "corr-ok"},
        "EVENTO_INVALIDO", "x" * 600, "", 1, T0,
    )
    assert chave == PT_D == env["aggregateId"]
    assert env["correlationId"] == "corr-ok"
    assert len(env["payload"]["detalhe"]) == 500

    _, env_sem_corr = evento_dlt("pedido_atualizado", {"correlationId": "inválida!"}, "EVENTO_INVALIDO", "", "", 1, T0)
    assert UUID(env_sem_corr["correlationId"]).version == 4
    assert env_sem_corr["payload"]["detalhe"] == "EVENTO_INVALIDO"
