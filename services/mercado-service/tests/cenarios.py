"""Massa de teste comum do §8.0 e montagem dos cenários S, E e L sobre os dublês."""
import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from app.contratos import PRODUTORES
from app.dominio import ErroMercado, Mercado
from app.jsonio import dumps, instante, ler_instante
from app.portas import ConfigMercado
from tests.dubles import EventBusMemoria, RelogioFalso, RepositorioMemoria

D = Decimal

COMP_A = "30000000-0000-4000-8000-000000000001"
FORN_X = "30000000-0000-4000-8000-000000000002"
FORN_Y = "30000000-0000-4000-8000-000000000003"
COMP_B = "30000000-0000-4000-8000-000000000004"
FORN_Z = "30000000-0000-4000-8000-000000000005"

P = "35000000-0000-4000-8000-000000000001"
Q = "35000000-0000-4000-8000-000000000002"

OF_X = "34000000-0000-4000-8000-000000000001"
OF_Y = "34000000-0000-4000-8000-000000000002"
OF_Q = "34000000-0000-4000-8000-000000000003"
OF_W = "34000000-0000-4000-8000-000000000004"

DEM_A = "36000000-0000-4000-8000-000000000001"
DEM_B = "36000000-0000-4000-8000-000000000002"
DEM_X = "36000000-0000-4000-8000-000000000009"

ANAPOLIS = {"cidade": "Anápolis", "uf": "GO"}
GOIANIA = {"cidade": "Goiânia", "uf": "GO"}
RIO_VERDE = {"cidade": "Rio Verde", "uf": "GO"}
JATAI = {"cidade": "Jataí", "uf": "GO"}

OFERTAS = {
    OF_X: (FORN_X, P, "8.00", "60.00", RIO_VERDE, "11:40"),
    OF_Y: (FORN_Y, P, "7.50", "40.00", JATAI, "11:45"),
    OF_Q: (FORN_Z, Q, "10.00", "10.00", None, "11:50"),
    OF_W: (FORN_Z, P, "8.20", "200.00", GOIANIA, "12:30"),
}

RAIZ = Path(__file__).resolve().parents[1]
CONTRATOS = Path(os.environ.get("MERCADO_CONTRATOS_DIR", RAIZ.parents[1] / "docs" / "mercado" / "contratos"))
if os.environ.get("MERCADO_CONTRATOS_DIR") and not (CONTRATOS / "modo_negociacao_definido.schema.json").exists():
    # Na imagem (etapa test) os contratos são obrigatórios: sem eles a suíte falha em vez de pular os testes.
    raise RuntimeError(f"MERCADO_CONTRATOS_DIR={CONTRATOS} não contém os contratos de docs/mercado/contratos")


def t(hora: str) -> datetime:
    """`"12:00"`, `"12:59:59.999"` (dia 2026-11-20, UTC) ou um instante ISO completo."""
    if "T" in hora:
        return ler_instante(hora)
    if len(hora) == 5:
        hora += ":00"
    return ler_instante(f"2026-11-20T{hora}Z")


def json_publicado(mensagem: dict):
    """A mensagem como o broker a receberia (JSON), para validar contra os schemas."""
    return json.loads(dumps(mensagem))


def _formatos() -> FormatChecker:
    formatos = FormatChecker()

    @formatos.checks("date-time", raises=ValueError)
    def _instante(valor):
        if not isinstance(valor, str):
            return True
        if "T" not in valor.upper() or len(valor) < 20:
            return False
        return ler_instante(valor) is not None

    return formatos


def validar_schema(topico: str, mensagem: dict):
    schema = json.loads((CONTRATOS / f"{topico}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=_formatos()).validate(json_publicado(mensagem))


def exemplo(nome: str) -> dict:
    return json.loads((CONTRATOS / f"{nome}.exemplo.json").read_text(encoding="utf-8"))


def contratos_disponiveis() -> bool:
    return (CONTRATOS / "modo_negociacao_definido.schema.json").exists()


def envelope(topico: str, payload, *, agregado=None, event_id=None, correlacao="teste-mrc", timestamp=None,
             source=None) -> dict:
    return {
        "eventId": event_id or str(uuid4()), "eventType": topico, "eventVersion": "1.0",
        "timestamp": timestamp or "2026-11-20T12:00:00.000Z", "source": source or PRODUTORES[topico],
        "correlationId": correlacao, "aggregateId": agregado, "payload": payload,
    }


def erro(funcao, *args, **kwargs) -> str:
    try:
        funcao(*args, **kwargs)
    except ErroMercado as exc:
        return exc.codigo
    raise AssertionError("Era esperado um ErroMercado")


class Ambiente:
    def __init__(self, config: ConfigMercado = ConfigMercado(), agora: str = "12:00"):
        self.repo = RepositorioMemoria()
        self.bus = EventBusMemoria(self.repo)
        self.clock = RelogioFalso(t(agora))
        self.config = config
        self.mercado = Mercado(self.repo, self.bus, self.clock, config)

    # ------------------------------------------------------------ relógio e consultas de apoio

    def em(self, hora: str | None):
        if hora is not None:
            self.clock.agora = t(hora)

    def publicados(self, topico: str | None = None) -> list[dict]:
        if topico is None:
            return [mensagem for _, _, mensagem in self.bus.publicados]
        return self.bus.do_topico(topico)

    def sugestao_ativa(self, produto: str = P) -> dict | None:
        with self.repo.transacao():
            return self.repo.obter_sugestao_ativa(produto)

    def sugestao(self, sugestao_id: str) -> dict:
        with self.repo.transacao():
            return self.repo.obter_sugestao(sugestao_id)

    # ------------------------------------------------------------ eventos consumidos

    def receber(self, topico: str, payload, *, agregado=None, hora=None, **opcoes) -> str:
        self.em(hora)
        mensagem = envelope(topico, payload, agregado=agregado, timestamp=instante(self.clock.agora), **opcoes)
        return self.mercado.consumir(topico, mensagem)

    def produto(self, produto=P, nome="Milho em grão", unidade="TON", hora="11:00", topico="produto_cadastrado"):
        campo = "cadastradoEm" if topico == "produto_cadastrado" else "atualizadoEm"
        payload = {"produtoId": produto, "nome": nome, "unidade": unidade, "categoria": "GRAOS",
                   campo: instante(t(hora))}
        return self.receber(topico, payload, agregado=produto, hora=hora)

    def oferta(self, fornecimento, fornecedor=None, produto=None, preco=None, quantidade=None, origem="padrao",
               hora=None, topico="fornecimento_criado", unidade="TON", **extras):
        base = OFERTAS.get(fornecimento, (fornecedor, produto, preco, quantidade, None, hora))
        fornecedor = fornecedor or base[0]
        produto = produto or base[1]
        preco = D(preco if preco is not None else base[2])
        quantidade = D(quantidade if quantidade is not None else base[3])
        origem = base[4] if origem == "padrao" else origem
        hora = hora or base[5]
        campo = "criadoEm" if topico == "fornecimento_criado" else "atualizadoEm"
        payload = {"fornecimentoId": fornecimento, "fornecedorId": fornecedor, "produtoId": produto,
                   "unidade": unidade, "preco": preco, "quantidadeDisponivel": quantidade, "origem": origem,
                   campo: instante(t(hora)), **extras}
        return self.receber(topico, payload, agregado=fornecimento, hora=hora)

    def atualizar_oferta(self, fornecimento, hora, **mudancas):
        return self.oferta(fornecimento, hora=hora, topico="fornecimento_atualizado", **mudancas)

    def estoque(self, fornecimento, quantidade, hora, operacao=None):
        payload = {"fornecimentoId": fornecimento, "quantidadeDisponivel": D(quantidade),
                   "atualizadoEm": instante(t(hora))}
        if operacao:
            payload["operacao"] = operacao
        return self.receber("estoque_atualizado", payload, agregado=fornecimento, hora=hora)

    def payload_demanda(self, demanda, comprador, produto, quantidade, *, preco_maximo=None, destino=None,
                        prazo=None, tipo="UNICA", unidade="TON", hora="11:55"):
        return {"demandaId": demanda, "compradorId": comprador, "produtoId": produto, "unidade": unidade,
                "quantidade": D(quantidade), "precoMaximo": None if preco_maximo is None else D(preco_maximo),
                "destino": destino or ANAPOLIS, "prazoDesejado": prazo, "tipo": tipo, "frequencia": None,
                "criadoEm": instante(t(hora))}

    def demanda(self, demanda, comprador, produto, quantidade, *, hora="11:55", **campos):
        payload = self.payload_demanda(demanda, comprador, produto, quantidade, hora=hora, **campos)
        return self.receber("demanda_criada", payload, agregado=demanda, hora=hora)

    def recorrente(self, origem, ocorrencia, comprador, produto, quantidade, *, hora="11:56", preco_maximo=None,
                   destino=None, prazo=None, unidade="TON"):
        payload = {"demandaId": origem, "ocorrenciaId": ocorrencia, "compradorId": comprador, "produtoId": produto,
                   "unidade": unidade, "quantidade": D(quantidade),
                   "precoMaximo": None if preco_maximo is None else D(preco_maximo), "destino": destino or ANAPOLIS,
                   "prazoDesejado": prazo, "geradoEm": instante(t(hora))}
        return self.receber("demanda_recorrente_gerada", payload, agregado=origem, hora=hora)

    def cancelar(self, demanda, comprador, hora="12:30", **opcoes):
        payload = {"demandaId": demanda, "compradorId": comprador, "motivo": "Compra feita por outro canal",
                   "canceladoEm": instante(t(hora))}
        return self.receber("demanda_cancelada", payload, agregado=demanda, hora=hora, **opcoes)

    def fechar(self, negociacao, status, alocacoes=(), *, tipo=None, comprador=None, demanda=None, hora=None,
               produto=P):
        if tipo is None:
            with self.repo.transacao():
                registrada = self.repo.obter_negociacao(negociacao)
            tipo = "LEILAO" if registrada and registrada["modo"] == "LEILAO_DIRETO" else "VENDA_DIRETA"
        payload = {"negociacaoId": negociacao, "tipo": tipo, "status": status, "compradorId": comprador,
                   "demandaId": demanda, "produtoId": produto,
                   "alocacoes": [{"fornecimentoId": fornecimento, "fornecedorId": FORN_X, "pedidoId": str(uuid4()),
                                  "quantidade": D(quantidade), "valorUnitario": D("1.00"), "valorTotal": D("1.00")}
                                 for fornecimento, quantidade in alocacoes]}
        return self.receber("negociacao_fechada", payload, agregado=negociacao, hora=hora)

    # ------------------------------------------------------------ comandos (API-07, API-11 a API-13)

    def direta(self, empresa, demanda, fornecimento, quantidade, *, hora=None, compra_imediata=False,
               correlacao="teste-mrc-05", idempotencia=None):
        self.em(hora)
        return self.mercado.abrir_direta(empresa, demanda, fornecimento, D(quantidade), compra_imediata,
                                         correlacao, idempotencia)

    def aceitar(self, empresa, sugestao, fornecimento, quantidade, *, hora=None, correlacao="teste-mrc-09"):
        self.em(hora)
        return self.mercado.aceitar(empresa, sugestao, fornecimento, D(quantidade), correlacao)

    def alterar(self, empresa, sugestao, fornecimento, quantidade, *, hora=None):
        self.em(hora)
        return self.mercado.alterar_aceite(empresa, sugestao, fornecimento, D(quantidade), "teste-mrc-09")

    def remover(self, empresa, sugestao, fornecimento, *, hora=None):
        self.em(hora)
        return self.mercado.remover_aceite(empresa, sugestao, fornecimento, "teste-mrc-09")

    def prazos(self, hora: str) -> int:
        self.em(hora)
        return self.mercado.executar_prazos()

    # ------------------------------------------------------------ cenários do §8.0

    def cenario_s(self):
        self.produto(P, "Milho em grão", hora="11:00")
        self.produto(Q, "Soja", hora="11:00")
        for oferta in (OF_X, OF_Y, OF_Q):
            self.oferta(oferta)
        self.dem_a()
        self.em("12:00")
        return self

    def dem_a(self):
        return self.demanda(DEM_A, COMP_A, P, "50.00", preco_maximo="9.00", destino=ANAPOLIS,
                            prazo="2026-11-30T23:59:59Z", hora="11:55")

    def dem_b(self):
        return self.demanda(DEM_B, COMP_B, P, "80.00", destino=GOIANIA, hora="11:57")

    def cenario_e(self):
        self.cenario_s()
        self.dem_b()
        self.em("12:00")
        return self

    def cenario_l(self) -> str:
        """Devolve o `negociacaoId` do leilão L1, fechado às 12:10 (T1)."""
        self.cenario_e()
        s1 = self.sugestao_ativa()["sugestaoId"]
        self.aceitar(FORN_X, s1, OF_X, "60.00", hora="12:00")
        resposta = self.aceitar(FORN_Y, s1, OF_Y, "40.00", hora="12:10", correlacao="teste-mrc-10")
        return resposta["negociacaoId"]
