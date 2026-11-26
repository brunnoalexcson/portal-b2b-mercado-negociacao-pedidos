"""Suíte de aceitação das ações da negociação direta (parte 3 da ESP-NEG-001 v1.4)."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.jsonio import loads
from app.main import executar_prazos
from app.portas import RepositorioIndisponivel
from tests.cenarios import (
    COMP_X, COMP_Y, CORR_D1, D1, FORN_A, FORN_B, L1, OF_B, P, PT_D,
    Ambiente, evento_d1, validar_erro_rfc7807, validar_schema,
)

D = Decimal


def corpo(resposta):
    return loads(resposta.text)


def codigo_erro(resposta) -> tuple[int, str]:
    dados = validar_erro_rfc7807(resposta)
    return resposta.status_code, dados["codigo"]


# ---------------------------------------------------------------- RF-07: enviar contraproposta

def test_ca24_comprador_responde_com_contraproposta():
    """CA-24: Comprador responde com contraproposta."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.bus.publicados.clear()
    amb.em("2026-11-21T12:00:00.000Z")

    resposta = amb.post(f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X)
    assert resposta.status_code == 201
    dados = corpo(resposta)
    assert UUID(dados["propostaId"]).version == 4
    assert dados == {
        "propostaId": dados["propostaId"],
        "negociacaoId": D1,
        "autor": "BUYER",
        "precoUnitario": D("125.00"),
        "sequencia": 2,
        "criadoEm": "2026-11-21T12:00:00.000Z",
    }

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))
    assert detalhe["precoAtual"] == D("125.00")
    assert detalhe["vez"] == "SUPPLIER"
    assert detalhe["expiraEm"] == "2026-11-23T12:00:00.000Z"
    assert detalhe["totalPropostas"] == 2
    assert amb.bus.publicados == []


def test_ca25_proposta_fora_da_vez_ou_acima_do_preco_da_oferta():
    """CA-25: Proposta fora da vez ou acima do preço da oferta."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()

    r_vez = amb.post(f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("128.00")}, "SUPPLIER", FORN_B)
    assert codigo_erro(r_vez) == (409, "FORA_DA_VEZ")

    r_teto = amb.post(f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("130.01")}, "BUYER", COMP_X)
    assert codigo_erro(r_teto) == (422, "PROPOSTA_ACIMA_DA_OFERTA")

    propostas = corpo(amb.get(f"/api/v1/negociacoes/{D1}/propostas", "BUYER", COMP_X))
    assert propostas["total"] == 1
    assert len(propostas["items"]) == 1

    assert amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X,
    ).status_code == 201

    r_forn = amb.post(f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("130.00")}, "SUPPLIER", FORN_B)
    assert r_forn.status_code == 201
    dados_forn = corpo(r_forn)
    assert dados_forn["autor"] == "SUPPLIER"
    assert dados_forn["precoUnitario"] == D("130.00")
    assert dados_forn["sequencia"] == 3


def test_ca26_proposta_em_negociacao_encerrada_ou_vencida():
    """CA-26: Proposta em negociação encerrada ou vencida."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.em("2026-11-22T12:00:00.000Z")

    r_vencida = amb.post(f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X)
    assert codigo_erro(r_vencida) == (409, "NEGOCIACAO_ENCERRADA")

    amb.em("12:05:00")
    amb.recusar_reserva(OF_B, PT_D, D1)
    r_recusada = amb.post(f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X)
    assert codigo_erro(r_recusada) == (409, "NEGOCIACAO_ENCERRADA")


def test_ca27_quem_nao_participa_nao_ve_a_negociacao_direta():
    """CA-27: Quem não participa não vê a negociação direta."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.inserir_l1()

    assert codigo_erro(
        amb.post(f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("128.00")}, "BUYER", COMP_Y),
    ) == (404, "NEGOCIACAO_NAO_ENCONTRADA")
    assert codigo_erro(
        amb.post(f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("128.00")}, "SUPPLIER", FORN_A),
    ) == (404, "NEGOCIACAO_NAO_ENCONTRADA")
    assert codigo_erro(
        amb.post(f"/api/v1/negociacoes/{L1}/propostas", {"precoUnitario": D("128.00")}, "SUPPLIER", FORN_A),
    ) == (409, "MODO_INCOMPATIVEL")


# ---------------------------------------------------------------- RF-08: aceitar a proposta

def test_ca28_fornecedor_aceita_a_proposta_do_comprador():
    """CA-28: Fornecedor aceita a proposta do comprador."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.confirmar_reserva(OF_B, PT_D, D1)
    assert amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X,
    ).status_code == 201
    amb.bus.publicados.clear()
    amb.em("13:00:00")

    resposta = amb.post(f"/api/v1/negociacoes/{D1}/aceitar", escopo="SUPPLIER", empresa=FORN_B)
    assert resposta.status_code == 200
    dados = corpo(resposta)
    assert dados["status"] == "ENCERRADO"
    assert dados["encerradoEm"] == "2026-11-20T13:00:00.000Z"
    assert dados["acoesPermitidas"] == []

    [(chave, fechada)] = amb.bus.de("negociacao_fechada")
    assert chave == D1
    assert fechada["payload"] == {
        "negociacaoId": D1,
        "tipo": "VENDA_DIRETA",
        "status": "ENCERRADO",
        "compradorId": COMP_X,
        "demandaId": None,
        "produtoId": P,
        "alocacoes": [
            {
                "fornecimentoId": OF_B,
                "fornecedorId": FORN_B,
                "pedidoId": PT_D,
                "quantidade": D("10.00"),
                "valorUnitario": D("125.00"),
                "valorTotal": D("1250.00"),
            }
        ],
    }
    validar_schema("negociacao_fechada", fechada)


def test_ca29_aceite_fora_da_vez_ou_com_reserva_pendente():
    """CA-29: Aceite fora da vez ou com reserva pendente."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    assert amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X,
    ).status_code == 201
    amb.bus.publicados.clear()

    assert codigo_erro(amb.post(f"/api/v1/negociacoes/{D1}/aceitar", escopo="BUYER", empresa=COMP_X)) == (
        409, "FORA_DA_VEZ",
    )
    assert codigo_erro(amb.post(f"/api/v1/negociacoes/{D1}/aceitar", escopo="SUPPLIER", empresa=FORN_B)) == (
        409, "RESERVA_PENDENTE",
    )
    assert amb.bus.publicados == []


def test_ca30_corpo_invalido_e_aceite_repetido():
    """CA-30: Corpo inválido e aceite repetido."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.confirmar_reserva(OF_B, PT_D, D1)
    assert amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X,
    ).status_code == 201
    amb.bus.publicados.clear()

    r_invalido = amb.post(f"/api/v1/negociacoes/{D1}/aceitar", {"precoUnitario": D("1.00")}, "SUPPLIER", FORN_B)
    assert codigo_erro(r_invalido) == (422, "VALIDACAO_INVALIDA")

    r_ok = amb.post(f"/api/v1/negociacoes/{D1}/aceitar", {}, "SUPPLIER", FORN_B)
    assert r_ok.status_code == 200
    [(_, fechada)] = amb.bus.de("negociacao_fechada")
    validar_schema("negociacao_fechada", fechada)

    r_rep = amb.post(f"/api/v1/negociacoes/{D1}/aceitar", escopo="SUPPLIER", empresa=FORN_B)
    assert codigo_erro(r_rep) == (409, "NEGOCIACAO_ENCERRADA")
    assert len(amb.bus.de("negociacao_fechada")) == 1


# ---------------------------------------------------------------- RF-09: recusar a negociação direta

def test_ca31_comprador_recusa_fora_da_sua_vez():
    """CA-31: Comprador recusa fora da sua vez."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    assert amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X,
    ).status_code == 201
    amb.bus.publicados.clear()

    resposta = amb.post(f"/api/v1/negociacoes/{D1}/recusar", escopo="BUYER", empresa=COMP_X)
    assert resposta.status_code == 200
    dados = corpo(resposta)
    assert dados["status"] == "RECUSADO"
    assert dados["acoesPermitidas"] == []

    [(chave, fechada)] = amb.bus.de("negociacao_fechada")
    assert chave == D1
    assert fechada["payload"] == {
        "negociacaoId": D1,
        "tipo": "VENDA_DIRETA",
        "status": "RECUSADO",
        "compradorId": None,
        "demandaId": None,
        "produtoId": P,
        "alocacoes": [],
    }
    validar_schema("negociacao_fechada", fechada)


def test_ca32_erros_na_recusa():
    """CA-32: Erros na recusa."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.inserir_l1()
    amb.em("2026-11-22T12:00:00.000Z")
    assert executar_prazos(amb.app) == 1

    assert codigo_erro(amb.post(f"/api/v1/negociacoes/{D1}/recusar", escopo="SUPPLIER", empresa=FORN_B)) == (
        409, "NEGOCIACAO_ENCERRADA",
    )
    assert codigo_erro(amb.post(f"/api/v1/negociacoes/{L1}/recusar", escopo="BUYER", empresa=COMP_X)) == (
        409, "MODO_INCOMPATIVEL",
    )
    assert codigo_erro(amb.post(f"/api/v1/negociacoes/{D1}/recusar", escopo="CARRIER", empresa=COMP_X)) == (
        403, "ACESSO_NEGADO",
    )


def test_ca63_na_compra_imediata_ninguem_age_manualmente():
    """CA-63: Na compra imediata ninguém age manualmente."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1(compraImediata=True)
    amb.bus.publicados.clear()

    assert codigo_erro(
        amb.post(f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X),
    ) == (409, "COMPRA_IMEDIATA")
    assert codigo_erro(amb.post(f"/api/v1/negociacoes/{D1}/aceitar", escopo="BUYER", empresa=COMP_X)) == (
        409, "COMPRA_IMEDIATA",
    )
    assert codigo_erro(amb.post(f"/api/v1/negociacoes/{D1}/recusar", escopo="BUYER", empresa=COMP_X)) == (
        409, "COMPRA_IMEDIATA",
    )
    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["status"] == "ABERTO"
    assert amb.bus.publicados == []

    assert codigo_erro(amb.post(f"/api/v1/negociacoes/{D1}/recusar", escopo="SUPPLIER", empresa=FORN_B)) == (
        409, "COMPRA_IMEDIATA",
    )

    amb.confirmar_reserva(OF_B, PT_D, D1)
    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["status"] == "ENCERRADO"
    [(_, fechada)] = amb.bus.de("negociacao_fechada")
    validar_schema("negociacao_fechada", fechada)

    assert codigo_erro(amb.post(f"/api/v1/negociacoes/{D1}/recusar", escopo="BUYER", empresa=COMP_X)) == (
        409, "NEGOCIACAO_ENCERRADA",
    )


# ---------------------------------------------------------------- RF-10: expirar a negociação direta

def test_ca33_negociacao_direta_sem_resposta_por_48_h():
    """CA-33: Negociação direta sem resposta por 48 h (incluindo 2ª chamada devolvendo 0 conforme RN-21)."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.bus.publicados.clear()

    amb.em("2026-11-22T11:59:59.999Z")
    assert executar_prazos(amb.app) == 0
    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["status"] == "ABERTO"
    assert amb.bus.publicados == []

    amb.em("2026-11-22T12:00:00.000Z")
    assert executar_prazos(amb.app) == 1
    assert executar_prazos(amb.app) == 0

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))
    assert detalhe["status"] == "EXPIRADO"
    assert detalhe["encerradoEm"] == "2026-11-22T12:00:00.000Z"
    assert detalhe["acoesPermitidas"] == []

    [(chave, fechada)] = amb.bus.de("negociacao_fechada")
    assert chave == D1
    assert fechada["correlationId"] == CORR_D1
    assert fechada["payload"] == {
        "negociacaoId": D1,
        "tipo": "VENDA_DIRETA",
        "status": "EXPIRADO",
        "compradorId": None,
        "demandaId": None,
        "produtoId": P,
        "alocacoes": [],
    }
    validar_schema("negociacao_fechada", fechada)


# ---------------------------------------------------------------- RF-14: idempotência das escritas HTTP

def test_ca44_reenvio_da_mesma_proposta():
    """CA-44 (adaptado para D1): Reenvio da mesma proposta devolve 201 idêntico, mesmo após estado final."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.bus.publicados.clear()

    primeira = amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X,
        chave_idempotencia="K1",
    )
    assert primeira.status_code == 201
    corpo_primeira = corpo(primeira)
    assert corpo_primeira["sequencia"] == 2

    # Mesmo corpo (inclusive com representação decimal equivalente) e mesma chave -> mesma resposta sem nova proposta.
    segunda = amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": 125}, "BUYER", COMP_X,
        chave_idempotencia="K1",
    )
    assert segunda.status_code == 201
    assert corpo(segunda) == corpo_primeira
    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["totalPropostas"] == 2
    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}/propostas", "BUYER", COMP_X))["total"] == 2

    # Depois de a negociação chegar a um estado final, o replay ainda devolve 201 com o mesmo corpo.
    assert amb.post(f"/api/v1/negociacoes/{D1}/recusar", escopo="SUPPLIER", empresa=FORN_B).status_code == 200
    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["status"] == "RECUSADO"
    [(_, fechada)] = amb.bus.de("negociacao_fechada")
    validar_schema("negociacao_fechada", fechada)

    terceira = amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X,
        chave_idempotencia="K1",
    )
    assert terceira.status_code == 201
    assert corpo(terceira) == corpo_primeira
    assert len(amb.bus.de("negociacao_fechada")) == 1


def test_ca45_chave_reutilizada_com_outro_corpo_de_outro_usuario_ou_invalida():
    """CA-45 (adaptado para D1): Chave reutilizada com outro corpo, de outro usuário ou inválida."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()

    r1 = amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X,
        chave_idempotencia="K1",
    )
    assert r1.status_code == 201
    assert corpo(r1)["sequencia"] == 2

    # Mesmo usuário e mesma chave com outro corpo -> 409 IDEMPOTENCIA_CONFLITO.
    r_conflito = amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("120.00")}, "BUYER", COMP_X,
        chave_idempotencia="K1",
    )
    assert codigo_erro(r_conflito) == (409, "IDEMPOTENCIA_CONFLITO")

    # Outro participante (FORN-B) com a mesma chave "K1" -> escopo novo, efeito novo.
    r_forn = amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("128.00")}, "SUPPLIER", FORN_B,
        chave_idempotencia="K1",
    )
    assert r_forn.status_code == 201
    assert corpo(r_forn)["sequencia"] == 3

    # Outro usuário (sub diferente) da mesma empresa COMP-X com a mesma chave "K1" -> escopo novo, efeito novo.
    jwt_outro_sub = amb.token("BUYER", COMP_X, sub="c3000000-0000-4000-8000-000000000002")
    r_outro_sub = amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("126.00")},
        jwt_token=jwt_outro_sub, chave_idempotencia="K1",
    )
    assert r_outro_sub.status_code == 201
    assert corpo(r_outro_sub)["sequencia"] == 4

    # Chave com espaço -> 422 VALIDACAO_INVALIDA.
    r_chave_invalida = amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("127.00")}, "SUPPLIER", FORN_B,
        chave_idempotencia="a b",
    )
    assert codigo_erro(r_chave_invalida) == (422, "VALIDACAO_INVALIDA")

    # Chave vazia "" -> equivale a ausente (sem idempotência memorizada).
    r_vazia_1 = amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("127.00")}, "SUPPLIER", FORN_B,
        chave_idempotencia="",
    )
    assert r_vazia_1.status_code == 201
    assert corpo(r_vazia_1)["sequencia"] == 5

    # Ao voltar a vez para FORN-B, outro corpo com chave "" cria proposta nova em vez de conflitar.
    assert amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("126.50")}, "BUYER", COMP_X,
    ).status_code == 201
    r_vazia_2 = amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("126.80")}, "SUPPLIER", FORN_B,
        chave_idempotencia="",
    )
    assert r_vazia_2.status_code == 201
    assert corpo(r_vazia_2)["sequencia"] == 7


# ---------------------------------------------------------------- §7.2, §10 e RN-22: precedência, corpos e atomicidade

def test_ordem_de_precedencia_dos_erros_nas_rotas_de_acao():
    """§7.2 e §10: Quando dois erros se aplicam em API-09, API-10 ou API-11, retorna-se o primeiro da lista."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.inserir_l1(status="DESERTO")

    d_imediata = "31000000-0000-4000-8000-000000000022"
    msg_imediata = evento_d1(negociacaoId=d_imediata, compraImediata=True)
    msg_imediata["eventId"] = str(uuid4())
    msg_imediata["aggregateId"] = d_imediata
    amb.entregar("modo_negociacao_definido", msg_imediata)

    for rota in ("propostas", "aceitar", "recusar"):
        # Passo 2 (TOKEN_INVALIDO) precede passo 5 (UUID e corpo inválidos).
        assert codigo_erro(
            amb.post(f"/api/v1/negociacoes/nao-uuid/{rota}", {"invalido": 1}, sem_auth=True),
        ) == (401, "TOKEN_INVALIDO")

        # Passo 3 (CONTEXTO_NAO_SELECIONADO) precede passo 4 (ACESSO_NEGADO) e passo 5 (VALIDACAO_INVALIDA).
        jwt_sem_ctx = amb.token("CARRIER", None, view_mode="NONE", tenant_id=None)
        assert codigo_erro(
            amb.post(f"/api/v1/negociacoes/nao-uuid/{rota}", {"invalido": 1}, jwt_token=jwt_sem_ctx),
        ) == (403, "CONTEXTO_NAO_SELECIONADO")

        # Passo 4 (ACESSO_NEGADO) precede passo 5 (VALIDACAO_INVALIDA).
        assert codigo_erro(
            amb.post(
                f"/api/v1/negociacoes/nao-uuid/{rota}", {"invalido": 1}, "CARRIER", COMP_X,
                chave_idempotencia="a b",
            ),
        ) == (403, "ACESSO_NEGADO")

        # Passo 5 (VALIDACAO_INVALIDA: corpo ou chave) precede passo 6 (NEGOCIACAO_NAO_ENCONTRADA).
        assert codigo_erro(
            amb.post(f"/api/v1/negociacoes/{D1}/{rota}", {"invalido": 1}, "BUYER", COMP_Y),
        ) == (422, "VALIDACAO_INVALIDA")
        corpo_valido = {"precoUnitario": D("125.00")} if rota == "propostas" else {}
        assert codigo_erro(
            amb.post(
                f"/api/v1/negociacoes/{D1}/{rota}", corpo_valido, "BUYER", COMP_Y,
                chave_idempotencia="a b",
            ),
        ) == (422, "VALIDACAO_INVALIDA")

        # Passo 6 (NEGOCIACAO_NAO_ENCONTRADA) precede passo 9 (COMPRA_IMEDIATA / NEGOCIACAO_ENCERRADA).
        assert codigo_erro(
            amb.post(f"/api/v1/negociacoes/{d_imediata}/{rota}", corpo_valido, "BUYER", COMP_Y),
        ) == (404, "NEGOCIACAO_NAO_ENCONTRADA")

        # Passo 7 (MODO_INCOMPATIVEL) precede passo 9 (NEGOCIACAO_ENCERRADA).
        assert codigo_erro(
            amb.post(f"/api/v1/negociacoes/{L1}/{rota}", corpo_valido, "BUYER", COMP_X),
        ) == (409, "MODO_INCOMPATIVEL")

    # API-09: COMPRA_IMEDIATA precede FORA_DA_VEZ e PROPOSTA_ACIMA_DA_OFERTA.
    assert codigo_erro(
        amb.post(f"/api/v1/negociacoes/{d_imediata}/propostas", {"precoUnitario": D("130.01")}, "SUPPLIER", FORN_B),
    ) == (409, "COMPRA_IMEDIATA")

    # API-09: FORA_DA_VEZ precede PROPOSTA_ACIMA_DA_OFERTA.
    assert codigo_erro(
        amb.post(f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("130.01")}, "SUPPLIER", FORN_B),
    ) == (409, "FORA_DA_VEZ")

    # API-10: COMPRA_IMEDIATA precede FORA_DA_VEZ e RESERVA_PENDENTE.
    assert codigo_erro(
        amb.post(f"/api/v1/negociacoes/{d_imediata}/aceitar", escopo="SUPPLIER", empresa=FORN_B),
    ) == (409, "COMPRA_IMEDIATA")

    # API-10: FORA_DA_VEZ precede RESERVA_PENDENTE (em D1 a vez é BUYER e a reserva está PENDENTE).
    assert codigo_erro(
        amb.post(f"/api/v1/negociacoes/{D1}/aceitar", escopo="SUPPLIER", empresa=FORN_B),
    ) == (409, "FORA_DA_VEZ")

    # Passo 8 (IDEMPOTENCIA_CONFLITO) precede passo 9 (NEGOCIACAO_ENCERRADA).
    assert amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X,
        chave_idempotencia="K-ORDEM",
    ).status_code == 201
    assert amb.post(f"/api/v1/negociacoes/{D1}/recusar", escopo="BUYER", empresa=COMP_X).status_code == 200
    assert codigo_erro(
        amb.post(
            f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("120.00")}, "BUYER", COMP_X,
            chave_idempotencia="K-ORDEM",
        ),
    ) == (409, "IDEMPOTENCIA_CONFLITO")

    # Em API-09, API-10 e API-11: NEGOCIACAO_ENCERRADA precede COMPRA_IMEDIATA, FORA_DA_VEZ e RESERVA_PENDENTE.
    amb.em("2026-11-22T12:00:00.000Z")
    assert codigo_erro(
        amb.post(f"/api/v1/negociacoes/{d_imediata}/propostas", {"precoUnitario": D("130.01")}, "SUPPLIER", FORN_B),
    ) == (409, "NEGOCIACAO_ENCERRADA")
    assert codigo_erro(
        amb.post(f"/api/v1/negociacoes/{d_imediata}/aceitar", escopo="SUPPLIER", empresa=FORN_B),
    ) == (409, "NEGOCIACAO_ENCERRADA")
    assert codigo_erro(
        amb.post(f"/api/v1/negociacoes/{d_imediata}/recusar", escopo="SUPPLIER", empresa=FORN_B),
    ) == (409, "NEGOCIACAO_ENCERRADA")


@pytest.mark.parametrize("rota", ["aceitar", "recusar"])
@pytest.mark.parametrize("corpo_invalido", [
    {"precoUnitario": D("125.00")},
    {"extra": 1},
    [],
    "texto",
    123,
    True,
])
def test_aceitar_e_recusar_rejeitam_corpo_diferente_de_vazio_ou_objeto_vazio(rota, corpo_invalido):
    """API-10 e API-11: Corpo diferente de vazio ou `{}` responde 422 VALIDACAO_INVALIDA."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.confirmar_reserva(OF_B, PT_D, D1)

    r = amb.post(f"/api/v1/negociacoes/{D1}/{rota}", corpo_invalido, "BUYER", COMP_X)
    assert codigo_erro(r) == (422, "VALIDACAO_INVALIDA")

    r_json_quebrado = amb.post(f"/api/v1/negociacoes/{D1}/{rota}", bruto="{invalido", escopo="BUYER", empresa=COMP_X)
    assert codigo_erro(r_json_quebrado) == (422, "VALIDACAO_INVALIDA")


def test_rn22_requisicao_rejeitada_nao_muda_estado_nao_memoriza_idempotencia_nem_publica_evento():
    """RN-22: Nenhuma requisição rejeitada (4xx ou 5xx) muda o estado, memoriza idempotência ou publica evento."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.bus.publicados.clear()

    detalhe_inicial = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))
    propostas_iniciais = corpo(amb.get(f"/api/v1/negociacoes/{D1}/propostas", "BUYER", COMP_X))

    # 401, 403, 404, 409, 422, 500 e 503 todos com Idempotency-Key "K-REJEITADA".
    assert codigo_erro(
        amb.post(
            f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, sem_auth=True,
            chave_idempotencia="K-REJEITADA",
        ),
    ) == (401, "TOKEN_INVALIDO")
    assert codigo_erro(
        amb.post(
            f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "CARRIER", COMP_X,
            chave_idempotencia="K-REJEITADA",
        ),
    ) == (403, "ACESSO_NEGADO")
    assert codigo_erro(
        amb.post(
            f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_Y,
            chave_idempotencia="K-REJEITADA",
        ),
    ) == (404, "NEGOCIACAO_NAO_ENCONTRADA")
    assert codigo_erro(
        amb.post(
            f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": "125.00"}, "BUYER", COMP_X,
            chave_idempotencia="K-REJEITADA",
        ),
    ) == (422, "VALIDACAO_INVALIDA")
    assert codigo_erro(
        amb.post(
            f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("130.01")}, "BUYER", COMP_X,
            chave_idempotencia="K-REJEITADA",
        ),
    ) == (422, "PROPOSTA_ACIMA_DA_OFERTA")
    assert codigo_erro(
        amb.post(
            f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "SUPPLIER", FORN_B,
            chave_idempotencia="K-REJEITADA",
        ),
    ) == (409, "FORA_DA_VEZ")
    assert codigo_erro(
        amb.post(
            f"/api/v1/negociacoes/{D1}/aceitar", {}, "BUYER", COMP_X,
            chave_idempotencia="K-REJEITADA",
        ),
    ) == (409, "RESERVA_PENDENTE")

    amb.repo.falhar("atualizar_negociacao", RuntimeError("erro após inserir_proposta"), vezes=1)
    assert codigo_erro(
        amb.post(
            f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("125.00")}, "BUYER", COMP_X,
            chave_idempotencia="K-REJEITADA",
        ),
    ) == (500, "ERRO_INTERNO")

    amb.repo.falhar("atualizar_negociacao", RepositorioIndisponivel("banco caiu no meio"), vezes=1)
    assert codigo_erro(
        amb.post(
            f"/api/v1/negociacoes/{D1}/recusar", {}, "BUYER", COMP_X,
            chave_idempotencia="K-REJEITADA",
        ),
    ) == (503, "DEPENDENCIA_INDISPONIVEL")

    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X)) == detalhe_inicial
    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}/propostas", "BUYER", COMP_X)) == propostas_iniciais
    assert amb.bus.publicados == []
    assert amb.repo.obter_idempotencia(
        f'["{COMP_X}", "c3000000-0000-4000-8000-000000000001", "POST", "/api/v1/negociacoes/{D1}/propostas", "K-REJEITADA"]'
    ) is None

    # A chave "K-REJEITADA" pode ser usada normalmente numa chamada válida subsequente.
    r_sucesso = amb.post(
        f"/api/v1/negociacoes/{D1}/propostas", {"precoUnitario": D("124.00")}, "BUYER", COMP_X,
        chave_idempotencia="K-REJEITADA",
    )
    assert r_sucesso.status_code == 201
    assert corpo(r_sucesso)["precoUnitario"] == D("124.00")
