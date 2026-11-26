"""Achados da revisão independente do commit a80ec78 (DG-67 e DG-68 da ESP-MRC-001)."""
from decimal import Decimal

from app.dominio import montar_lote
from app.jsonio import fingerprint
from tests.cenarios import COMP_A, DEM_A, FORN_X, FORN_Y, FORN_Z, OF_X, OF_Y, P, Q, Ambiente, t

D = Decimal
OF_V = "34000000-0000-4000-8000-000000000005"
MODELO = "36000000-0000-4000-8000-000000000003"
OCORRENCIA = "36000000-0000-4000-8000-000000000031"
DEM_N = "36000000-0000-4000-8000-000000000010"


def _apos_bloqueio(repo, metodo, alvo, efeito):
    """Simula a outra réplica concluindo `efeito` enquanto esta esperava o bloqueio de `alvo`."""
    original = getattr(repo, metodo)
    feito = []

    def bloquear(chave):
        original(chave)
        if chave == alvo and not feito:
            feito.append(chave)
            efeito()
    setattr(repo, metodo, bloquear)


def test_oferta_que_muda_de_produto_sai_da_sugestao_em_formacao_do_produto_anterior():
    amb = Ambiente().cenario_e()
    amb.oferta(OF_V, FORN_Z, P, "8.30", "10.00", origem=None, hora="11:58")
    s1 = amb.sugestao_ativa()["sugestaoId"]
    amb.aceitar(FORN_X, s1, OF_X, "50.00", hora="12:00")
    amb.aceitar(FORN_Y, s1, OF_Y, "10.00", hora="12:01")
    amb.atualizar_oferta(OF_X, "12:02", produto=Q)
    with amb.repo.transacao():
        assert [a["fornecimentoId"] for a in amb.repo.listar_aceites(s1)] == [OF_Y]
    livre = {i["fornecimentoId"]: i["quantidadeDisponivel"] for i in amb.mercado.vitrine(Q, FORN_Z, None, 1, 10)["items"]}
    assert livre[OF_X] == D("60.00")
    amb.prazos("13:00")
    [leilao] = amb.publicados("leilao_iniciado")
    assert leilao["payload"]["produtoId"] == P
    assert [i["fornecimentoId"] for i in leilao["payload"]["itens"]] == [OF_Y]


def test_ultimo_aceite_que_muda_de_produto_reabre_a_sugestao():
    amb = Ambiente().cenario_e()
    s1 = amb.sugestao_ativa()["sugestaoId"]
    amb.aceitar(FORN_X, s1, OF_X, "50.00", hora="12:00")
    amb.atualizar_oferta(OF_X, "12:02", produto=Q)
    sugestao = amb.sugestao(s1)
    assert (sugestao["situacao"], sugestao["janelaEncerraEm"]) == ("ABERTA", None)


def test_lote_nunca_inclui_oferta_de_outro_produto():
    oferta = {"fornecimentoId": "a", "fornecedorId": "f", "produtoId": "q", "unidade": "TON", "preco": D("8.00"),
              "quantidadeDisponivel": D("10.00"), "origem": None}
    aceite = {"sugestaoId": "s", "fornecimentoId": "a", "fornecedorId": "f", "quantidade": D("10.00"),
              "aceitoEm": t("12:00")}
    assert montar_lote([aceite], {"a": oferta}, "p") == []
    assert len(montar_lote([aceite], {"a": oferta}, "q")) == 1


def test_ocorrencia_revalida_o_modelo_depois_do_bloqueio():
    amb = Ambiente().cenario_s()
    amb.demanda(MODELO, COMP_A, P, "10.00", tipo="RECORRENTE", hora="11:58")

    def cancelamento_da_outra_replica():
        modelo = amb.repo.obter_modelo(MODELO)
        modelo["cancelado"] = True
        amb.repo.salvar_modelo(modelo)
    _apos_bloqueio(amb.repo, "bloquear_id_demanda", MODELO, cancelamento_da_outra_replica)
    assert amb.recorrente(MODELO, OCORRENCIA, COMP_A, P, "10.00", hora="12:01") == "ignorado"
    with amb.repo.transacao():
        assert amb.repo.obter_demanda(OCORRENCIA) is None


def test_demanda_revalida_o_cancelamento_antecipado_depois_do_bloqueio():
    amb = Ambiente().cenario_s()

    def cancelamento_da_outra_replica():
        amb.repo.salvar_cancelamento_antecipado({"demandaId": DEM_N, "compradorId": COMP_A,
                                                 "canceladoEm": t("12:00"), "recebidoEm": t("12:00")})
    _apos_bloqueio(amb.repo, "bloquear_id_demanda", DEM_N, cancelamento_da_outra_replica)
    amb.demanda(DEM_N, COMP_A, P, "10.00", hora="12:01")
    assert amb.mercado.demanda(COMP_A, DEM_N)["situacao"] == "CANCELADA"


def test_ids_de_demanda_sao_bloqueados_antes_dos_produtos():
    amb = Ambiente().cenario_s()
    amb.demanda(MODELO, COMP_A, P, "10.00", tipo="RECORRENTE", hora="11:58")
    amb.repo.bloqueios.clear()
    amb.recorrente(MODELO, OCORRENCIA, COMP_A, P, "10.00", hora="12:01")
    assert amb.repo.bloqueios == [f"demanda:{MODELO}", f"demanda:{OCORRENCIA}", P]
    amb.repo.bloqueios.clear()
    amb.cancelar(MODELO, COMP_A, hora="12:02")
    assert amb.repo.bloqueios == [f"demanda:{MODELO}", P]


def test_produto_compara_o_instante_depois_do_bloqueio():
    amb = Ambiente().cenario_s()

    def atualizacao_da_outra_replica():
        amb.repo.salvar_produto({"produtoId": P, "nome": "Milho 12h02", "unidade": "TON",
                                 "atualizadoEm": t("12:02")})
    _apos_bloqueio(amb.repo, "bloquear_produto", P, atualizacao_da_outra_replica)
    assert amb.produto(P, "Milho 12h01", hora="12:01", topico="produto_atualizado") == "ignorado"
    with amb.repo.transacao():
        assert amb.repo.obter_produto(P)["nome"] == "Milho 12h02"


def test_direta_usa_a_unidade_do_produto_conhecido():
    amb = Ambiente().cenario_s()
    amb.produto(P, unidade="KG", hora="12:01", topico="produto_atualizado")
    resposta = amb.direta(COMP_A, DEM_A, OF_Y, "10.00", hora="12:02")
    assert resposta["unidade"] == "KG"
    assert amb.publicados("modo_negociacao_definido")[-1]["payload"]["unidade"] == "KG"


def test_idempotencia_trata_inteiro_e_decimal_equivalentes_como_o_mesmo_corpo():
    corpos = [{"quantidade": valor} for valor in (30, D("30"), D("30.0"), D("30.00"), D("3E+1"))]
    assert len({fingerprint(corpo) for corpo in corpos}) == 1
    assert fingerprint({"quantidade": 30}) != fingerprint({"quantidade": D("30.01")})
    assert fingerprint({"ativo": True}) != fingerprint({"ativo": 1})


def test_lote_respeita_o_limite_monetario_sozinho():
    def oferta(fid, preco):
        return {"fornecimentoId": fid, "fornecedorId": "f-" + fid, "produtoId": "p", "unidade": "TON",
                "preco": D(preco), "quantidadeDisponivel": D("1000000000.00"), "origem": None}

    def aceite(fid, quantidade):
        return {"sugestaoId": "s", "fornecimentoId": fid, "fornecedorId": "f-" + fid, "quantidade": D(quantidade),
                "aceitoEm": t("12:00")}
    ofertas = {"a": oferta("a", "60000.00"), "b": oferta("b", "50000.00"), "c": oferta("c", "1.00")}
    itens = montar_lote([aceite("a", "1000000000.00"), aceite("b", "1000000000.00"), aceite("c", "1.00")],
                        ofertas)
    assert [(i["fornecimentoId"], i["valorItem"]) for i in itens] == [
        ("a", D("60000000000000.00")), ("c", D("1.00"))]
