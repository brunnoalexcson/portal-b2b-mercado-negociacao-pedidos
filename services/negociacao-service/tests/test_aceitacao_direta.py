"""Suíte de aceitação da negociação direta (parte 2 da ESP-NEG-001 v1.4) sobre as portas do §5.1."""
import copy
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.http import ERROS
from app.jsonio import formato_json, loads
from app.portas import RepositorioIndisponivel
from tests.cenarios import (
    AUSENTE, COMP_X, COMP_Y, CONTRATOS, CORR_D1, D1, FORN_A, FORN_B, FORN_C, L1, OF_B, OF_D, P, PT_A, PT_D,
    Ambiente, confirmar_reserva, evento_d1, evento_pedido_atualizado, exemplo, recusar_reserva, t, token,
    validar_erro_rfc7807, validar_schema,
)
from tests.dubles import OUTRA_CHAVE_PRIVADA

D = Decimal


def corpo(resposta):
    return loads(resposta.text)


def codigo_erro(resposta) -> tuple[int, str]:
    dados = validar_erro_rfc7807(resposta)
    return resposta.status_code, dados["codigo"]


# ---------------------------------------------------------------- RF-02: abrir a negociação direta

def test_ca04_negociacao_direta_aberta_pelo_mercado():
    """CA-04: Negociação direta aberta pelo Mercado."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()

    resposta = amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X)
    assert resposta.status_code == 200
    assert corpo(resposta) == {
        "negociacaoId": D1,
        "modo": "VENDA_DIRETA",
        "status": "ABERTO",
        "produtoId": P,
        "fornecimentoId": OF_B,
        "fornecedorId": FORN_B,
        "compradorId": COMP_X,
        "quantidade": D("10.00"),
        "precoOferta": D("130.00"),
        "precoAtual": D("130.00"),
        "vez": "BUYER",
        "compraImediata": False,
        "expiraEm": "2026-11-22T12:00:00.000Z",
        "reservaSituacao": "PENDENTE",
        "totalPropostas": 1,
        "criadoEm": "2026-11-20T12:00:00.000Z",
        "encerradoEm": None,
        "agora": "2026-11-20T12:00:00.000Z",
        "acoesPermitidas": ["PROPOR", "RECUSAR"],
    }

    assert len(amb.bus.publicados) == 1
    [(chave, msg)] = amb.bus.de("pedido_temporario_solicitado")
    assert chave == D1
    assert msg["correlationId"] == CORR_D1
    assert msg["payload"] == {
        "negociacaoId": D1,
        "tipo": "VENDA_DIRETA",
        "fornecimentoId": OF_B,
        "fornecedorId": FORN_B,
        "produtoId": P,
        "quantidade": D("10.00"),
        "precoUnitario": D("130.00"),
    }

    propostas = corpo(amb.get(f"/api/v1/negociacoes/{D1}/propostas", "BUYER", COMP_X))
    assert propostas["total"] == 1
    [item] = propostas["items"]
    assert UUID(item["propostaId"]).version == 4
    assert item == {
        "propostaId": item["propostaId"],
        "negociacaoId": D1,
        "autor": "SUPPLIER",
        "precoUnitario": D("130.00"),
        "sequencia": 1,
        "criadoEm": "2026-11-20T12:00:00.000Z",
    }

    detalhe_forn = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "SUPPLIER", FORN_B))
    assert detalhe_forn["acoesPermitidas"] == ["RECUSAR"]


def test_ca05_negociacao_direta_invalida_ou_repetida():
    """CA-05: Negociação direta inválida ou repetida."""
    amb = Ambiente("12:00:00")
    nid_invalida = "31000000-0000-4000-8000-000000000005"
    base = evento_d1(negociacaoId=nid_invalida)
    base["aggregateId"] = nid_invalida

    oferta_dupla = copy.deepcopy(base["payload"]["ofertas"][0])
    oferta_texto = {**copy.deepcopy(base["payload"]["ofertas"][0]), "quantidade": "10.00"}
    alteracoes = [
        {"demandas": []},
        {"ofertas": [oferta_dupla, copy.deepcopy(oferta_dupla)]},
        {"ofertas": [oferta_texto]},
        {"produtoId": AUSENTE},
    ]
    for mudanca in alteracoes:
        msg = copy.deepcopy(base)
        msg["eventId"] = str(uuid4())
        for campo, valor in mudanca.items():
            if valor is AUSENTE:
                del msg["payload"][campo]
            else:
                msg["payload"][campo] = valor
        amb.entregar("modo_negociacao_definido", msg)

    dlts = amb.bus.do_topico("negociacao.dlt")
    assert len(dlts) == 4
    assert all(m["payload"]["error"] == "EVENTO_INVALIDO" and m["payload"]["failedAtAttempt"] == 1 for m in dlts)
    assert amb.bus.do_topico("pedido_temporario_solicitado") == []
    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{nid_invalida}", "BUYER", COMP_X)) == (
        404, "NEGOCIACAO_NAO_ENCONTRADA",
    )

    amb.abrir_d1()
    assert len(amb.bus.do_topico("pedido_temporario_solicitado")) == 1

    repetido = evento_d1()
    repetido["eventId"] = str(uuid4())
    amb.entregar("modo_negociacao_definido", repetido)

    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["totalPropostas"] == 1
    assert len(amb.bus.do_topico("pedido_temporario_solicitado")) == 1
    assert len(amb.bus.do_topico("negociacao.dlt")) == 4


# ---------------------------------------------------------------- RF-03: registrar o resultado da reserva

def test_ca07_pedido_atualizado_que_nao_muda_nada_na_direta():
    """CA-07 (adaptado para D1): pedido_atualizado que não muda nada."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.bus.publicados.clear()

    ignorados = [
        evento_pedido_atualizado(D1, OF_B, PT_D, "PENDENTE"),
        evento_pedido_atualizado(D1, OF_B, PT_D, "ENCERRADA"),
        confirmar_reserva(OF_B, PT_D, negociacao_id="31000000-0000-4000-8000-000000000099"),
        confirmar_reserva(OF_D, PT_D, negociacao_id=D1),
    ]
    for msg in ignorados:
        amb.entregar("pedido_atualizado", msg)

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))
    assert (detalhe["status"], detalhe["reservaSituacao"]) == ("ABERTO", "PENDENTE")
    assert amb.bus.publicados == []

    # Depois de confirmada, uma recusa tardia para a mesma oferta também é ignorada (RN-04).
    amb.confirmar_reserva(OF_B, PT_D, D1)
    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["reservaSituacao"] == "CONFIRMADA"
    amb.recusar_reserva(OF_B, PT_D, D1)

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))
    assert (detalhe["status"], detalhe["reservaSituacao"]) == ("ABERTO", "CONFIRMADA")
    assert amb.bus.publicados == []


def test_ca08_reserva_recusada_na_negociacao_direta():
    """CA-08: Reserva recusada na negociação direta."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.em("12:05:00")

    amb.recusar_reserva(OF_B, PT_D, D1, correlation_id="teste-neg-08")

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))
    assert detalhe["status"] == "RECUSADO"
    assert detalhe["reservaSituacao"] == "RECUSADA"
    assert detalhe["encerradoEm"] == "2026-11-20T12:05:00.000Z"
    assert detalhe["acoesPermitidas"] == []

    [(chave, fechada)] = amb.bus.de("negociacao_fechada")
    assert chave == D1
    assert fechada["correlationId"] == "teste-neg-08"
    assert fechada["payload"] == {
        "negociacaoId": D1,
        "tipo": "VENDA_DIRETA",
        "status": "RECUSADO",
        "compradorId": None,
        "demandaId": None,
        "produtoId": P,
        "alocacoes": [],
    }


def test_ca62_compra_imediata_fecha_sozinha_quando_a_reserva_confirma():
    """CA-62: Compra imediata fecha sozinha quando a reserva confirma."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1(compraImediata=True)

    detalhe_comp = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))
    assert (detalhe_comp["status"], detalhe_comp["compraImediata"], detalhe_comp["acoesPermitidas"]) == (
        "ABERTO", True, [],
    )
    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}", "SUPPLIER", FORN_B))["acoesPermitidas"] == []

    amb.em("12:00:05")
    amb.confirmar_reserva(OF_B, PT_D, D1, correlation_id="teste-neg-62")

    detalhe_final = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))
    assert detalhe_final["status"] == "ENCERRADO"
    assert detalhe_final["reservaSituacao"] == "CONFIRMADA"
    assert detalhe_final["encerradoEm"] == "2026-11-20T12:00:05.000Z"
    assert detalhe_final["acoesPermitidas"] == []

    [(chave, fechada)] = amb.bus.de("negociacao_fechada")
    assert chave == D1
    assert fechada["correlationId"] == "teste-neg-62"
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
                "valorUnitario": D("130.00"),
                "valorTotal": D("1300.00"),
            }
        ],
    }

    # Compra imediata com reserva recusada fecha como RECUSADO e alocacoes [].
    amb_recusa = Ambiente("12:00:00")
    amb_recusa.abrir_d1(compraImediata=True)
    amb_recusa.recusar_reserva(OF_B, PT_D, D1)
    assert corpo(amb_recusa.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["status"] == "RECUSADO"
    [(_, fechada_recusa)] = amb_recusa.bus.de("negociacao_fechada")
    assert (fechada_recusa["payload"]["status"], fechada_recusa["payload"]["alocacoes"]) == ("RECUSADO", [])

    # compraImediata como string vai para a DLT e não cria a negociação.
    nid_invalido = "31000000-0000-4000-8000-000000000006"
    invalido = evento_d1(negociacaoId=nid_invalido, compraImediata="true")
    invalido["eventId"] = str(uuid4())
    invalido["aggregateId"] = nid_invalido
    amb.entregar("modo_negociacao_definido", invalido)
    [dlt] = amb.bus.do_topico("negociacao.dlt")
    assert dlt["payload"]["error"] == "EVENTO_INVALIDO"
    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{nid_invalido}", "BUYER", COMP_X)) == (
        404, "NEGOCIACAO_NAO_ENCONTRADA",
    )


# ---------------------------------------------------------------- RF-11: consultar negociações

def test_ca34_detalhe_da_direta_com_agora_e_acoes():
    """CA-34 (parte da direta): Detalhe reflete `agora` e `acoesPermitidas` conforme a vez e a reserva."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.em("13:20:00")

    detalhe_comp = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))
    assert detalhe_comp["agora"] == "2026-11-20T13:20:00.000Z"
    assert detalhe_comp["acoesPermitidas"] == ["PROPOR", "RECUSAR"]

    detalhe_forn = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "SUPPLIER", FORN_B))
    assert detalhe_forn["agora"] == "2026-11-20T13:20:00.000Z"
    assert detalhe_forn["acoesPermitidas"] == ["RECUSAR"]

    amb.confirmar_reserva(OF_B, PT_D, D1)
    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["acoesPermitidas"] == [
        "PROPOR", "ACEITAR", "RECUSAR",
    ]


def test_ca35_listagem_com_visibilidade_filtro_e_paginacao():
    """CA-35 (só com D1): Listagem com visibilidade, filtro modo e paginação."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()

    lista = corpo(amb.get("/api/v1/negociacoes", "BUYER", COMP_X))
    assert lista == {
        "items": [
            {
                "negociacaoId": D1,
                "modo": "VENDA_DIRETA",
                "status": "ABERTO",
                "produtoId": P,
                "dataInicio": None,
                "dataFim": None,
                "expiraEm": "2026-11-22T12:00:00.000Z",
                "criadoEm": "2026-11-20T12:00:00.000Z",
            }
        ],
        "page": 1,
        "size": 10,
        "total": 1,
    }
    assert corpo(amb.get("/api/v1/negociacoes", "SUPPLIER", FORN_B))["total"] == 1

    # COMP-Y não participa de D1: não vê nenhuma negociação direta.
    assert corpo(amb.get("/api/v1/negociacoes", "BUYER", COMP_Y)) == {
        "items": [], "page": 1, "size": 10, "total": 0,
    }
    assert corpo(amb.get("/api/v1/negociacoes?modo=VENDA_DIRETA", "BUYER", COMP_Y)) == {
        "items": [], "page": 1, "size": 10, "total": 0,
    }
    assert corpo(amb.get("/api/v1/negociacoes?modo=VENDA_DIRETA", "BUYER", COMP_X))["total"] == 1
    assert corpo(amb.get("/api/v1/negociacoes?modo=LEILAO_DIRETO", "BUYER", COMP_X)) == {
        "items": [], "page": 1, "size": 10, "total": 0,
    }

    for query in ("size=101", "size=0", "page=0", "page=a", "status=CANCELADO", "modo=LEILAO_REVERSO"):
        assert codigo_erro(amb.get(f"/api/v1/negociacoes?{query}", "BUYER", COMP_X)) == (422, "VALIDACAO_INVALIDA")


def test_ca36_detalhe_invisivel_ou_inexistente():
    """CA-36: Detalhe invisível ou inexistente."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()

    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_Y)) == (404, "NEGOCIACAO_NAO_ENCONTRADA")
    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{D1}", "SUPPLIER", FORN_A)) == (404, "NEGOCIACAO_NAO_ENCONTRADA")

    # Simula a vez do fornecedor com reserva confirmada para verificar ["PROPOR", "ACEITAR", "RECUSAR"].
    amb.confirmar_reserva(OF_B, PT_D, D1)
    with amb.repo.transacao():
        neg = amb.repo.obter_negociacao(D1)
        neg["vez"] = "SUPPLIER"
        amb.repo.atualizar_negociacao(neg)

    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "SUPPLIER", FORN_B))
    assert detalhe["acoesPermitidas"] == ["PROPOR", "ACEITAR", "RECUSAR"]

    assert codigo_erro(amb.get("/api/v1/negociacoes/123", "BUYER", COMP_X)) == (422, "VALIDACAO_INVALIDA")


# ---------------------------------------------------------------- RF-12: listar propostas

def _gravar_segunda_proposta(amb: Ambiente) -> str:
    proposta_id = "36000000-0000-4000-8000-000000000102"
    with amb.repo.transacao():
        amb.repo.inserir_proposta({
            "propostaId": proposta_id,
            "negociacaoId": D1,
            "autor": "BUYER",
            "empresaId": COMP_X,
            "precoUnitario": D("125.00"),
            "sequencia": 2,
            "criadoEm": t("2026-11-21T12:00:00.000Z"),
        })
    return proposta_id


def test_ca37_paginacao_das_propostas_de_d1():
    """CA-37 (parte das propostas de D1): Paginação em GET /api/v1/negociacoes/D1/propostas."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    p2 = _gravar_segunda_proposta(amb)

    pagina_2 = corpo(amb.get(f"/api/v1/negociacoes/{D1}/propostas?page=2&size=1", "BUYER", COMP_X))
    assert pagina_2 == {
        "items": [
            {
                "propostaId": p2,
                "negociacaoId": D1,
                "autor": "BUYER",
                "precoUnitario": D("125.00"),
                "sequencia": 2,
                "criadoEm": "2026-11-21T12:00:00.000Z",
            }
        ],
        "page": 2,
        "size": 1,
        "total": 2,
    }


def test_ca38_propostas_e_visibilidade_na_direta():
    """CA-38 (parte das propostas de D1): Listagem de propostas em ordem de sequência e erros de visibilidade/modo."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    p2 = _gravar_segunda_proposta(amb)

    resposta = amb.get(f"/api/v1/negociacoes/{D1}/propostas", "SUPPLIER", FORN_B)
    assert resposta.status_code == 200
    dados = corpo(resposta)
    p1 = dados["items"][0]["propostaId"]
    assert dados == {
        "items": [
            {
                "propostaId": p1,
                "negociacaoId": D1,
                "autor": "SUPPLIER",
                "precoUnitario": D("130.00"),
                "sequencia": 1,
                "criadoEm": "2026-11-20T12:00:00.000Z",
            },
            {
                "propostaId": p2,
                "negociacaoId": D1,
                "autor": "BUYER",
                "precoUnitario": D("125.00"),
                "sequencia": 2,
                "criadoEm": "2026-11-21T12:00:00.000Z",
            },
        ],
        "page": 1,
        "size": 10,
        "total": 2,
    }

    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{D1}/propostas", "BUYER", COMP_Y)) == (
        404, "NEGOCIACAO_NAO_ENCONTRADA",
    )

    # Se a negociação for LEILAO_DIRETO, a rota de propostas responde 409 MODO_INCOMPATIVEL.
    with amb.repo.transacao():
        leilao = copy.deepcopy(amb.repo.obter_negociacao(D1))
        leilao.update(negociacaoId=L1, modo="LEILAO_DIRETO")
        amb.repo.inserir_negociacao(leilao)
    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{L1}/propostas", "BUYER", COMP_X)) == (409, "MODO_INCOMPATIVEL")


# ---------------------------------------------------------------- RF-13: autenticar e autorizar

def test_ca39_token_invalido():
    """CA-39: Token inválido."""
    amb = Ambiente("12:00:00")
    agora = amb.relogio.now()

    sem_auth = amb.get("/api/v1/negociacoes", sem_auth=True)
    assert codigo_erro(sem_auth) == (401, "TOKEN_INVALIDO")
    assert sem_auth.headers["www-authenticate"] == "Bearer"

    vazio = amb.get("/api/v1/negociacoes", sem_auth=True, headers={"Authorization": "Bearer "})
    assert codigo_erro(vazio) == (401, "TOKEN_INVALIDO")
    assert vazio.headers["www-authenticate"] == "Bearer"

    tokens_invalidos = [
        token("BUYER", COMP_X, agora=agora, chave=OUTRA_CHAVE_PRIVADA),
        token("BUYER", COMP_X, agora=agora, chave="segredo-hs256-com-32-bytes-123456", algoritmo="HS256"),
        token("BUYER", COMP_X, agora=agora, kid="desconhecido"),
        token("BUYER", COMP_X, agora=agora, iss="outro"),
        token("BUYER", COMP_X, agora=agora, aud="outra"),
        token("BUYER", COMP_X, agora=agora, sub=AUSENTE),
        token("BUYER", COMP_X, agora=agora, exp=int(agora.timestamp())),
    ]
    for jwt_invalido in tokens_invalidos:
        resposta = amb.get("/api/v1/negociacoes", jwt_token=jwt_invalido)
        assert codigo_erro(resposta) == (401, "TOKEN_INVALIDO")
        assert resposta.headers["www-authenticate"] == "Bearer"


def test_ca40_token_sem_contexto_de_empresa():
    """CA-40: Token sem contexto de empresa."""
    amb = Ambiente("12:00:00")
    casos = [
        amb.token("BUYER", None, view_mode="NONE", tenant_id=None),
        amb.token("BUYER", None, view_mode="MACRO_GROUP", tenant_id=None),
        amb.token(None, COMP_X, view_mode="SINGLE", active_scope=None),
    ]
    for jwt_sem_contexto in casos:
        assert codigo_erro(amb.get("/api/v1/negociacoes", jwt_token=jwt_sem_contexto)) == (
            403, "CONTEXTO_NAO_SELECIONADO",
        )


def test_ca41_escopos_sem_acesso():
    """CA-41 (primeira parte): Escopos CARRIER e TENANT_ADMIN sem acesso."""
    amb = Ambiente("12:00:00")
    for escopo in ("CARRIER", "TENANT_ADMIN"):
        assert codigo_erro(amb.get("/api/v1/negociacoes", escopo, COMP_X)) == (403, "ACESSO_NEGADO")


def test_ca42_dependencia_indisponivel_e_falha_inesperada():
    """CA-42 (adaptado para D1): Dependência indisponível e falha inesperada."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()

    amb.chaves.falha = ConnectionError("JWKS fora do ar")
    assert codigo_erro(amb.get("/api/v1/negociacoes", "BUYER", COMP_X)) == (503, "DEPENDENCIA_INDISPONIVEL")
    amb.chaves.falha = False

    amb.repo.falhar("obter_negociacao", RepositorioIndisponivel("banco fora"), vezes=1)
    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X)) == (503, "DEPENDENCIA_INDISPONIVEL")

    amb.repo.falhar("obter_negociacao", RuntimeError("erro inesperado"), vezes=1)
    assert codigo_erro(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X)) == (500, "ERRO_INTERNO")


def test_ca43_correlacao():
    """CA-43: Correlação em sucesso e erro."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()

    resposta = amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X, headers={"X-Correlation-Id": "teste-neg-57"})
    assert resposta.status_code == 200
    assert resposta.headers["x-correlation-id"] == "teste-neg-57"

    for invalido in (None, {"X-Correlation-Id": "a b"}, {"X-Correlation-Id": "a" * 129}):
        r = amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X, headers=invalido)
        assert UUID(r.headers["x-correlation-id"]).version == 4

    erro = amb.get("/api/v1/negociacoes/123", "BUYER", COMP_X, headers={"X-Correlation-Id": "teste-neg-57"})
    assert codigo_erro(erro) == (422, "VALIDACAO_INVALIDA")
    dados = corpo(erro)
    assert dados["correlationId"] == "teste-neg-57"
    assert dados["instance"] == "/api/v1/negociacoes/123"


# ---------------------------------------------------------------- RF-15: consumir mensagens com idempotência e DLT

def test_ca46_cinco_entregas_do_mesmo_evento():
    """CA-46: Cinco entregas do mesmo evento."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    event_id = "38000000-0000-4000-8000-000000000060"
    recusa = recusar_reserva(OF_B, PT_D, D1, event_id=event_id)

    for _ in range(5):
        amb.entregar("pedido_atualizado", recusa)

    assert corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["status"] == "RECUSADO"
    assert len(amb.bus.de("negociacao_fechada")) == 1
    assert amb.repo.evento_processado("pedidos-service", event_id) is True


def test_ca47_mensagem_invalida_vai_para_a_dlt():
    """CA-47: Mensagem inválida vai para a DLT."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()

    sem_payload = exemplo("negociacao.dlt")["payload"]["originalEvent"]
    amb.entregar("pedido_atualizado", sem_payload)

    [(chave, dlt)] = amb.bus.de("negociacao.dlt")
    assert chave == PT_A
    assert dlt["payload"]["topicoOrigem"] == "pedido_atualizado"
    assert dlt["payload"]["originalEvent"] == sem_payload
    assert dlt["payload"]["error"] == "EVENTO_INVALIDO"
    assert dlt["payload"]["detalhe"]
    assert dlt["payload"]["stackTrace"] == ""
    assert dlt["payload"]["failedAtAttempt"] == 1

    # Mensagem inválida não é deduplicada: reentrega gera outra publicação na DLT (RN-19, RN-20).
    amb.entregar("pedido_atualizado", sem_payload)
    assert len(amb.bus.de("negociacao.dlt")) == 2

    base = confirmar_reserva(OF_B, PT_D, D1)
    variacoes = []
    for campo, valor in (("eventType", "pedido_criado"), ("eventVersion", "2.0"), ("source", "outro-service")):
        m = copy.deepcopy(base)
        m["eventId"] = str(uuid4())
        m[campo] = valor
        variacoes.append(m)
    for campo_ausente in ("status", "reservaSituacao"):
        m = copy.deepcopy(base)
        m["eventId"] = str(uuid4())
        del m["payload"][campo_ausente]
        variacoes.append(m)

    for m in variacoes:
        amb.entregar("pedido_atualizado", m)

    assert len(amb.bus.de("negociacao.dlt")) == 7
    detalhe = corpo(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))
    assert (detalhe["status"], detalhe["reservaSituacao"]) == ("ABERTO", "PENDENTE")


def test_ca48_chave_da_dlt_sem_aggregate_id():
    """CA-48: Chave da DLT sem aggregateId."""
    amb = Ambiente("12:00:00")

    amb.entregar("modo_negociacao_definido", {"eventId": "E-62", "payload": {}})
    amb.entregar("modo_negociacao_definido", {"payload": {}})

    chaves = [chave for chave, _ in amb.bus.de("negociacao.dlt")]
    assert chaves == ["E-62", "desconhecido"]


def test_ca49_falha_transitoria_falha_persistente_e_banco_indisponivel():
    """CA-49 (adaptado para D1): Falha transitória, falha persistente e banco indisponível."""
    # 1. Falha transitória nas 2 primeiras transações e sucesso na 3ª.
    amb1 = Ambiente("12:00:00")
    amb1.abrir_d1()
    amb1.repo.falhar_processamento(vezes=2)
    amb1.confirmar_reserva(OF_B, PT_D, D1)

    assert corpo(amb1.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["reservaSituacao"] == "CONFIRMADA"
    assert amb1.relogio.esperas == [1, 2]
    assert amb1.bus.de("negociacao.dlt") == []

    # 2. Falha persistente em todas as transações de processamento -> 3 tentativas e DLT.
    amb2 = Ambiente("12:00:00")
    amb2.abrir_d1()
    amb2.repo.falhar_processamento(vezes=None)
    amb2.confirmar_reserva(OF_B, PT_D, D1)

    assert amb2.relogio.esperas == [1, 2]
    [(chave, dlt)] = amb2.bus.de("negociacao.dlt")
    assert chave == PT_D
    assert dlt["payload"]["error"] == "PROCESSAMENTO_FALHOU"
    assert dlt["payload"]["failedAtAttempt"] == 3
    assert "ValueError" in dlt["payload"]["stackTrace"]
    assert corpo(amb2.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))["reservaSituacao"] == "PENDENTE"

    # 3. Banco indisponível em transacao() -> propaga RepositorioIndisponivel sem publicar nada.
    amb3 = Ambiente("12:00:00")
    amb3.abrir_d1()
    amb3.bus.publicados.clear()
    amb3.repo.falhar("transacao", RepositorioIndisponivel("banco fora"))
    msg = confirmar_reserva(OF_B, PT_D, D1)

    with pytest.raises(RepositorioIndisponivel):
        amb3.entregar("pedido_atualizado", msg)

    assert amb3.bus.publicados == []
    amb3.repo.limpar_falhas()
    assert amb3.repo.evento_processado("pedidos-service", msg["eventId"]) is False


# ---------------------------------------------------------------- RF-16 e RNFs: métricas, erros, schemas e isolamento

def test_ca50_metricas_depois_de_listar_negociacoes():
    """CA-50 (parte sobre /metrics): Métricas registram GET /api/v1/negociacoes com status 200."""
    amb = Ambiente("12:00:00")
    assert amb.get("/api/v1/negociacoes", "BUYER", COMP_X).status_code == 200

    metricas = amb.cliente.get("/metrics")
    assert metricas.status_code == 200
    assert metricas.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'http_requests_total{metodo="GET",rota="/api/v1/negociacoes",status="200"} 1.0' in metricas.text


def test_ca52_formato_de_erro_em_todos_os_codigos_da_parte_2():
    """CA-52: Todas as respostas 4xx e 5xx seguem application/problem+json e o catálogo do §10."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    with amb.repo.transacao():
        leilao = copy.deepcopy(amb.repo.obter_negociacao(D1))
        leilao.update(negociacaoId=L1, modo="LEILAO_DIRETO")
        amb.repo.inserir_negociacao(leilao)

    respostas = [
        amb.get("/api/v1/negociacoes", sem_auth=True),
        amb.get("/api/v1/negociacoes", jwt_token=amb.token("BUYER", None, view_mode="NONE", tenant_id=None)),
        amb.get("/api/v1/negociacoes", "CARRIER", COMP_X),
        amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_Y),
        amb.get(f"/api/v1/negociacoes/{L1}/propostas", "BUYER", COMP_X),
        amb.get("/api/v1/negociacoes/nao-e-uuid", "BUYER", COMP_X),
    ]
    amb.repo.falhar("obter_negociacao", RuntimeError("falha"), vezes=1)
    respostas.append(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))
    amb.repo.falhar("obter_negociacao", RepositorioIndisponivel("indisponível"), vezes=1)
    respostas.append(amb.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X))

    codigos = {validar_erro_rfc7807(r)["codigo"] for r in respostas}
    assert codigos == set(ERROS)


def test_ca53_envelope_chave_e_schema_dos_eventos_e_exemplos():
    """CA-53: Mensagens publicadas e arquivos .exemplo.json válidos contra os schemas Draft 2020-12."""
    amb = Ambiente("12:00:00")
    amb.abrir_d1()
    amb.em("12:05:00")
    amb.recusar_reserva(OF_B, PT_D, D1)

    d2 = "31000000-0000-4000-8000-000000000022"
    msg_d2 = evento_d1(negociacaoId=d2, compraImediata=True)
    msg_d2["eventId"] = str(uuid4())
    msg_d2["aggregateId"] = d2
    amb.em("12:10:00")
    amb.entregar("modo_negociacao_definido", msg_d2)
    amb.em("12:11:00")
    amb.confirmar_reserva(OF_B, PT_D, d2)

    tempos_esperados = [
        formato_json(t("12:00:00")),
        formato_json(t("12:05:00")),
        formato_json(t("12:10:00")),
        formato_json(t("12:11:00")),
    ]
    assert len(amb.bus.publicados) == 4
    event_ids = set()
    for (topico, chave, mensagem), instante_esperado in zip(amb.bus.publicados, tempos_esperados):
        assert topico != "negociacao.dlt"
        assert chave == mensagem["aggregateId"] == mensagem["payload"]["negociacaoId"]
        assert mensagem["eventType"] == topico
        assert mensagem["eventVersion"] == "1.0"
        assert mensagem["source"] == "negociacao-service"
        assert mensagem["timestamp"] == instante_esperado
        assert UUID(mensagem["eventId"]).version == 4
        event_ids.add(mensagem["eventId"])
        validar_schema(topico, mensagem)
    assert len(event_ids) == 4

    exemplos = sorted(CONTRATOS.glob("*.exemplo.json"))
    assert len(exemplos) == 7
    for caminho in exemplos:
        topico = caminho.name.removesuffix(".exemplo.json")
        validar_schema(topico, exemplo(topico))


def test_ca54_duas_aplicacoes_nao_compartilham_negociacoes():
    """CA-54 (segunda parte): Duas aplicações de create_app com repositórios diferentes não compartilham estado."""
    amb1 = Ambiente("12:00:00")
    amb2 = Ambiente("12:00:00")

    amb1.abrir_d1()

    assert amb1.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X).status_code == 200
    assert codigo_erro(amb2.get(f"/api/v1/negociacoes/{D1}", "BUYER", COMP_X)) == (404, "NEGOCIACAO_NAO_ENCONTRADA")
