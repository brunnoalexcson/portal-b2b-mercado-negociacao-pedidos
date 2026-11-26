"""Suíte de aceitação da ESP-PED-001 v2.2 (§8): os 39 CAs, em memória, pelas portas do §5.1."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.main import create_app, reconciliar
from app.portas import RepositorioIndisponivel
from tests.dubles import Barramento, Chaves, Relogio, Repositorio

N = "31000000-0000-4000-8000-000000000001"
PROD = "35000000-0000-4000-8000-000000000001"
F1E, F2E, F3E = (f"a1000000-0000-4000-8000-00000000000{i}" for i in (1, 2, 3))
COMP = "b2000000-0000-4000-8000-000000000002"
OUTRA = "c3000000-0000-4000-8000-000000000003"
OFERTAS = {
    1: ("34000000-0000-4000-8000-000000000001", F1E, "Rio Verde", 5.00, 120.00),
    2: ("34000000-0000-4000-8000-000000000002", F2E, "Jataí", 15.00, 125.00),
    3: ("34000000-0000-4000-8000-000000000003", F3E, "Mineiros", 10.00, 118.00),
}
PRODUTORES = {"pedido_temporario_solicitado": ("negociacao-service", "negociacaoId"),
              "negociacao_fechada": ("negociacao-service", "negociacaoId"),
              "produto_cadastrado": ("produtos-service", "produtoId"),
              "produto_atualizado": ("produtos-service", "produtoId"),
              "fornecimento_criado": ("fornecimentos-service", "fornecimentoId"),
              "estoque_atualizado": ("fornecimentos-service", "fornecimentoId"),
              "reserva_estoque_recusada": ("fornecimentos-service", "pedidoId"),
              "solicitacao_frete_criada": ("logistica-service", "solicitacaoFreteId"),
              "frete_selecionado": ("logistica-service", "pedidoId"),
              "entrega_concluida": ("logistica-service", "pedidoId"),
              "frete_indisponivel": ("logistica-service", "pedidoId")}
TOKENS = {"F1": ("usr-f1", F1E, "SUPPLIER"), "F2": ("usr-f2", F2E, "SUPPLIER"), "COMP": ("usr-comp", COMP, "BUYER"),
          "OUTRA-B": ("usr-outra-b", OUTRA, "BUYER"), "OUTRA-S": ("usr-outra-s", OUTRA, "SUPPLIER"),
          "CARRIER": ("usr-carrier", OUTRA, "CARRIER"), "ADMIN": ("usr-admin", None, None),
          "SEM-CONTEXTO": ("usr-sem", None, "BUYER")}
DESTINO = {"cidade": "Anápolis", "uf": "GO", "dataEntregaDesejada": "2026-11-27"}
CHAVE = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Ambiente:
    def __init__(self):
        self.relogio, self.bus, self.repo = Relogio(), Barramento(), Repositorio()
        self.chaves = Chaves({"k1": CHAVE.public_key()})
        self.app = create_app(self.bus, self.relogio, self.repo, self.chaves)
        self.cliente = TestClient(self.app)
        self.p = {}

    # eventos ---------------------------------------------------------
    def enviar(self, topico, payload, event_id=None, correlacao="teste-ped-01", **envelope):
        fonte, chave = PRODUTORES[topico]
        mensagem = {"eventId": event_id or str(uuid4()), "eventType": topico, "eventVersion": "1.0",
                    "timestamp": "2026-11-20T12:00:00.000Z", "source": fonte, "correlationId": correlacao,
                    "aggregateId": payload.get(chave), "payload": payload, **envelope}
        self.bus.handlers[topico](mensagem)
        return mensagem

    def produto(self, topico="produto_cadastrado", nome="Milho em grão", **env):
        return self.enviar(topico, {"produtoId": PROD, "nome": nome, "unidade": "TON", "tipoTransporte": "GRANEL",
                                    "pesoUnitarioKg": None}, **env)

    def oferta(self, i, **extra):
        forn, emp, cidade, _, _ = OFERTAS[i]
        return self.enviar("fornecimento_criado", {"fornecimentoId": forn, "fornecedorId": emp, "produtoId": PROD,
                                                   "unidade": "TON", "origem": {"cidade": cidade, "uf": "GO"},
                                                   **extra})

    def solicitar(self, i, **extra):
        forn, emp, _, qtd, preco = OFERTAS[i]
        antes = len(self.bus.de("pedido_criado"))
        m = self.enviar("pedido_temporario_solicitado", {
            "negociacaoId": N, "tipo": "LEILAO", "fornecimentoId": forn, "fornecedorId": emp, "produtoId": PROD,
            "quantidade": qtd, "precoUnitario": preco, **{k: v for k, v in extra.items() if k != "event_id"}},
            event_id=extra.get("event_id"))
        criados = self.bus.de("pedido_criado")
        if len(criados) > antes:
            self.p[i] = criados[-1][0]
        return m

    def reservar(self, i, qtd=None, **extra):
        forn, emp, _, q, _ = OFERTAS[i]
        return self.enviar("estoque_atualizado", {
            "fornecimentoId": forn, "produtoId": PROD, "fornecedorId": emp, "operacao": "RESERVA",
            "quantidadeMovimentada": qtd if qtd is not None else q, "quantidadeDisponivel": 95.0,
            "quantidadeReservada": 5.0, "pedidoId": self.p[i], "atualizadoEm": "2026-11-20T12:00:04.000Z", **extra})

    def recusar(self, i, motivo="ESTOQUE_INSUFICIENTE", **env):
        forn, _, _, q, _ = OFERTAS[i]
        return self.enviar("reserva_estoque_recusada", {
            "pedidoId": self.p[i], "fornecimentoId": forn, "quantidadeSolicitada": q, "quantidadeDisponivel": 2.0,
            "motivo": motivo, "recusadoEm": "2026-11-20T12:00:04.000Z"}, **env)

    def fechamento(self, qtd_p1=5.00):
        return {"negociacaoId": N, "tipo": "LEILAO", "status": "ENCERRADO", "compradorId": COMP, "produtoId": PROD,
                "demandaId": None,
                "alocacoes": [{"fornecimentoId": OFERTAS[1][0], "fornecedorId": F1E, "pedidoId": self.p[1],
                               "quantidade": qtd_p1, "valorUnitario": 130.00, "valorTotal": 650.00},
                              {"fornecimentoId": OFERTAS[2][0], "fornecedorId": F2E, "pedidoId": self.p[2],
                               "quantidade": 15.00, "valorUnitario": 130.00, "valorTotal": 1950.00}]}

    def fechar(self, payload=None, **env):
        return self.enviar("negociacao_fechada", payload or self.fechamento(), **env)

    def deserto(self, **env):
        return self.enviar("negociacao_fechada", {"negociacaoId": N, "tipo": "LEILAO", "status": "DESERTO",
                                                  "compradorId": None, "produtoId": PROD, "alocacoes": []}, **env)

    def solicitacao_frete(self, sol="sol-go-21", i=1):
        return self.enviar("solicitacao_frete_criada", {"solicitacaoFreteId": sol, "pedidoId": self.p[i],
                                                        "status": "ABERTA"})

    def selecionar(self, sol="sol-go-21", tentativa=1, entrega="ent-go-31", transp="tra-go-8", i=1, **extra):
        return self.enviar("frete_selecionado", {
            "pedidoId": self.p[i], "solicitacaoFreteId": sol, "entregaId": entrega, "cotacaoId": "cot-go-18",
            "transportadoraId": transp, "tentativa": tentativa, "preco": 150.00, "prazoDias": 2,
            "dataEntregaPrevista": "2026-11-23", "criterio": "ESCOLHA_COMPRADOR", "status": "FRETE_SELECIONADO",
            **extra})

    def entregar(self, entrega="ent-go-31", transp="tra-go-8", pedido=None):
        return self.enviar("entrega_concluida", {"pedidoId": pedido or self.p[1], "entregaId": entrega,
                                                 "transportadoraId": transp,
                                                 "entregueEm": "2026-11-23T16:00:00.000Z"})

    # HTTP ------------------------------------------------------------
    def token(self, nome, **claims):
        sub, tenant, escopo = TOKENS[nome]
        agora = int(self.relogio.agora.timestamp())
        dados = {"iss": "identity-service", "aud": "b2b-platform", "sub": sub, "exp": agora + 900}
        if nome == "ADMIN":
            dados["platform_role"] = "PLATFORM_ADMIN"
        else:
            dados["active_scope"] = escopo
            if tenant:
                dados["tenant_id"] = tenant
        dados.update(claims)
        return {"Authorization": "Bearer " + jwt.encode(dados, CHAVE, algorithm="RS256", headers={"kid": "k1"})}

    def get(self, caminho, quem, **kw):
        return self.cliente.get(caminho, headers=self.token(quem), **kw)

    def put_destino(self, quem, i=1, corpo=None, chave=None):
        headers = self.token(quem)
        if chave:
            headers["Idempotency-Key"] = chave
        return self.cliente.put(f"/api/v1/pedidos/{self.p[i]}/destino", json=corpo or DESTINO, headers=headers)

    def pedido(self, i):
        return self.repo.pedidos[self.p[i]]

    def dlt(self):
        return [m["payload"] for _, m in self.bus.de("pedidos.dlt")]

    def atualizados(self, i=None):
        return [m["payload"] for k, m in self.bus.de("pedido_atualizado") if i is None or k == self.p[i]]

    # cenários (§8.0) ---------------------------------------------------
    def B(self):
        self.produto()
        for i in (1, 2, 3):
            self.oferta(i)
        return self

    def T(self):
        self.B()
        for i in (1, 2, 3):
            self.solicitar(i)
        return self

    def R(self):
        self.T()
        for i in (1, 2, 3):
            self.reservar(i)
        return self

    def V(self):
        self.R()
        self.fechar()
        return self

    def A(self):
        self.V()
        assert self.put_destino("COMP").status_code == 200
        self.solicitacao_frete()
        return self


@pytest.fixture
def amb():
    return Ambiente()


# RF-01 -----------------------------------------------------------------

def test_ca01_projecoes_alimentam_o_snapshot(amb):
    amb.produto()
    amb.oferta(1)
    amb.solicitar(1)
    (_, criado), = amb.bus.de("pedido_criado")
    carga = criado["payload"]
    assert (carga["produtoNome"], carga["unidade"], carga["tipoTransporte"]) == ("Milho em grão", "TON", "GRANEL")
    assert carga["pesoKg"] == 5000 and carga["origem"] == {"cidade": "Rio Verde", "uf": "GO"}


def test_ca02_produto_com_mesmo_timestamp_e_conflito(amb):
    amb.B()
    amb.produto("produto_atualizado", nome="Milho tipo 2")
    conflito, = amb.repo.acoes("EVENTO_CONFLITANTE")
    assert conflito["pedidoId"] is None
    amb.solicitar(1)
    assert amb.bus.de("pedido_criado")[0][1]["payload"]["produtoNome"] == "Milho em grão"


def test_ca03_oferta_com_uf_invalida_vai_para_dlt(amb):
    amb.B()
    forn = "34000000-0000-4000-8000-000000000009"
    amb.enviar("fornecimento_criado", {"fornecimentoId": forn, "fornecedorId": F1E, "produtoId": PROD,
                                       "unidade": "TON", "origem": {"cidade": "Goiânia", "uf": "go"}})
    (chave, msg), = amb.bus.de("pedidos.dlt")
    assert chave == forn and msg["payload"]["error"] == "EVENTO_INVALIDO" and msg["payload"]["failedAtAttempt"] == 1
    assert forn not in amb.repo.ofertas


# RF-02 -----------------------------------------------------------------

def test_ca04_pedido_temporario_criado(amb):
    amb.B()
    amb.solicitar(1)
    p = amb.pedido(1)
    assert (p["status"], p["compradorId"], p["reservaSituacao"], p["versao"]) == ("CRIADO", None, "PENDENTE", 1)
    assert p["quantidade"] == Decimal("5.00") and p["pesoKg"] == 5000 and p["valorTotal"] == Decimal("600.00")
    (chave, msg), = amb.bus.de("pedido_criado")
    assert chave == amb.p[1] == msg["aggregateId"]
    assert msg["payload"]["compradorId"] is None and msg["payload"]["destino"] is None
    assert msg["payload"]["fornecimentoId"] == OFERTAS[1][0]
    v1, = amb.repo.listar_historico(amb.p[1], 0, 10)[0]
    assert (v1["versao"], v1["statusAnterior"], v1["origem"]) == (1, None, "pedido_temporario_solicitado")


def test_ca05_solicitacao_repetida_nao_duplica(amb):
    amb.B()
    m = amb.solicitar(1)
    for _ in range(4):
        amb.bus.handlers["pedido_temporario_solicitado"](m)
    amb.solicitar(1)
    assert len(amb.repo.pedidos) == 1 and len(amb.bus.de("pedido_criado")) == 1


def test_ca06_leilao_conjunto_cria_um_pedido_por_oferta(amb):
    amb.B()
    amb.solicitar(1)
    amb.solicitar(2)
    assert {p["fornecimentoId"]: p["valorTotal"] for p in amb.repo.pedidos.values()} == {
        OFERTAS[1][0]: Decimal("600.00"), OFERTAS[2][0]: Decimal("1875.00")}
    chaves = [k for k, _ in amb.bus.de("pedido_criado")]
    assert len(chaves) == 2 and len(set(chaves)) == 2


def test_ca07_solicitacao_espera_projecao_e_reconciliador_nao_duplica(amb):
    amb.oferta(1)
    amb.solicitar(1)
    assert not amb.repo.pedidos and not amb.bus.publicados
    amb.produto(correlacao="teste-ped-02")
    assert len(amb.repo.pedidos) == 1
    (_, criado), = amb.bus.de("pedido_criado")
    assert criado["correlationId"] == "teste-ped-01"
    assert reconciliar(amb.app) == 0 and reconciliar(amb.app) == 0
    assert len(amb.bus.publicados) == 1


def test_ca08_solicitacao_para_negociacao_encerrada_e_descartada(amb):
    amb.B()
    amb.deserto()
    amb.solicitar(3)
    assert not amb.repo.pedidos
    assert amb.repo.acoes("SOLICITACAO_DESCARTADA")[0]["negociacaoId"] == N


def test_ca09_fornecedor_divergente_vai_para_dlt(amb):
    amb.B()
    amb.solicitar(1, fornecedorId=F2E)
    (chave, msg), = amb.bus.de("pedidos.dlt")
    assert chave == N
    assert msg["payload"] | {"originalEvent": None} == {"originalEvent": None, "error": "CONTRATO_DIVERGENTE",
                                                         "stackTrace": None, "failedAtAttempt": 1}
    assert not amb.repo.pedidos


def test_ca10_outra_solicitacao_com_dados_diferentes_e_conflito(amb):
    amb.T()
    amb.solicitar(1, quantidade=6.00)
    assert amb.pedido(1)["quantidade"] == Decimal("5.00") and amb.pedido(1)["versao"] == 1
    assert amb.repo.acoes("EVENTO_CONFLITANTE")[0]["pedidoId"] == amb.p[1]


# RF-03 -----------------------------------------------------------------

def test_ca11_reserva_confirmada(amb):
    amb.T()
    amb.reservar(1)
    p = amb.pedido(1)
    assert (p["status"], p["reservaSituacao"], p["versao"]) == ("CRIADO", "CONFIRMADA", 2)
    carga, = amb.atualizados(1)
    assert carga == {"pedidoId": amb.p[1], "negociacaoId": N, "fornecimentoId": OFERTAS[1][0], "status": "CRIADO",
                     "statusAnterior": "CRIADO", "reservaSituacao": "CONFIRMADA", "versao": 2}


def test_ca12_reserva_recusada_cancela_uma_vez(amb):
    amb.T()
    amb.recusar(1)
    amb.recusar(1)
    p = amb.pedido(1)
    assert (p["status"], p["reservaSituacao"], p["motivoCancelamento"], p["versao"]) == (
        "CANCELADO", "RECUSADA", "ESTOQUE_INSUFICIENTE", 2)
    carga, = amb.atualizados(1)
    assert (carga["status"], carga["reservaSituacao"], carga["motivo"]) == ("CANCELADO", "RECUSADA",
                                                                           "ESTOQUE_INSUFICIENTE")


def test_ca13_reposicao_e_liberacao_nao_mudam_o_pedido(amb):
    amb.V()
    antes = len(amb.bus.publicados)
    v1, v3 = amb.pedido(1)["versao"], amb.pedido(3)["versao"]
    amb.enviar("estoque_atualizado", {"fornecimentoId": OFERTAS[1][0], "produtoId": PROD, "fornecedorId": F1E,
                                      "operacao": "REPOSICAO", "quantidadeMovimentada": 10.0,
                                      "quantidadeDisponivel": 105.0, "quantidadeReservada": 5.0, "pedidoId": None,
                                      "atualizadoEm": "2026-11-20T13:00:00.000Z"})
    amb.reservar(3, operacao="LIBERACAO")
    assert len(amb.bus.publicados) == antes
    assert (amb.pedido(1)["versao"], amb.pedido(3)["versao"]) == (v1, v3)
    registro, = amb.repo.acoes("ESTOQUE_REGISTRADO")
    assert registro["pedidoId"] == amb.p[3]


def test_ca14_reserva_com_quantidade_diferente_e_conflito(amb):
    amb.T()
    amb.reservar(1, qtd=4.00)
    assert (amb.pedido(1)["reservaSituacao"], amb.pedido(1)["versao"]) == ("PENDENTE", 1)
    assert amb.repo.acoes("EVENTO_CONFLITANTE")[0]["pedidoId"] == amb.p[1]


# RF-04 -----------------------------------------------------------------

def test_ca15_venda_concluida_no_leilao_conjunto(amb):
    amb.R()
    amb.fechar()
    p1, p2 = amb.pedido(1), amb.pedido(2)
    assert (p1["status"], p1["compradorId"], p1["valorUnitario"], p1["valorTotal"], p1["versao"]) == (
        "VENDA_CONCLUIDA", COMP, Decimal("130.00"), Decimal("650.00"), 3)
    assert p1["vendidoEm"] == datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    assert (p2["status"], p2["valorTotal"]) == ("VENDA_CONCLUIDA", Decimal("1950.00"))
    assert [c["status"] for c in amb.atualizados(1)][-1] == "VENDA_CONCLUIDA"
    assert [c["status"] for c in amb.atualizados(2)][-1] == "VENDA_CONCLUIDA"


def test_ca16_oferta_fora_do_lote_e_cancelada(amb):
    amb.R()
    amb.fechar()
    p3 = amb.pedido(3)
    assert (p3["status"], p3["motivoCancelamento"], p3["reservaSituacao"]) == ("CANCELADO", "FORA_DO_LOTE",
                                                                             "ENCERRADA")
    carga = amb.atualizados(3)[-1]
    assert (carga["status"], carga["motivo"], carga["reservaSituacao"], carga["versao"]) == (
        "CANCELADO", "FORA_DO_LOTE", "ENCERRADA", 3)


def test_ca17_leilao_deserto_cancela_todos_uma_vez(amb):
    amb.R()
    amb.deserto()
    amb.deserto()
    for i in (1, 2, 3):
        assert (amb.pedido(i)["status"], amb.pedido(i)["motivoCancelamento"]) == ("CANCELADO",
                                                                                 "NEGOCIACAO_SEM_VENDA")
        assert [c["status"] for c in amb.atualizados(i)].count("CANCELADO") == 1


def test_ca18_fechamento_incompativel_e_rejeitado_inteiro(amb):
    amb.R()
    amb.fechar(amb.fechamento(qtd_p1=4.00))
    (chave, msg), = amb.bus.de("pedidos.dlt")
    assert chave == N and msg["payload"]["error"] == "FECHAMENTO_INCOMPATIVEL"
    assert msg["payload"]["stackTrace"] is None and msg["payload"]["failedAtAttempt"] == 1
    assert all((amb.pedido(i)["status"], amb.pedido(i)["versao"]) == ("CRIADO", 2) for i in (1, 2, 3))
    amb.fechar()
    assert [amb.pedido(i)["status"] for i in (1, 2, 3)] == ["VENDA_CONCLUIDA", "VENDA_CONCLUIDA", "CANCELADO"]


def test_ca19_segundo_fechamento_diferente_nao_desfaz(amb):
    amb.V()
    antes = len(amb.atualizados())
    amb.deserto()
    assert amb.pedido(1)["status"] == amb.pedido(2)["status"] == "VENDA_CONCLUIDA"
    assert amb.repo.acoes("EVENTO_CONFLITANTE")[0]["negociacaoId"] == N
    assert len(amb.atualizados()) == antes


# RF-05 -----------------------------------------------------------------

def test_ca20_destino_informado_e_repetido(amb):
    amb.V()
    r = amb.put_destino("COMP")
    assert r.status_code == 200
    corpo = r.json()
    assert (corpo["status"], corpo["destino"], corpo["dataEntregaDesejada"], corpo["versao"]) == (
        "AGUARDANDO_FRETE", {"cidade": "Anápolis", "uf": "GO"}, "2026-11-27", 4)
    carga = amb.atualizados(1)[-1]
    assert carga == {"pedidoId": amb.p[1], "negociacaoId": N, "fornecimentoId": OFERTAS[1][0],
                     "status": "AGUARDANDO_FRETE", "statusAnterior": "VENDA_CONCLUIDA", "reservaSituacao": "CONFIRMADA",
                     "versao": 4, "compradorId": COMP, "fornecedorId": F1E, "produtoId": PROD,
                     "produtoNome": "Milho em grão", "quantidade": 5, "unidade": "TON", "pesoKg": 5000,
                     "tipoTransporte": "GRANEL", "valorTotal": 650, "origem": {"cidade": "Rio Verde", "uf": "GO"},
                     "destino": {"cidade": "Anápolis", "uf": "GO"}, "dataEntregaDesejada": "2026-11-27"}
    antes = len(amb.bus.publicados)
    r = amb.put_destino("COMP")
    assert r.status_code == 200 and r.json()["versao"] == 4 and len(amb.bus.publicados) == antes


def test_ca21_quem_nao_pode_informar_destino(amb):
    amb.V()
    assert amb.put_destino("F1").json()["codigo"] == "ACESSO_NEGADO"
    r = amb.put_destino("OUTRA-B")
    assert (r.status_code, r.json()["codigo"]) == (404, "PEDIDO_NAO_ENCONTRADO")
    r = amb.put_destino("COMP", i=3)
    assert (r.status_code, r.json()["codigo"]) == (404, "PEDIDO_NAO_ENCONTRADO")
    assert amb.put_destino("COMP").status_code == 200
    r = amb.put_destino("COMP", corpo={"cidade": "Goiânia", "uf": "GO"})
    assert (r.status_code, r.json()["codigo"]) == (409, "TRANSICAO_INVALIDA")


@pytest.mark.parametrize("corpo", [
    {"cidade": "Anápolis", "uf": "GO", "dataEntregaDesejada": "2026-11-19"}, {"cidade": "Anápolis", "uf": "go"},
    {"cidade": "   ", "uf": "GO"}, {"cidade": "Anápolis", "uf": "GO", "frete": 1}])
def test_ca22_destino_invalido(amb, corpo):
    amb.V()
    r = amb.put_destino("COMP", corpo=corpo)
    assert (r.status_code, r.json()["codigo"]) == (422, "VALIDACAO_INVALIDA")
    assert (amb.pedido(1)["status"], amb.pedido(1)["versao"]) == ("VENDA_CONCLUIDA", 3)


def test_ca23_chave_de_idempotencia(amb):
    amb.V()
    corpo = {"cidade": "Anápolis", "uf": "GO"}
    r1 = amb.put_destino("COMP", corpo=corpo, chave="dest-1")
    r2 = amb.put_destino("COMP", corpo=corpo, chave="dest-1")
    assert r1.status_code == r2.status_code == 200 and r1.json() == r2.json()
    r3 = amb.put_destino("COMP", corpo={"cidade": "Goiânia", "uf": "GO"}, chave="dest-1")
    assert (r3.status_code, r3.json()["codigo"]) == (409, "IDEMPOTENCIA_CONFLITO")


# RF-06 -----------------------------------------------------------------

def test_ca24_frete_selecionado(amb):
    amb.A()
    amb.selecionar()
    p = amb.pedido(1)
    assert (p["status"], p["frete"]["cotacaoId"], p["frete"]["tentativa"], p["versao"]) == (
        "FRETE_SELECIONADO", "cot-go-18", 1, 5)
    carga = amb.atualizados(1)[-1]
    detalhe = amb.get(f"/api/v1/pedidos/{amb.p[1]}", "COMP").json()
    assert carga["status"] == "FRETE_SELECIONADO" and carga["frete"] == detalhe["frete"]


def test_ca25_troca_de_transportadora(amb):
    amb.A()
    amb.selecionar()
    antes = len(amb.atualizados())
    amb.selecionar(tentativa=2, entrega="ent-go-32", transp="tra-go-9")
    amb.selecionar()
    amb.entregar()
    p = amb.pedido(1)
    assert (p["status"], p["frete"]["tentativa"], p["versao"]) == ("FRETE_SELECIONADO", 2, 6)
    assert len(amb.atualizados()) == antes + 1
    assert amb.repo.acoes("ENTREGA_DESCARTADA")[0]["pedidoId"] == amb.p[1]


def test_ca26_entrega_antes_da_selecao(amb):
    amb.A()
    amb.entregar()
    assert amb.pedido(1)["status"] == "AGUARDANDO_FRETE"
    amb.selecionar()
    p = amb.pedido(1)
    assert (p["status"], p["entregueEm"], p["versao"]) == (
        "ENTREGUE", datetime(2026, 11, 23, 16, tzinfo=timezone.utc), 6)
    historico = amb.get(f"/api/v1/pedidos/{amb.p[1]}/historico", "COMP").json()["items"]
    assert [(h["status"], h["versao"]) for h in historico[-2:]] == [("FRETE_SELECIONADO", 5), ("ENTREGUE", 6)]
    assert historico[-1]["origem"] == "entrega_concluida"


def test_ca27_frete_indisponivel_nao_cancela(amb):
    amb.A()
    antes = len(amb.atualizados())
    amb.enviar("frete_indisponivel", {"pedidoId": amb.p[1], "solicitacaoFreteId": "sol-go-21",
                                      "motivo": "SEM_COTACAO", "ocorridoEm": "2026-11-21T02:00:00.000Z"})
    assert (amb.pedido(1)["status"], amb.pedido(1)["versao"]) == ("AGUARDANDO_FRETE", 4)
    detalhe = amb.get(f"/api/v1/pedidos/{amb.p[1]}", "COMP").json()
    assert detalhe["ocorrenciaFrete"] == {"motivo": "SEM_COTACAO", "ocorridoEm": "2026-11-21T02:00:00.000Z"}
    assert amb.repo.acoes("FRETE_INDISPONIVEL") and len(amb.atualizados()) == antes
    amb.solicitacao_frete("sol-go-22")
    amb.selecionar(sol="sol-go-22")
    assert (amb.pedido(1)["status"], amb.pedido(1)["frete"]["solicitacaoFreteId"]) == ("FRETE_SELECIONADO",
                                                                                     "sol-go-22")


def test_ca28_evento_de_frete_fora_de_estado_ou_sem_pedido(amb):
    amb.V()
    amb.selecionar()
    assert amb.pedido(1)["status"] == "VENDA_CONCLUIDA" and amb.repo.acoes("EVENTO_FORA_DE_ESTADO")
    amb.entregar(pedido="32000000-0000-4000-8000-000000000099")
    assert amb.repo.acoes("EVENTO_NAO_CORRELACIONADO") and not amb.bus.de("pedidos.dlt")


# RF-07 -----------------------------------------------------------------

def test_ca29_visibilidade_do_fornecedor_e_do_comprador(amb):
    amb.T()
    r = amb.get(f"/api/v1/pedidos?negociacaoId={N}", "F1").json()
    assert r["total"] == 1 and r["items"][0]["pedidoId"] == amb.p[1] and r["items"][0]["compradorId"] is None
    assert amb.get(f"/api/v1/pedidos?negociacaoId={N}", "COMP").json() == {"items": [], "page": 1, "size": 10,
                                                                           "total": 0}
    for i in (1, 2, 3):
        amb.reservar(i)
    amb.fechar()
    assert amb.get("/api/v1/pedidos", "COMP").json()["total"] == 2
    assert amb.get(f"/api/v1/pedidos/{amb.p[1]}", "COMP").json()["status"] == "VENDA_CONCLUIDA"
    historico = amb.get(f"/api/v1/pedidos/{amb.p[1]}/historico", "COMP").json()["items"]
    assert [h["versao"] for h in historico] == [1, 2, 3]
    r = amb.get(f"/api/v1/pedidos/{amb.p[3]}", "ADMIN")
    assert r.status_code == 200 and r.json()["status"] == "CANCELADO"


@pytest.mark.parametrize("caminho", ["/api/v1/pedidos?page=1&size=101",
                                     "/api/v1/pedidos?status=CRIADO&status=CANCELADO", "/api/v1/pedidos/abc"])
def test_ca30_paginacao_e_parametros_invalidos(amb, caminho):
    amb.T()
    r = amb.get(caminho, "F1")
    assert (r.status_code, r.json()["codigo"]) == (422, "VALIDACAO_INVALIDA")
    assert r.headers["content-type"].startswith("application/problem+json")


def test_ca31_pedido_de_outra_empresa(amb):
    amb.T()
    assert amb.get("/api/v1/pedidos", "OUTRA-S").json()["total"] == 0
    for caminho in (f"/api/v1/pedidos/{amb.p[1]}", f"/api/v1/pedidos/{amb.p[1]}/historico"):
        r = amb.get(caminho, "OUTRA-S")
        assert (r.status_code, r.json()["codigo"]) == (404, "PEDIDO_NAO_ENCONTRADO")


# RF-08 -----------------------------------------------------------------

def test_ca32_token_invalido_e_chaves_indisponiveis(amb):
    amb.T()
    r = amb.cliente.get("/api/v1/pedidos")
    assert (r.status_code, r.json()["codigo"], r.headers["www-authenticate"]) == (401, "TOKEN_INVALIDO", "Bearer")
    expirado = amb.token("F1", exp=int(amb.relogio.agora.timestamp()))
    assert amb.cliente.get("/api/v1/pedidos", headers=expirado).json()["codigo"] == "TOKEN_INVALIDO"
    amb.chaves.falhar = True
    r = amb.get("/api/v1/pedidos", "F1")
    assert (r.status_code, r.json()["codigo"]) == (503, "DEPENDENCIA_INDISPONIVEL")


def test_ca33_contexto_e_escopo(amb):
    amb.V()
    assert amb.get("/api/v1/pedidos", "SEM-CONTEXTO").json()["codigo"] == "CONTEXTO_NAO_SELECIONADO"
    assert amb.get("/api/v1/pedidos", "CARRIER").json()["codigo"] == "ACESSO_NEGADO"
    assert amb.get(f"/api/v1/pedidos?negociacaoId={N}", "ADMIN").json()["total"] == 3
    r = amb.put_destino("ADMIN")
    assert (r.status_code, r.json()["codigo"], amb.pedido(1)["versao"]) == (403, "ACESSO_NEGADO", 3)


# RF-09 -----------------------------------------------------------------

def test_ca34_envelope_sem_payload(amb):
    amb.T()
    amb.bus.handlers["frete_selecionado"]({"eventId": str(uuid4()), "eventType": "frete_selecionado",
                                           "eventVersion": "1.0", "timestamp": "2026-11-20T14:00:00.000Z",
                                           "source": "logistica-service", "correlationId": "teste-ped-01",
                                           "aggregateId": amb.p[1]})
    (chave, msg), = amb.bus.de("pedidos.dlt")
    assert (chave, msg["payload"]["error"], msg["payload"]["failedAtAttempt"]) == (amb.p[1], "EVENTO_INVALIDO", 1)
    assert amb.pedido(1)["versao"] == 1


def test_ca35_falha_transitoria_e_esgotada(amb, monkeypatch):
    amb.T()
    original = amb.repo.atualizar_pedido
    falhas = {"n": 0}

    def uma_falha(pedido):
        falhas["n"] += 1
        if falhas["n"] == 1:
            raise ValueError("falha injetada")
        original(pedido)

    monkeypatch.setattr(amb.repo, "atualizar_pedido", uma_falha)
    amb.recusar(1)
    assert amb.relogio.esperas == [1] and amb.pedido(1)["status"] == "CANCELADO" and not amb.dlt()
    assert len(amb.atualizados(1)) == 1

    def sempre(pedido):
        raise ValueError("falha injetada")

    monkeypatch.setattr(amb.repo, "atualizar_pedido", sempre)
    amb.relogio.esperas.clear()
    amb.recusar(2)
    assert amb.relogio.esperas == [1, 2]
    falha, = amb.dlt()
    assert (falha["error"], falha["failedAtAttempt"]) == ("TENTATIVAS_ESGOTADAS", 3)
    assert (amb.pedido(2)["status"], amb.pedido(2)["versao"]) == ("CRIADO", 1)


def test_ca36_banco_indisponivel_volta_ao_adaptador(amb):
    amb.T()
    antes = len(amb.bus.publicados)
    amb.repo.falhar_transacao = True
    with pytest.raises(RepositorioIndisponivel):
        amb.recusar(1)
    assert len(amb.bus.publicados) == antes


def test_ca37_mensagem_duplicada_e_invalida(amb):
    amb.T()
    m = {"eventId": str(uuid4()), "eventType": "frete_indisponivel", "eventVersion": "1.0",
         "timestamp": "2026-11-20T14:00:00.000Z", "source": "logistica-service", "correlationId": "teste-ped-01",
         "aggregateId": amb.p[1], "payload": {"pedidoId": amb.p[1], "solicitacaoFreteId": "sol-go-21",
                                              "ocorridoEm": "2026-11-21T02:00:00.000Z"}}
    amb.bus.handlers["frete_indisponivel"](m)
    amb.bus.handlers["frete_indisponivel"](m)
    assert [d["error"] for d in amb.dlt()] == ["EVENTO_INVALIDO", "EVENTO_INVALIDO"]


# RF-10 -----------------------------------------------------------------

def test_ca38_prontidao(amb):
    assert amb.cliente.get("/health/ready").json() == {"status": "ready"}
    amb.bus.pronto = False
    r = amb.cliente.get("/health/ready")
    assert (r.status_code, r.json()) == (503, {"status": "not_ready"})
    assert amb.cliente.get("/health").json() == {"status": "up"}
    assert amb.get("/api/v1/pedidos", "F1").status_code == 200


def test_ca39_falha_inesperada(amb, monkeypatch):
    amb.T()

    def quebra(*args, **kwargs):
        raise ValueError("falha injetada")

    monkeypatch.setattr(amb.repo, "listar_pedidos", quebra)
    r = amb.get("/api/v1/pedidos", "F1")
    assert (r.status_code, r.json()["codigo"]) == (500, "ERRO_INTERNO")
    assert r.headers["content-type"].startswith("application/problem+json")


# RNFs verificáveis em memória ----------------------------------------------

def test_rnf01_aritmetica_decimal():
    from app import dominio
    assert dominio.valor_total(Decimal("0.10"), Decimal("0.20")) == Decimal("0.02")
    assert dominio.valor_total(Decimal("1.25"), Decimal("8.00")) == Decimal("10.00")
    assert dominio.peso_kg(Decimal("1.25"), "TON", None) == 1250


def test_rnf02_erro_tem_os_oito_campos(amb):
    r = amb.cliente.get("/api/v1/pedidos", headers={"X-Correlation-Id": "corr-1"})
    assert set(r.json()) == {"type", "title", "status", "codigo", "detail", "instance", "correlationId", "timestamp"}
    assert r.json()["correlationId"] == r.headers["x-correlation-id"] == "corr-1"


def test_rnf03_envelope_dos_eventos_publicados(amb):
    amb.V()
    ids = set()
    for topico, chave, msg in amb.bus.publicados:
        assert chave == msg["aggregateId"] == msg["payload"]["pedidoId"]
        assert (msg["eventType"], msg["eventVersion"], msg["source"]) == (topico, "1.0", "pedidos-service")
        assert msg["timestamp"] == "2026-11-20T12:00:00.000Z"
        ids.add(msg["eventId"])
    assert len(ids) == len(amb.bus.publicados)


def test_rnf04_aplicacoes_nao_compartilham_estado():
    a, b = Ambiente(), Ambiente()
    a.B()
    a.solicitar(1)
    assert a.repo.pedidos and not b.repo.pedidos
    assert b.get("/api/v1/pedidos", "F1").json()["total"] == 0


def test_metricas_publicas(amb):
    amb.T()
    texto = amb.cliente.get("/metrics").text
    assert "pedidos_eventos_consumidos_total" in texto and "pedidos_http_requisicoes_total" in texto


def test_chave_dlt_desconhecida_e_mensagem_nao_objeto(amb):
    amb.bus.handlers["produto_cadastrado"](["nao", "objeto"])
    (chave, msg), = amb.bus.de("pedidos.dlt")
    assert chave == "desconhecido" and msg["payload"]["error"] == "EVENTO_INVALIDO"


def test_segredos_sao_omitidos_na_dlt(amb):
    m = amb.produto(token="segredo")  # campo extra no envelope; payload válido
    amb.bus.publicados.clear()
    m["payload"]["nome"] = "   "
    amb.bus.handlers["produto_cadastrado"](m)
    (_, falha), = amb.bus.de("pedidos.dlt")
    assert falha["payload"]["originalEvent"]["token"] == "[omitido]"


def test_destino_data_hoje_brasilia(amb):
    amb.relogio.agora = datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc)  # 20/11 23h em Brasília
    amb.V()
    r = amb.put_destino("COMP", corpo={"cidade": "Anápolis", "uf": "GO", "dataEntregaDesejada": "2026-11-20"})
    assert r.status_code == 200


def test_token_com_nbf_futuro_e_audiencia_em_lista(amb):
    agora = int(amb.relogio.agora.timestamp())
    assert amb.cliente.get("/api/v1/pedidos", headers=amb.token("F1", aud=["x", "b2b-platform"])).status_code == 200
    r = amb.cliente.get("/api/v1/pedidos", headers=amb.token("F1", nbf=agora + 1))
    assert r.status_code == 401


def test_view_mode_diferente_de_single(amb):
    r = amb.cliente.get("/api/v1/pedidos", headers=amb.token("F1", view_mode="NONE"))
    assert r.json()["codigo"] == "CONTEXTO_NAO_SELECIONADO"


def test_pendencia_por_peso_ausente(amb):
    amb.enviar("produto_cadastrado", {"produtoId": PROD, "nome": "Caixa", "unidade": "CX",
                                      "tipoTransporte": "CARGA_SECA", "pesoUnitarioKg": None})
    amb.enviar("fornecimento_criado", {"fornecimentoId": OFERTAS[1][0], "fornecedorId": F1E, "produtoId": PROD,
                                       "unidade": "CX", "origem": {"cidade": "Rio Verde", "uf": "GO"}})
    amb.solicitar(1)
    assert not amb.repo.pedidos
    pendente, = amb.repo.pendentes.values()
    assert pendente["causa"] == "PESO_AUSENTE"
    amb.enviar("produto_atualizado", {"produtoId": PROD, "nome": "Caixa", "unidade": "CX",
                                      "tipoTransporte": "CARGA_SECA", "pesoUnitarioKg": 2.5},
               timestamp="2026-11-20T13:00:00.000Z")
    pedido, = amb.repo.pedidos.values()
    assert pedido["pesoKg"] == Decimal("12.500") and not amb.repo.pendentes


def test_reserva_expira_idempotencia_em_24h(amb):
    amb.V()
    corpo = {"cidade": "Anápolis", "uf": "GO"}
    amb.put_destino("COMP", corpo=corpo, chave="dest-1")
    amb.relogio.agora += timedelta(hours=25)
    r = amb.put_destino("COMP", corpo={"cidade": "Goiânia", "uf": "GO"}, chave="dest-1")
    assert (r.status_code, r.json()["codigo"]) == (409, "TRANSICAO_INVALIDA")
