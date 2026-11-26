"""Regras puras: valorItem, incremento, participação (RN-22, RN-23, RNF-01), gatilho, balanço e lote (RN-21)."""
import random
from decimal import Decimal
from datetime import datetime, timezone

import pytest

from app.contratos import formatar_percentual
from app.dominio import (
    gatilho_sugestao, incremento_minimo, montar_lote, participacoes, situacao_balanco, validar_config, valor_item,
)
from app.portas import ConfigMercado

D = Decimal
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def test_valor_item_arredonda_meio_para_cima():
    assert valor_item(D("60.00"), D("8.00")) == D("480.00")
    assert valor_item(D("0.01"), D("0.01")) == D("0.00")
    assert valor_item(D("0.05"), D("0.10")) == D("0.01")
    assert valor_item(D("9999999999.99"), D("99999999999999.99")) > D("99999999999999.99")


def test_incremento_minimo():
    assert incremento_minimo(D("780.00"), D("1")) == D("7.80")
    assert incremento_minimo(D("480.00"), D("1")) == D("4.80")
    assert incremento_minimo(D("0.30"), D("1")) == D("0.01")
    assert incremento_minimo(D("100.00"), D("0")) == D("0.01")
    assert incremento_minimo(D("1000.50"), D("0.125")) == D("1.25")


def test_participacoes_dos_exemplos_da_rnf01():
    assert participacoes([D("480.00"), D("300.00")]) == [D("0.6154"), D("0.3846")]
    assert participacoes([D("0.10"), D("0.20")]) == [D("0.3333"), D("0.6667")]
    assert participacoes([D("5.00")] * 3) == [D("0.3334"), D("0.3333"), D("0.3333")]
    assert participacoes([D("0.01"), D("1000.00")]) == [D("0.0001"), D("0.9999")]
    iguais = participacoes([D("1.00")] * 155)
    assert iguais == [D("0.0065")] * 80 + [D("0.0064")] * 75
    assert sum(iguais) == D("1.0000")


def test_participacoes_com_piso_tira_do_maior_sem_ficar_negativa():
    partes = participacoes([D("0.01")] * 999 + [D("1000000.00")])
    assert sum(partes) == D("1.0000")
    assert min(partes) == D("0.0001")
    assert partes[-1] == D("0.9001")


def test_participacoes_somam_um_em_lotes_aleatorios():
    gerador = random.Random(7)
    for _ in range(300):
        tamanho = gerador.randint(1, 1000)
        valores = [D(gerador.choice([1, 1, 3, 17, 250, 99999])) / 100 * gerador.randint(1, 9) for _ in range(tamanho)]
        partes = participacoes([v.quantize(D("0.01")) or D("0.01") for v in valores])
        assert sum(partes) == D("1.0000")
        assert all(D("0.0001") <= parte <= 1 for parte in partes)
        assert all(parte == parte.quantize(D("0.0001")) for parte in partes)


def test_gatilho_e_situacao_do_balanco():
    assert situacao_balanco(D("0"), D("0")) == "SEM_DEMANDA"
    assert situacao_balanco(D("100"), D("0")) == "SEM_DEMANDA"
    assert situacao_balanco(D("0"), D("1")) == "SEM_OFERTA"
    assert situacao_balanco(D("100"), D("100")) == "OFERTA_SUFICIENTE"
    assert situacao_balanco(D("100"), D("130")) == "DEMANDA_EXCEDENTE"
    assert gatilho_sugestao(D("100"), D("130"), D("0"))
    assert not gatilho_sugestao(D("100"), D("100"), D("0"))
    assert not gatilho_sugestao(D("0"), D("130"), D("0"))
    assert not gatilho_sugestao(D("100"), D("150"), D("50"))
    assert gatilho_sugestao(D("100"), D("151"), D("50"))


def _oferta(fornecimento, quantidade, preco="8.00"):
    return {"fornecimentoId": fornecimento, "fornecedorId": "f-" + fornecimento, "produtoId": "p",
            "unidade": "TON", "preco": D(preco), "quantidadeDisponivel": D(quantidade), "origem": None}


def _aceite(fornecimento, quantidade):
    return {"sugestaoId": "s", "fornecimentoId": fornecimento, "fornecedorId": "f-" + fornecimento,
            "quantidade": D(quantidade), "aceitoEm": T0}


def test_lote_usa_minimo_entre_aceite_e_disponivel_e_tira_itens_sem_valor():
    ofertas = {"a": _oferta("a", "30.00"), "b": _oferta("b", "0.00"), "c": _oferta("c", "5.00", "0.01"),
               "d": _oferta("d", "10.00", "0.00"), "e": None}
    aceites = [_aceite("a", "60.00"), _aceite("b", "10.00"), _aceite("c", "0.01"), _aceite("d", "1.00"),
               _aceite("e", "1.00")]
    itens = montar_lote(aceites, ofertas)
    assert [(i["fornecimentoId"], i["quantidade"], i["valorItem"], i["participacao"]) for i in itens] == [
        ("a", D("30.00"), D("240.00"), D("1.0000"))]


def test_lote_respeita_limites_tecnicos_na_ordem():
    ofertas = {f"o{i}": _oferta(f"o{i}", "1.00", "1.00") for i in range(1005)}
    aceites = [_aceite(f"o{i}", "1.00") for i in range(1005)]
    itens = montar_lote(aceites, ofertas)
    assert len(itens) == 1000
    assert itens[-1]["fornecimentoId"] == "o999"

    grandes = {"a": _oferta("a", "9999999999.99", "1.00"), "b": _oferta("b", "1.00", "1.00"),
               "c": _oferta("c", "9000000000.00", "11111.12"), "d": _oferta("d", "0.01", "1.00")}
    itens = montar_lote([_aceite("a", "9999999999.99"), _aceite("b", "1.00"), _aceite("c", "9000000000.00"),
                         _aceite("d", "0.01")], grandes)
    assert [i["fornecimentoId"] for i in itens] == ["a"]


def test_percentual_publicado_tem_no_minimo_duas_casas():
    assert str(formatar_percentual(D("1"))) == "1.00"
    assert str(formatar_percentual(D("1.5"))) == "1.50"
    assert str(formatar_percentual(D("0.125"))) == "0.125"


@pytest.mark.parametrize("config", [
    ConfigMercado(excesso_demanda_minimo_percentual=D("-1")),
    ConfigMercado(leilao_janela_adesao_minutos=0),
    ConfigMercado(leilao_duracao_horas=0),
    ConfigMercado(leilao_incremento_minimo_percentual=D("-0.01")),
    ConfigMercado(leilao_duracao_horas=True),
])
def test_configuracao_invalida_impede_subida(config):
    with pytest.raises(ValueError):
        validar_config(config)
