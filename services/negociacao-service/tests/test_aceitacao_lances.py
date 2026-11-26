"""Suíte de aceitação dos lances do leilão (parte 6 da ESP-NEG-001 v1.4)."""
from decimal import Decimal
from uuid import UUID

from app.jsonio import formato_json, loads
from app.main import executar_prazos
from tests.cenarios import (
    COMP_X, COMP_Y, CORR_L1, D1, DEM_X, DEM_Y, FORN_A, FORN_B, FORN_C, L1, L4, OF_A, OF_B, P, PT_A, PT_B, SUB_PADRAO,
    Ambiente, item_leilao, t, validar_erro_rfc7807, validar_schema,
)

D = Decimal


def corpo(resposta):
    return loads(resposta.text)


def codigo_erro(resposta) -> tuple[int, str]:
    dados = validar_erro_rfc7807(resposta)
    return resposta.status_code, dados["codigo"]


def _validar_lance_realizado(chave: str, mensagem: dict, *, negociacao_id: str, lance_id: str, comprador_id: str,
                             valor: Decimal, sequencia: int, criado_em: str, correlacao: str | None = None) -> None:
    assert chave == mensagem["aggregateId"] == negociacao_id
    assert mensagem["eventType"] == "lance_realizado"
    assert mensagem["eventVersion"] == "1.0"
    assert mensagem["source"] == "negociacao-service"
    assert mensagem["timestamp"] == criado_em
    assert UUID(mensagem["eventId"]).version == 4
    if correlacao is not None:
        assert mensagem["correlationId"] == correlacao
    assert mensagem["payload"] == {
        "negociacaoId": negociacao_id,
        "lanceId": lance_id,
        "compradorId": comprador_id,
        "valor": valor,
        "sequencia": sequencia,
        "criadoEm": t(criado_em),
    }
    assert formato_json(mensagem["payload"]["criadoEm"]) == criado_em
    validar_schema("lance_realizado", mensagem)


# ---------------------------------------------------------------- RF-05: dar lance

def test_ca13_primeiro_lance_igual_ao_preco_inicial_com_demanda():
    """CA-13: Primeiro lance igual ao preço inicial, com demanda."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()
    amb.bus.publicados.clear()

    amb.em("13:05:00")
    resposta = amb.post(
        f"/api/v1/negociacoes/{L1}/lances",
        {"valor": D("2550.00"), "demandaId": DEM_X},
        "BUYER",
        COMP_X,
        headers={"X-Correlation-Id": "teste-neg-13"},
    )
    assert resposta.status_code == 201
    assert resposta.headers["x-correlation-id"] == "teste-neg-13"
    dados = corpo(resposta)
    assert UUID(dados["lanceId"]).version == 4
    assert dados == {
        "lanceId": dados["lanceId"],
        "negociacaoId": L1,
        "compradorId": COMP_X,
        "valor": D("2550.00"),
        "sequencia": 1,
        "criadoEm": "2026-11-20T13:05:00.000Z",
    }
    assert "demandaId" not in dados

    assert len(amb.bus.publicados) == 1
    [(chave, msg)] = amb.bus.de("lance_realizado")
    _validar_lance_realizado(
        chave,
        msg,
        negociacao_id=L1,
        lance_id=dados["lanceId"],
        comprador_id=COMP_X,
        valor=D("2550.00"),
        sequencia=1,
        criado_em="2026-11-20T13:05:00.000Z",
        correlacao="teste-neg-13",
    )

    # O demandaId é gravado no repositório em memória para o fechamento do leilão (RN-07, RN-10).
    [gravado], total = amb.repo.listar_lances(L1, 0, 10)
    assert total == 1
    assert gravado["lanceId"] == dados["lanceId"]
    assert gravado["demandaId"] == DEM_X
    assert gravado["usuarioId"] == SUB_PADRAO


def test_ca14_valor_minimo_e_incremento():
    """CA-14: Valor mínimo e incremento (RN-07 e RN-22 nos lances recusados)."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()
    amb.bus.publicados.clear()
    amb.em("13:05:00")

    recusado_1 = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2549.99")}, "BUYER", COMP_X)
    assert codigo_erro(recusado_1) == (422, "LANCE_INSUFICIENTE")
    assert amb.bus.publicados == []
    assert amb.repo.listar_lances(L1, 0, 10) == ([], 0)
    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))["totalLances"] == 0

    aceito_1 = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2550.00")}, "BUYER", COMP_X)
    assert aceito_1.status_code == 201
    dados_1 = corpo(aceito_1)
    assert dados_1["sequencia"] == 1
    assert len(amb.bus.de("lance_realizado")) == 1

    amb.em("13:10:00")
    recusado_2 = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2575.49")}, "BUYER", COMP_Y)
    assert codigo_erro(recusado_2) == (422, "LANCE_INSUFICIENTE")
    assert len(amb.bus.de("lance_realizado")) == 1
    assert amb.repo.listar_lances(L1, 0, 10)[1] == 1
    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))["totalLances"] == 1

    aceito_2 = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2575.50")}, "BUYER", COMP_Y)
    assert aceito_2.status_code == 201
    dados_2 = corpo(aceito_2)
    assert dados_2["sequencia"] == 2
    assert dados_2["valor"] == D("2575.50")

    eventos = amb.bus.de("lance_realizado")
    assert len(eventos) == 2
    _validar_lance_realizado(
        eventos[0][0], eventos[0][1], negociacao_id=L1, lance_id=dados_1["lanceId"], comprador_id=COMP_X,
        valor=D("2550.00"), sequencia=1, criado_em="2026-11-20T13:05:00.000Z",
    )
    _validar_lance_realizado(
        eventos[1][0], eventos[1][1], negociacao_id=L1, lance_id=dados_2["lanceId"], comprador_id=COMP_Y,
        valor=D("2575.50"), sequencia=2, criado_em="2026-11-20T13:10:00.000Z",
    )


def test_ca15_lance_fora_da_janela():
    """CA-15: Lance fora da janela (AGUARDANDO_RESERVAS, dataFim vencido sem executar_prazos e ENCERRADO)."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1()
    amb.bus.publicados.clear()

    # 1. AGUARDANDO_RESERVAS às 12:00:30.
    amb.em("12:00:30")
    r_aguardando = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("3000.00")}, "BUYER", COMP_X)
    assert codigo_erro(r_aguardando) == (409, "LEILAO_NAO_ABERTO")

    # 2. ABERTO mas relógio exatamente em dataFim (15:00:00), sem executar_prazos.
    amb.em("12:01:00")
    amb.confirmar_reserva(OF_A, PT_A, L1, correlation_id=CORR_L1)
    amb.confirmar_reserva(OF_B, PT_B, L1, correlation_id=CORR_L1)
    amb.em("15:00:00")
    r_vencido = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("3000.00")}, "BUYER", COMP_X)
    assert codigo_erro(r_vencido) == (409, "LEILAO_NAO_ABERTO")

    # 3. ENCERRADO.
    amb.em("13:00:00")
    with amb.repo.transacao():
        neg = amb.repo.obter_negociacao(L1)
        neg["status"] = "ENCERRADO"
        amb.repo.atualizar_negociacao(neg)
    r_encerrado = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("3000.00")}, "BUYER", COMP_X)
    assert codigo_erro(r_encerrado) == (409, "LEILAO_NAO_ABERTO")

    assert amb.bus.publicados == []
    assert amb.repo.listar_lances(L1, 0, 10) == ([], 0)


def test_ca16_conflito_de_interesse_no_leilao():
    """CA-16: Conflito de interesse no leilão (fornecedor confirmado ou com oferta recusada)."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1()
    amb.em("12:01:00")
    amb.confirmar_reserva(OF_A, PT_A, L1, correlation_id=CORR_L1)
    amb.recusar_reserva(OF_B, PT_B, L1, correlation_id=CORR_L1)
    amb.bus.publicados.clear()

    r_forn_a = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("3000.00")}, "BUYER", FORN_A)
    assert codigo_erro(r_forn_a) == (403, "CONFLITO_DE_INTERESSE")

    r_forn_b = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("3000.00")}, "BUYER", FORN_B)
    assert codigo_erro(r_forn_b) == (403, "CONFLITO_DE_INTERESSE")

    assert amb.bus.publicados == []
    assert amb.repo.listar_lances(L1, 0, 10) == ([], 0)
    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))["totalLances"] == 0


def test_ca17_escopo_corpo_e_modo_no_lance():
    """CA-17: Escopo, corpo e modo no lance."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()
    amb.abrir_d1()
    amb.bus.publicados.clear()

    r_escopo = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("3000.00")}, "SUPPLIER", FORN_C)
    assert codigo_erro(r_escopo) == (403, "ACESSO_NEGADO")

    corpos_invalidos = [
        {"valor": "3000.00"},
        {"valor": D("3000.001")},
        {"valor": 0},
        {"valor": D("-1.00")},
        {"valor": True},
        {},
        {"valor": D("3000.00"), "comprador": "x"},
        {"valor": D("3000.00"), "demandaId": ""},
        {"valor": D("3000.00"), "demandaId": "a b"},
        {"valor": D("3000.00"), "demandaId": 1},
    ]
    for invalido in corpos_invalidos:
        resposta = amb.post(f"/api/v1/negociacoes/{L1}/lances", invalido, "BUYER", COMP_X)
        assert codigo_erro(resposta) == (422, "VALIDACAO_INVALIDA")

    r_modo = amb.post(f"/api/v1/negociacoes/{D1}/lances", {"valor": D("3000.00")}, "BUYER", COMP_X)
    assert codigo_erro(r_modo) == (409, "MODO_INCOMPATIVEL")

    assert amb.bus.publicados == []
    assert amb.repo.listar_lances(L1, 0, 10) == ([], 0)


def test_ca18_precedencia_de_erros_no_lance():
    """CA-18: Precedência de erros no lance (VALIDACAO_INVALIDA -> CONFLITO_DE_INTERESSE -> LEILAO_NAO_ABERTO)."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()
    amb.bus.publicados.clear()

    with amb.repo.transacao():
        neg = amb.repo.obter_negociacao(L1)
        neg["status"] = "ENCERRADO"
        amb.repo.atualizar_negociacao(neg)

    r_validacao = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": "abc"}, "BUYER", FORN_A)
    assert codigo_erro(r_validacao) == (422, "VALIDACAO_INVALIDA")

    r_conflito = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("1.00")}, "BUYER", FORN_A)
    assert codigo_erro(r_conflito) == (403, "CONFLITO_DE_INTERESSE")

    with amb.repo.transacao():
        neg = amb.repo.obter_negociacao(L1)
        neg["status"] = "ABERTO"
        amb.repo.atualizar_negociacao(neg)
    amb.em("15:00:00")

    r_nao_aberto = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("1.00")}, "BUYER", COMP_X)
    assert codigo_erro(r_nao_aberto) == (409, "LEILAO_NAO_ABERTO")

    assert amb.bus.publicados == []
    assert amb.repo.listar_lances(L1, 0, 10) == ([], 0)


def test_ca19_autor_do_melhor_lance_pode_cobrir_o_proprio_lance():
    """CA-19: O autor do melhor lance pode cobrir o próprio lance."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()
    amb.bus.publicados.clear()

    amb.em("13:05:00")
    r1 = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2550.00")}, "BUYER", COMP_X)
    assert r1.status_code == 201

    amb.em("13:10:00")
    r2 = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2575.50")}, "BUYER", COMP_X)
    assert r2.status_code == 201
    dados_2 = corpo(r2)
    assert dados_2["sequencia"] == 2
    assert dados_2["compradorId"] == COMP_X
    assert dados_2["valor"] == D("2575.50")

    eventos = amb.bus.de("lance_realizado")
    assert len(eventos) == 2
    _validar_lance_realizado(
        eventos[1][0], eventos[1][1], negociacao_id=L1, lance_id=dados_2["lanceId"], comprador_id=COMP_X,
        valor=D("2575.50"), sequencia=2, criado_em="2026-11-20T13:10:00.000Z",
    )


# ---------------------------------------------------------------- RF-04: CA-11 completa com lance em leilão DESERTO

def test_ca11_nenhuma_reserva_confirmada_e_lance_em_leilao_deserto():
    """CA-11: Nenhuma reserva confirmada, lance às 12:30 recusado com 409 LEILAO_NAO_ABERTO e executar_prazos às 15:00."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1()
    amb.recusar_reserva(OF_A, PT_A, L1, correlation_id=CORR_L1)
    amb.bus.publicados.clear()

    amb.em("12:02:00")
    amb.recusar_reserva(OF_B, PT_B, L1, correlation_id=CORR_L1)

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))
    assert detalhe["status"] == "DESERTO"
    assert detalhe["compradorId"] is None
    assert detalhe["encerradoEm"] == "2026-11-20T12:02:00.000Z"

    assert len(amb.bus.publicados) == 1
    [(chave, fechada)] = amb.bus.de("negociacao_fechada")
    assert chave == L1
    assert fechada["payload"] == {
        "negociacaoId": L1,
        "tipo": "LEILAO",
        "status": "DESERTO",
        "compradorId": None,
        "demandaId": None,
        "produtoId": P,
        "alocacoes": [],
    }

    amb.em("12:30:00")
    r_lance = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("3000.00")}, "BUYER", COMP_X)
    assert codigo_erro(r_lance) == (409, "LEILAO_NAO_ABERTO")
    assert amb.bus.de("lance_realizado") == []
    assert amb.repo.listar_lances(L1, 0, 10) == ([], 0)

    amb.em("15:00:00")
    assert executar_prazos(amb.app) == 0
    assert len(amb.bus.publicados) == 1


# ---------------------------------------------------------------- RF-11 e RF-12: detalhe, listagem de lances e sigilo

def test_ca34_detalhe_do_leilao_com_sigilo_e_acoes():
    """CA-34: Detalhe do leilão com sigilo e ações igual ao exemplo 'Detalhe de leilão' do §7.2."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()

    amb.em("13:05:00")
    r1 = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2550.00")}, "BUYER", COMP_X)
    assert r1.status_code == 201

    amb.em("13:10:00")
    r2 = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2600.00"), "demandaId": DEM_Y}, "BUYER", COMP_Y)
    assert r2.status_code == 201
    lance_2 = corpo(r2)

    amb.em("13:20:00")
    r_comp_x = amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X)
    assert r_comp_x.status_code == 200
    assert corpo(r_comp_x) == {
        "negociacaoId": L1,
        "modo": "LEILAO_DIRETO",
        "status": "ABERTO",
        "produtoId": P,
        "dataInicio": "2026-11-20T12:00:00.000Z",
        "dataFim": "2026-11-20T15:00:00.000Z",
        "quantidadeTotal": D("20.00"),
        "precoInicial": D("2550.00"),
        "incrementoMinimo": D("25.50"),
        "melhorLance": {
            "lanceId": lance_2["lanceId"],
            "negociacaoId": L1,
            "compradorId": "OCULTO",
            "valor": D("2600.00"),
            "sequencia": 2,
            "criadoEm": "2026-11-20T13:10:00.000Z",
        },
        "totalLances": 2,
        "compradorId": None,
        "ofertas": [
            {
                "negociacaoId": L1,
                "fornecimentoId": OF_A,
                "fornecedorId": FORN_A,
                "quantidade": D("5.00"),
                "precoUnitario": D("120.00"),
                "valorItem": D("600.00"),
                "participacao": D("0.2353"),
                "situacao": "CONFIRMADA",
            },
            {
                "negociacaoId": L1,
                "fornecimentoId": OF_B,
                "fornecedorId": FORN_B,
                "quantidade": D("15.00"),
                "precoUnitario": D("130.00"),
                "valorItem": D("1950.00"),
                "participacao": D("0.7647"),
                "situacao": "CONFIRMADA",
            },
        ],
        "criadoEm": "2026-11-20T12:00:00.000Z",
        "encerradoEm": None,
        "agora": "2026-11-20T13:20:00.000Z",
        "acoesPermitidas": ["DAR_LANCE"],
    }

    detalhe_comp_y = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_Y))
    assert detalhe_comp_y["melhorLance"]["compradorId"] == COMP_Y

    detalhe_forn_a = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "SUPPLIER", FORN_A))
    assert detalhe_forn_a["melhorLance"]["compradorId"] == COMP_Y
    assert detalhe_forn_a["acoesPermitidas"] == []

    amb_aguardando = Ambiente("12:00:00")
    amb_aguardando.abrir_l1()
    amb_aguardando.em("12:00:30")
    assert corpo(amb_aguardando.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))["acoesPermitidas"] == []


def test_ca37_lances_com_sigilo_e_paginacao():
    """CA-37: Lances com sigilo e paginação (API-06)."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()

    amb.em("13:05:00")
    lance_1 = corpo(amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2550.00")}, "BUYER", COMP_X))
    amb.em("13:10:00")
    lance_2 = corpo(amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2600.00")}, "BUYER", COMP_Y))

    r_comp_x = amb.get(f"/api/v1/negociacoes/{L1}/lances", "BUYER", COMP_X)
    assert r_comp_x.status_code == 200
    assert corpo(r_comp_x) == {
        "items": [
            {
                "lanceId": lance_1["lanceId"],
                "negociacaoId": L1,
                "compradorId": COMP_X,
                "valor": D("2550.00"),
                "sequencia": 1,
                "criadoEm": "2026-11-20T13:05:00.000Z",
            },
            {
                "lanceId": lance_2["lanceId"],
                "negociacaoId": L1,
                "compradorId": "OCULTO",
                "valor": D("2600.00"),
                "sequencia": 2,
                "criadoEm": "2026-11-20T13:10:00.000Z",
            },
        ],
        "page": 1,
        "size": 10,
        "total": 2,
    }

    r_forn_b = amb.get(f"/api/v1/negociacoes/{L1}/lances", "SUPPLIER", FORN_B)
    assert r_forn_b.status_code == 200
    assert [item["compradorId"] for item in corpo(r_forn_b)["items"]] == [COMP_X, COMP_Y]

    r_pag = amb.get(f"/api/v1/negociacoes/{L1}/lances?page=2&size=1", "BUYER", COMP_X)
    assert r_pag.status_code == 200
    assert corpo(r_pag) == {
        "items": [
            {
                "lanceId": lance_2["lanceId"],
                "negociacaoId": L1,
                "compradorId": "OCULTO",
                "valor": D("2600.00"),
                "sequencia": 2,
                "criadoEm": "2026-11-20T13:10:00.000Z",
            }
        ],
        "page": 2,
        "size": 1,
        "total": 2,
    }


def test_ca38_lances_em_negociacao_direta_modo_incompativel():
    """CA-38: GET /api/v1/negociacoes/D1/lances responde 409 MODO_INCOMPATIVEL."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.abrir_l1()

    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{D1}/lances", "BUYER", COMP_X)) == (409, "MODO_INCOMPATIVEL")
    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{L1}/propostas", "BUYER", COMP_X)) == (409, "MODO_INCOMPATIVEL")
    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{D1}/lances", "BUYER", COMP_Y)) == (
        404, "NEGOCIACAO_NAO_ENCONTRADA",
    )


# ---------------------------------------------------------------- RF-13: escopos sem acesso no lance

def test_ca41_escopo_supplier_com_platform_admin_nao_da_lance():
    """CA-41 (segunda parte): SUPPLIER com platform_role PLATFORM_ADMIN e roles BUYER_PURCHASER recebe 403 ACESSO_NEGADO."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()
    amb.bus.publicados.clear()

    jwt_admin = amb.token(
        "SUPPLIER",
        FORN_C,
        platform_role="PLATFORM_ADMIN",
        roles=["BUYER_PURCHASER"],
        client_id="svc-teste",
    )
    resposta = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("3000.00")}, jwt_token=jwt_admin)
    assert codigo_erro(resposta) == (403, "ACESSO_NEGADO")
    assert amb.bus.publicados == []
    assert amb.repo.listar_lances(L1, 0, 10) == ([], 0)


# ---------------------------------------------------------------- RF-14: idempotência HTTP nos lances

def test_ca44_reenvio_do_mesmo_lance():
    """CA-44: Reenvio do mesmo lance em L1 ABERTO e depois em L1 ENCERRADO."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()
    amb.bus.publicados.clear()
    amb.em("13:05:00")

    r1 = amb.post(
        f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2550.00")}, "BUYER", COMP_X, chave_idempotencia="K1",
    )
    assert r1.status_code == 201
    primeiro = corpo(r1)

    r2 = amb.post(
        f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2550.00")}, "BUYER", COMP_X, chave_idempotencia="K1",
    )
    assert r2.status_code == 201
    assert corpo(r2) == primeiro
    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))["totalLances"] == 1
    assert len(amb.bus.de("lance_realizado")) == 1

    with amb.repo.transacao():
        neg = amb.repo.obter_negociacao(L1)
        neg["status"] = "ENCERRADO"
        amb.repo.atualizar_negociacao(neg)

    r3 = amb.post(
        f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2550.00")}, "BUYER", COMP_X, chave_idempotencia="K1",
    )
    assert r3.status_code == 201
    assert corpo(r3) == primeiro
    assert len(amb.bus.de("lance_realizado")) == 1


def test_ca45_chave_reutilizada_com_outro_corpo_outro_usuario_ou_invalida():
    """CA-45: Chave reutilizada com outro corpo, de outro usuário ou inválida."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()
    amb.bus.publicados.clear()
    amb.em("13:05:00")

    r1 = amb.post(
        f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2550.00")}, "BUYER", COMP_X, chave_idempotencia="K1",
    )
    assert r1.status_code == 201

    r_conflito = amb.post(
        f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2600.00")}, "BUYER", COMP_X, chave_idempotencia="K1",
    )
    assert codigo_erro(r_conflito) == (409, "IDEMPOTENCIA_CONFLITO")
    assert len(amb.bus.de("lance_realizado")) == 1

    r_outro = amb.post(
        f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2600.00")}, "BUYER", COMP_Y, chave_idempotencia="K1",
    )
    assert r_outro.status_code == 201
    assert corpo(r_outro)["sequencia"] == 2
    assert len(amb.bus.de("lance_realizado")) == 2

    r_chave_invalida = amb.post(
        f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2700.00")}, "BUYER", COMP_Y, chave_idempotencia="a b",
    )
    assert codigo_erro(r_chave_invalida) == (422, "VALIDACAO_INVALIDA")
    assert len(amb.bus.de("lance_realizado")) == 2

    r_chave_vazia = amb.post(
        f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2700.00")}, "BUYER", COMP_Y, chave_idempotencia="",
    )
    assert r_chave_vazia.status_code == 201
    assert corpo(r_chave_vazia)["sequencia"] == 3
    assert len(amb.bus.de("lance_realizado")) == 3


# ---------------------------------------------------------------- RNF-01: exatidão decimal nos lances

def test_ca51_exatidao_decimal_nos_lances():
    """CA-51 (parte do lance): Lote de 0.10 + 0.20 aceita lance 0.30 e lance inteiro 2550 sai com 2 casas (2550.00)."""
    amb = Ambiente("12:00:00")
    amb.abrir_leilao(
        4,
        itens=[
            item_leilao(OF_A, "1.00", "0.10", "0.10", "0.3333"),
            item_leilao(OF_B, "1.00", "0.20", "0.20", "0.6667"),
        ],
        preco_inicial="0.30",
        incremento_minimo="0.01",
    )
    amb.em("12:01:00")
    amb.confirmar_reserva(OF_A, PT_A, L4, correlation_id=CORR_L1)
    amb.confirmar_reserva(OF_B, PT_B, L4, correlation_id=CORR_L1)
    amb.bus.publicados.clear()

    amb.em("13:00:00")
    r_l4 = amb.post(f"/api/v1/negociacoes/{L4}/lances", {"valor": D("0.30")}, "BUYER", COMP_X)
    assert r_l4.status_code == 201
    dados_l4 = corpo(r_l4)
    assert dados_l4["valor"] == D("0.30")
    assert dados_l4["valor"].as_tuple().exponent == -2

    amb.abrir_l1_aberto()
    amb.bus.publicados.clear()
    amb.em("13:05:00")
    r_l1 = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": 2550}, "BUYER", COMP_X)
    assert r_l1.status_code == 201
    assert '"valor":2550.00' in r_l1.text.replace(" ", "")
    dados_l1 = corpo(r_l1)
    assert dados_l1["valor"] == D("2550.00")
    assert dados_l1["valor"].as_tuple().exponent == -2

    [(chave, evento)] = amb.bus.de("lance_realizado")
    _validar_lance_realizado(
        chave, evento, negociacao_id=L1, lance_id=dados_l1["lanceId"], comprador_id=COMP_X,
        valor=D("2550.00"), sequencia=1, criado_em="2026-11-20T13:05:00.000Z",
    )
