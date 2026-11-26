"""Regras da negociação direta (§6.2, §9) e o serviço que as aplica sobre as portas (§5.1)."""
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP
from uuid import uuid4

from app import contratos
from app.contratos import Q2, Q4, EventoInvalido, q2
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


# ---------------------------------------------------------------- leilão: regras puras

def incremento_minimo(preco_inicial, percentual):
    """RN-05: `max(0.01, P' × percentual / 100)`, 2 casas, ROUND_HALF_UP (RN-22 da ESP-MRC-001)."""
    return max(Q2, q2(preco_inicial * percentual / 100))


def participacoes(valores: list) -> list:
    """RN-05: maior resto, piso de 0.0001 e soma 1.0000 (RN-23 da ESP-MRC-001). Empate: menor `ordem`."""
    total = sum(valores)
    exatos = [valor / total for valor in valores]
    truncados = [exato.quantize(Q4, rounding=ROUND_DOWN) for exato in exatos]
    partes = [max(Q4, truncado) for truncado in truncados]
    restos = [exato - truncado for exato, truncado in zip(exatos, truncados)]
    diferenca = 1 - sum(partes)
    if diferenca > 0:
        for i in sorted(range(len(partes)), key=lambda i: (-restos[i], i))[:int(diferenca / Q4)]:
            partes[i] += Q4
    elif diferenca < 0:
        falta = -diferenca
        for i in sorted(range(len(partes)), key=lambda i: (-partes[i], i)):
            retirada = min(falta, partes[i] - Q4)
            partes[i] -= retirada
            falta -= retirada
    return partes


def recalcular_lote(negociacao: dict, lote: list[dict]) -> None:
    """RN-05: quantidade, preço inicial, incremento e participações só com as ofertas confirmadas."""
    preco = q2(sum(o["valorItem"] for o in lote))
    for oferta, parte in zip(lote, participacoes([o["valorItem"] for o in lote])):
        oferta["participacao"] = parte
    negociacao.update(quantidadeTotal=q2(sum(o["quantidade"] for o in lote)), precoInicial=preco,
                      incrementoMinimo=incremento_minimo(preco, negociacao["incrementoMinimoPercentual"]))


def ratear(valor, preco_inicial, lote: list[dict]) -> list[dict]:
    """RN-11: proporção exata do `valorItem`, centavos para baixo; a sobra vai à oferta de maior valor (empate: menor
    `ordem`). O lote vem em ordem de `ordem`."""
    totais = [(valor * o["valorItem"] / preco_inicial).quantize(Q2, rounding=ROUND_DOWN) for o in lote]
    maior = max(range(len(lote)), key=lambda i: (lote[i]["valorItem"], -i))
    totais[maior] += valor - sum(totais)
    return [{"fornecimentoId": o["fornecimentoId"], "fornecedorId": o["fornecedorId"], "pedidoId": o["pedidoId"],
             "quantidade": q2(o["quantidade"]), "valorUnitario": q2(total / o["quantidade"]), "valorTotal": q2(total)}
            for o, total in zip(lote, totais)]


def fornecedor_do_leilao(ofertas: list[dict], ator: Ator) -> bool:
    return any(o["fornecedorId"] == ator.empresa for o in ofertas)


def sigilo(comprador_id, ofertas: list[dict], ator: Ator):
    """RN-09: o comprador só aparece para ele mesmo e para os fornecedores do leilão."""
    if comprador_id is None or comprador_id == ator.empresa or fornecedor_do_leilao(ofertas, ator):
        return comprador_id
    return "OCULTO"


def leilao_aberto(negociacao: dict, agora: datetime) -> bool:
    """RN-06: `dataFim` vencido já recusa lances antes do encerramento mudar o status."""
    return negociacao["status"] == "ABERTO" and agora < negociacao["dataFim"]


def acoes_leilao(negociacao: dict, ofertas: list[dict], ator: Ator, agora: datetime) -> list[str]:
    """RN-17 no leilão."""
    pode = leilao_aberto(negociacao, agora) and ator.escopo == "BUYER" and not fornecedor_do_leilao(ofertas, ator)
    return ["DAR_LANCE"] if pode else []


def lance(l: dict) -> dict:
    """Projeção "Lance" sem máscara: resposta da API-07 e payload do `lance_realizado`."""
    return {"lanceId": l["lanceId"], "negociacaoId": l["negociacaoId"], "compradorId": l["compradorId"],
            "valor": q2(l["valor"]), "sequencia": l["sequencia"], "criadoEm": l["criadoEm"]}


def lance_com_sigilo(l: dict, ofertas: list[dict], ator: Ator) -> dict:
    return {**lance(l), "compradorId": sigilo(l["compradorId"], ofertas, ator)}


def oferta_leilao(o: dict) -> dict:
    return {"negociacaoId": o["negociacaoId"], "fornecimentoId": o["fornecimentoId"], "fornecedorId": o["fornecedorId"],
            "quantidade": q2(o["quantidade"]), "precoUnitario": q2(o["precoUnitario"]), "valorItem": q2(o["valorItem"]),
            "participacao": o["participacao"].quantize(Q4, rounding=ROUND_HALF_UP), "situacao": o["situacao"]}


def detalhe_leilao(negociacao: dict, ofertas: list[dict], melhor: dict | None, ator: Ator, agora: datetime) -> dict:
    n = negociacao
    return {"negociacaoId": n["negociacaoId"], "modo": n["modo"], "status": n["status"], "produtoId": n["produtoId"],
            "dataInicio": n["dataInicio"], "dataFim": n["dataFim"], "quantidadeTotal": q2(n["quantidadeTotal"]),
            "precoInicial": q2(n["precoInicial"]), "incrementoMinimo": q2(n["incrementoMinimo"]),
            "melhorLance": None if melhor is None else lance_com_sigilo(melhor, ofertas, ator),
            "totalLances": n["totalLances"],
            "compradorId": sigilo(n["compradorId"], ofertas, ator), "ofertas": [oferta_leilao(o) for o in ofertas],
            "criadoEm": n["criadoEm"], "encerradoEm": n["encerradoEm"], "agora": agora,
            "acoesPermitidas": acoes_leilao(n, ofertas, ator, agora)}


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

    def _fechar(self, op: _Operacao, negociacao: dict, status: str, alocacoes: list, demanda_id=None):
        negociacao.update(status=status, encerradoEm=op.agora)
        self.repo.atualizar_negociacao(negociacao)
        self._publicar(op, "negociacao_fechada", negociacao,
                       contratos.negociacao_fechada(negociacao, alocacoes, demanda_id))

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
        """RN-04, RN-14 e RN-24 na negociação direta; no leilão, `_reserva_leilao`."""
        # negociacaoId do Pedidos é texto livre: o que não é UUID não é negociação deste serviço
        if p["reservaSituacao"] not in ("CONFIRMADA", "RECUSADA") or not contratos.UUID_TEXTO.fullmatch(
                p["negociacaoId"]):
            return "ignorado"
        negociacao = self.repo.obter_negociacao(p["negociacaoId"], bloquear=True)
        if negociacao is not None and negociacao["modo"] == "LEILAO_DIRETO":
            return self._reserva_leilao(op, negociacao, p)
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
            if negociacao["modo"] == "LEILAO_DIRETO":
                return detalhe_leilao(negociacao, self.repo.listar_ofertas_leilao(negociacao_id),
                                      self._melhor_lance(negociacao), ator, self.agora())
        return detalhe_direta(negociacao, ator, self.agora())

    def lances(self, ator: Ator, negociacao_id: str, pagina: int, tamanho: int) -> dict:
        with self.repo.transacao():
            negociacao = self._visivel(ator, negociacao_id)
            if negociacao["modo"] != "LEILAO_DIRETO":
                raise ErroNegocio("MODO_INCOMPATIVEL")
            itens, total = self.repo.listar_lances(negociacao_id, (pagina - 1) * tamanho, tamanho)
            ofertas = self.repo.listar_ofertas_leilao(negociacao_id)
        return {"items": [lance_com_sigilo(l, ofertas, ator) for l in itens], "page": pagina, "size": tamanho,
                "total": total}

    def _melhor_lance(self, negociacao: dict) -> dict | None:
        """O lance de `melhorLanceId`: cada lance aceito supera o anterior (RN-07), então é sempre o último."""
        if negociacao["melhorLanceId"] is None:
            return None
        [melhor], _ = self.repo.listar_lances(negociacao["negociacaoId"], negociacao["totalLances"] - 1, 1)
        return melhor

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
        return self._agir_direta(ator, negociacao_id, correlacao, idempotencia, regra)

    def aceitar(self, ator: Ator, negociacao_id: str, correlacao: str,
                idempotencia: Idempotencia | None = None) -> dict:
        def regra(op, negociacao):
            self._na_vez(negociacao, ator)
            if negociacao["reservaSituacao"] != "CONFIRMADA":
                raise ErroNegocio("RESERVA_PENDENTE")
            self._fechar(op, negociacao, "ENCERRADO", [alocacao(negociacao, negociacao["precoAtual"])])
            return detalhe_direta(negociacao, ator, op.agora)
        return self._agir_direta(ator, negociacao_id, correlacao, idempotencia, regra)

    def recusar(self, ator: Ator, negociacao_id: str, correlacao: str,
                idempotencia: Idempotencia | None = None) -> dict:
        def regra(op, negociacao):
            self._fechar(op, negociacao, "RECUSADO", [])
            return detalhe_direta(negociacao, ator, op.agora)
        return self._agir_direta(ator, negociacao_id, correlacao, idempotencia, regra)

    @staticmethod
    def _na_vez(negociacao: dict, ator: Ator):
        if ator.escopo != negociacao["vez"]:
            raise ErroNegocio("FORA_DA_VEZ")

    def _agir_direta(self, ator, negociacao_id, correlacao, idempotencia, regra) -> dict:
        def na_direta(op, negociacao):
            if not aceitando(negociacao, op.agora):
                raise ErroNegocio("NEGOCIACAO_ENCERRADA")
            if negociacao["compraImediata"]:
                raise ErroNegocio("COMPRA_IMEDIATA")
            return regra(op, negociacao)
        return self._agir(ator, negociacao_id, correlacao, idempotencia, "VENDA_DIRETA", na_direta)

    def _agir(self, ator, negociacao_id, correlacao, idempotencia, modo, regra) -> dict:
        """Ordem do §10 a partir do passo 6; a regra da rota valida, altera e devolve o corpo da resposta."""
        with self.repo.transacao():
            op = _Operacao(self.agora(), correlacao)
            negociacao = self.repo.obter_negociacao(negociacao_id, bloquear=True)
            if negociacao is None or not visivel(negociacao, ator):
                raise ErroNegocio("NEGOCIACAO_NAO_ENCONTRADA")
            if negociacao["modo"] != modo:
                raise ErroNegocio("MODO_INCOMPATIVEL")
            # o bloqueio da negociação já serializa a mesma chave entre as réplicas
            memorizada = self._replay(idempotencia, op.agora)
            if memorizada is not None:
                return memorizada
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

    # ------------------------------------------------------------ lance (RF-05)

    def dar_lance(self, ator: Ator, negociacao_id: str, corpo: tuple, correlacao: str,
                  idempotencia: Idempotencia | None = None) -> dict:
        """RN-08, RN-06 e RN-07, na ordem do §7.2, com o leilão bloqueado pelo `_agir`."""
        valor, demanda_id = corpo

        def regra(op, negociacao):
            ofertas = self.repo.listar_ofertas_leilao(negociacao_id)
            if fornecedor_do_leilao(ofertas, ator):
                raise ErroNegocio("CONFLITO_DE_INTERESSE")
            if not leilao_aberto(negociacao, op.agora):
                raise ErroNegocio("LEILAO_NAO_ABERTO")
            melhor = self._melhor_lance(negociacao)
            minimo = negociacao["precoInicial"] if melhor is None else melhor["valor"] + negociacao["incrementoMinimo"]
            if valor < minimo:
                raise ErroNegocio("LANCE_INSUFICIENTE")
            novo = {"lanceId": str(uuid4()), "negociacaoId": negociacao_id, "compradorId": ator.empresa,
                    "usuarioId": ator.usuario, "valor": valor, "demandaId": demanda_id,
                    "sequencia": negociacao["totalLances"] + 1, "criadoEm": op.agora}
            self.repo.inserir_lance(novo)
            negociacao.update(melhorLanceId=novo["lanceId"], totalLances=novo["sequencia"])
            self.repo.atualizar_negociacao(negociacao)
            self._publicar(op, "lance_realizado", negociacao, lance(novo))
            return lance(novo)
        return self._agir(ator, negociacao_id, correlacao, idempotencia, "LEILAO_DIRETO", regra)

    # ------------------------------------------------------------ prazos (RF-06, RF-10, RN-21)

    def executar_prazos(self) -> int:
        agora = self.agora()
        with self.repo.transacao():
            vencidas = self.repo.listar_vencidas(agora, 100)
        mudancas = 0
        for negociacao_id in vencidas:
            with self.repo.transacao():
                negociacao = self.repo.obter_negociacao(negociacao_id, bloquear=True)
                if negociacao is None:
                    continue
                op = _Operacao(agora, negociacao["correlationId"])
                if self._vencer(op, negociacao):
                    self._enviar(op)
                    mudancas += 1
        return mudancas

    def _vencer(self, op: _Operacao, negociacao: dict) -> bool:
        """RN-14 na direta e RN-10 no leilão, com a condição conferida de novo sob o bloqueio (RN-21)."""
        if negociacao["modo"] == "VENDA_DIRETA":
            if negociacao["status"] != "ABERTO" or op.agora < negociacao["expiraEm"]:
                return False
            self._fechar(op, negociacao, "EXPIRADO", [])
            return True
        if negociacao["status"] not in ("AGUARDANDO_RESERVAS", "ABERTO") or op.agora < negociacao["dataFim"]:
            return False
        # só há lance com o leilão ABERTO (RN-06): AGUARDANDO_RESERVAS chega aqui sem melhor lance
        melhor = self._melhor_lance(negociacao)
        if melhor is None:
            self._fechar(op, negociacao, "DESERTO", [])
            return True
        lote = [o for o in self.repo.listar_ofertas_leilao(negociacao["negociacaoId"]) if o["situacao"] == "CONFIRMADA"]
        negociacao["compradorId"] = melhor["compradorId"]
        self._fechar(op, negociacao, "ENCERRADO", ratear(melhor["valor"], negociacao["precoInicial"], lote),
                     melhor["demandaId"])
        return True

    # ------------------------------------------------------------ leilão (RN-01, RN-03, RN-04, RN-05)

    def _leilao_iniciado(self, op: _Operacao, p: dict) -> str:
        """RN-01 e RN-03."""
        if self.repo.obter_negociacao(p["negociacaoId"]) is not None:
            return "ignorado"
        negociacao = {
            "negociacaoId": p["negociacaoId"], "modo": "LEILAO_DIRETO", "status": "AGUARDANDO_RESERVAS",
            "produtoId": p["produtoId"], "dataInicio": p["iniciaEm"], "dataFim": p["encerraEm"],
            "quantidadeTotal": p["quantidadeTotal"], "precoInicial": p["precoInicial"],
            "incrementoMinimo": p["incrementoMinimo"], "incrementoMinimoPercentual": p["incrementoMinimoPercentual"],
            "melhorLanceId": None, "totalLances": 0, "compradorId": None, "fornecimentoId": None,
            "fornecedorId": None, "quantidade": None, "precoOferta": None, "precoAtual": None, "vez": None,
            "compraImediata": False, "expiraEm": None, "reservaSituacao": None, "pedidoId": None,
            "totalPropostas": 0, "correlationId": op.correlacao, "criadoEm": op.agora, "encerradoEm": None,
        }
        self.repo.inserir_negociacao(negociacao)
        for ordem, item in enumerate(p["itens"], start=1):
            oferta = {**item, "negociacaoId": p["negociacaoId"], "ordem": ordem, "situacao": "PENDENTE",
                      "pedidoId": None}
            self.repo.inserir_oferta_leilao(oferta)
            self._publicar(op, "pedido_temporario_solicitado", negociacao,
                           contratos.pedido_temporario_leilao(negociacao, oferta))
        return "aplicado"

    def _reserva_leilao(self, op: _Operacao, negociacao: dict, p: dict) -> str:
        """RN-04 e RN-05: muda a oferta pendente e, sem nenhuma pendente, libera os lances ou fecha deserto."""
        if negociacao["status"] != "AGUARDANDO_RESERVAS":
            return "ignorado"
        ofertas = self.repo.listar_ofertas_leilao(negociacao["negociacaoId"])
        oferta = next((o for o in ofertas if o["fornecimentoId"] == p["fornecimentoId"]), None)
        if oferta is None or oferta["situacao"] != "PENDENTE":
            return "ignorado"
        oferta["situacao"] = p["reservaSituacao"]
        if p["reservaSituacao"] == "CONFIRMADA":
            oferta["pedidoId"] = p["pedidoId"]
        self.repo.atualizar_oferta_leilao(oferta)
        if any(o["situacao"] == "PENDENTE" for o in ofertas):
            return "aplicado"
        lote = [o for o in ofertas if o["situacao"] == "CONFIRMADA"]
        if not lote:
            self._fechar(op, negociacao, "DESERTO", [])
            return "aplicado"
        if len(lote) < len(ofertas):
            recalcular_lote(negociacao, lote)
            for confirmada in lote:
                self.repo.atualizar_oferta_leilao(confirmada)
        negociacao["status"] = "ABERTO"
        self.repo.atualizar_negociacao(negociacao)
        return "aplicado"
