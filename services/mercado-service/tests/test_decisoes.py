"""Decisões da etapa 1 (perguntas à ESP) e regras que os CAs não exercitam diretamente."""
from decimal import Decimal

from tests.cenarios import (
    COMP_A, COMP_B, DEM_A, DEM_B, FORN_X, FORN_Y, FORN_Z, OF_W, OF_X, OF_Y, P, Q, Ambiente, erro, t,
)

D = Decimal
R = "35000000-0000-4000-8000-000000000003"
OF_V = "34000000-0000-4000-8000-000000000005"
OF_U = "34000000-0000-4000-8000-000000000006"


def test_decisao_1_api07_e_aptidao_comparam_o_prazo_com_agora():
    amb = Ambiente().cenario_s()
    amb.em("2026-11-30T23:59:59.001Z")
    assert erro(amb.direta, COMP_A, DEM_A, OF_Y, "10.00") == "DEMANDA_ENCERRADA"
    itens = amb.mercado.vitrine(P, COMP_A, DEM_A, 1, 10)["items"]
    assert [item["apta"] for item in itens] == [False, False]
    assert amb.mercado.balanco(P)["demandaTotal"] == D("50.00")
    assert amb.mercado.demanda(COMP_A, DEM_A)["prazoVencido"] is False


def test_decisao_2_saldo_negociavel_nunca_fica_negativo():
    amb = Ambiente().cenario_s()
    amb.oferta(OF_W, hora="12:01")
    d1 = amb.direta(COMP_A, DEM_A, OF_Y, "30.00")["negociacaoId"]
    amb.direta(COMP_A, DEM_A, OF_X, "20.00")
    amb.fechar(d1, "ENCERRADO", [(OF_Y, "40.00")], comprador=COMP_A, hora="12:30")
    resposta = amb.mercado.demanda(COMP_A, DEM_A)
    assert (resposta["saldo"], resposta["emNegociacao"], resposta["saldoNegociavel"]) == (
        D("10.00"), D("20.00"), D("0.00"))
    assert amb.mercado.balanco(P)["demandaTotal"] == D("0.00")
    assert erro(amb.direta, COMP_A, DEM_A, OF_W, "0.01") == "QUANTIDADE_ACIMA_DO_SALDO"


def test_decisao_3_unidade_do_leilao_vem_do_primeiro_item_sem_produto():
    amb = Ambiente()
    amb.oferta(OF_V, FORN_X, R, "1.00", "10.00", origem=None, hora="11:00", unidade="KG")
    amb.oferta(OF_U, FORN_Y, R, "1.00", "10.00", origem=None, hora="11:01", unidade="TON")
    amb.demanda(DEM_A, COMP_A, R, "100.00", hora="11:02")
    sugestao = amb.sugestao_ativa(R)["sugestaoId"]
    assert amb.aceitar(FORN_Y, sugestao, OF_U, "10.00", hora="12:00")["unidade"] is None
    resposta = amb.aceitar(FORN_X, sugestao, OF_V, "10.00", hora="12:01")
    assert resposta["situacao"] == "CONVERTIDA"
    negociacao = amb.mercado.negociacao(COMP_B, "BUYER", resposta["negociacaoId"])
    assert negociacao["unidade"] == "TON"
    assert [item["fornecimentoId"] for item in negociacao["itens"]] == [OF_U, OF_V]


def test_decisao_3_produto_atualizado_nao_revalida_copias():
    amb = Ambiente().cenario_s()
    amb.produto(P, unidade="KG", hora="12:01", topico="produto_atualizado")
    assert amb.mercado.balanco(P)["unidade"] == "KG"
    assert amb.mercado.balanco(P)["ofertaTotal"] == D("100.00")
    assert amb.oferta(OF_W, hora="12:02") == "dlt"


def test_decisao_5_percentual_com_casas_extras_no_leilao_iniciado():
    from app.portas import ConfigMercado
    amb = Ambiente(ConfigMercado(leilao_incremento_minimo_percentual=D("0.125")))
    amb.cenario_l()
    payload = amb.publicados("leilao_iniciado")[0]["payload"]
    assert (str(payload["incrementoMinimoPercentual"]), payload["incrementoMinimo"]) == ("0.125", D("0.98"))


def test_decisao_8_resposta_da_conversao_vem_dos_aceites():
    amb = Ambiente().cenario_e()
    s1 = amb.sugestao_ativa()["sugestaoId"]
    amb.aceitar(FORN_X, s1, OF_X, "60.00", hora="12:00")
    amb.estoque(OF_X, "30.00", "12:05")
    resposta = amb.aceitar(FORN_Y, s1, OF_Y, "40.00", hora="12:10")
    assert (resposta["situacao"], resposta["quantidadeNoLote"], resposta["fornecedoresNoLote"]) == (
        "CONVERTIDA", D("100.00"), 2)
    [leilao] = amb.publicados("leilao_iniciado")
    assert [(i["fornecimentoId"], i["quantidade"]) for i in leilao["payload"]["itens"]] == [
        (OF_X, D("30.00")), (OF_Y, D("40.00"))]


def test_decisao_8_empresa_com_dois_aceites():
    amb = Ambiente().cenario_e()
    amb.oferta(OF_V, FORN_Z, P, "8.30", "5.00", origem=None, hora="12:00")
    amb.oferta(OF_U, FORN_Z, P, "8.40", "5.00", origem=None, hora="12:00")
    s1 = amb.sugestao_ativa()["sugestaoId"]
    amb.aceitar(FORN_Z, s1, OF_U, "5.00", hora="12:01")
    resposta = amb.aceitar(FORN_Z, s1, OF_V, "5.00", hora="12:02")
    assert (resposta["quantidadeNoLote"], resposta["fornecedoresNoLote"], resposta["minhaParticipacao"]) == (
        D("10.00"), 1, {"fornecimentoId": OF_U, "quantidade": D("5.00")})
    amb.aceitar(FORN_X, s1, OF_X, "10.00", hora="12:03")
    assert amb.mercado.sugestoes(FORN_Z, 1, 10)["items"][0]["fornecedoresNoLote"] == 2


def test_rn19_saida_do_ultimo_aceite_com_gatilho_falso_encerra():
    amb = Ambiente().cenario_e()
    s1 = amb.sugestao_ativa()["sugestaoId"]
    amb.aceitar(FORN_X, s1, OF_X, "60.00", hora="12:00")
    amb.cancelar(DEM_B, COMP_B, hora="12:05")
    assert amb.sugestao(s1)["situacao"] == "EM_FORMACAO"
    amb.remover(FORN_X, s1, OF_X, hora="12:06")
    sugestao = amb.sugestao(s1)
    assert (sugestao["situacao"], sugestao["encerradaEm"]) == ("ENCERRADA", t("12:06"))
    assert amb.publicados() == []


def test_rn21c_api07_que_tira_o_gatilho_fecha_o_lote_depois_da_direta():
    amb = Ambiente().cenario_e()
    amb.oferta(OF_V, FORN_Z, P, "8.30", "10.00", origem=None, hora="11:58")
    s1 = amb.sugestao_ativa()["sugestaoId"]
    amb.aceitar(FORN_X, s1, OF_X, "60.00", hora="12:00")
    amb.direta(COMP_B, DEM_B, OF_Y, "40.00", hora="12:01")
    assert amb.sugestao(s1)["situacao"] == "EM_FORMACAO"
    direta = amb.direta(COMP_A, DEM_A, OF_Y, "40.00", hora="12:02", correlacao="teste-direta")
    assert amb.sugestao(s1)["situacao"] == "CONVERTIDA"
    eventos = [(topico, mensagem["payload"]["negociacaoId"]) for topico, _, mensagem in amb.bus.publicados[1:]]
    leilao = amb.sugestao(s1)["negociacaoId"]
    assert eventos == [("modo_negociacao_definido", direta["negociacaoId"]),
                       ("modo_negociacao_definido", leilao), ("leilao_iniciado", leilao)]
    assert {m["correlationId"] for _, _, m in amb.bus.publicados[1:]} == {"teste-direta"}


def test_rn27_acordo_em_demanda_cancelada_abate_sem_reabrir():
    amb = Ambiente().cenario_s()
    d1 = amb.direta(COMP_A, DEM_A, OF_Y, "30.00")["negociacaoId"]
    amb.cancelar(DEM_A, COMP_A, hora="12:10")
    amb.fechar(d1, "ENCERRADO", [(OF_Y, "30.00")], comprador=COMP_A, hora="12:20")
    resposta = amb.mercado.demanda(COMP_A, DEM_A)
    assert (resposta["situacao"], resposta["saldo"], resposta["emNegociacao"]) == ("CANCELADA", D("20.00"),
                                                                                   D("0.00"))


def test_rn01_oferta_que_muda_de_produto_recalcula_os_dois():
    amb = Ambiente().cenario_s()
    amb.repo.bloqueios.clear()
    amb.atualizar_oferta(OF_X, "12:01", produto=Q)
    assert amb.repo.bloqueios == [P, Q]
    assert amb.mercado.balanco(P)["ofertaTotal"] == D("40.00")
    assert amb.mercado.balanco(Q)["ofertaTotal"] == D("70.00")


def test_escritas_bloqueiam_o_produto_antes_das_regras():
    amb = Ambiente().cenario_e()
    amb.repo.bloqueios.clear()
    s1 = amb.sugestao_ativa()["sugestaoId"]
    amb.aceitar(FORN_X, s1, OF_X, "60.00")
    amb.direta(COMP_A, DEM_A, OF_Y, "10.00")
    assert erro(amb.direta, COMP_A, DEM_A, OF_Y, "10.00") == "NEGOCIACAO_JA_ABERTA"
    assert amb.repo.bloqueios == [P, P, P]


def test_demanda_criada_com_id_existente_e_ignorada():
    amb = Ambiente().cenario_s()
    assert amb.demanda(DEM_A, COMP_A, P, "99.00", hora="12:01") == "ignorado"
    assert amb.mercado.demanda(COMP_A, DEM_A)["quantidade"] == D("50.00")
