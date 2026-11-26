"""Suíte de aceitação da abertura do leilão e liberação dos lances (parte 5 da ESP-NEG-001 v1.4)."""
import copy
from decimal import Decimal
from uuid import UUID, uuid4

from app.jsonio import formato_json, loads
from app.main import executar_prazos
from tests.cenarios import (
    AUSENTE, COMP_X, COMP_Y, CORR_L1, D1, FORN_A, FORN_B, FORN_C, L1, L2, L4, OF_A, OF_B, OF_D, OF_E, P, PT_A, PT_B,
    PT_D, Ambiente, confirmar_reserva, evento_d1, evento_l1, evento_pedido_atualizado, item_leilao, recusar_reserva, t,
    validar_erro_rfc7807, validar_schema,
)

D = Decimal


def corpo(resposta):
    return loads(resposta.text)


def codigo_erro(resposta) -> tuple[int, str]:
    dados = validar_erro_rfc7807(resposta)
    return resposta.status_code, dados["codigo"]


# ---------------------------------------------------------------- RF-01: abrir o leilão

def test_ca01_leilao_aberto_pelo_mercado():
    """CA-01: Leilão aberto pelo Mercado."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1()

    resposta = amb.get(f"/api/v1/negociacoes/{L1}", "SUPPLIER", FORN_C)
    assert resposta.status_code == 200
    assert corpo(resposta) == {
        "negociacaoId": L1,
        "modo": "LEILAO_DIRETO",
        "status": "AGUARDANDO_RESERVAS",
        "produtoId": P,
        "dataInicio": "2026-11-20T12:00:00.000Z",
        "dataFim": "2026-11-20T15:00:00.000Z",
        "quantidadeTotal": D("20.00"),
        "precoInicial": D("2550.00"),
        "incrementoMinimo": D("25.50"),
        "melhorLance": None,
        "totalLances": 0,
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
                "situacao": "PENDENTE",
            },
            {
                "negociacaoId": L1,
                "fornecimentoId": OF_B,
                "fornecedorId": FORN_B,
                "quantidade": D("15.00"),
                "precoUnitario": D("130.00"),
                "valorItem": D("1950.00"),
                "participacao": D("0.7647"),
                "situacao": "PENDENTE",
            },
        ],
        "criadoEm": "2026-11-20T12:00:00.000Z",
        "encerradoEm": None,
        "agora": "2026-11-20T12:00:00.000Z",
        "acoesPermitidas": [],
    }

    assert len(amb.bus.publicados) == 2
    mensagens = amb.bus.de("pedido_temporario_solicitado")
    assert len(mensagens) == 2
    (chave_a, msg_a), (chave_b, msg_b) = mensagens
    assert chave_a == chave_b == L1
    assert msg_a["correlationId"] == msg_b["correlationId"] == CORR_L1
    assert msg_a["payload"] == {
        "negociacaoId": L1,
        "tipo": "LEILAO",
        "fornecimentoId": OF_A,
        "fornecedorId": FORN_A,
        "produtoId": P,
        "quantidade": D("5.00"),
        "precoUnitario": D("120.00"),
    }
    assert msg_b["payload"] == {
        "negociacaoId": L1,
        "tipo": "LEILAO",
        "fornecimentoId": OF_B,
        "fornecedorId": FORN_B,
        "produtoId": P,
        "quantidade": D("15.00"),
        "precoUnitario": D("130.00"),
    }


def test_ca02_evento_repetido_negociacao_ja_conhecida_e_modo_de_leilao():
    """CA-02: Evento repetido, negociação já conhecida e modo de leilão."""
    amb = Ambiente("12:00:00")
    original = amb.abrir_l1()
    amb.bus.publicados.clear()

    # Mesmo leilao_iniciado com o mesmo eventId -> ignorado por deduplicação.
    amb.entregar("leilao_iniciado", original)

    # Outro eventId e quantidadeTotal 30.00 para o mesmo negociacaoId L1 -> ignorado pela RN-01.
    outro_evento = evento_l1(quantidadeTotal=D("30.00"))
    outro_evento["eventId"] = str(uuid4())
    amb.entregar("leilao_iniciado", outro_evento)

    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))["quantidadeTotal"] == D("20.00")
    assert amb.bus.publicados == []
    assert amb.bus.do_topico("negociacao.dlt") == []

    # modo_negociacao_definido válido com modo LEILAO_DIRETO -> registrado como processado e ignorado (RN-02).
    nid_modo = "31000000-0000-4000-8000-000000000003"
    msg_modo = evento_d1(negociacaoId=nid_modo, modo="LEILAO_DIRETO")
    msg_modo["eventId"] = str(uuid4())
    msg_modo["aggregateId"] = nid_modo
    amb.entregar("modo_negociacao_definido", msg_modo)

    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{nid_modo}", "BUYER", COMP_X)) == (
        404, "NEGOCIACAO_NAO_ENCONTRADA",
    )
    assert amb.bus.publicados == []
    assert amb.bus.do_topico("negociacao.dlt") == []


def test_ca03_leilao_iniciado_invalido():
    """CA-03: leilao_iniciado inválido (cada uma das 8 alterações gera 1 DLT EVENTO_INVALIDO)."""
    amb = Ambiente("12:00:00")
    nid_invalido = "31000000-0000-4000-8000-000000000004"
    base = evento_l1(negociacaoId=nid_invalido)
    base["aggregateId"] = nid_invalido

    itens_base = base["payload"]["itens"]
    item_participacao_invalida = [
        {**copy.deepcopy(itens_base[0]), "participacao": D("1.5")},
        copy.deepcopy(itens_base[1]),
    ]
    itens_fornecimento_repetido = [
        copy.deepcopy(itens_base[0]),
        {**copy.deepcopy(itens_base[1]), "fornecimentoId": OF_A},
    ]

    alteracoes = [
        ("envelope", "source", "negociacao-service"),
        ("payload", "itens", []),
        ("payload", "encerraEm", base["payload"]["iniciaEm"]),
        ("payload", "precoInicial", D("2550.001")),
        ("payload", "itens", item_participacao_invalida),
        ("payload", "itens", itens_fornecimento_repetido),
        ("payload", "incrementoMinimo", AUSENTE),
        ("payload", "incrementoMinimoPercentual", AUSENTE),
    ]
    for nivel, campo, valor in alteracoes:
        msg = copy.deepcopy(base)
        msg["eventId"] = str(uuid4())
        alvo = msg if nivel == "envelope" else msg["payload"]
        if valor is AUSENTE:
            del alvo[campo]
        else:
            alvo[campo] = valor
        amb.entregar("leilao_iniciado", msg)

    dlts = amb.bus.do_topico("negociacao.dlt")
    assert len(dlts) == 8
    assert all(m["payload"]["error"] == "EVENTO_INVALIDO" and m["payload"]["failedAtAttempt"] == 1 for m in dlts)
    assert amb.bus.do_topico("pedido_temporario_solicitado") == []
    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{nid_invalido}", "BUYER", COMP_X)) == (
        404, "NEGOCIACAO_NAO_ENCONTRADA",
    )


# ---------------------------------------------------------------- RF-03: registrar o resultado da reserva no leilão

def test_ca06_reserva_confirmada_no_leilao_com_outra_ainda_pendente():
    """CA-06: Reserva confirmada no leilão com outra ainda pendente."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1()
    amb.bus.publicados.clear()

    amb.confirmar_reserva(OF_A, PT_A, L1, correlation_id=CORR_L1)

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))
    assert detalhe["status"] == "AGUARDANDO_RESERVAS"
    assert [o["situacao"] for o in detalhe["ofertas"]] == ["CONFIRMADA", "PENDENTE"]
    assert amb.bus.publicados == []


def test_ca07_pedido_atualizado_que_nao_muda_nada():
    """CA-07: pedido_atualizado que não muda nada."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1()
    amb.confirmar_reserva(OF_A, PT_A, L1, correlation_id=CORR_L1)
    amb.bus.publicados.clear()

    ignorados = [
        evento_pedido_atualizado(L1, OF_B, PT_B, "PENDENTE", correlation_id=CORR_L1),
        evento_pedido_atualizado(L1, OF_B, PT_B, "ENCERRADA", correlation_id=CORR_L1),
        confirmar_reserva(OF_B, PT_B, negociacao_id="31000000-0000-4000-8000-000000000099", correlation_id=CORR_L1),
        confirmar_reserva(OF_D, PT_D, negociacao_id=L1, correlation_id=CORR_L1),
        recusar_reserva(OF_A, PT_A, negociacao_id=L1, correlation_id=CORR_L1),
    ]
    for msg in ignorados:
        amb.entregar("pedido_atualizado", msg)

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))
    assert detalhe["status"] == "AGUARDANDO_RESERVAS"
    assert [o["situacao"] for o in detalhe["ofertas"]] == ["CONFIRMADA", "PENDENTE"]
    assert amb.bus.publicados == []
    assert amb.bus.do_topico("negociacao.dlt") == []


# ---------------------------------------------------------------- RF-04: liberar os lances

def test_ca09_todas_as_reservas_confirmadas():
    """CA-09: Todas as reservas confirmadas."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1()
    amb.confirmar_reserva(OF_A, PT_A, L1, correlation_id=CORR_L1)
    amb.bus.publicados.clear()

    amb.em("12:01:00")
    amb.confirmar_reserva(OF_B, PT_B, L1, correlation_id=CORR_L1)

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))
    assert detalhe["status"] == "ABERTO"
    assert detalhe["quantidadeTotal"] == D("20.00")
    assert detalhe["precoInicial"] == D("2550.00")
    assert detalhe["incrementoMinimo"] == D("25.50")
    assert [o["participacao"] for o in detalhe["ofertas"]] == [D("0.2353"), D("0.7647")]
    assert [o["situacao"] for o in detalhe["ofertas"]] == ["CONFIRMADA", "CONFIRMADA"]
    assert detalhe["acoesPermitidas"] == ["DAR_LANCE"]
    assert amb.bus.publicados == []


def test_ca10_oferta_recusada_sai_do_lote_e_o_lote_e_recalculado():
    """CA-10: Oferta recusada sai do lote e o lote é recalculado (L1, L2 com empate de restos e L4 com piso 0.0001)."""
    amb = Ambiente("12:00:00")

    # Caso 1: L1 com OF-A CONFIRMADA e OF-B RECUSADA.
    amb.abrir_l1()
    amb.confirmar_reserva(OF_A, PT_A, L1, correlation_id=CORR_L1)
    amb.bus.publicados.clear()
    amb.recusar_reserva(OF_B, PT_B, L1, correlation_id=CORR_L1)

    detalhe_l1 = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))
    assert detalhe_l1["status"] == "ABERTO"
    assert detalhe_l1["quantidadeTotal"] == D("5.00")
    assert detalhe_l1["precoInicial"] == D("600.00")
    assert detalhe_l1["incrementoMinimo"] == D("6.00")
    assert detalhe_l1["ofertas"][0]["fornecimentoId"] == OF_A
    assert detalhe_l1["ofertas"][0]["participacao"] == D("1.0000")
    assert detalhe_l1["ofertas"][0]["situacao"] == "CONFIRMADA"
    assert detalhe_l1["ofertas"][1]["fornecimentoId"] == OF_B
    assert detalhe_l1["ofertas"][1]["situacao"] == "RECUSADA"
    assert amb.bus.publicados == []

    # Caso 2: L2 com empate de restos (0.0001 fica com a menor ordem entre as confirmadas: OF-B).
    amb.abrir_leilao(
        2,
        itens=[
            item_leilao(OF_A, "2.00", "100.00", "200.00", "0.4000"),
            item_leilao(OF_B, "1.00", "100.00", "100.00", "0.2000"),
            item_leilao(OF_D, "1.00", "100.00", "100.00", "0.2000"),
            item_leilao(OF_E, "1.00", "100.00", "100.00", "0.2000"),
        ],
        preco_inicial="500.00",
        incremento_minimo="5.00",
    )
    amb.bus.publicados.clear()
    amb.confirmar_reserva(OF_B, PT_B, L2, correlation_id=CORR_L1)
    amb.confirmar_reserva(OF_D, PT_D, L2, correlation_id=CORR_L1)
    amb.confirmar_reserva(OF_E, "32000000-0000-4000-8000-000000000004", L2, correlation_id=CORR_L1)
    amb.recusar_reserva(OF_A, PT_A, L2, correlation_id=CORR_L1)

    detalhe_l2 = corpo(amb.get(f"/api/v1/negociacoes/{L2}", "BUYER", COMP_X))
    assert detalhe_l2["status"] == "ABERTO"
    assert detalhe_l2["quantidadeTotal"] == D("3.00")
    assert detalhe_l2["precoInicial"] == D("300.00")
    assert detalhe_l2["incrementoMinimo"] == D("3.00")
    ofertas_l2 = {o["fornecimentoId"]: o for o in detalhe_l2["ofertas"]}
    assert ofertas_l2[OF_A]["situacao"] == "RECUSADA"
    assert (ofertas_l2[OF_B]["participacao"], ofertas_l2[OF_B]["situacao"]) == (D("0.3334"), "CONFIRMADA")
    assert (ofertas_l2[OF_D]["participacao"], ofertas_l2[OF_D]["situacao"]) == (D("0.3333"), "CONFIRMADA")
    assert (ofertas_l2[OF_E]["participacao"], ofertas_l2[OF_E]["situacao"]) == (D("0.3333"), "CONFIRMADA")
    assert amb.bus.publicados == []

    # Caso 3: L4 com piso de 0.0001 para OF-A e diferença subtraída da maior participação (OF-B -> 0.9999).
    amb.abrir_leilao(
        4,
        itens=[
            item_leilao(OF_A, "1.00", "0.01", "0.01", "0.0001"),
            item_leilao(OF_B, "1.00", "1000.00", "1000.00", "0.9989"),
            item_leilao(OF_D, "1.00", "1.00", "1.00", "0.0010"),
        ],
        preco_inicial="1001.01",
        incremento_minimo="10.01",
        incrementoMinimoPercentual=D("1.00"),
    )
    amb.bus.publicados.clear()
    amb.confirmar_reserva(OF_A, PT_A, L4, correlation_id=CORR_L1)
    amb.confirmar_reserva(OF_B, PT_B, L4, correlation_id=CORR_L1)
    amb.recusar_reserva(OF_D, PT_D, L4, correlation_id=CORR_L1)

    detalhe_l4 = corpo(amb.get(f"/api/v1/negociacoes/{L4}", "BUYER", COMP_X))
    assert detalhe_l4["status"] == "ABERTO"
    assert detalhe_l4["quantidadeTotal"] == D("2.00")
    assert detalhe_l4["precoInicial"] == D("1000.01")
    assert detalhe_l4["incrementoMinimo"] == D("10.00")
    ofertas_l4 = {o["fornecimentoId"]: o for o in detalhe_l4["ofertas"]}
    assert (ofertas_l4[OF_A]["participacao"], ofertas_l4[OF_A]["situacao"]) == (D("0.0001"), "CONFIRMADA")
    assert (ofertas_l4[OF_B]["participacao"], ofertas_l4[OF_B]["situacao"]) == (D("0.9999"), "CONFIRMADA")
    assert ofertas_l4[OF_D]["situacao"] == "RECUSADA"
    assert amb.bus.publicados == []


def test_ca11_nenhuma_reserva_confirmada_leilao_deserto():
    """CA-11: Nenhuma reserva confirmada (o lance às 12:30 entra com a API-07)."""
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
    assert detalhe["acoesPermitidas"] == []
    assert [o["situacao"] for o in detalhe["ofertas"]] == ["RECUSADA", "RECUSADA"]

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

    amb.em("15:00:00")
    assert executar_prazos(amb.app) == 0
    assert len(amb.bus.de("negociacao_fechada")) == 1


# ---------------------------------------------------------------- RF-11 e RF-12: consultas do leilão

def test_ca34_detalhe_do_leilao_sem_lances_e_acoes_permitidas():
    """CA-34 (parte do leilão, sem lances): Detalhe do leilão e acoesPermitidas conforme escopo, empresa e prazo."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1()

    # Enquanto AGUARDANDO_RESERVAS, ninguém pode dar lance.
    amb.em("12:00:30")
    detalhe_aguardando = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))
    assert detalhe_aguardando["status"] == "AGUARDANDO_RESERVAS"
    assert detalhe_aguardando["melhorLance"] is None
    assert detalhe_aguardando["totalLances"] == 0
    assert detalhe_aguardando["agora"] == "2026-11-20T12:00:30.000Z"
    assert detalhe_aguardando["acoesPermitidas"] == []

    # Libera os lances (ABERTO) com agora < dataFim.
    amb.em("12:01:00")
    amb.confirmar_reserva(OF_A, PT_A, L1, correlation_id=CORR_L1)
    amb.confirmar_reserva(OF_B, PT_B, L1, correlation_id=CORR_L1)
    amb.em("13:20:00")

    detalhe_comprador = corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))
    assert detalhe_comprador["status"] == "ABERTO"
    assert detalhe_comprador["melhorLance"] is None
    assert detalhe_comprador["totalLances"] == 0
    assert detalhe_comprador["compradorId"] is None
    assert detalhe_comprador["agora"] == "2026-11-20T13:20:00.000Z"
    assert detalhe_comprador["acoesPermitidas"] == ["DAR_LANCE"]

    # Fornecedor do leilão (mesmo com escopo BUYER) e qualquer token SUPPLIER recebem acoesPermitidas [].
    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", FORN_A))["acoesPermitidas"] == []
    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", FORN_B))["acoesPermitidas"] == []
    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "SUPPLIER", FORN_A))["acoesPermitidas"] == []
    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "SUPPLIER", FORN_C))["acoesPermitidas"] == []

    # Quando agora >= dataFim, mesmo ainda em status ABERTO, acoesPermitidas é [].
    amb.em("15:00:00")
    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))["acoesPermitidas"] == []
    amb.em("15:05:00")
    assert corpo(amb.get(f"/api/v1/negociacoes/{L1}", "BUYER", COMP_X))["acoesPermitidas"] == []


def test_ca35_listagem_completa_com_l1_e_d1():
    """CA-35: Listagem com visibilidade e paginação contendo L1 e D1."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1()
    amb.abrir_d1()

    item_d1 = {
        "negociacaoId": D1,
        "modo": "VENDA_DIRETA",
        "status": "ABERTO",
        "produtoId": P,
        "dataInicio": None,
        "dataFim": None,
        "expiraEm": "2026-11-22T12:00:00.000Z",
        "criadoEm": "2026-11-20T12:00:00.000Z",
    }
    item_l1 = {
        "negociacaoId": L1,
        "modo": "LEILAO_DIRETO",
        "status": "AGUARDANDO_RESERVAS",
        "produtoId": P,
        "dataInicio": "2026-11-20T12:00:00.000Z",
        "dataFim": "2026-11-20T15:00:00.000Z",
        "expiraEm": None,
        "criadoEm": "2026-11-20T12:00:00.000Z",
    }

    # Mesmo criadoEm: desempate por negociacaoId decrescente (D1 antes de L1).
    assert corpo(amb.get("/api/v1/negociacoes", "BUYER", COMP_X)) == {
        "items": [item_d1, item_l1],
        "page": 1,
        "size": 10,
        "total": 2,
    }

    # COMP-Y não participa de D1, mas vê o leilão L1.
    assert corpo(amb.get("/api/v1/negociacoes", "BUYER", COMP_Y)) == {
        "items": [item_l1],
        "page": 1,
        "size": 10,
        "total": 1,
    }
    assert corpo(amb.get("/api/v1/negociacoes?modo=VENDA_DIRETA", "BUYER", COMP_Y)) == {
        "items": [],
        "page": 1,
        "size": 10,
        "total": 0,
    }

    for query in ("size=101", "size=0", "page=0", "page=a", "status=CANCELADO"):
        assert codigo_erro(amb.get(f"/api/v1/negociacoes?{query}", "BUYER", COMP_X)) == (422, "VALIDACAO_INVALIDA")


def test_ca38_propostas_em_leilao_aberto_pelo_handler():
    """CA-38 (parte de L1): GET /api/v1/negociacoes/L1/propostas em L1 aberto pelo handler responde 409 MODO_INCOMPATIVEL."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1()

    resposta = amb.get(f"/api/v1/negociacoes/{L1}/propostas", "BUYER", COMP_X)
    assert codigo_erro(resposta) == (409, "MODO_INCOMPATIVEL")


# ---------------------------------------------------------------- RNF-03: envelope, chave e schema

def test_ca53_envelope_chave_e_schema_dos_eventos_do_leilao():
    """CA-53 (parte do leilão): pedido_temporario_solicitado (tipo LEILAO) e negociacao_fechada (DESERTO) válidos contra os schemas."""
    amb = Ambiente("12:00:00")
    amb.abrir_l1()
    amb.em("12:01:00")
    amb.recusar_reserva(OF_A, PT_A, L1, correlation_id=CORR_L1)
    amb.em("12:02:00")
    amb.recusar_reserva(OF_B, PT_B, L1, correlation_id=CORR_L1)

    tempos_esperados = [
        ("pedido_temporario_solicitado", formato_json(t("12:00:00"))),
        ("pedido_temporario_solicitado", formato_json(t("12:00:00"))),
        ("negociacao_fechada", formato_json(t("12:02:00"))),
    ]
    assert len(amb.bus.publicados) == 3
    event_ids = set()
    for (topico, chave, mensagem), (topico_esperado, instante_esperado) in zip(amb.bus.publicados, tempos_esperados):
        assert topico == topico_esperado
        assert chave == mensagem["aggregateId"] == mensagem["payload"]["negociacaoId"] == L1
        assert mensagem["eventType"] == topico
        assert mensagem["eventVersion"] == "1.0"
        assert mensagem["source"] == "negociacao-service"
        assert mensagem["timestamp"] == instante_esperado
        assert mensagem["payload"]["tipo"] == "LEILAO"
        assert UUID(mensagem["eventId"]).version == 4
        event_ids.add(mensagem["eventId"])
        validar_schema(topico, mensagem)
    assert len(event_ids) == 3
    assert amb.bus.publicados[2][2]["payload"]["status"] == "DESERTO"
