"""Suíte de aceitação pela API (`create_app` com dublês): §7.1, §7.2, §10 e os CAs que dependem do HTTP."""
from decimal import Decimal

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.jsonio import loads
from app.main import create_app, executar_prazos
from app.portas import ConfigMercado
from tests.cenarios import (
    COMP_A, COMP_B, DEM_A, DEM_B, FORN_X, FORN_Y, FORN_Z, OF_Q, OF_W, OF_X, OF_Y, P, Q, Ambiente,
    contratos_disponiveis, envelope, t, validar_schema,
)

D = Decimal
KID = "mercado-teste"
CHAVE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OUTRA_CHAVE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OF_V = "34000000-0000-4000-8000-000000000005"
SEM = object()


class Chaves:
    def __init__(self):
        self.chaves = {KID: CHAVE.public_key()}
        self.falha = False

    def obter(self, kid):
        if self.falha:
            raise ConnectionError("JWKS fora do ar")
        return self.chaves.get(kid)


class Api:
    def __init__(self, config: ConfigMercado = ConfigMercado()):
        self.amb = Ambiente(config)
        self.chaves = Chaves()
        self.app = create_app(self.amb.bus, self.amb.clock, self.amb.repo, self.chaves, config)
        self.amb.mercado = self.app.state.mercado
        self.cliente = TestClient(self.app)

    def token(self, empresa=COMP_A, escopo="BUYER", chave=CHAVE, algoritmo="RS256", cabecalho=None, **claims):
        base = {"iss": "identity-service", "aud": "b2b-platform", "sub": "usr-1",
                "exp": int(self.amb.clock.agora.timestamp()) + 300, "tenant_id": empresa, "active_scope": escopo,
                "view_mode": "SINGLE"}
        base.update(claims)
        base = {k: v for k, v in base.items() if v is not SEM}
        return jwt.encode(base, chave, algorithm=algoritmo,
                          headers={"kid": KID} if cabecalho is None else cabecalho)

    def chamar(self, metodo, caminho, empresa=COMP_A, escopo="BUYER", corpo=None, texto=None, headers=None,
               token=None):
        cabecalhos = {"Authorization": f"Bearer {token or self.token(empresa, escopo)}"}
        cabecalhos.update(headers or {})
        if corpo is not None:
            texto = __import__("json").dumps(corpo)
        if texto is not None:
            cabecalhos.setdefault("Content-Type", "application/json")
        return self.cliente.request(metodo, caminho, content=texto, headers=cabecalhos)

    def get(self, caminho, **kw):
        return self.chamar("GET", caminho, **kw)

    def post(self, caminho, **kw):
        return self.chamar("POST", caminho, **kw)


@pytest.fixture
def api():
    return Api()


def corpo(resposta):
    return loads(resposta.text)


def codigo(resposta):
    assert resposta.headers["content-type"] == "application/problem+json"
    return resposta.status_code, corpo(resposta)["codigo"]


def direta(api, demanda, oferta, qtd, empresa=COMP_A, **kw):
    return api.post("/api/v1/mercado/negociacoes", empresa=empresa,
                    texto=f'{{"demandaId": "{demanda}", "fornecimentoId": "{oferta}", "quantidade": {qtd}}}', **kw)


def s1(api):
    return api.amb.sugestao_ativa()["sugestaoId"]


# ---------------------------------------------------------------- números lidos como Decimal

def test_json_le_numeros_como_decimal():
    assert type(loads("0.1")) is Decimal and loads("0.1") == D("0.1")
    assert type(loads('{"q": 7.5}')["q"]) is Decimal


def test_api_aceita_0_1_7_5_9_99_e_30_000_e_recusa_0_001_e_texto(api):
    api.amb.cenario_s()
    api.amb.oferta(OF_W, hora="12:01")
    api.amb.oferta(OF_V, FORN_Z, P, "8.50", "50.00", origem=None, hora="12:01")
    api.amb.em("12:02")
    for oferta, texto, esperado in ((OF_Y, "0.1", "0.10"), (OF_X, "7.5", "7.50"), (OF_W, "9.99", "9.99"),
                                    (OF_V, "30.000", "30.00")):
        resposta = direta(api, DEM_A, oferta, texto)
        assert resposta.status_code == 201, resposta.text
        assert f'"quantidadeTotal": {esperado}' in resposta.text
    assert corpo(api.get(f"/api/v1/mercado/demandas/{DEM_A}"))["emNegociacao"] == D("47.59")
    for texto in ("0.001", '"30.00"'):
        assert codigo(direta(api, DEM_A, OF_Q, texto)) == (422, "VALIDACAO_INVALIDA")
    aceite = api.amb.publicados("modo_negociacao_definido")[0]["payload"]["ofertas"][0]["quantidade"]
    assert str(aceite) == "0.10"


@pytest.mark.parametrize("texto,resultado,guardado", [
    ("0.1", "aplicado", "0.10"), ("7.5", "aplicado", "7.50"), ("9.99", "aplicado", "9.99"),
    ("30.000", "aplicado", "30.00"), ("0.001", "dlt", None), ('"30.00"', "dlt", None),
])
def test_consumo_aceita_e_recusa_os_mesmos_numeros(texto, resultado, guardado):
    amb = Ambiente()
    bruto = ('{"eventId": "e-1", "eventType": "demanda_criada", "eventVersion": "1.0", '
             '"timestamp": "2026-11-20T11:55:00.000Z", "source": "demanda-service", "correlationId": "c-1", '
             f'"aggregateId": "{DEM_A}", "payload": {{"demandaId": "{DEM_A}", "compradorId": "{COMP_A}", '
             f'"produtoId": "{P}", "unidade": "TON", "quantidade": {texto}, "precoMaximo": null, '
             '"destino": {"cidade": "Anápolis", "uf": "GO"}, "tipo": "UNICA", '
             '"criadoEm": "2026-11-20T11:55:00.000Z"}}')
    assert amb.mercado.consumir("demanda_criada", loads(bruto)) == resultado
    if guardado:
        assert str(amb.repo.tabela("demandas")[DEM_A]["quantidade"]) == guardado
    else:
        assert amb.repo.tabela("demandas") == {}


# ---------------------------------------------------------------- caminhos principais pela API

def test_ca15_balanco_pela_api(api):
    api.amb.cenario_e()
    resposta = api.get(f"/api/v1/mercado/produtos/{P}/balanco", empresa=FORN_X, escopo="SUPPLIER")
    assert resposta.status_code == 200
    assert resposta.headers["content-type"] == "application/json"
    assert corpo(resposta) == {
        "produtoId": P, "unidade": "TON", "ofertaTotal": D("100.00"), "demandaTotal": D("130.00"),
        "situacao": "DEMANDA_EXCEDENTE", "situacaoDesde": "2026-11-20T11:57:00.000Z", "excessoDemanda": D("30.00"),
        "sugestaoId": s1(api), "agora": "2026-11-20T12:00:00.000Z"}
    assert '"ofertaTotal": 100.00' in resposta.text


def test_ca26_direta_pela_api_com_location(api):
    api.amb.cenario_s()
    api.amb.em("12:05")
    resposta = direta(api, DEM_A, OF_Y, "30.00", headers={"X-Correlation-Id": "teste-mrc-05"})
    assert resposta.status_code == 201
    negociacao = corpo(resposta)["negociacaoId"]
    assert resposta.headers["location"] == f"/api/v1/mercado/negociacoes/{negociacao}"
    assert resposta.headers["x-correlation-id"] == "teste-mrc-05"
    assert '"participacao": 1.0000' in resposta.text and '"valorReferencia": 225.00' in resposta.text
    assert corpo(api.get(resposta.headers["location"])) == corpo(resposta) | {"agora": "2026-11-20T12:05:00.000Z"}
    [mensagem] = api.amb.publicados("modo_negociacao_definido")
    assert mensagem["correlationId"] == "teste-mrc-05"


def test_ca39_aceites_pela_api(api):
    api.amb.cenario_e()
    caminho = f"/api/v1/mercado/sugestoes-leilao/{s1(api)}/aceites"
    primeiro = api.post(caminho, empresa=FORN_X, escopo="SUPPLIER",
                        corpo={"fornecimentoId": OF_X, "quantidade": 60})
    assert (primeiro.status_code, corpo(primeiro)["situacao"]) == (201, "EM_FORMACAO")
    alterado = api.chamar("PUT", f"{caminho}/{OF_X}", empresa=FORN_X, escopo="SUPPLIER", texto='{"quantidade": 50}')
    assert (alterado.status_code, corpo(alterado)["quantidadeNoLote"]) == (200, D("50.00"))
    removido = api.chamar("DELETE", f"{caminho}/{OF_X}", empresa=FORN_X, escopo="SUPPLIER")
    assert (removido.status_code, removido.content) == (204, b"")
    api.post(caminho, empresa=FORN_X, escopo="SUPPLIER", corpo={"fornecimentoId": OF_X, "quantidade": 60})
    api.amb.em("12:10")
    fechado = api.post(caminho, empresa=FORN_Y, escopo="SUPPLIER", texto=f'{{"fornecimentoId": "{OF_Y}", '
                       '"quantidade": 40.00}', headers={"X-Correlation-Id": "teste-mrc-10"})
    assert (fechado.status_code, corpo(fechado)["situacao"]) == (201, "CONVERTIDA")
    assert [m["correlationId"] for m in api.amb.publicados()] == ["teste-mrc-10", "teste-mrc-10"]
    assert api.chamar("PUT", f"{caminho}/{OF_X}", empresa=FORN_X, escopo="SUPPLIER",
                      texto='{"quantidade": 1}').status_code == 409


def test_executar_prazos_pelo_main(api):
    api.amb.cenario_e()
    api.amb.aceitar(FORN_X, s1(api), OF_X, "60.00", hora="12:00")
    api.amb.em("13:00")
    assert executar_prazos(api.app) == 1
    assert executar_prazos(api.app) == 0
    assert len(api.amb.publicados("leilao_iniciado")) == 1


def test_create_app_assina_os_nove_topicos_e_conta_o_consumo(api):
    assert sorted(api.amb.bus.assinaturas) == sorted([
        "produto_cadastrado", "produto_atualizado", "fornecimento_criado", "fornecimento_atualizado",
        "estoque_atualizado", "demanda_criada", "demanda_recorrente_gerada", "demanda_cancelada",
        "negociacao_fechada"])
    mensagem = envelope("produto_cadastrado", {"produtoId": P, "nome": "Milho", "unidade": "TON",
                                               "cadastradoEm": "2026-11-20T11:00:00Z"}, agregado=P)
    api.amb.bus.entregar("produto_cadastrado", mensagem)
    api.amb.bus.entregar("produto_cadastrado", mensagem)
    texto = api.cliente.get("/metrics").text
    assert 'mercado_eventos_consumidos_total{resultado="aplicado",topico="produto_cadastrado"} 1.0' in texto
    assert 'mercado_eventos_consumidos_total{resultado="duplicado",topico="produto_cadastrado"} 1.0' in texto


def test_configuracao_invalida_impede_a_criacao_da_aplicacao():
    amb = Ambiente()
    with pytest.raises(ValueError):
        create_app(amb.bus, amb.clock, amb.repo, Chaves(), ConfigMercado(leilao_janela_adesao_minutos=0))


# ---------------------------------------------------------------- CAs pendentes da etapa 2

def test_ca17_produto_desconhecido_e_escopo_sem_acesso(api):
    api.amb.cenario_s()
    resposta = api.get("/api/v1/mercado/produtos/35000000-0000-4000-8000-000000000077/balanco")
    assert codigo(resposta) == (404, "PRODUTO_NAO_ENCONTRADO")
    assert codigo(api.get(f"/api/v1/mercado/produtos/{P}/balanco", empresa=FORN_Z, escopo="CARRIER")) == (
        403, "ACESSO_NEGADO")


def test_ca21_comprador_nao_consulta_sugestoes(api):
    api.amb.cenario_e()
    assert codigo(api.get("/api/v1/mercado/sugestoes-leilao")) == (403, "ACESSO_NEGADO")
    resposta = api.get("/api/v1/mercado/sugestoes-leilao", empresa=FORN_X, escopo="SUPPLIER")
    assert corpo(resposta)["total"] == 1


def test_ca24_demanda_de_outra_empresa_outro_produto_ou_fornecedor(api):
    api.amb.cenario_e()
    assert codigo(api.get(f"/api/v1/mercado/produtos/{P}/ofertas?demandaId={DEM_B}")) == (
        404, "DEMANDA_NAO_ENCONTRADA")
    assert codigo(api.get(f"/api/v1/mercado/produtos/{Q}/ofertas?demandaId={DEM_A}")) == (
        422, "PRODUTO_DIFERENTE")
    assert codigo(api.get(f"/api/v1/mercado/produtos/{P}/ofertas?demandaId={DEM_A}", empresa=FORN_X,
                          escopo="SUPPLIER")) == (422, "VALIDACAO_INVALIDA")
    itens = corpo(api.get(f"/api/v1/mercado/produtos/{P}/ofertas?demandaId={DEM_A}"))["items"]
    assert [(i["fornecimentoId"], i["apta"]) for i in itens] == [(OF_Y, True), (OF_X, True)]


def test_ca25_demanda_visivel_so_ao_dono(api):
    api.amb.cenario_e()
    assert codigo(api.get(f"/api/v1/mercado/demandas/{DEM_B}")) == (404, "DEMANDA_NAO_ENCONTRADA")
    assert codigo(api.get(f"/api/v1/mercado/demandas/{DEM_B}", empresa=FORN_X, escopo="SUPPLIER")) == (
        403, "ACESSO_NEGADO")
    assert corpo(api.get(f"/api/v1/mercado/demandas/{DEM_B}", empresa=COMP_B))["situacao"] == "AGUARDANDO"


def test_ca32_escopo_e_corpo_invalidos(api):
    api.amb.cenario_s()
    valido = f'{{"demandaId": "{DEM_A}", "fornecimentoId": "{OF_Y}", "quantidade": 30.00}}'
    rota = "/api/v1/mercado/negociacoes"
    assert codigo(api.post(rota, empresa=FORN_X, escopo="SUPPLIER", texto=valido)) == (403, "ACESSO_NEGADO")
    for texto in (
        f'{{"demandaId": "{DEM_A}", "fornecimentoId": "{OF_Y}", "quantidade": "30.00"}}',
        f'{{"demandaId": "{DEM_A}", "fornecimentoId": "{OF_Y}", "quantidade": 0.001}}',
        f'{{"demandaId": "{DEM_A}", "fornecimentoId": "{OF_Y}", "quantidade": 30.00, "compraImediata": "true"}}',
        f'{{"demandaId": "{DEM_A}", "fornecimentoId": "{OF_Y}", "quantidade": 30.00, "compraImediata": null}}',
        f'{{"demandaId": "{DEM_A}", "fornecimentoId": "{OF_Y}", "quantidade": 30.00, "extra": 1}}',
        f'{{"demandaId": "{DEM_A}", "fornecimentoId": "{OF_Y}"}}',
        f'{{"demandaId": "{DEM_A}", "fornecimentoId": "{OF_Y}", "quantidade": true}}',
        f'{{"demandaId": "{DEM_A}", "fornecimentoId": "{OF_Y}", "quantidade": NaN}}',
        f'{{"demandaId": "{DEM_A}", "fornecimentoId": "{OF_Y}", "quantidade": 10000000000}}',
        f'{{"demandaId": "id com espaço", "fornecimentoId": "{OF_Y}", "quantidade": 1}}',
        '{"demandaId": ', "[]", "",
    ):
        assert codigo(api.post(rota, texto=texto)) == (422, "VALIDACAO_INVALIDA"), texto
    api.amb.atualizar_oferta(OF_Y, "12:01", preco="0.01")
    assert codigo(direta(api, DEM_A, OF_Y, "0.01")) == (422, "VALIDACAO_INVALIDA")
    assert api.amb.publicados() == []
    assert corpo(api.get(f"/api/v1/mercado/demandas/{DEM_A}"))["emNegociacao"] == D("0.00")


def test_ca33_idempotency_key_repetida_e_compra_imediata(api):
    api.amb.cenario_s()
    chave = {"Idempotency-Key": "k-1"}
    primeira = direta(api, DEM_A, OF_Y, "30.00", headers=chave)
    segunda = direta(api, DEM_A, OF_Y, "30.00", headers=chave | {"X-Correlation-Id": "outra"})
    assert (primeira.status_code, segunda.status_code) == (201, 201)
    assert segunda.text == primeira.text
    assert segunda.headers["location"] == primeira.headers["location"]
    assert segunda.headers["x-correlation-id"] == "outra"
    assert len(api.amb.publicados("modo_negociacao_definido")) == 1
    assert codigo(direta(api, DEM_A, OF_Y, "10.00", headers=chave)) == (409, "IDEMPOTENCIA_CONFLITO")
    resposta = api.post("/api/v1/mercado/negociacoes", texto=(
        f'{{"demandaId": "{DEM_A}", "fornecimentoId": "{OF_X}", "quantidade": 10.00, "compraImediata": true}}'))
    assert resposta.status_code == 201
    dados = corpo(resposta)
    assert dados["compraImediata"] is True
    assert [(i["fornecimentoId"], i["quantidade"], i["precoUnitario"], i["valorItem"], i["participacao"])
            for i in dados["itens"]] == [(OF_X, D("10.00"), D("8.00"), D("80.00"), D("1.0000"))]
    mensagem = api.amb.publicados("modo_negociacao_definido")[-1]
    assert mensagem["payload"]["compraImediata"] is True
    if contratos_disponiveis():
        validar_schema("modo_negociacao_definido", mensagem)
    demanda = corpo(api.get(f"/api/v1/mercado/demandas/{DEM_A}"))
    assert (demanda["emNegociacao"], demanda["saldoNegociavel"]) == (D("40.00"), D("10.00"))


def test_idempotencia_escopo_por_empresa_formato_e_rejeicao_nao_memoriza(api):
    api.amb.cenario_e()
    assert codigo(direta(api, DEM_A, OF_Y, "1.00", headers={"Idempotency-Key": "com espaco"})) == (
        422, "VALIDACAO_INVALIDA")
    assert codigo(direta(api, DEM_A, OF_Y, "50.01", headers={"Idempotency-Key": "k-2"})) == (
        422, "QUANTIDADE_ACIMA_DO_SALDO")
    assert direta(api, DEM_A, OF_Y, "1.00", headers={"Idempotency-Key": "k-2"}).status_code == 201
    assert direta(api, DEM_B, OF_Y, "1.00", empresa=COMP_B, headers={"Idempotency-Key": "k-2"}).status_code == 201
    assert direta(api, DEM_A, OF_X, "1.00", headers={"Idempotency-Key": ""}).status_code == 201
    assert len(api.amb.repo.tabela("idempotencia")) == 2
    caminho = f"/api/v1/mercado/sugestoes-leilao/{s1(api)}/aceites"
    aceite = {"fornecimentoId": OF_X, "quantidade": 10}
    cabecalho = {"Idempotency-Key": "a-1"}
    um = api.post(caminho, empresa=FORN_X, escopo="SUPPLIER", corpo=aceite, headers=cabecalho)
    dois = api.post(caminho, empresa=FORN_X, escopo="SUPPLIER", corpo=aceite, headers=cabecalho)
    assert (um.status_code, dois.status_code, um.text == dois.text) == (201, 201, True)
    sem_sugestao = api.post("/api/v1/mercado/sugestoes-leilao/33000000-0000-4000-8000-000000000077/aceites",
                            empresa=FORN_X, escopo="SUPPLIER", corpo=aceite, headers=cabecalho)
    assert codigo(sem_sugestao) == (404, "SUGESTAO_NAO_ENCONTRADA")


def test_ca50_negociacoes_visiveis_e_paginacao(api):
    api.amb.cenario_e()
    api.amb.aceitar(FORN_X, s1(api), OF_X, "60.00", hora="12:00")
    api.amb.direta(COMP_A, DEM_A, OF_Y, "10.00", hora="12:05")
    api.amb.aceitar(FORN_Y, s1(api), OF_Y, "40.00", hora="12:10")
    assert corpo(api.get("/api/v1/mercado/negociacoes"))["total"] == 2
    assert corpo(api.get("/api/v1/mercado/negociacoes?modo=VENDA_DIRETA", empresa=COMP_B))["total"] == 0
    assert corpo(api.get("/api/v1/mercado/negociacoes", empresa=FORN_Y, escopo="SUPPLIER"))["total"] == 2
    assert codigo(api.get("/api/v1/mercado/negociacoes?size=101")) == (422, "VALIDACAO_INVALIDA")
    for consulta in ("page=0", "size=0", "page=abc", "page=-1", "page=1.0", "modo=LEILAO", "situacao=ENCERRADA"):
        assert codigo(api.get(f"/api/v1/mercado/negociacoes?{consulta}")) == (422, "VALIDACAO_INVALIDA"), consulta
    pagina = corpo(api.get("/api/v1/mercado/negociacoes?page=2&size=1&outro=ignorado"))
    assert (pagina["page"], pagina["size"], pagina["total"], len(pagina["items"])) == (2, 1, 2, 1)
    assert corpo(api.get("/api/v1/mercado/negociacoes?page=3&size=1"))["items"] == []


def test_ca53_token_ausente_sem_contexto_e_escopo_errado(api):
    rota = f"/api/v1/mercado/produtos/{P}/balanco"
    resposta = api.cliente.get(rota)
    assert codigo(resposta) == (401, "TOKEN_INVALIDO")
    assert resposta.headers["www-authenticate"] == "Bearer"
    assert codigo(api.get(rota, token=api.token(view_mode="NONE"))) == (403, "CONTEXTO_NAO_SELECIONADO")
    assert codigo(api.get(rota, empresa=FORN_Z, escopo="CARRIER")) == (403, "ACESSO_NEGADO")


@pytest.mark.parametrize("descricao,montar,esperado", [
    ("basic", lambda a: "Basic abc", 401),
    ("bearer vazio", lambda a: "Bearer ", 401),
    ("malformado", lambda a: "Bearer abc.def", 401),
    ("hs256", lambda a: "Bearer " + a.token(chave="s" * 64, algoritmo="HS256"), 401),
    ("sem kid", lambda a: "Bearer " + a.token(cabecalho={}), 401),
    ("kid desconhecido", lambda a: "Bearer " + a.token(cabecalho={"kid": "outro"}), 401),
    ("assinatura", lambda a: "Bearer " + a.token(chave=OUTRA_CHAVE), 401),
    ("iss", lambda a: "Bearer " + a.token(iss="outro"), 401),
    ("aud", lambda a: "Bearer " + a.token(aud="outro"), 401),
    ("aud lista", lambda a: "Bearer " + a.token(aud=["x", "b2b-platform"]), 200),
    ("sem sub", lambda a: "Bearer " + a.token(sub=SEM), 401),
    ("sub vazio", lambda a: "Bearer " + a.token(sub="  "), 401),
    ("sem exp", lambda a: "Bearer " + a.token(exp=SEM), 401),
    ("exp float", lambda a: "Bearer " + a.token(exp=int(a.amb.clock.agora.timestamp()) + 300.5), 401),
    ("exp agora", lambda a: "Bearer " + a.token(exp=int(a.amb.clock.agora.timestamp())), 401),
    ("exp agora+1", lambda a: "Bearer " + a.token(exp=int(a.amb.clock.agora.timestamp()) + 1), 200),
    ("sem tenant", lambda a: "Bearer " + a.token(tenant_id=SEM), 403),
    ("tenant vazio", lambda a: "Bearer " + a.token(tenant_id=""), 403),
    ("tenant nulo", lambda a: "Bearer " + a.token(tenant_id=None), 403),
    ("escopo nulo", lambda a: "Bearer " + a.token(escopo=None), 403),
    ("sem view_mode", lambda a: "Bearer " + a.token(view_mode=SEM), 403),
    ("tenant admin", lambda a: "Bearer " + a.token(escopo="TENANT_ADMIN"), 403),
    ("platform admin", lambda a: "Bearer " + a.token(escopo="CARRIER", platform_role="PLATFORM_ADMIN"), 403),
])
def test_validacao_do_token(api, descricao, montar, esperado):
    api.amb.produto(P)
    resposta = api.cliente.get(f"/api/v1/mercado/produtos/{P}/balanco", headers={"Authorization": montar(api)})
    assert resposta.status_code == esperado, descricao
    if esperado == 401:
        assert resposta.headers["www-authenticate"] == "Bearer"


def test_token_vem_antes_da_validacao_e_dos_recursos(api):
    assert api.cliente.get("/api/v1/mercado/produtos/id inválido/balanco").status_code == 401
    assert codigo(api.get("/api/v1/mercado/produtos/id%20inv%C3%A1lido/balanco")) == (422, "VALIDACAO_INVALIDA")
    assert codigo(api.get("/api/v1/mercado/negociacoes/nao-e-uuid")) == (422, "VALIDACAO_INVALIDA")
    assert codigo(api.get(f"/api/v1/mercado/produtos/{'x' * 65}/ofertas")) == (422, "VALIDACAO_INVALIDA")


def test_ca54_chaves_publicas_indisponiveis(api):
    api.chaves.falha = True
    assert codigo(api.get(f"/api/v1/mercado/produtos/{P}/balanco")) == (503, "DEPENDENCIA_INDISPONIVEL")


def test_ca58_saude_prontidao_e_metricas(api):
    resposta = api.cliente.get("/health")
    assert (resposta.status_code, corpo(resposta)) == (200, {"status": "up"})
    api.amb.repo.pronto = False
    assert codigo(api.cliente.get("/health/ready")) == (503, "DEPENDENCIA_INDISPONIVEL")
    api.amb.repo.pronto = True
    api.amb.bus.pronto = False
    assert api.cliente.get("/health/ready").status_code == 503
    api.amb.bus.pronto = True
    resposta = api.cliente.get("/health/ready")
    assert (resposta.status_code, corpo(resposta)) == (200, {"status": "ready"})
    api.amb.cenario_e()
    api.get(f"/api/v1/mercado/produtos/{P}/balanco")
    resposta = api.cliente.get("/metrics")
    assert resposta.status_code == 200 and resposta.headers["content-type"].startswith("text/plain")
    for nome in ("mercado_http_requisicoes_total", "mercado_http_latencia_segundos", "mercado_eventos_consumidos_total",
                 "mercado_outbox_pendentes", "mercado_sugestoes_ativas 1.0"):
        assert nome in resposta.text
    assert ('mercado_http_requisicoes_total{metodo="GET",rota="/api/v1/mercado/produtos/{produtoId}/balanco",'
            'status="200"} 1.0') in resposta.text
    assert P not in resposta.text


def test_ca59_excecao_inesperada_do_repositorio(api):
    api.amb.cenario_s()
    api.amb.repo.falhar("obter_demanda", ValueError)
    resposta = direta(api, DEM_A, OF_Y, "10.00")
    assert codigo(resposta) == (500, "ERRO_INTERNO")
    assert api.amb.publicados() == []


def test_banco_indisponivel_responde_503(api):
    api.amb.cenario_s()
    from app.portas import RepositorioIndisponivel
    api.amb.repo.falhar("transacao", RepositorioIndisponivel)
    assert codigo(api.get(f"/api/v1/mercado/produtos/{P}/balanco")) == (503, "DEPENDENCIA_INDISPONIVEL")


# ---------------------------------------------------------------- formato de erro e correlação (§7.1, §10)

def test_formato_do_erro_rfc7807(api):
    api.amb.cenario_e()
    rota = f"/api/v1/mercado/sugestoes-leilao/{s1(api)}/aceites"
    api.amb.oferta(OF_W, hora="12:30")
    api.amb.em("13:00")
    resposta = api.post(rota + "?x=1", empresa=FORN_X, escopo="SUPPLIER", corpo={"fornecimentoId": OF_X,
                                                                                 "quantidade": 1},
                        headers={"X-Correlation-Id": "teste-mrc-09"})
    dados = corpo(resposta)
    assert set(dados) == {"type", "title", "status", "codigo", "detail", "instance", "correlationId", "timestamp"}
    assert dados["type"] == "about:blank"
    assert dados["instance"] == rota
    assert (dados["correlationId"], resposta.headers["x-correlation-id"]) == ("teste-mrc-09", "teste-mrc-09")
    assert dados["timestamp"] == "2026-11-20T13:00:00.000Z"


def test_conflito_de_sugestao_encerrada_com_titulo_e_detalhe_do_catalogo(api):
    api.amb.cenario_e()
    sugestao = s1(api)
    api.amb.oferta(OF_W, hora="12:30")
    resposta = api.post(f"/api/v1/mercado/sugestoes-leilao/{sugestao}/aceites", empresa=FORN_X, escopo="SUPPLIER",
                        corpo={"fornecimentoId": OF_X, "quantidade": 1})
    dados = corpo(resposta)
    assert (resposta.status_code, dados["title"], dados["codigo"], dados["detail"]) == (
        409, "Conflito", "SUGESTAO_ENCERRADA", "A sugestão de leilão não aceita mais mudanças.")


@pytest.mark.parametrize("recebido,ecoa", [("teste-1.a_B", True), ("x" * 128, True), ("", False),
                                           ("com espaco", False), ("x" * 129, False)])
def test_correlacao(api, recebido, ecoa):
    resposta = api.cliente.get("/health", headers={"X-Correlation-Id": recebido})
    devolvido = resposta.headers["x-correlation-id"]
    assert (devolvido == recebido) is ecoa
    if not ecoa:
        assert len(devolvido) == 36


def test_rota_inexistente_usa_resposta_padrao(api):
    resposta = api.cliente.get("/api/v1/mercado/inexistente")
    assert resposta.status_code == 404 and "x-correlation-id" in resposta.headers
    assert api.cliente.get("/openapi.json").status_code == 200
