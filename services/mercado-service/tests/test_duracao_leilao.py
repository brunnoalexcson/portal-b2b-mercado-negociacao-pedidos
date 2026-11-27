"""DG-70 (professor, 27/11): duração do leilão de 24 h por padrão, definida pelo fornecedor no primeiro aceite."""
from decimal import Decimal

from tests.cenarios import FORN_X, FORN_Y, OF_X, OF_Y, Ambiente
from tests.test_http import api, corpo, s1  # noqa: F401  (fixture e utilitários da API)

D = Decimal


def _aceitar(amb, empresa, sugestao, oferta, quantidade, hora, duracao=None):
    amb.em(hora)
    return amb.mercado.aceitar(empresa, sugestao, oferta, D(quantidade), "teste-mrc-09", duracao_horas=duracao)


def test_primeiro_aceite_define_a_duracao_e_os_seguintes_nao_mudam():
    amb = Ambiente().cenario_e()
    s1_id = amb.sugestao_ativa()["sugestaoId"]
    assert _aceitar(amb, FORN_X, s1_id, OF_X, "60.00", "12:00", 48)["duracaoLeilaoHoras"] == 48
    fechado = _aceitar(amb, FORN_Y, s1_id, OF_Y, "40.00", "12:05", 12)
    assert (fechado["situacao"], fechado["duracaoLeilaoHoras"]) == ("CONVERTIDA", 48)
    payload = amb.publicados("leilao_iniciado")[-1]["payload"]
    assert (payload["iniciaEm"], payload["encerraEm"]) == ("2026-11-20T12:05:00.000Z", "2026-11-22T12:05:00.000Z")


def test_sem_duracao_vale_o_padrao_de_24_horas():
    amb = Ambiente().cenario_e()
    s1_id = amb.sugestao_ativa()["sugestaoId"]
    assert _aceitar(amb, FORN_X, s1_id, OF_X, "60.00", "12:00")["duracaoLeilaoHoras"] == 24
    _aceitar(amb, FORN_Y, s1_id, OF_Y, "40.00", "12:05", 72)
    assert amb.publicados("leilao_iniciado")[-1]["payload"]["encerraEm"] == "2026-11-21T12:05:00.000Z"


def test_sugestao_que_volta_a_aberta_esquece_a_duracao():
    amb = Ambiente().cenario_e()
    s1_id = amb.sugestao_ativa()["sugestaoId"]
    _aceitar(amb, FORN_X, s1_id, OF_X, "60.00", "12:00", 48)
    amb.remover(FORN_X, s1_id, OF_X, hora="12:01")
    assert amb.sugestao(s1_id)["duracaoLeilaoHoras"] is None
    assert _aceitar(amb, FORN_X, s1_id, OF_X, "60.00", "12:02")["duracaoLeilaoHoras"] == 24


def test_duracao_pela_api(api):  # noqa: F811
    api.amb.cenario_e()
    caminho = f"/api/v1/mercado/sugestoes-leilao/{s1(api)}/aceites"
    for invalido in ("0", "-1", "1.5", "48.0", "\"48\"", "true", "null", "1000000"):
        resposta = api.post(caminho, empresa=FORN_X, escopo="SUPPLIER",
                            texto=f'{{"fornecimentoId": "{OF_X}", "quantidade": 60, "duracaoLeilaoHoras": {invalido}}}')
        assert (resposta.status_code, corpo(resposta)["codigo"]) == (422, "VALIDACAO_INVALIDA"), invalido
    valido = api.post(caminho, empresa=FORN_X, escopo="SUPPLIER",
                      corpo={"fornecimentoId": OF_X, "quantidade": 60, "duracaoLeilaoHoras": 999999})
    assert (valido.status_code, corpo(valido)["duracaoLeilaoHoras"]) == (201, 999999)
