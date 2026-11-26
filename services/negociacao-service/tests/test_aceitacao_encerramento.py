"""Suíte de aceitação do encerramento do leilão e rateio (parte 7 da ESP-NEG-001 v1.5)."""
from decimal import Decimal
from uuid import UUID

from app.jsonio import loads
from app.main import executar_prazos
from tests.cenarios import (
    COMP_X, COMP_Y, CORR_D1, CORR_L1, D1, DEM_X, DEM_Y, FORN_A, FORN_B, FORN_C, L1, L3, L4, OF_A, OF_B, OF_D, P, PT_A,
    PT_B, PT_D, Ambiente, exemplo, item_leilao, validar_erro_rfc7807, validar_schema,
)

D = Decimal


def corpo(resposta):
    return loads(resposta.text)


def codigo_erro(resposta) -> tuple[int, str]:
    dados = validar_erro_rfc7807(resposta)
    return resposta.status_code, dados["codigo"]


# ---------------------------------------------------------------- RF-06: encerrar o leilão em dataFim e ratear

def test_ca20_leilao_conjunto_encerrado_com_vencedor():
    """CA-20: Leilão conjunto encerrado com vencedor e payload igual a negociacao_fechada.exemplo.json."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()

    amb.em("13:05:00")
    r1 = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2550.00")}, "BUYER", COMP_X)
    assert r1.status_code == 201

    amb.em("13:10:00")
    r2 = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2600.00"), "demandaId": DEM_Y}, "BUYER", COMP_Y)
    assert r2.status_code == 201
    amb.bus.publicados.clear()

    amb.em("15:00:00")
    assert executar_prazos(amb.app) == 1

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_Y))
    assert detalhe["status"] == "ENCERRADO"
    assert detalhe["compradorId"] == COMP_Y
    assert detalhe["encerradoEm"] == "2026-11-20T15:00:00.000Z"
    assert detalhe["acoesPermitidas"] == []

    assert len(amb.bus.publicados) == 1
    [(chave, fechada)] = amb.bus.de("negociacao_fechada")
    assert chave == L1
    assert fechada["correlationId"] == CORR_L1
    assert fechada["timestamp"] == "2026-11-20T15:00:00.000Z"
    assert fechada["payload"] == exemplo("negociacao_fechada")["payload"] == {
        "negociacaoId": L1,
        "tipo": "LEILAO",
        "status": "ENCERRADO",
        "compradorId": COMP_Y,
        "demandaId": DEM_Y,
        "produtoId": P,
        "alocacoes": [
            {
                "fornecimentoId": OF_A,
                "fornecedorId": FORN_A,
                "pedidoId": PT_A,
                "quantidade": D("5.00"),
                "valorUnitario": D("122.35"),
                "valorTotal": D("611.76"),
            },
            {
                "fornecimentoId": OF_B,
                "fornecedorId": FORN_B,
                "pedidoId": PT_B,
                "quantidade": D("15.00"),
                "valorUnitario": D("132.55"),
                "valorTotal": D("1988.24"),
            },
        ],
    }
    validar_schema("negociacao_fechada", fechada)


def test_ca21_leilao_aberto_sem_lances():
    """CA-21: Leilão aberto sem lances encerra como DESERTO em dataFim."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()
    amb.bus.publicados.clear()

    amb.em("15:00:00")
    assert executar_prazos(amb.app) == 1

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))
    assert detalhe["status"] == "DESERTO"
    assert detalhe["compradorId"] is None
    assert detalhe["encerradoEm"] == "2026-11-20T15:00:00.000Z"
    assert detalhe["acoesPermitidas"] == []

    assert len(amb.bus.publicados) == 1
    [(chave, fechada)] = amb.bus.de("negociacao_fechada")
    assert chave == L1
    assert fechada["correlationId"] == CORR_L1
    assert fechada["payload"] == {
        "negociacaoId": L1,
        "tipo": "LEILAO",
        "status": "DESERTO",
        "compradorId": None,
        "demandaId": None,
        "produtoId": P,
        "alocacoes": [],
    }
    validar_schema("negociacao_fechada", fechada)


def test_ca22_encerramento_uma_unica_vez_e_no_instante_certo():
    """CA-22: Encerramento uma única vez e no instante certo (0 às 14:59:59.999; 1 e depois 0 às 15:00:00)."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()
    amb.em("13:05:00")
    assert amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2550.00")}, "BUYER", COMP_X).status_code == 201
    amb.em("13:10:00")
    assert amb.post(
        f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2600.00"), "demandaId": DEM_Y}, "BUYER", COMP_Y,
    ).status_code == 201
    amb.bus.publicados.clear()

    amb.em("14:59:59.999")
    assert executar_prazos(amb.app) == 0
    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_Y))["status"] == "ABERTO"
    assert amb.bus.de("negociacao_fechada") == []

    amb.em("15:00:00")
    assert executar_prazos(amb.app) == 1
    assert executar_prazos(amb.app) == 0

    fechadas = amb.bus.de("negociacao_fechada")
    assert len(fechadas) == 1
    assert fechadas[0][0] == L1
    validar_schema("negociacao_fechada", fechadas[0][1])


def test_ca23_rateio_com_centavos_que_sobram_e_lote_recalculado():
    """CA-23: Rateio com centavos que sobram (L3) e lote recalculado após recusa de oferta (L1)."""
    amb = Ambiente("12:00:00")

    # Parte 1: L3 com 3 ofertas confirmadas e lance 1000.01 sem demandaId -> sobra de 0.02 vai para OF-B (maior valor).
    amb.abrir_leilao(
        3,
        itens=[
            item_leilao(OF_A, "1.00", "120.00", "120.00", "0.3333"),
            item_leilao(OF_B, "1.00", "130.00", "130.00", "0.3611"),
            item_leilao(OF_D, "1.00", "110.00", "110.00", "0.3056"),
        ],
        preco_inicial="360.00",
        incremento_minimo="3.60",
    )
    amb.em("12:01:00")
    amb.confirmar_reserva(OF_A, PT_A, L3, correlation_id=CORR_L1)
    amb.confirmar_reserva(OF_B, PT_B, L3, correlation_id=CORR_L1)
    amb.confirmar_reserva(OF_D, PT_D, L3, correlation_id=CORR_L1)

    amb.em("13:00:00")
    assert amb.post(f"/api/v1/negociacoes/{L3}/lances", {"valor": D("1000.01")}, "BUYER", COMP_X).status_code == 201
    amb.bus.publicados.clear()

    amb.em("15:00:00")
    assert executar_prazos(amb.app) == 1

    [(chave_l3, fechada_l3)] = amb.bus.de("negociacao_fechada")
    assert chave_l3 == L3
    assert fechada_l3["payload"] == {
        "negociacaoId": L3,
        "tipo": "LEILAO",
        "status": "ENCERRADO",
        "compradorId": COMP_X,
        "demandaId": None,
        "produtoId": P,
        "alocacoes": [
            {
                "fornecimentoId": OF_A,
                "fornecedorId": FORN_A,
                "pedidoId": PT_A,
                "quantidade": D("1.00"),
                "valorUnitario": D("333.33"),
                "valorTotal": D("333.33"),
            },
            {
                "fornecimentoId": OF_B,
                "fornecedorId": FORN_B,
                "pedidoId": PT_B,
                "quantidade": D("1.00"),
                "valorUnitario": D("361.13"),
                "valorTotal": D("361.13"),
            },
            {
                "fornecimentoId": OF_D,
                "fornecedorId": FORN_C,
                "pedidoId": PT_D,
                "quantidade": D("1.00"),
                "valorUnitario": D("305.55"),
                "valorTotal": D("305.55"),
            },
        ],
    }
    assert sum(a["valorTotal"] for a in fechada_l3["payload"]["alocacoes"]) == D("1000.01")
    assert all(a["valorUnitario"] == a["valorTotal"] for a in fechada_l3["payload"]["alocacoes"])
    validar_schema("negociacao_fechada", fechada_l3)

    # Parte 2: L1 com OF-B recusada (lote só com OF-A) e lance vencedor 700.00 de COMP-X.
    amb2 = Ambiente("12:00:00")
    amb2.abrir_l1()
    amb2.em("12:01:00")
    amb2.confirmar_reserva(OF_A, PT_A, L1, correlation_id=CORR_L1)
    amb2.recusar_reserva(OF_B, PT_B, L1, correlation_id=CORR_L1)

    amb2.em("13:00:00")
    assert amb2.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("700.00")}, "BUYER", COMP_X).status_code == 201
    amb2.bus.publicados.clear()

    amb2.em("15:00:00")
    assert executar_prazos(amb2.app) == 1

    [(chave_l1, fechada_l1)] = amb2.bus.de("negociacao_fechada")
    assert chave_l1 == L1
    assert fechada_l1["payload"]["alocacoes"] == [
        {
            "fornecimentoId": OF_A,
            "fornecedorId": FORN_A,
            "pedidoId": PT_A,
            "quantidade": D("5.00"),
            "valorUnitario": D("140.00"),
            "valorTotal": D("700.00"),
        }
    ]
    validar_schema("negociacao_fechada", fechada_l1)


# ---------------------------------------------------------------- RF-04: reserva ainda pendente em dataFim

def test_ca12_reserva_ainda_pendente_em_data_fim():
    """CA-12: Reserva ainda pendente em dataFim encerra como DESERTO e ignora confirmação tardia."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1()
    amb.em("12:01:00")
    amb.confirmar_reserva(OF_A, PT_A, L1, correlation_id=CORR_L1)
    amb.bus.publicados.clear()

    amb.em("14:59:59.999")
    assert executar_prazos(amb.app) == 0
    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))["status"] == "AGUARDANDO_RESERVAS"
    assert amb.bus.publicados == []

    amb.em("15:00:00")
    assert executar_prazos(amb.app) == 1

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))
    assert detalhe["status"] == "DESERTO"
    assert detalhe["compradorId"] is None
    assert detalhe["encerradoEm"] == "2026-11-20T15:00:00.000Z"

    assert len(amb.bus.publicados) == 1
    [(chave, fechada)] = amb.bus.de("negociacao_fechada")
    assert chave == L1
    assert fechada["correlationId"] == "teste-neg-01"
    assert fechada["payload"] == {
        "negociacaoId": L1,
        "tipo": "LEILAO",
        "status": "DESERTO",
        "compradorId": None,
        "demandaId": None,
        "produtoId": P,
        "alocacoes": [],
    }
    validar_schema("negociacao_fechada", fechada)

    # A confirmação de OF-B que chega depois do encerramento DESERTO é ignorada (RN-04, RN-21).
    amb.bus.publicados.clear()
    amb.em("15:01:00")
    amb.confirmar_reserva(OF_B, PT_B, L1, correlation_id=CORR_L1)

    detalhe_pos = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))
    assert detalhe_pos["status"] == "DESERTO"
    assert [o["situacao"] for o in detalhe_pos["ofertas"]] == ["CONFIRMADA", "PENDENTE"]
    assert amb.bus.publicados == []


# ---------------------------------------------------------------- RNF-01: CA-51 completa com encerramento e rateio

def test_ca51_exatidao_decimal():
    """CA-51: Lote de 0.10 + 0.20 com lance 0.30 gera alocações 0.10 e 0.20; lance 2550 devolve 2550.00."""
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
    amb.abrir_l1_aberto()
    amb.confirmar_reserva(OF_A, PT_A, L4, correlation_id=CORR_L1)
    amb.confirmar_reserva(OF_B, PT_B, L4, correlation_id=CORR_L1)

    amb.em("13:00:00")
    r_l4 = amb.post(f"/api/v1/negociacoes/{L4}/lances", {"valor": D("0.30")}, "BUYER", COMP_X)
    assert r_l4.status_code == 201
    assert corpo(r_l4)["valor"] == D("0.30")

    r_l1 = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": 2550}, "BUYER", COMP_X)
    assert r_l1.status_code == 201
    assert '"valor":2550.00' in r_l1.text.replace(" ", "")
    assert corpo(r_l1)["valor"] == D("2550.00")

    amb.bus.publicados.clear()
    amb.em("15:00:00")
    assert executar_prazos(amb.app) == 2

    fechadas = dict(amb.bus.de("negociacao_fechada"))
    alocacoes_l4 = fechadas[L4]["payload"]["alocacoes"]
    assert alocacoes_l4 == [
        {
            "fornecimentoId": OF_A,
            "fornecedorId": FORN_A,
            "pedidoId": PT_A,
            "quantidade": D("1.00"),
            "valorUnitario": D("0.10"),
            "valorTotal": D("0.10"),
        },
        {
            "fornecimentoId": OF_B,
            "fornecedorId": FORN_B,
            "pedidoId": PT_B,
            "quantidade": D("1.00"),
            "valorUnitario": D("0.20"),
            "valorTotal": D("0.20"),
        },
    ]
    assert sum(a["valorTotal"] for a in alocacoes_l4) == D("0.30")
    assert sum(a["valorTotal"] for a in fechadas[L1]["payload"]["alocacoes"]) == D("2550.00")
    validar_schema("negociacao_fechada", fechadas[L4])
    validar_schema("negociacao_fechada", fechadas[L1])


# ---------------------------------------------------------------- RN-21 e RN-09: prazos combinados, bloqueio e sigilo

def test_rn21_direta_vencida_e_leilao_vencido_na_mesma_chamada():
    """RN-21: Uma direta vencida e um leilão vencido na mesma chamada devolvem 2, e nova chamada devolve 0."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.abrir_l1_aberto()
    amb.em("13:00:00")
    assert amb.post(
        f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2550.00"), "demandaId": DEM_X}, "BUYER", COMP_X,
    ).status_code == 201
    amb.bus.publicados.clear()

    # Em 2026-11-22T12:00:00Z, L1 (dataFim 2026-11-20T15:00:00Z) e D1 (expiraEm 2026-11-22T12:00:00Z) estão vencidos.
    amb.em("2026-11-22T12:00:00.000Z")
    assert executar_prazos(amb.app) == 2
    assert executar_prazos(amb.app) == 0

    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))["status"] == "ENCERRADO"
    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["status"] == "EXPIRADO"

    fechadas = amb.bus.de("negociacao_fechada")
    assert [chave for chave, _ in fechadas] == [L1, D1]
    assert fechadas[0][1]["correlationId"] == CORR_L1
    assert fechadas[0][1]["payload"]["status"] == "ENCERRADO"
    assert fechadas[0][1]["payload"]["demandaId"] == DEM_X
    assert fechadas[1][1]["correlationId"] == CORR_D1
    assert fechadas[1][1]["payload"]["status"] == "EXPIRADO"
    assert fechadas[1][1]["payload"]["demandaId"] is None
    for _, msg in fechadas:
        assert UUID(msg["eventId"]).version == 4
        validar_schema("negociacao_fechada", msg)


def test_rn21_e_rn09_leilao_encerrado_recusa_lance_e_aplica_sigilo_no_comprador_id():
    """RN-21 e RN-09: Leilão encerrado por executar_prazos recusa novo lance (409) e aplica sigilo em compradorId."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1_aberto()
    amb.em("13:05:00")
    assert amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2550.00")}, "BUYER", COMP_X).status_code == 201
    amb.em("13:10:00")
    assert amb.post(
        f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2600.00"), "demandaId": DEM_Y}, "BUYER", COMP_Y,
    ).status_code == 201

    amb.em("15:00:00")
    assert executar_prazos(amb.app) == 1
    assert executar_prazos(amb.app) == 0
    amb.bus.publicados.clear()

    # Mesmo que o relógio voltasse ou avance, o status ENCERRADO é final e recusa lances com 409 LEILAO_NAO_ABERTO.
    amb.em("15:05:00")
    r_lance = amb.post(f"/api/v1/negociacoes/{L1}/lances", {"valor": D("2700.00")}, "BUYER", COMP_X)
    assert codigo_erro(r_lance) == (409, "LEILAO_NAO_ABERTO")
    assert amb.bus.publicados == []
    assert amb.repo.listar_lances(L1, 0, 10)[1] == 2

    # Vencedor (COMP-Y) e fornecedores do leilão (FORN-A e FORN-B) veem o compradorId real; demais veem OCULTO.
    for escopo, empresa in (("BUYER", COMP_Y), ("SUPPLIER", FORN_A), ("SUPPLIER", FORN_B), ("BUYER", FORN_A)):
        detalhe_visivel = corpo(amb.get(f"/api/v1/negociacoes/{L1}", escopo, empresa))
        assert detalhe_visivel["status"] == "ENCERRADO"
        assert detalhe_visivel["compradorId"] == COMP_Y
        assert detalhe_visivel["melhorLance"]["compradorId"] == COMP_Y
        assert detalhe_visivel["acoesPermitidas"] == []

    for escopo, empresa in (("BUYER", COMP_X), ("SUPPLIER", FORN_C), ("BUYER", FORN_C)):
        detalhe_oculto = corpo(amb.get(f"/api/v1/negociacoes/{L1}", escopo, empresa))
        assert detalhe_oculto["status"] == "ENCERRADO"
        assert detalhe_oculto["compradorId"] == "OCULTO"
        assert detalhe_oculto["melhorLance"]["compradorId"] == "OCULTO"
        assert detalhe_oculto["acoesPermitidas"] == []
