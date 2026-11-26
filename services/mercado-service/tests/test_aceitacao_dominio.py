"""Critérios de aceitação do §8 no nível do domínio, com os dublês das portas.

As partes de cada CA que dependem da camada HTTP (token, escopo, corpo, paginação inválida, códigos HTTP,
RFC 7807, `/health`) ficam para a suíte HTTP da etapa 3; aqui cada erro é verificado pelo `codigo` do §10."""
import uuid
from decimal import Decimal

import pytest

from app.dominio import Idempotencia, Mercado
from app.portas import ConfigMercado, RepositorioIndisponivel
from tests.cenarios import (
    ANAPOLIS, COMP_A, COMP_B, DEM_A, DEM_B, DEM_X, FORN_X, FORN_Y, FORN_Z, GOIANIA, JATAI, OF_Q, OF_W, OF_X, OF_Y, P,
    Q, RIO_VERDE, Ambiente, contratos_disponiveis, envelope, erro, exemplo, t, validar_schema,
)
from tests.dubles import EventBusMemoria, RelogioFalso, RepositorioMemoria

D = Decimal
precisa_contratos = pytest.mark.skipif(not contratos_disponiveis(), reason="docs/mercado/contratos indisponível")


@pytest.fixture
def amb():
    return Ambiente()


def vitrine(amb, produto=P, empresa=COMP_A, demanda=None):
    return amb.mercado.vitrine(produto, empresa, demanda, 1, 10)


def ofertas_na_vitrine(amb, **kwargs):
    return {item["fornecimentoId"]: item for item in vitrine(amb, **kwargs)["items"]}


def demanda(amb, demanda_id=DEM_A, empresa=COMP_A):
    return amb.mercado.demanda(empresa, demanda_id)


def balanco(amb, produto=P):
    return amb.mercado.balanco(produto)


def dlts(amb):
    return amb.publicados("mercado.dlt")


# ---------------------------------------------------------------- RF-01

def test_ca01_oferta_nova_aparece_na_vitrine(amb):
    amb.produto(P)
    amb.oferta(OF_X)
    assert vitrine(amb)["items"] == [{"fornecimentoId": OF_X, "fornecedorId": FORN_X, "preco": D("8.00"),
                                      "quantidadeDisponivel": D("60.00"), "origem": RIO_VERDE, "apta": None}]


def test_ca02_evento_de_estoque_mais_antigo_e_ignorado(amb):
    amb.produto(P)
    amb.oferta(OF_X)
    assert amb.estoque(OF_X, "30.00", "12:20") == "aplicado"
    assert amb.estoque(OF_X, "50.00", "12:10") == "ignorado"
    assert ofertas_na_vitrine(amb)[OF_X]["quantidadeDisponivel"] == D("30.00")
    assert len(amb.repo.tabela("processados")) == 4
    assert dlts(amb) == []
    amb.atualizar_oferta(OF_X, "12:10", preco="8.50", quantidade="50.00")
    item = ofertas_na_vitrine(amb)[OF_X]
    assert (item["preco"], item["quantidadeDisponivel"]) == (D("8.50"), D("30.00"))


def test_ca03_alteracao_de_preco_e_oferta_zerada(amb):
    amb.cenario_s()
    amb.atualizar_oferta(OF_X, "12:01", preco="8.50", quantidade="60.00")
    assert ofertas_na_vitrine(amb)[OF_X]["preco"] == D("8.50")
    amb.atualizar_oferta(OF_X, "12:02", preco="0.00")
    assert OF_X not in ofertas_na_vitrine(amb)
    assert balanco(amb)["ofertaTotal"] == D("40.00")


def test_ca04_oferta_com_unidade_diferente_vai_para_a_dlt(amb):
    amb.produto(P, unidade="TON")
    assert amb.oferta(OF_X, unidade="KG") == "dlt"
    [mensagem] = dlts(amb)
    assert amb.bus.publicados[-1][1] == OF_X
    assert mensagem["payload"]["error"] == "EVENTO_INVALIDO"
    assert mensagem["payload"]["failedAtAttempt"] == 1
    assert mensagem["payload"]["detalhe"] == "Unidade diferente da do produto."
    assert vitrine(amb)["items"] == []


def test_ca05_estoque_de_oferta_desconhecida_e_ignorado(amb):
    oferta = "34000000-0000-4000-8000-000000000099"
    assert amb.estoque(oferta, "10.00", "12:00") == "ignorado"
    assert amb.repo.tabela("ofertas") == {}
    assert len(amb.repo.tabela("processados")) == 1
    assert amb.publicados() == []


# ---------------------------------------------------------------- RF-02

def test_ca06_produto_cadastrado_define_a_unidade_do_balanco(amb):
    amb.produto(P, "Milho em grão", "TON")
    resposta = balanco(amb)
    assert (resposta["unidade"], resposta["ofertaTotal"], resposta["demandaTotal"], resposta["situacao"]) == (
        "TON", D("0.00"), D("0.00"), "SEM_DEMANDA")
    assert resposta["situacaoDesde"] is None


def test_ca07_produto_sem_nome_vai_para_a_dlt(amb):
    assert amb.produto(P, nome="   ") == "dlt"
    assert dlts(amb)[0]["payload"]["error"] == "EVENTO_INVALIDO"
    assert erro(balanco, amb) == "PRODUTO_NAO_ENCONTRADO"


# ---------------------------------------------------------------- RF-03

def test_ca08_demanda_unica_aguardando_oferta(amb):
    amb.produto(P)
    amb.dem_a()
    resposta = demanda(amb)
    assert (resposta["situacao"], resposta["saldo"], resposta["emNegociacao"], resposta["saldoNegociavel"]) == (
        "AGUARDANDO", D("50.00"), D("0.00"), D("50.00"))
    assert (balanco(amb)["demandaTotal"], balanco(amb)["situacao"]) == (D("50.00"), "SEM_OFERTA")
    assert amb.repo.tabela("sugestoes") == {}


def test_ca09_modelo_recorrente_nao_entra_no_balanco(amb):
    amb.cenario_s()
    amb.demanda("36000000-0000-4000-8000-000000000003", COMP_A, P, "10.00", tipo="RECORRENTE", hora="12:00")
    assert balanco(amb)["demandaTotal"] == D("50.00")
    assert erro(demanda, amb, "36000000-0000-4000-8000-000000000003") == "DEMANDA_NAO_ENCONTRADA"


def test_ca10_ocorrencia_de_demanda_recorrente_entra_no_balanco(amb):
    amb.cenario_s()
    amb.recorrente("36000000-0000-4000-8000-000000000003", "36000000-0000-4000-8000-000000000031", COMP_A, P,
                   "10.00", hora="12:00")
    resposta = demanda(amb, "36000000-0000-4000-8000-000000000031")
    assert (resposta["demandaOrigemId"], resposta["situacao"]) == ("36000000-0000-4000-8000-000000000003",
                                                                   "AGUARDANDO")
    assert balanco(amb)["demandaTotal"] == D("60.00")


def test_ca11_demanda_sem_destino_vai_para_a_dlt(amb):
    payload = amb.payload_demanda(DEM_A, COMP_A, P, "50.00")
    del payload["destino"]
    assert amb.receber("demanda_criada", payload, agregado=DEM_A) == "dlt"
    assert amb.bus.publicados[-1][1] == DEM_A
    assert (dlts(amb)[0]["payload"]["error"], dlts(amb)[0]["payload"]["failedAtAttempt"]) == ("EVENTO_INVALIDO", 1)
    assert erro(demanda, amb) == "DEMANDA_NAO_ENCONTRADA"


# ---------------------------------------------------------------- RF-04

def test_ca12_demanda_aguardando_e_cancelada(amb):
    amb.cenario_e()
    s1 = amb.sugestao_ativa()["sugestaoId"]
    amb.cancelar(DEM_B, COMP_B, hora="12:30")
    resposta = demanda(amb, DEM_B, COMP_B)
    assert (resposta["situacao"], resposta["encerradaEm"]) == ("CANCELADA", t("12:30"))
    assert (balanco(amb)["demandaTotal"], balanco(amb)["situacao"]) == (D("50.00"), "OFERTA_SUFICIENTE")
    desconhecida = "36000000-0000-4000-8000-000000000009"
    amb.cancelar(desconhecida, COMP_B, hora="12:31")
    amb.demanda(desconhecida, COMP_B, P, "10.00", hora="12:32")
    assert demanda(amb, desconhecida, COMP_B)["situacao"] == "CANCELADA"
    assert balanco(amb)["demandaTotal"] == D("50.00")
    assert amb.sugestao(s1)["situacao"] == "ENCERRADA"


def test_ca13_cancelar_modelo_recorrente_cancela_ocorrencias_abertas(amb):
    modelo = "36000000-0000-4000-8000-000000000003"
    amb.cenario_s()
    amb.demanda(modelo, COMP_A, P, "10.00", tipo="RECORRENTE", hora="11:58")
    amb.recorrente(modelo, "36000000-0000-4000-8000-000000000031", COMP_A, P, "10.00", hora="11:59")
    amb.recorrente(modelo, "36000000-0000-4000-8000-000000000032", COMP_A, P, "10.00", hora="12:00")
    assert balanco(amb)["demandaTotal"] == D("70.00")
    amb.cancelar(modelo, COMP_A, hora="12:10")
    for ocorrencia in ("36000000-0000-4000-8000-000000000031", "36000000-0000-4000-8000-000000000032"):
        assert demanda(amb, ocorrencia)["situacao"] == "CANCELADA"
    assert balanco(amb)["demandaTotal"] == D("50.00")
    antes = len(amb.publicados())
    assert amb.recorrente(modelo, "36000000-0000-4000-8000-000000000033", COMP_A, P, "10.00",
                          hora="12:20") == "ignorado"
    assert erro(demanda, amb, "36000000-0000-4000-8000-000000000033") == "DEMANDA_NAO_ENCONTRADA"
    assert len(amb.publicados()) == antes
    assert len(amb.repo.tabela("processados")) == 11


def test_ca14_cancelamento_repetido_desconhecido_e_modelo_ainda_desconhecido(amb):
    amb.cenario_s()
    amb.cancelar(DEM_A, COMP_A, hora="12:00")
    demandas_antes = dict(amb.repo.tabela("demandas"))
    processados = len(amb.repo.tabela("processados"))
    assert amb.cancelar(DEM_A, COMP_A, hora="12:01") == "ignorado"
    amb.cancelar("36000000-0000-4000-8000-000000000098", COMP_B, hora="12:02")
    assert amb.repo.tabela("demandas") == demandas_antes
    assert len(amb.repo.tabela("processados")) == processados + 2
    assert amb.publicados() == []
    origem = "36000000-0000-4000-8000-000000000004"
    amb.recorrente(origem, "36000000-0000-4000-8000-000000000041", COMP_B, P, "10.00", hora="12:03")
    amb.cancelar(origem, COMP_B, hora="12:04")
    assert demanda(amb, "36000000-0000-4000-8000-000000000041", COMP_B)["situacao"] == "CANCELADA"
    assert amb.recorrente(origem, "36000000-0000-4000-8000-000000000042", COMP_B, P, "10.00",
                          hora="12:05") == "ignorado"
    assert erro(demanda, amb, "36000000-0000-4000-8000-000000000042", COMP_B) == "DEMANDA_NAO_ENCONTRADA"
    assert amb.publicados() == []


# ---------------------------------------------------------------- RF-05

def test_ca15_demanda_maior_que_a_oferta(amb):
    amb.cenario_e()
    resposta = balanco(amb)
    assert resposta == {
        "produtoId": P, "unidade": "TON", "ofertaTotal": D("100.00"), "demandaTotal": D("130.00"),
        "situacao": "DEMANDA_EXCEDENTE", "situacaoDesde": t("11:57"), "excessoDemanda": D("30.00"),
        "sugestaoId": amb.sugestao_ativa()["sugestaoId"], "agora": t("12:00"),
    }
    assert amb.sugestao_ativa()["abertaEm"] == t("11:57")


def test_ca16_oferta_cobre_a_demanda(amb):
    amb.cenario_s()
    resposta = balanco(amb)
    assert (resposta["ofertaTotal"], resposta["demandaTotal"], resposta["situacao"], resposta["excessoDemanda"],
            resposta["sugestaoId"]) == (D("100.00"), D("50.00"), "OFERTA_SUFICIENTE", D("0.00"), None)


def test_ca17_produto_desconhecido(amb):
    amb.cenario_s()
    assert erro(balanco, amb, "35000000-0000-4000-8000-000000000077") == "PRODUTO_NAO_ENCONTRADO"


def test_ca18_excesso_abaixo_do_limiar_nao_abre_sugestao():
    amb = Ambiente(ConfigMercado(excesso_demanda_minimo_percentual=D("50")))
    amb.cenario_e()
    resposta = balanco(amb)
    assert (resposta["situacao"], resposta["excessoDemanda"], resposta["sugestaoId"]) == (
        "DEMANDA_EXCEDENTE", D("30.00"), None)
    amb.demanda("36000000-0000-4000-8000-000000000005", COMP_B, P, "21.00", destino=GOIANIA, hora="12:01")
    resposta = balanco(amb)
    assert resposta["demandaTotal"] == D("151.00")
    assert resposta["sugestaoId"] is not None


# ---------------------------------------------------------------- RF-06

def test_ca19_sugestao_aparece_so_para_fornecedores_elegiveis(amb):
    amb.cenario_e()
    [item] = amb.mercado.sugestoes(FORN_X, 1, 10)["items"]
    assert item == {
        "sugestaoId": amb.sugestao_ativa()["sugestaoId"], "produtoId": P, "unidade": "TON", "situacao": "ABERTA",
        "excessoDemanda": D("30.00"), "quantidadeNoLote": D("0.00"), "fornecedoresNoLote": 0,
        "janelaEncerraEm": None, "duracaoLeilaoHoras": 24, "minhaParticipacao": None, "negociacaoId": None,
        "agora": t("12:00"),
    }
    assert amb.mercado.sugestoes(FORN_Z, 1, 10) == {"items": [], "page": 1, "size": 10, "total": 0}


def test_ca20_sugestao_some_quando_a_oferta_passa_a_cobrir_a_demanda(amb):
    amb.cenario_e()
    s1 = amb.sugestao_ativa()["sugestaoId"]
    amb.oferta(OF_W, hora="12:30")
    assert (amb.sugestao(s1)["situacao"], amb.sugestao(s1)["encerradaEm"]) == ("ENCERRADA", t("12:30"))
    assert amb.mercado.sugestoes(FORN_X, 1, 10)["items"] == []
    assert balanco(amb)["situacao"] == "OFERTA_SUFICIENTE"


# ---------------------------------------------------------------- RF-07

def test_ca22_vitrine_ordenada_por_menor_preco_com_aptidao(amb):
    amb.cenario_e()
    resposta = vitrine(amb, demanda=DEM_A)
    assert [(i["fornecimentoId"], i["preco"], i["apta"]) for i in resposta["items"]] == [
        (OF_Y, D("7.50"), True), (OF_X, D("8.00"), True)]
    assert resposta["total"] == 2
    assert resposta["items"][0]["origem"] == JATAI


def test_ca23_oferta_acima_do_preco_maximo_nao_e_apta(amb):
    amb.cenario_e()
    amb.atualizar_oferta(OF_X, "12:01", preco="9.50")
    itens = ofertas_na_vitrine(amb, demanda=DEM_A)
    assert (itens[OF_Y]["apta"], itens[OF_X]["apta"], itens[OF_X]["preco"]) == (True, False, D("9.50"))


def test_ca24_demanda_de_outra_empresa_ou_de_outro_produto(amb):
    amb.cenario_e()
    assert erro(vitrine, amb, demanda=DEM_B) == "DEMANDA_NAO_ENCONTRADA"
    assert erro(vitrine, amb, produto=Q, demanda=DEM_A) == "PRODUTO_DIFERENTE"


def test_ca25_demanda_visivel_so_ao_dono(amb):
    amb.cenario_e()
    assert erro(demanda, amb, DEM_B, COMP_A) == "DEMANDA_NAO_ENCONTRADA"


# ---------------------------------------------------------------- RF-08

def test_ca26_comprador_abre_negociacao_direta(amb):
    amb.cenario_s()
    resposta = amb.direta(COMP_A, DEM_A, OF_Y, "30.00", hora="12:05")
    negociacao = resposta["negociacaoId"]
    assert resposta == {
        "negociacaoId": negociacao, "modo": "VENDA_DIRETA", "produtoId": P, "unidade": "TON", "situacao": "ABERTA",
        "demandaId": DEM_A, "compradorId": COMP_A, "compraImediata": False, "quantidadeTotal": D("30.00"),
        "valorReferencia": D("225.00"), "precoInicial": None, "incrementoMinimo": None, "sugestaoId": None,
        "iniciaEm": None, "encerraEm": None,
        "itens": [{"fornecimentoId": OF_Y, "fornecedorId": FORN_Y, "quantidade": D("30.00"),
                   "precoUnitario": D("7.50"), "valorItem": D("225.00"), "participacao": D("1.0000"),
                   "origem": JATAI}],
        "definidaEm": t("12:05"), "encerradaEm": None, "contextoProduto": None, "agora": t("12:05"),
    }
    [(topico, chave, mensagem)] = amb.bus.publicados
    assert (topico, chave, mensagem["aggregateId"]) == ("modo_negociacao_definido", negociacao, negociacao)
    assert mensagem["correlationId"] == "teste-mrc-05"
    assert mensagem["payload"]["compraImediata"] is False
    assert mensagem["payload"]["demandas"][0]["quantidade"] == D("30.00")
    assert mensagem["payload"]["demandas"][0]["destino"] == ANAPOLIS
    saldo = demanda(amb)
    assert (saldo["saldo"], saldo["emNegociacao"], saldo["saldoNegociavel"]) == (D("50.00"), D("30.00"), D("20.00"))
    assert balanco(amb)["demandaTotal"] == D("20.00")


@precisa_contratos
def test_ca26_evento_igual_ao_exemplo_e_valido_contra_o_schema(amb):
    amb.cenario_s()
    negociacao = amb.direta(COMP_A, DEM_A, OF_Y, "30.00", hora="12:05")["negociacaoId"]
    [mensagem] = amb.publicados("modo_negociacao_definido")
    validar_schema("modo_negociacao_definido", mensagem)
    esperado = exemplo("modo_negociacao_definido")
    esperado["payload"]["negociacaoId"] = esperado["aggregateId"] = negociacao
    from tests.cenarios import json_publicado
    publicado = json_publicado(mensagem)
    assert {k: v for k, v in publicado.items() if k != "eventId"} == {
        k: v for k, v in esperado.items() if k != "eventId"}
    uuid.UUID(publicado["eventId"])


def test_ca27_negociacoes_simultaneas_limitadas_ao_saldo(amb):
    amb.cenario_s()
    amb.direta(COMP_A, DEM_A, OF_Y, "30.00", hora="12:05")
    assert erro(amb.direta, COMP_A, DEM_A, OF_X, "20.01") == "QUANTIDADE_ACIMA_DO_SALDO"
    amb.direta(COMP_A, DEM_A, OF_X, "20.00")
    assert (demanda(amb)["emNegociacao"], demanda(amb)["saldoNegociavel"]) == (D("50.00"), D("0.00"))
    assert erro(amb.direta, COMP_A, DEM_A, OF_Y, "1.00") == "NEGOCIACAO_JA_ABERTA"


def test_ca28_preco_acima_do_maximo_e_quantidade_acima_do_disponivel(amb):
    amb.cenario_e()
    amb.atualizar_oferta(OF_X, "12:01", preco="9.50")
    assert erro(amb.direta, COMP_A, DEM_A, OF_X, "10.00") == "PRECO_ACIMA_DO_MAXIMO"
    assert erro(amb.direta, COMP_B, DEM_B, OF_Y, "40.01") == "QUANTIDADE_INDISPONIVEL"


def test_ca29_empresa_nao_negocia_com_oferta_propria(amb):
    amb.cenario_s()
    amb.demanda(DEM_X, FORN_X, P, "5.00", destino=RIO_VERDE, hora="11:58")
    antes = len(amb.publicados())
    assert erro(amb.direta, FORN_X, DEM_X, OF_X, "5.00") == "CONFLITO_DE_INTERESSE"
    assert len(amb.publicados()) == antes


def test_ca30_oferta_inexistente_ou_de_outro_produto(amb):
    amb.cenario_s()
    assert erro(amb.direta, COMP_A, DEM_A, "34000000-0000-4000-8000-000000000099", "1.00") == "OFERTA_INDISPONIVEL"
    assert erro(amb.direta, COMP_A, DEM_A, OF_Q, "1.00") == "PRODUTO_DIFERENTE"


def test_ca31_demanda_de_outra_empresa_ou_encerrada(amb):
    amb.cenario_e()
    amb.cancelar(DEM_A, COMP_A, hora="12:00")
    assert erro(amb.direta, COMP_A, DEM_B, OF_Y, "1.00") == "DEMANDA_NAO_ENCONTRADA"
    assert erro(amb.direta, COMP_A, DEM_A, OF_Y, "1.00") == "DEMANDA_ENCERRADA"


def test_ca32_valor_do_item_zerado_e_rejeitado_sem_efeito(amb):
    amb.cenario_s()
    amb.atualizar_oferta(OF_Y, "12:01", preco="0.01")
    antes = len(amb.publicados())
    assert erro(amb.direta, COMP_A, DEM_A, OF_Y, "0.01") == "VALIDACAO_INVALIDA"
    assert len(amb.publicados()) == antes
    assert demanda(amb)["emNegociacao"] == D("0.00")


def test_ca33_compra_imediata(amb):
    amb.cenario_s()
    amb.direta(COMP_A, DEM_A, OF_Y, "30.00")
    resposta = amb.direta(COMP_A, DEM_A, OF_X, "10.00", compra_imediata=True)
    assert resposta["compraImediata"] is True
    assert [(i["fornecimentoId"], i["quantidade"], i["precoUnitario"], i["valorItem"], i["participacao"])
            for i in resposta["itens"]] == [(OF_X, D("10.00"), D("8.00"), D("80.00"), D("1.0000"))]
    mensagem = amb.publicados("modo_negociacao_definido")[-1]
    assert mensagem["payload"]["compraImediata"] is True
    if contratos_disponiveis():
        validar_schema("modo_negociacao_definido", mensagem)
    assert (demanda(amb)["emNegociacao"], demanda(amb)["saldoNegociavel"]) == (D("40.00"), D("10.00"))


def test_ca33_idempotencia_no_dominio(amb):
    amb.cenario_s()
    chave = Idempotencia("escopo-k-1", "hash-30", 201)
    primeira = amb.direta(COMP_A, DEM_A, OF_Y, "30.00", idempotencia=chave)
    segunda = amb.direta(COMP_A, DEM_A, OF_Y, "30.00", idempotencia=chave)
    assert segunda == primeira
    assert len(amb.publicados("modo_negociacao_definido")) == 1
    outro = Idempotencia("escopo-k-1", "hash-10", 201)
    assert erro(amb.direta, COMP_A, DEM_A, OF_Y, "10.00", idempotencia=outro) == "IDEMPOTENCIA_CONFLITO"
    amb.em("2026-11-21T12:00:00.000Z")
    vencida = amb.direta(COMP_A, DEM_A, OF_X, "10.00", idempotencia=outro)
    assert vencida["negociacaoId"] != primeira["negociacaoId"]


# ---------------------------------------------------------------- RF-09

def ca34(amb):
    amb.cenario_e()
    s1 = amb.sugestao_ativa()["sugestaoId"]
    resposta = amb.aceitar(FORN_X, s1, OF_X, "60.00", hora="12:00")
    return s1, resposta


def test_ca34_primeiro_aceite_abre_a_janela_de_adesao(amb):
    s1, resposta = ca34(amb)
    assert (resposta["situacao"], resposta["quantidadeNoLote"], resposta["fornecedoresNoLote"],
            resposta["janelaEncerraEm"], resposta["minhaParticipacao"]) == (
        "EM_FORMACAO", D("60.00"), 1, t("13:00"), {"fornecimentoId": OF_X, "quantidade": D("60.00")})
    assert balanco(amb)["ofertaTotal"] == D("40.00")
    assert OF_X not in ofertas_na_vitrine(amb)
    assert amb.publicados() == []
    [visao_y] = amb.mercado.sugestoes(FORN_Y, 1, 10)["items"]
    assert (visao_y["excessoDemanda"], visao_y["quantidadeNoLote"], visao_y["minhaParticipacao"]) == (
        D("90.00"), D("60.00"), None)


def test_ca35_alterar_quantidade_e_sair_do_lote(amb):
    s1, _ = ca34(amb)
    assert amb.alterar(FORN_X, s1, OF_X, "50.00")["quantidadeNoLote"] == D("50.00")
    assert ofertas_na_vitrine(amb)[OF_X]["quantidadeDisponivel"] == D("10.00")
    assert amb.remover(FORN_X, s1, OF_X) is None
    sugestao = amb.sugestao(s1)
    assert (sugestao["situacao"], sugestao["janelaEncerraEm"], sugestao["correlationId"]) == ("ABERTA", None, None)
    assert ofertas_na_vitrine(amb)[OF_X]["quantidadeDisponivel"] == D("60.00")


def test_ca36_oferta_de_outra_empresa_outro_produto_repetida_ou_acima_do_disponivel(amb):
    s1, _ = ca34(amb)
    assert erro(amb.aceitar, FORN_Y, s1, OF_X, "10.00") == "OFERTA_DE_OUTRA_EMPRESA"
    assert erro(amb.aceitar, FORN_Z, s1, OF_Q, "1.00") == "PRODUTO_DIFERENTE"
    assert erro(amb.aceitar, FORN_X, s1, OF_X, "1.00") == "ACEITE_JA_REGISTRADO"
    assert erro(amb.aceitar, FORN_Y, s1, OF_Y, "40.01") == "QUANTIDADE_INDISPONIVEL"
    assert erro(amb.alterar, FORN_Y, s1, OF_Y, "1.00") == "ACEITE_NAO_ENCONTRADO"


def test_ca37_sugestao_inexistente_ou_encerrada(amb):
    assert erro(amb.aceitar, FORN_X, "33000000-0000-4000-8000-000000000077", OF_X, "1.00") == \
        "SUGESTAO_NAO_ENCONTRADA"
    amb.cenario_e()
    s1 = amb.sugestao_ativa()["sugestaoId"]
    amb.oferta(OF_W, hora="12:30")
    assert erro(amb.aceitar, FORN_X, s1, OF_X, "1.00") == "SUGESTAO_ENCERRADA"


def test_ca38_oferta_zerada_nao_entra_no_lote(amb):
    amb.cenario_e()
    amb.atualizar_oferta(OF_Y, "12:01", quantidade="0.00")
    s1 = amb.sugestao_ativa()["sugestaoId"]
    assert erro(amb.aceitar, FORN_Y, s1, OF_Y, "1.00") == "OFERTA_INDISPONIVEL"


# ---------------------------------------------------------------- RF-10

def test_ca39_lote_fecha_quando_todos_os_elegiveis_aceitam(amb):
    s1, _ = ca34(amb)
    resposta = amb.aceitar(FORN_Y, s1, OF_Y, "40.00", hora="12:10", correlacao="teste-mrc-10")
    negociacao = resposta["negociacaoId"]
    assert resposta["situacao"] == "CONVERTIDA" and negociacao is not None
    assert [(topico, chave) for topico, chave, _ in amb.bus.publicados] == [
        ("modo_negociacao_definido", negociacao), ("leilao_iniciado", negociacao)]
    modo, leilao = amb.publicados()
    assert (modo["payload"]["modo"], modo["payload"]["demandas"], modo["payload"]["valorReferencia"]) == (
        "LEILAO_DIRETO", [], D("780.00"))
    p = leilao["payload"]
    assert (p["quantidadeTotal"], p["precoInicial"], p["incrementoMinimo"], p["iniciaEm"], p["encerraEm"]) == (
        D("100.00"), D("780.00"), D("7.80"), "2026-11-20T12:10:00.000Z", "2026-11-21T12:10:00.000Z")
    assert [(i["fornecimentoId"], i["quantidade"], i["precoUnitario"], i["valorItem"], i["participacao"])
            for i in p["itens"]] == [(OF_X, D("60.00"), D("8.00"), D("480.00"), D("0.6154")),
                                     (OF_Y, D("40.00"), D("7.50"), D("300.00"), D("0.3846"))]
    assert modo["correlationId"] == leilao["correlationId"] == "teste-mrc-10"


@precisa_contratos
def test_ca39_eventos_iguais_aos_exemplos_e_validos_contra_os_schemas(amb):
    s1, _ = ca34(amb)
    negociacao = amb.aceitar(FORN_Y, s1, OF_Y, "40.00", hora="12:10", correlacao="teste-mrc-10")["negociacaoId"]
    from tests.cenarios import json_publicado
    modo, leilao = amb.publicados()
    validar_schema("modo_negociacao_definido", modo)
    validar_schema("leilao_iniciado", leilao)
    for nome, mensagem in (("modo_negociacao_definido.leilao", modo), ("leilao_iniciado", leilao)):
        esperado = exemplo(nome)
        esperado["payload"]["negociacaoId"] = esperado["aggregateId"] = negociacao
        if "sugestaoId" in esperado["payload"]:
            esperado["payload"]["sugestaoId"] = s1
        publicado = json_publicado(mensagem)
        assert {k: v for k, v in publicado.items() if k != "eventId"} == {
            k: v for k, v in esperado.items() if k != "eventId"}
    assert modo["eventId"] != leilao["eventId"]


def test_ca40_lote_fecha_no_fim_da_janela_com_um_unico_fornecedor(amb):
    s1, _ = ca34(amb)
    assert amb.prazos("12:59:59.999") == 0
    assert amb.publicados() == []
    assert amb.prazos("13:00") == 1
    [leilao] = amb.publicados("leilao_iniciado")
    p = leilao["payload"]
    assert [(i["fornecimentoId"], i["quantidade"], i["participacao"]) for i in p["itens"]] == [
        (OF_X, D("60.00"), D("1.0000"))]
    assert (p["precoInicial"], p["incrementoMinimo"], p["encerraEm"]) == (
        D("480.00"), D("4.80"), "2026-11-21T13:00:00.000Z")
    assert leilao["correlationId"] == "teste-mrc-09"
    publicados = len(amb.publicados())
    assert amb.prazos("13:00") == 0
    assert len(amb.publicados()) == publicados


def test_ca41_oferta_nova_durante_a_formacao_fecha_o_lote(amb):
    s1, _ = ca34(amb)
    amb.oferta(OF_W, hora="12:20")
    assert amb.sugestao(s1)["situacao"] == "CONVERTIDA"
    [leilao] = amb.publicados("leilao_iniciado")
    assert [i["fornecimentoId"] for i in leilao["payload"]["itens"]] == [OF_X]
    assert leilao["payload"]["iniciaEm"] == "2026-11-20T12:20:00.000Z"
    assert leilao["correlationId"] == "teste-mrc"


def test_ca42_item_cuja_oferta_encerrou_durante_a_formacao_sai_do_lote(amb):
    s1, _ = ca34(amb)
    amb.estoque(OF_X, "0.00", "12:05", operacao="ENCERRAMENTO")
    amb.prazos("13:00")
    assert amb.sugestao(s1)["situacao"] == "ENCERRADA"
    assert amb.repo.tabela("negociacoes") == {}
    assert amb.publicados() == []
    nova = amb.sugestao_ativa()
    assert nova is not None and nova["sugestaoId"] != s1 and nova["situacao"] == "ABERTA"


def test_ca43_fornecedor_com_leilao_aberto_nao_entra_em_outra_sugestao(amb):
    amb.cenario_e()
    s1 = amb.sugestao_ativa()["sugestaoId"]
    amb.aceitar(FORN_X, s1, OF_X, "60.00", hora="12:00")
    assert amb.aceitar(FORN_Y, s1, OF_Y, "20.00", hora="12:00")["situacao"] == "CONVERTIDA"
    amb.estoque(OF_X, "0.00", "12:01")
    amb.estoque(OF_Y, "20.00", "12:01")
    assert amb.mercado.sugestoes(FORN_Y, 1, 10)["items"] == []
    s2 = amb.sugestao_ativa()["sugestaoId"]
    assert s2 != s1
    assert erro(amb.aceitar, FORN_Y, s2, OF_Y, "10.00") == "FORNECEDOR_EM_LEILAO"


# ---------------------------------------------------------------- RF-11

def test_ca44_acordo_na_direta_abate_o_saldo(amb):
    amb.cenario_s()
    d1 = amb.direta(COMP_A, DEM_A, OF_Y, "30.00", hora="12:05")["negociacaoId"]
    amb.fechar(d1, "ENCERRADO", [(OF_Y, "30.00")], comprador=COMP_A, hora="12:30")
    assert amb.mercado.negociacao(COMP_A, "BUYER", d1)["situacao"] == "CONCLUIDA"
    resposta = demanda(amb)
    assert (resposta["saldo"], resposta["emNegociacao"], resposta["situacao"]) == (D("20.00"), D("0.00"),
                                                                                   "AGUARDANDO")
    d2 = amb.direta(COMP_A, DEM_A, OF_X, "20.00", hora="12:40")["negociacaoId"]
    amb.fechar(d2, "ENCERRADO", [(OF_X, "20.00")], comprador=COMP_A, hora="12:50")
    resposta = demanda(amb)
    assert (resposta["saldo"], resposta["situacao"], resposta["encerradaEm"]) == (D("0.00"), "ATENDIDA",
                                                                                  t("12:50"))


def test_ca45_direta_sem_acordo_devolve_a_quantidade(amb):
    amb.cenario_s()
    d1 = amb.direta(COMP_A, DEM_A, OF_Y, "30.00", hora="12:05")["negociacaoId"]
    amb.fechar(d1, "RECUSADO", [], hora="12:30")
    assert amb.mercado.negociacao(COMP_A, "BUYER", d1)["situacao"] == "SEM_ACORDO"
    resposta = demanda(amb)
    assert (resposta["saldo"], resposta["emNegociacao"], resposta["saldoNegociavel"]) == (
        D("50.00"), D("0.00"), D("50.00"))


def test_ca46_leilao_vencido_com_demanda_indicada(amb):
    l1 = amb.cenario_l()
    amb.fechar(l1, "ENCERRADO", [(OF_X, "60.00"), (OF_Y, "40.00")], comprador=COMP_B, demanda=DEM_B,
               hora="2026-11-21T12:10:00.000Z")
    assert amb.mercado.negociacao(COMP_B, "BUYER", l1)["situacao"] == "CONCLUIDA"
    resposta = demanda(amb, DEM_B, COMP_B)
    assert (resposta["saldo"], resposta["situacao"]) == (D("0.00"), "ATENDIDA")


def test_ca47_leilao_deserto_nao_muda_demandas(amb):
    l1 = amb.cenario_l()
    amb.fechar(l1, "DESERTO", [], hora="2026-11-21T12:10:00.000Z")
    assert amb.mercado.negociacao(COMP_A, "BUYER", l1)["situacao"] == "SEM_ACORDO"
    assert (demanda(amb)["situacao"], demanda(amb)["saldo"]) == ("AGUARDANDO", D("50.00"))
    assert (demanda(amb, DEM_B, COMP_B)["situacao"], demanda(amb, DEM_B, COMP_B)["saldo"]) == (
        "AGUARDANDO", D("80.00"))


def test_ca48_fechamento_desconhecido_repetido_ou_com_demanda_de_outro_comprador(amb):
    l1 = amb.cenario_l()
    antes = (dict(amb.repo.tabela("negociacoes")), dict(amb.repo.tabela("demandas")), len(amb.publicados()))
    assert amb.fechar("31000000-0000-4000-8000-000000000099", "ENCERRADO", [(OF_X, "1.00")], tipo="LEILAO",
                      comprador=COMP_B) == "ignorado"
    assert (amb.repo.tabela("negociacoes"), amb.repo.tabela("demandas"), len(amb.publicados())) == antes
    amb.fechar(l1, "ENCERRADO", [(OF_X, "60.00"), (OF_Y, "40.00")], comprador=COMP_B, demanda=DEM_A)
    assert amb.mercado.negociacao(COMP_B, "BUYER", l1)["situacao"] == "CONCLUIDA"
    assert (demanda(amb)["situacao"], demanda(amb)["saldo"]) == ("AGUARDANDO", D("50.00"))
    assert amb.fechar(l1, "DESERTO", []) == "ignorado"
    assert amb.mercado.negociacao(COMP_B, "BUYER", l1)["situacao"] == "CONCLUIDA"


# ---------------------------------------------------------------- RF-12

def test_ca49_demanda_com_prazo_vencido_sai_do_balanco(amb):
    amb.cenario_s()
    assert amb.prazos("2026-11-30T23:59:59.000Z") == 0
    assert demanda(amb)["prazoVencido"] is False
    assert amb.prazos("2026-11-30T23:59:59.001Z") == 1
    resposta = demanda(amb)
    assert (resposta["situacao"], resposta["prazoVencido"], resposta["encerradaEm"]) == ("AGUARDANDO", True, None)
    assert balanco(amb)["demandaTotal"] == D("0.00")
    assert erro(amb.direta, COMP_A, DEM_A, OF_Y, "10.00") == "DEMANDA_ENCERRADA"


# ---------------------------------------------------------------- RF-13

def test_ca50_cada_empresa_ve_leiloes_e_as_proprias_diretas(amb):
    amb.cenario_e()
    s1 = amb.sugestao_ativa()["sugestaoId"]
    amb.aceitar(FORN_X, s1, OF_X, "60.00", hora="12:00")
    amb.direta(COMP_A, DEM_A, OF_Y, "10.00", hora="12:05")
    assert amb.aceitar(FORN_Y, s1, OF_Y, "40.00", hora="12:10")["situacao"] == "CONVERTIDA"
    assert amb.mercado.negociacoes(COMP_A, "BUYER", None, None, 1, 10)["total"] == 2
    assert amb.mercado.negociacoes(COMP_B, "BUYER", "VENDA_DIRETA", None, 1, 10)["total"] == 0
    resposta = amb.mercado.negociacoes(FORN_Y, "SUPPLIER", None, None, 1, 10)
    assert resposta["total"] == 2
    assert [item["modo"] for item in resposta["items"]] == ["LEILAO_DIRETO", "VENDA_DIRETA"]
    assert set(resposta["items"][0]) == {"negociacaoId", "modo", "produtoId", "situacao", "quantidadeTotal",
                                         "valorReferencia", "definidaEm"}


def test_ca51_leilao_aberto_mostra_que_a_oferta_passou_a_cobrir_a_demanda(amb):
    l1 = amb.cenario_l()
    s1 = amb.sugestao(amb.repo.tabela("negociacoes")[l1]["sugestaoId"])["sugestaoId"]
    amb.estoque(OF_X, "0.00", "12:15")
    amb.estoque(OF_Y, "0.00", "12:15")
    amb.oferta(OF_W, hora="12:30")
    amb.em("13:00")
    resposta = amb.mercado.negociacao(COMP_B, "BUYER", l1)
    assert resposta == {
        "negociacaoId": l1, "modo": "LEILAO_DIRETO", "produtoId": P, "unidade": "TON", "situacao": "ABERTA",
        "demandaId": None, "compradorId": None, "compraImediata": False, "quantidadeTotal": D("100.00"),
        "valorReferencia": D("780.00"), "precoInicial": D("780.00"), "incrementoMinimo": D("7.80"),
        "sugestaoId": s1, "iniciaEm": t("12:10"), "encerraEm": t("2026-11-21T12:10:00.000Z"),
        "itens": [
            {"fornecimentoId": OF_X, "fornecedorId": FORN_X, "quantidade": D("60.00"), "precoUnitario": D("8.00"),
             "valorItem": D("480.00"), "participacao": D("0.6154"), "origem": RIO_VERDE},
            {"fornecimentoId": OF_Y, "fornecedorId": FORN_Y, "quantidade": D("40.00"), "precoUnitario": D("7.50"),
             "valorItem": D("300.00"), "participacao": D("0.3846"), "origem": JATAI},
        ],
        "definidaEm": t("12:10"), "encerradaEm": None,
        "contextoProduto": {"situacao": "OFERTA_SUFICIENTE", "desde": t("12:30")}, "agora": t("13:00"),
    }


def test_ca52_direta_de_outra_empresa(amb):
    amb.cenario_s()
    d1 = amb.direta(COMP_A, DEM_A, OF_Y, "10.00")["negociacaoId"]
    assert erro(amb.mercado.negociacao, COMP_B, "BUYER", d1) == "NEGOCIACAO_NAO_ENCONTRADA"
    assert amb.mercado.negociacao(FORN_Y, "SUPPLIER", d1)["negociacaoId"] == d1
    assert erro(amb.mercado.negociacao, FORN_X, "SUPPLIER", d1) == "NEGOCIACAO_NAO_ENCONTRADA"


# ---------------------------------------------------------------- RF-15

def test_ca55_mesma_demanda_entregue_cinco_vezes(amb):
    amb.produto(P)
    amb.produto(Q, "Soja")
    for oferta in (OF_X, OF_Y, OF_Q):
        amb.oferta(oferta)
    amb.em("11:55")
    mensagem = envelope("demanda_criada", amb.payload_demanda(DEM_A, COMP_A, P, "50.00", preco_maximo="9.00"),
                        agregado=DEM_A, timestamp="2026-11-20T11:55:00.000Z")
    resultados = [amb.mercado.consumir("demanda_criada", mensagem) for _ in range(5)]
    assert resultados == ["aplicado"] + ["duplicado"] * 4
    assert list(amb.repo.tabela("demandas")) == [DEM_A]
    assert balanco(amb)["demandaTotal"] == D("50.00")
    assert [chave for chave in amb.repo.tabela("processados") if chave[1] == mensagem["eventId"]] == [
        ("demanda-service", mensagem["eventId"])]


def test_ca56_envelope_sem_payload_duas_vezes(amb):
    mensagem = envelope("demanda_criada", None, agregado=DEM_A, correlacao="teste-mrc-dem",
                        event_id="37000000-0000-4000-8000-000000000099", timestamp="2026-11-20T11:59:00.000Z")
    del mensagem["payload"]
    for _ in range(2):
        assert amb.mercado.consumir("demanda_criada", mensagem) == "dlt"
    assert [chave for _, chave, _ in amb.bus.publicados] == [DEM_A, DEM_A]
    for dlt in dlts(amb):
        assert (dlt["payload"]["topicoOrigem"], dlt["payload"]["error"], dlt["payload"]["stackTrace"],
                dlt["payload"]["failedAtAttempt"]) == ("demanda_criada", "EVENTO_INVALIDO", "", 1)
        assert dlt["payload"]["originalEvent"] == mensagem
        assert dlt["correlationId"] == "teste-mrc-dem"
        if contratos_disponiveis():
            validar_schema("mercado.dlt", dlt)
    assert amb.repo.tabela("processados") == {}
    if contratos_disponiveis():
        esperado = exemplo("mercado.dlt")["payload"]
        assert {k: v for k, v in dlts(amb)[0]["payload"].items()} == esperado


def test_ca56_mensagem_duplicada_e_invalida_vai_para_a_dlt_de_novo(amb):
    amb.produto(P, unidade="TON")
    payload = amb.payload_demanda(DEM_A, COMP_A, P, "50.00", unidade="KG")
    mensagem = envelope("demanda_criada", payload, agregado=DEM_A)
    assert [amb.mercado.consumir("demanda_criada", mensagem) for _ in range(2)] == ["dlt", "dlt"]
    assert len(dlts(amb)) == 2


def test_ca57_banco_indisponivel_e_falha_persistente(amb):
    mensagem = envelope("demanda_criada", amb.payload_demanda(DEM_A, COMP_A, P, "50.00"), agregado=DEM_A)
    amb.repo.falhar("transacao", RepositorioIndisponivel)
    with pytest.raises(RepositorioIndisponivel):
        amb.mercado.consumir("demanda_criada", mensagem)
    assert amb.publicados() == []
    assert amb.repo.tabela("processados") == {}
    amb.repo.falhar("transacao", ValueError)
    assert amb.mercado.consumir("demanda_criada", mensagem) == "dlt"
    assert amb.clock.esperas == [1, 2]
    [dlt] = dlts(amb)
    assert (dlt["payload"]["error"], dlt["payload"]["failedAtAttempt"]) == ("PROCESSAMENTO_FALHOU", 3)
    assert "ValueError" in dlt["payload"]["stackTrace"]
    if contratos_disponiveis():
        validar_schema("mercado.dlt", dlt)


def test_falha_de_banco_no_meio_do_efeito_desfaz_tudo(amb):
    amb.cenario_s()
    amb.repo.falhar("salvar_situacao_balanco", RepositorioIndisponivel)
    with pytest.raises(RepositorioIndisponivel):
        amb.dem_b()
    assert DEM_B not in amb.repo.tabela("demandas")
    assert amb.repo.tabela("sugestoes") == {}


# ---------------------------------------------------------------- RF-17

def test_ca59_excecao_inesperada_do_repositorio_nao_publica(amb):
    amb.cenario_s()
    amb.repo.falhar("obter_demanda", ValueError)
    with pytest.raises(ValueError):
        amb.direta(COMP_A, DEM_A, OF_Y, "10.00")
    assert amb.publicados() == []


# ---------------------------------------------------------------- RNF-03, RNF-04

def test_rnf03_envelope_dos_eventos_publicados(amb):
    amb.cenario_l()
    eventos = [mensagem for topico, chave, mensagem in amb.bus.publicados]
    for topico, chave, mensagem in amb.bus.publicados:
        assert chave == mensagem["aggregateId"] == mensagem["payload"]["negociacaoId"]
        assert (mensagem["eventType"], mensagem["eventVersion"], mensagem["source"]) == (topico, "1.0",
                                                                                         "mercado-service")
        assert mensagem["timestamp"] == "2026-11-20T12:10:00.000Z"
        assert uuid.UUID(mensagem["eventId"]).version == 4
    assert len({mensagem["eventId"] for mensagem in eventos}) == len(eventos)


def test_rnf04_aplicacoes_com_repositorios_diferentes_nao_compartilham_estado():
    primeira, segunda = Ambiente(), Ambiente()
    primeira.cenario_e()
    assert segunda.repo.tabela("demandas") == {}
    assert segunda.sugestao_ativa() is None
    assert erro(segunda.mercado.balanco, P) == "PRODUTO_NAO_ENCONTRADO"


def test_handlers_dos_nove_topicos():
    repo = RepositorioMemoria()
    bus = EventBusMemoria(repo)
    mercado = Mercado(repo, bus, RelogioFalso(t("12:00")))
    for topico in ("produto_cadastrado", "produto_atualizado", "fornecimento_criado", "fornecimento_atualizado",
                   "estoque_atualizado", "demanda_criada", "demanda_recorrente_gerada", "demanda_cancelada",
                   "negociacao_fechada"):
        bus.subscribe(topico, mercado.handler(topico))
    bus.entregar("produto_cadastrado", envelope("produto_cadastrado", {
        "produtoId": P, "nome": "Milho", "unidade": "TON", "cadastradoEm": "2026-11-20T11:00:00Z"}, agregado=P))
    assert repo.tabela("produtos")[P]["nome"] == "Milho"
