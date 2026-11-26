"""Testes unitários das funções puras de `app/dominio.py` e `app/contratos.py`."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from app.contratos import (
    MAX_QUANTIDADE, MAX_VALOR, EventoInvalido, chave_dlt, corpo_proposta, corpo_vazio, evento_dlt,
    pedido_temporario_leilao, validar_evento,
)
from app.dominio import (
    Ator, aceitando, acoes_leilao, acoes_permitidas, alocacao, detalhe_direta, detalhe_leilao, fornecedor_do_leilao,
    incremento_minimo, oferta_leilao, participante, participacoes, recalcular_lote, resumo, sigilo, visivel,
)
from app.jsonio import fingerprint, loads
from tests.cenarios import (
    COMP_X, COMP_Y, CORR_L1, D1, FORN_A, FORN_B, FORN_C, L1, OF_A, OF_B, OF_D, P, PT_A, PT_B, PT_D, confirmar_reserva,
    evento_d1, evento_l1, t,
)

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


def test_aceitando_status_e_limite_de_prazo():
    neg = _negociacao_direta()
    assert aceitando(neg, T0) is True
    assert aceitando(neg, EXPIRA - timedelta(milliseconds=1)) is True
    assert aceitando(neg, EXPIRA) is False
    assert aceitando(neg, EXPIRA + timedelta(seconds=1)) is False
    for status_final in ("ENCERRADO", "RECUSADO", "EXPIRADO"):
        assert aceitando(_negociacao_direta(status=status_final), T0) is False


def test_corpo_proposta_valido_e_casos_invalidos():
    assert corpo_proposta({"precoUnitario": D("125.00")}) == D("125.00")
    assert corpo_proposta({"precoUnitario": 125}) == D("125")
    assert corpo_proposta({"precoUnitario": D("0.01")}) == D("0.01")
    assert corpo_proposta({"precoUnitario": MAX_VALOR}) == MAX_VALOR

    invalidos = [
        {"precoUnitario": "125.00"},
        {"precoUnitario": True},
        {"precoUnitario": False},
        {"precoUnitario": 125.0},
        {"precoUnitario": 0},
        {"precoUnitario": D("0.00")},
        {"precoUnitario": -1},
        {"precoUnitario": D("-0.01")},
        {"precoUnitario": D("125.001")},
        {"precoUnitario": D("125.00"), "extra": 1},
        {"precoUnitario": MAX_VALOR + D("0.01")},
        {},
        None,
        [],
    ]
    for caso in invalidos:
        with pytest.raises(EventoInvalido):
            corpo_proposta(caso)


def test_corpo_vazio_aceita_none_ou_dict_vazio_e_recusa_restante():
    assert corpo_vazio(None) == {}
    assert corpo_vazio({}) == {}

    for invalido in ({"precoUnitario": D("125.00")}, {"a": 1}, [], "", 0, False):
        with pytest.raises(EventoInvalido):
            corpo_vazio(invalido)


def test_fingerprint_normaliza_decimais_e_ordem_de_chaves():
    h_int = fingerprint(loads('{"precoUnitario": 125}'))
    h_1 = fingerprint(loads('{"precoUnitario": 125.0}'))
    h_2 = fingerprint(loads('{"precoUnitario": 125.00}'))
    assert h_int == h_1 == h_2

    assert fingerprint({"a": 1, "b": [D("2.0"), {"c": 3, "d": D("4.00")}]}) == fingerprint(
        {"b": [2, {"d": 4, "c": D("3.0")}], "a": D("1.00")},
    )
    assert h_2 != fingerprint({"precoUnitario": D("125.01")})
    assert fingerprint({"v": True}) != fingerprint({"v": 1})


# ---------------------------------------------------------------- leilão: regras puras e contrato

def _negociacao_leilao(**alteracoes) -> dict:
    fim = T0 + timedelta(hours=3)
    base = {
        "negociacaoId": L1,
        "modo": "LEILAO_DIRETO",
        "status": "ABERTO",
        "produtoId": P,
        "dataInicio": T0,
        "dataFim": fim,
        "quantidadeTotal": D("20"),
        "precoInicial": D("2550"),
        "incrementoMinimo": D("25.5"),
        "incrementoMinimoPercentual": D("1.00"),
        "melhorLanceId": None,
        "totalLances": 0,
        "compradorId": None,
        "fornecimentoId": None,
        "fornecedorId": None,
        "quantidade": None,
        "precoOferta": None,
        "precoAtual": None,
        "vez": None,
        "compraImediata": False,
        "expiraEm": None,
        "reservaSituacao": None,
        "pedidoId": None,
        "totalPropostas": 0,
        "correlationId": CORR_L1,
        "criadoEm": T0,
        "encerradoEm": None,
    }
    base.update(alteracoes)
    return base


def _ofertas_l1() -> list[dict]:
    return [
        {
            "negociacaoId": L1,
            "fornecimentoId": OF_A,
            "fornecedorId": FORN_A,
            "quantidade": D("5"),
            "precoUnitario": D("120"),
            "valorItem": D("600"),
            "participacao": D("0.2353"),
            "ordem": 1,
            "situacao": "CONFIRMADA",
            "pedidoId": PT_A,
        },
        {
            "negociacaoId": L1,
            "fornecimentoId": OF_B,
            "fornecedorId": FORN_B,
            "quantidade": D("15"),
            "precoUnitario": D("130"),
            "valorItem": D("1950"),
            "participacao": D("0.7647"),
            "ordem": 2,
            "situacao": "CONFIRMADA",
            "pedidoId": PT_B,
        },
    ]


def test_incremento_minimo_exemplo_rn05_piso_e_round_half_up():
    # Exemplo da RN-05: 600.00 x 1% = 6.00.
    assert incremento_minimo(D("600.00"), D("1.00")) == D("6.00")

    # Piso de 0.01 quando o produto fica abaixo de um centavo ou o percentual é zero.
    assert incremento_minimo(D("0.30"), D("1.00")) == D("0.01")
    assert incremento_minimo(D("0.01"), D("0.10")) == D("0.01")
    assert incremento_minimo(D("2550.00"), D("0")) == D("0.01")

    # Arredondamento ROUND_HALF_UP na 3ª casa decimal (0.005 sobe para 0.01; 0.0049 desce para 0.00).
    assert incremento_minimo(D("100.50"), D("1.00")) == D("1.01")
    assert incremento_minimo(D("100.49"), D("1.00")) == D("1.00")
    assert incremento_minimo(D("1000.01"), D("1.00")) == D("10.00")


def test_participacoes_soma_maior_resto_empate_piso_e_diferenca_negativa():
    # Uma oferta só dá sempre 1.0000.
    assert participacoes([D("600.00")]) == [D("1.0000")]

    # Maior resto: 110/360 = 0.30555... tem o maior resto (0.000055...), recebendo +0.0001 mesmo sendo o menor valor.
    partes_resto = participacoes([D("120.00"), D("130.00"), D("110.00")])
    assert partes_resto == [D("0.3333"), D("0.3611"), D("0.3056")]
    assert sum(partes_resto) == D("1.0000")

    # Empate de restos: o +0.0001 fica com a menor posição (índice 0).
    partes_empate = participacoes([D("100.00"), D("100.00"), D("100.00")])
    assert partes_empate == [D("0.3334"), D("0.3333"), D("0.3333")]
    assert sum(partes_empate) == D("1.0000")

    # Piso de 0.0001 e diferença negativa tirada da maior participação.
    partes_piso = participacoes([D("0.01"), D("1000.00")])
    assert partes_piso == [D("0.0001"), D("0.9999")]
    assert sum(partes_piso) == D("1.0000")

    partes_duplo_piso = participacoes([D("0.01"), D("0.01"), D("1000.00")])
    assert partes_duplo_piso == [D("0.0001"), D("0.0001"), D("0.9998")]
    assert sum(partes_duplo_piso) == D("1.0000")

    # Diferença negativa de -0.0004 em que a maior participação (0.0003) só pode ceder 0.0002 sem cair abaixo de 0.0001,
    # tirando o restante da seguinte de maior participação (empate pela menor posição).
    valores_limite = [D("3")] * 3333 + [D("0.2")] * 5
    partes_limite = participacoes(valores_limite)
    assert sum(partes_limite) == D("1.0000")
    assert min(partes_limite) == D("0.0001")
    assert partes_limite[0] == D("0.0001")
    assert partes_limite[1] == D("0.0001")
    assert partes_limite[2] == D("0.0003")
    assert partes_limite[-5:] == [D("0.0001")] * 5


def test_recalcular_lote_atualiza_negociacao_e_participacoes_do_lote():
    neg = _negociacao_leilao(incrementoMinimoPercentual=D("1.00"))
    ofertas = _ofertas_l1()
    lote = [ofertas[0]]

    recalcular_lote(neg, lote)

    assert neg["quantidadeTotal"] == D("5.00")
    assert neg["precoInicial"] == D("600.00")
    assert neg["incrementoMinimo"] == D("6.00")
    assert lote[0]["participacao"] == D("1.0000")
    # A oferta fora do lote não é alterada por recalcular_lote.
    assert ofertas[1]["participacao"] == D("0.7647")


def test_sigilo_comprador_fornecedor_do_leilao_terceiro_e_none():
    ofertas = _ofertas_l1()
    ofertas[1]["situacao"] = "RECUSADA"

    proprio_comprador = Ator("u1", COMP_X, "BUYER")
    outro_comprador = Ator("u2", COMP_Y, "BUYER")
    forn_confirmado = Ator("u3", FORN_A, "SUPPLIER")
    forn_recusado_como_buyer = Ator("u4", FORN_B, "BUYER")
    forn_terceiro = Ator("u5", FORN_C, "SUPPLIER")

    assert fornecedor_do_leilao(ofertas, forn_confirmado) is True
    assert fornecedor_do_leilao(ofertas, forn_recusado_como_buyer) is True
    assert fornecedor_do_leilao(ofertas, forn_terceiro) is False

    assert sigilo(COMP_X, ofertas, proprio_comprador) == COMP_X
    assert sigilo(COMP_X, ofertas, forn_confirmado) == COMP_X
    assert sigilo(COMP_X, ofertas, forn_recusado_como_buyer) == COMP_X
    assert sigilo(COMP_X, ofertas, outro_comprador) == "OCULTO"
    assert sigilo(COMP_X, ofertas, forn_terceiro) == "OCULTO"
    assert sigilo(None, ofertas, outro_comprador) is None
    assert sigilo(None, ofertas, forn_terceiro) is None


def test_acoes_leilao_escopo_status_conflito_e_prazo():
    ofertas = _ofertas_l1()
    ofertas[1]["situacao"] = "RECUSADA"
    leilao = _negociacao_leilao(status="ABERTO")
    fim = leilao["dataFim"]

    assert acoes_leilao(leilao, ofertas, Ator("u1", COMP_X, "BUYER"), T0) == ["DAR_LANCE"]
    assert acoes_leilao(leilao, ofertas, Ator("u1", COMP_X, "BUYER"), fim - timedelta(milliseconds=1)) == ["DAR_LANCE"]

    # Fornecedor do leilão (inclusive com oferta recusada e escopo BUYER), SUPPLIER, prazo vencido e outros status -> [].
    assert acoes_leilao(leilao, ofertas, Ator("u2", FORN_A, "BUYER"), T0) == []
    assert acoes_leilao(leilao, ofertas, Ator("u3", FORN_B, "BUYER"), T0) == []
    assert acoes_leilao(leilao, ofertas, Ator("u4", COMP_X, "SUPPLIER"), T0) == []
    assert acoes_leilao(leilao, ofertas, Ator("u5", FORN_C, "SUPPLIER"), T0) == []
    assert acoes_leilao(leilao, ofertas, Ator("u1", COMP_X, "BUYER"), fim) == []
    assert acoes_leilao(leilao, ofertas, Ator("u1", COMP_X, "BUYER"), fim + timedelta(seconds=1)) == []
    for status in ("AGUARDANDO_RESERVAS", "ENCERRADO", "DESERTO"):
        assert acoes_leilao(_negociacao_leilao(status=status), ofertas, Ator("u1", COMP_X, "BUYER"), T0) == []


def test_oferta_leilao_e_detalhe_leilao_formatam_campos_e_aplicam_sigilo():
    ofertas = _ofertas_l1()
    proj_oferta = oferta_leilao(ofertas[0])
    assert proj_oferta == {
        "negociacaoId": L1,
        "fornecimentoId": OF_A,
        "fornecedorId": FORN_A,
        "quantidade": D("5.00"),
        "precoUnitario": D("120.00"),
        "valorItem": D("600.00"),
        "participacao": D("0.2353"),
        "situacao": "CONFIRMADA",
    }
    assert "ordem" not in proj_oferta
    assert "pedidoId" not in proj_oferta

    leilao = _negociacao_leilao(compradorId=COMP_Y)
    detalhe_terceiro = detalhe_leilao(leilao, ofertas, Ator("u1", COMP_X, "BUYER"), T0)
    assert detalhe_terceiro["quantidadeTotal"] == D("20.00")
    assert detalhe_terceiro["precoInicial"] == D("2550.00")
    assert detalhe_terceiro["incrementoMinimo"] == D("25.50")
    assert detalhe_terceiro["melhorLance"] is None
    assert detalhe_terceiro["totalLances"] == 0
    assert detalhe_terceiro["compradorId"] == "OCULTO"
    assert detalhe_terceiro["acoesPermitidas"] == ["DAR_LANCE"]
    assert len(detalhe_terceiro["ofertas"]) == 2
    assert "incrementoMinimoPercentual" not in detalhe_terceiro
    assert "melhorLanceId" not in detalhe_terceiro
    assert "correlationId" not in detalhe_terceiro

    detalhe_fornecedor = detalhe_leilao(leilao, ofertas, Ator("u2", FORN_A, "SUPPLIER"), T0)
    assert detalhe_fornecedor["compradorId"] == COMP_Y
    assert detalhe_fornecedor["acoesPermitidas"] == []

    assert pedido_temporario_leilao(leilao, ofertas[0]) == {
        "negociacaoId": L1,
        "tipo": "LEILAO",
        "fornecimentoId": OF_A,
        "fornecedorId": FORN_A,
        "produtoId": P,
        "quantidade": D("5.00"),
        "precoUnitario": D("120.00"),
    }


def test_validar_evento_leilao_iniciado_aceita_percentual_com_muitas_casas_e_zero():
    ev = validar_evento("leilao_iniciado", _com(evento_l1(), "payload.incrementoMinimoPercentual", D("1.23456789")))
    assert ev.payload["incrementoMinimoPercentual"] == D("1.23456789")
    assert ev.payload["quantidadeTotal"] == D("20.00")
    assert len(ev.payload["itens"]) == 2

    ev_zero = validar_evento("leilao_iniciado", _com(evento_l1(), "payload.incrementoMinimoPercentual", 0))
    assert ev_zero.payload["incrementoMinimoPercentual"] == D("0")


@pytest.mark.parametrize("caminho,valor", [
    ("payload.itens.0.participacao", D("0.23531")),
    ("payload.itens.0.participacao", D("0")),
    ("payload.itens.0.participacao", 0),
    ("payload.itens.0.participacao", D("1.0001")),
    ("payload.itens.0.participacao", "0.2353"),
    ("payload.itens.0.participacao", True),
    ("payload.incrementoMinimoPercentual", D("-0.01")),
    ("payload.incrementoMinimoPercentual", -1),
    ("payload.incrementoMinimoPercentual", "1.00"),
    ("payload.incrementoMinimoPercentual", True),
    ("payload.encerraEm", "2026-11-20T15:00:00"),
    ("payload.iniciaEm", "2026-11-20T12:00:00"),
    ("payload.itens.0.valorItem", REMOVER),
])
def test_validar_evento_leilao_iniciado_recusa_casos_invalidos(caminho, valor):
    with pytest.raises(EventoInvalido):
        validar_evento("leilao_iniciado", _com(evento_l1(), caminho, valor))

