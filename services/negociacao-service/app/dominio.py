"""Regras da negociação direta (§6.2, §9) e o serviço que as aplica sobre as portas (§5.1)."""
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app import contratos
from app.contratos import EventoInvalido, q2
from app.portas import Clock, EventBus, NegociacaoRepositorio, RepositorioIndisponivel

log = logging.getLogger(__name__)
PRAZO_RESPOSTA = timedelta(hours=48)
VALIDADE_IDEMPOTENCIA = timedelta(hours=24)
FINAIS = ("ENCERRADO", "DESERTO", "RECUSADO", "EXPIRADO")


class ErroNegocio(Exception):
    """Erro do catálogo do §10, respondido pela API com o `codigo`."""

    def __init__(self, codigo):
        super().__init__(codigo)
        self.codigo = codigo


@dataclass(frozen=True)
class Ator:
    usuario: str
    empresa: str
    escopo: str


@dataclass(frozen=True)
class Idempotencia:
    """RN-18: escopo da chave, hash do corpo da requisição e status da resposta de sucesso."""
    escopo: str
    hash_corpo: str
    status: int


# ---------------------------------------------------------------- regras puras

def utc(instante: datetime) -> datetime:
    return instante.replace(tzinfo=timezone.utc) if instante.tzinfo is None else instante.astimezone(timezone.utc)


def participante(negociacao: dict, ator: Ator) -> bool:
    return ((ator.escopo == "BUYER" and ator.empresa == negociacao["compradorId"])
            or (ator.escopo == "SUPPLIER" and ator.empresa == negociacao["fornecedorId"]))


def visivel(negociacao: dict, ator: Ator) -> bool:
    """RN-16: leilão visível a todos; direta só aos participantes."""
    return negociacao["modo"] == "LEILAO_DIRETO" or participante(negociacao, ator)


def aceitando(negociacao: dict, agora: datetime) -> bool:
    """RN-12: o prazo vencido já recusa ações antes de `executar_prazos` mudar o status."""
    return negociacao["status"] == "ABERTO" and agora < negociacao["expiraEm"]


def acoes_permitidas(negociacao: dict, ator: Ator, agora: datetime) -> list[str]:
    """RN-17 na negociação direta; na compra imediata ninguém age (RN-24)."""
    if negociacao["compraImediata"] or not aceitando(negociacao, agora) or not participante(negociacao, ator):
        return []
    da_vez = ator.escopo == negociacao["vez"]
    acoes = ["PROPOR"] if da_vez else []
    if da_vez and negociacao["reservaSituacao"] == "CONFIRMADA":
        acoes.append("ACEITAR")
    return acoes + ["RECUSAR"]


def alocacao(negociacao: dict, preco) -> dict:
    """RN-13: a alocação única da direta."""
    return {"fornecimentoId": negociacao["fornecimentoId"], "fornecedorId": negociacao["fornecedorId"],
            "pedidoId": negociacao["pedidoId"], "quantidade": q2(negociacao["quantidade"]),
            "valorUnitario": q2(preco), "valorTotal": q2(negociacao["quantidade"] * preco)}


def resumo(negociacao: dict) -> dict:
    direta = negociacao["modo"] == "VENDA_DIRETA"
    return {"negociacaoId": negociacao["negociacaoId"], "modo": negociacao["modo"], "status": negociacao["status"],
            "produtoId": negociacao["produtoId"], "dataInicio": None if direta else negociacao["dataInicio"],
            "dataFim": negociacao["dataFim"], "expiraEm": negociacao["expiraEm"], "criadoEm": negociacao["criadoEm"]}


def detalhe_direta(negociacao: dict, ator: Ator, agora: datetime) -> dict:
    n = negociacao
    return {"negociacaoId": n["negociacaoId"], "modo": n["modo"], "status": n["status"], "produtoId": n["produtoId"],
            "fornecimentoId": n["fornecimentoId"], "fornecedorId": n["fornecedorId"],
            "compradorId": n["compradorId"], "quantidade": q2(n["quantidade"]), "precoOferta": q2(n["precoOferta"]),
            "precoAtual": q2(n["precoAtual"]), "vez": n["vez"], "compraImediata": n["compraImediata"],
            "expiraEm": n["expiraEm"], "reservaSituacao": n["reservaSituacao"], "totalPropostas": n["totalPropostas"],
            "criadoEm": n["criadoEm"], "encerradoEm": n["encerradoEm"], "agora": agora,
            "acoesPermitidas": acoes_permitidas(n, ator, agora)}


def proposta(p: dict) -> dict:
    return {"propostaId": p["propostaId"], "negociacaoId": p["negociacaoId"], "autor": p["autor"],
            "precoUnitario": q2(p["precoUnitario"]), "sequencia": p["sequencia"], "criadoEm": p["criadoEm"]}


# ---------------------------------------------------------------- serviço

@dataclass
class _Operacao:
    agora: datetime
    correlacao: str
    eventos: list = field(default_factory=list)


class Negociacoes:
    def __init__(self, repositorio: NegociacaoRepositorio, event_bus: EventBus, clock: Clock):
        self.repo = repositorio
        self.bus = event_bus
        self.clock = clock

    def agora(self) -> datetime:
        return utc(self.clock.now())

    # ------------------------------------------------------------ consumo (RN-19, RN-20)

    def handler(self, topico: str):
        def tratar(mensagem: dict) -> None:
            self.consumir(topico, mensagem)
        return tratar

    def consumir(self, topico: str, mensagem) -> str:
        try:
            evento = contratos.validar_evento(topico, mensagem)
        except EventoInvalido as erro:
            return self._dlt(topico, mensagem, "EVENTO_INVALIDO", erro.detalhe, "", 1)
        aplicar = getattr(self, "_" + topico)
        pilha, detalhe = "", ""
        for tentativa in (1, 2, 3):
            try:
                with self.repo.transacao():
                    if self.repo.evento_processado(evento.source, evento.event_id):
                        return "duplicado"
                    op = _Operacao(self.agora(), evento.correlation_id)
                    resultado = aplicar(op, evento.payload)
                    self.repo.registrar_evento_processado(evento.source, evento.event_id, topico)
                    self._enviar(op)
                return resultado
            except RepositorioIndisponivel:
                raise
            except Exception as exc:
                pilha = traceback.format_exc()
                detalhe = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.warning("Falha ao processar mensagem (execução %s de 3)", tentativa,
                            extra={"correlationId": evento.correlation_id, "eventId": evento.event_id})
                if tentativa < 3:
                    self.clock.sleep(tentativa)
        return self._dlt(topico, mensagem, "PROCESSAMENTO_FALHOU", detalhe, pilha, 3)

    def _dlt(self, topico, mensagem, erro, detalhe, pilha, tentativa) -> str:
        chave, envelope = contratos.evento_dlt(topico, mensagem, erro, detalhe, pilha, tentativa, self.agora())
        log.warning("Mensagem enviada para negociacao.dlt: %s", erro,
                    extra={"correlationId": envelope["correlationId"]})
        try:
            # a outbox só grava dentro de uma transação; banco fora do ar propaga e a mensagem é reentregue
            with self.repo.transacao():
                self.bus.publish(contratos.TOPICO_DLT, chave, envelope)
        except RepositorioIndisponivel:
            raise
        except Exception:
            log.exception("Falha ao publicar em negociacao.dlt", extra={"correlationId": envelope["correlationId"]})
        return "dlt"

    def _enviar(self, op: _Operacao):
        """Último passo da transação (§5.1): validar → alterar → publicar."""
        for topico, chave, envelope in op.eventos:
            self.bus.publish(topico, chave, envelope)

    def _publicar(self, op: _Operacao, topico: str, negociacao: dict, payload: dict):
        chave = negociacao["negociacaoId"]
        op.eventos.append((topico, chave, contratos.envelope(topico, chave, payload, op.agora, op.correlacao)))

    def _fechar(self, op: _Operacao, negociacao: dict, status: str, alocacoes: list):
        negociacao.update(status=status, encerradoEm=op.agora)
        self.repo.atualizar_negociacao(negociacao)
        self._publicar(op, "negociacao_fechada", negociacao, contratos.negociacao_fechada(negociacao, alocacoes))

    def _modo_negociacao_definido(self, op: _Operacao, p: dict) -> str:
        """RN-02 e RN-03."""
        if p["modo"] != "VENDA_DIRETA" or self.repo.obter_negociacao(p["negociacaoId"]) is not None:
            return "ignorado"
        negociacao = {
            "negociacaoId": p["negociacaoId"], "modo": "VENDA_DIRETA", "status": "ABERTO",
            "produtoId": p["produtoId"], "dataInicio": op.agora, "dataFim": None, "quantidadeTotal": None,
            "precoInicial": None, "incrementoMinimo": None, "incrementoMinimoPercentual": None,
            "melhorLanceId": None, "totalLances": 0, "compradorId": p["compradorId"],
            "fornecimentoId": p["fornecimentoId"], "fornecedorId": p["fornecedorId"], "quantidade": p["quantidade"],
            "precoOferta": p["precoUnitario"], "precoAtual": p["precoUnitario"], "vez": "BUYER",
            "compraImediata": p["compraImediata"], "expiraEm": op.agora + PRAZO_RESPOSTA,
            "reservaSituacao": "PENDENTE", "pedidoId": None, "totalPropostas": 1, "correlationId": op.correlacao,
            "criadoEm": op.agora, "encerradoEm": None,
        }
        self.repo.inserir_negociacao(negociacao)
        self.repo.inserir_proposta({
            "propostaId": str(uuid4()), "negociacaoId": p["negociacaoId"], "autor": "SUPPLIER",
            "empresaId": p["fornecedorId"], "precoUnitario": p["precoUnitario"], "sequencia": 1,
            "criadoEm": op.agora,
        })
        self._publicar(op, "pedido_temporario_solicitado", negociacao,
                       contratos.pedido_temporario_solicitado(negociacao))
        return "aplicado"

    def _pedido_atualizado(self, op: _Operacao, p: dict) -> str:
        """RN-04, RN-14 e RN-24 na negociação direta."""
        # negociacaoId do Pedidos é texto livre: o que não é UUID não é negociação deste serviço
        if p["reservaSituacao"] not in ("CONFIRMADA", "RECUSADA") or not contratos.UUID_TEXTO.fullmatch(
                p["negociacaoId"]):
            return "ignorado"
        negociacao = self.repo.obter_negociacao(p["negociacaoId"], bloquear=True)
        if (negociacao is None or negociacao["status"] in FINAIS or negociacao["reservaSituacao"] != "PENDENTE"
                or negociacao["fornecimentoId"] != p["fornecimentoId"]):
            return "ignorado"
        if p["reservaSituacao"] == "RECUSADA":
            negociacao["reservaSituacao"] = "RECUSADA"
            self._fechar(op, negociacao, "RECUSADO", [])
            return "aplicado"
        negociacao.update(reservaSituacao="CONFIRMADA", pedidoId=p["pedidoId"])
        if negociacao["compraImediata"]:
            self._fechar(op, negociacao, "ENCERRADO", [alocacao(negociacao, negociacao["precoOferta"])])
        else:
            self.repo.atualizar_negociacao(negociacao)
        return "aplicado"

    # ------------------------------------------------------------ consultas (RF-11, RF-12)

    def _visivel(self, ator: Ator, negociacao_id: str) -> dict:
        negociacao = self.repo.obter_negociacao(negociacao_id)
        if negociacao is None or not visivel(negociacao, ator):
            raise ErroNegocio("NEGOCIACAO_NAO_ENCONTRADA")
        return negociacao

    def listar(self, ator: Ator, modo, status, pagina: int, tamanho: int) -> dict:
        with self.repo.transacao():
            itens, total = self.repo.listar_negociacoes(ator.empresa, ator.escopo, modo, status,
                                                        (pagina - 1) * tamanho, tamanho)
        return {"items": [resumo(n) for n in itens], "page": pagina, "size": tamanho, "total": total}

    def detalhe(self, ator: Ator, negociacao_id: str) -> dict:
        with self.repo.transacao():
            negociacao = self._visivel(ator, negociacao_id)
        # shortcut: só a projeção da direta; o detalhe do leilão entra com a abertura do leilão
        return detalhe_direta(negociacao, ator, self.agora())

    def propostas(self, ator: Ator, negociacao_id: str, pagina: int, tamanho: int) -> dict:
        with self.repo.transacao():
            negociacao = self._visivel(ator, negociacao_id)
            if negociacao["modo"] != "VENDA_DIRETA":
                raise ErroNegocio("MODO_INCOMPATIVEL")
            itens, total = self.repo.listar_propostas(negociacao_id, (pagina - 1) * tamanho, tamanho)
        return {"items": [proposta(p) for p in itens], "page": pagina, "size": tamanho, "total": total}

    # ------------------------------------------------------------ ações da direta (RF-07 a RF-09, RN-18)

    def propor(self, ator: Ator, negociacao_id: str, preco, correlacao: str,
               idempotencia: Idempotencia | None = None) -> dict:
        def regra(op, negociacao):
            self._na_vez(negociacao, ator)
            if preco > negociacao["precoOferta"]:
                raise ErroNegocio("PROPOSTA_ACIMA_DA_OFERTA")
            nova = {"propostaId": str(uuid4()), "negociacaoId": negociacao_id, "autor": ator.escopo,
                    "empresaId": ator.empresa, "precoUnitario": preco,
                    "sequencia": negociacao["totalPropostas"] + 1, "criadoEm": op.agora}
            self.repo.inserir_proposta(nova)
            negociacao.update(vez="SUPPLIER" if ator.escopo == "BUYER" else "BUYER", precoAtual=preco,
                              expiraEm=op.agora + PRAZO_RESPOSTA, totalPropostas=nova["sequencia"])
            self.repo.atualizar_negociacao(negociacao)
            return proposta(nova)
        return self._agir(ator, negociacao_id, correlacao, idempotencia, regra)

    def aceitar(self, ator: Ator, negociacao_id: str, correlacao: str,
                idempotencia: Idempotencia | None = None) -> dict:
        def regra(op, negociacao):
            self._na_vez(negociacao, ator)
            if negociacao["reservaSituacao"] != "CONFIRMADA":
                raise ErroNegocio("RESERVA_PENDENTE")
            self._fechar(op, negociacao, "ENCERRADO", [alocacao(negociacao, negociacao["precoAtual"])])
            return detalhe_direta(negociacao, ator, op.agora)
        return self._agir(ator, negociacao_id, correlacao, idempotencia, regra)

    def recusar(self, ator: Ator, negociacao_id: str, correlacao: str,
                idempotencia: Idempotencia | None = None) -> dict:
        def regra(op, negociacao):
            self._fechar(op, negociacao, "RECUSADO", [])
            return detalhe_direta(negociacao, ator, op.agora)
        return self._agir(ator, negociacao_id, correlacao, idempotencia, regra)

    @staticmethod
    def _na_vez(negociacao: dict, ator: Ator):
        if ator.escopo != negociacao["vez"]:
            raise ErroNegocio("FORA_DA_VEZ")

    def _agir(self, ator, negociacao_id, correlacao, idempotencia, regra) -> dict:
        """Ordem do §10 a partir do passo 6; a regra da rota valida, altera e devolve o corpo da resposta."""
        with self.repo.transacao():
            op = _Operacao(self.agora(), correlacao)
            negociacao = self.repo.obter_negociacao(negociacao_id, bloquear=True)
            if negociacao is None or not visivel(negociacao, ator):
                raise ErroNegocio("NEGOCIACAO_NAO_ENCONTRADA")
            if negociacao["modo"] != "VENDA_DIRETA":
                raise ErroNegocio("MODO_INCOMPATIVEL")
            # o bloqueio da negociação já serializa a mesma chave entre as réplicas
            memorizada = self._replay(idempotencia, op.agora)
            if memorizada is not None:
                return memorizada
            if not aceitando(negociacao, op.agora):
                raise ErroNegocio("NEGOCIACAO_ENCERRADA")
            if negociacao["compraImediata"]:
                raise ErroNegocio("COMPRA_IMEDIATA")
            corpo = regra(op, negociacao)
            if idempotencia is not None:
                self.repo.salvar_idempotencia(idempotencia.escopo, {
                    "hashCorpo": idempotencia.hash_corpo, "status": idempotencia.status, "corpo": corpo,
                    "location": None, "criadoEm": op.agora})
            self._enviar(op)
        return corpo

    def _replay(self, idempotencia: Idempotencia | None, agora: datetime) -> dict | None:
        registro = None if idempotencia is None else self.repo.obter_idempotencia(idempotencia.escopo)
        if registro is None or registro["criadoEm"] + VALIDADE_IDEMPOTENCIA <= agora:
            return None
        if registro["hashCorpo"] != idempotencia.hash_corpo:
            raise ErroNegocio("IDEMPOTENCIA_CONFLITO")
        return registro["corpo"]

    # ------------------------------------------------------------ prazos (RF-10, RN-21)

    def executar_prazos(self) -> int:
        agora = self.agora()
        with self.repo.transacao():
            vencidas = self.repo.listar_vencidas(agora, 100)
        mudancas = 0
        for negociacao_id in vencidas:
            with self.repo.transacao():
                negociacao = self.repo.obter_negociacao(negociacao_id, bloquear=True)
                # shortcut: só a direta expira; o encerramento do leilão em dataFim (RN-10) entra com a abertura
                # do leilão, e até lá um leilão vencido é deixado como está
                if (negociacao is None or negociacao["modo"] != "VENDA_DIRETA" or negociacao["status"] != "ABERTO"
                        or agora < negociacao["expiraEm"]):
                    continue
                op = _Operacao(agora, negociacao["correlationId"])
                self._fechar(op, negociacao, "EXPIRADO", [])
                self._enviar(op)
                mudancas += 1
        return mudancas
