"""Contratos de entrada e saída: corpos HTTP (§7.2), eventos consumidos (§7.3) e eventos publicados (§7.4)."""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

from app.jsonio import formato_json

FONTE = "negociacao-service"
VERSAO = "1.0"
TOPICO_DLT = "negociacao.dlt"
Q2 = Decimal("0.01")
CAMPOS_ENVELOPE = ("eventId", "eventType", "eventVersion", "timestamp", "source", "correlationId",
                   "aggregateId", "payload")
PRODUTORES = {"modo_negociacao_definido": "mercado-service", "pedido_atualizado": "pedidos-service"}
TIPO = {"LEILAO_DIRETO": "LEILAO", "VENDA_DIRETA": "VENDA_DIRETA"}
CORRELACAO = re.compile(r"[A-Za-z0-9._-]{1,128}")
UUID_TEXTO = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# §4: valor monetário com até 12 dígitos inteiros, quantidade com até 10
MAX_VALOR = Decimal("999999999999.99")
MAX_QUANTIDADE = Decimal("9999999999.99")


def q2(valor):
    return valor.quantize(Q2, rounding=ROUND_HALF_UP)


class EventoInvalido(ValueError):
    """Entrada fora do contrato: no consumo vai para a `negociacao.dlt` com o `detalhe` (RN-20); na API, 422."""

    def __init__(self, detalhe):
        super().__init__(detalhe)
        self.detalhe = detalhe


@dataclass(frozen=True)
class Evento:
    topico: str
    event_id: str
    source: str
    correlation_id: str
    payload: dict


def _invalido(campo):
    raise EventoInvalido(f"Campo inválido: {campo}.")


def _campo(dados, nome, caminho):
    if not isinstance(dados, dict):
        _invalido(caminho)
    if nome not in dados:
        raise EventoInvalido(f"Campo obrigatório ausente: {caminho}.{nome}.")
    return dados[nome]


def _texto(dados, nome, caminho):
    valor = _campo(dados, nome, caminho)
    if not isinstance(valor, str) or not valor.strip() or len(valor) > 64:
        _invalido(f"{caminho}.{nome}")
    return valor


def _uuid(dados, nome, caminho):
    valor = _campo(dados, nome, caminho)
    if not isinstance(valor, str) or not UUID_TEXTO.fullmatch(valor):
        _invalido(f"{caminho}.{nome}")
    return valor


def _numero(dados, nome, caminho, maximo):
    """Número JSON lido como Decimal, > 0, com até 2 casas (§4). Texto, booleano e float são recusados."""
    valor = _campo(dados, nome, caminho)
    if isinstance(valor, bool) or not isinstance(valor, (int, Decimal)):
        _invalido(f"{caminho}.{nome}")
    valor = Decimal(valor)
    if not valor.is_finite() or valor <= 0 or valor > maximo or valor != valor.quantize(Q2):
        _invalido(f"{caminho}.{nome}")
    return valor


def _lista_de_um(dados, nome, caminho):
    valor = _campo(dados, nome, caminho)
    if not isinstance(valor, list) or len(valor) != 1:
        _invalido(f"{caminho}.{nome}")
    return valor[0]


def _instante(valor, campo):
    try:
        lido = datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        _invalido(campo)
    if lido.utcoffset() is None:
        _invalido(campo)


def corpo_proposta(corpo):
    """Corpo da API-09: só `precoUnitario`, valor monetário (§4)."""
    if not isinstance(corpo, dict) or set(corpo) != {"precoUnitario"}:
        _invalido("corpo")
    return _numero(corpo, "precoUnitario", "corpo", MAX_VALOR)


def corpo_vazio(corpo):
    """Corpo da API-10 e da API-11: ausente (`None`) ou `{}`."""
    if corpo not in (None, {}):
        _invalido("corpo")
    return {}


def _modo_negociacao_definido(p):
    payload = {"negociacaoId": _uuid(p, "negociacaoId", "payload"), "modo": _campo(p, "modo", "payload")}
    if payload["modo"] not in ("VENDA_DIRETA", "LEILAO_DIRETO"):
        _invalido("payload.modo")
    if payload["modo"] == "LEILAO_DIRETO":
        # o leilão é aberto pelo leilao_iniciado; os demais campos não são validados (RN-02)
        return payload
    demanda = _lista_de_um(p, "demandas", "payload")
    oferta = _lista_de_um(p, "ofertas", "payload")
    compra_imediata = p.get("compraImediata", False)
    if not isinstance(compra_imediata, bool):
        _invalido("payload.compraImediata")
    return {
        **payload, "produtoId": _texto(p, "produtoId", "payload"),
        "compradorId": _texto(demanda, "compradorId", "payload.demandas[0]"),
        "fornecimentoId": _texto(oferta, "fornecimentoId", "payload.ofertas[0]"),
        "fornecedorId": _texto(oferta, "fornecedorId", "payload.ofertas[0]"),
        "quantidade": _numero(oferta, "quantidade", "payload.ofertas[0]", MAX_QUANTIDADE),
        "precoUnitario": _numero(oferta, "precoUnitario", "payload.ofertas[0]", MAX_VALOR),
        "compraImediata": compra_imediata,
    }


def _pedido_atualizado(p):
    situacao = _campo(p, "reservaSituacao", "payload")
    if situacao not in ("PENDENTE", "CONFIRMADA", "RECUSADA", "ENCERRADA"):
        _invalido("payload.reservaSituacao")
    status = _campo(p, "status", "payload")
    if not isinstance(status, str) or not status.strip():
        _invalido("payload.status")
    return {"pedidoId": _texto(p, "pedidoId", "payload"), "negociacaoId": _texto(p, "negociacaoId", "payload"),
            "fornecimentoId": _texto(p, "fornecimentoId", "payload"), "reservaSituacao": situacao}


PAYLOADS = {"modo_negociacao_definido": _modo_negociacao_definido, "pedido_atualizado": _pedido_atualizado}


def validar_evento(topico, mensagem):
    """Validação comum do §7.3 e do payload do tópico."""
    if not isinstance(mensagem, dict):
        raise EventoInvalido("Mensagem não é um objeto JSON.")
    for campo in CAMPOS_ENVELOPE:
        if campo not in mensagem:
            raise EventoInvalido(f"Campo obrigatório ausente: {campo}.")
    if set(mensagem) - set(CAMPOS_ENVELOPE):
        raise EventoInvalido("Campo fora do envelope.")
    event_id = mensagem["eventId"]
    if not isinstance(event_id, str) or not event_id.strip():
        _invalido("eventId")
    if mensagem["eventType"] != topico:
        raise EventoInvalido("eventType diferente do tópico.")
    if mensagem["eventVersion"] != VERSAO:
        _invalido("eventVersion")
    if mensagem["source"] != PRODUTORES[topico]:
        raise EventoInvalido("source diferente do produtor do tópico.")
    correlacao = mensagem["correlationId"]
    if not isinstance(correlacao, str) or not CORRELACAO.fullmatch(correlacao):
        _invalido("correlationId")
    _instante(mensagem["timestamp"], "timestamp")
    agregado = mensagem["aggregateId"]
    if not isinstance(agregado, str) or not agregado.strip() or len(agregado) > 128:
        _invalido("aggregateId")
    if not isinstance(mensagem["payload"], dict):
        _invalido("payload")
    return Evento(topico, event_id, mensagem["source"], correlacao, PAYLOADS[topico](mensagem["payload"]))


def envelope(topico, chave, payload, agora, correlacao):
    return {
        "eventId": str(uuid4()), "eventType": topico, "eventVersion": VERSAO,
        "timestamp": formato_json(agora.astimezone(timezone.utc)), "source": FONTE, "correlationId": correlacao,
        "aggregateId": chave, "payload": payload,
    }


def pedido_temporario_solicitado(negociacao):
    return {
        "negociacaoId": negociacao["negociacaoId"], "tipo": TIPO[negociacao["modo"]],
        "fornecimentoId": negociacao["fornecimentoId"], "fornecedorId": negociacao["fornecedorId"],
        "produtoId": negociacao["produtoId"], "quantidade": q2(negociacao["quantidade"]),
        "precoUnitario": q2(negociacao["precoOferta"]),
    }


def negociacao_fechada(negociacao, alocacoes):
    encerrado = negociacao["status"] == "ENCERRADO"
    return {
        "negociacaoId": negociacao["negociacaoId"], "tipo": TIPO[negociacao["modo"]], "status": negociacao["status"],
        "compradorId": negociacao["compradorId"] if encerrado else None,
        # demandaId só existe no leilão encerrado (lance vencedor); a direta sempre manda null
        "demandaId": None, "produtoId": negociacao["produtoId"], "alocacoes": alocacoes,
    }


def chave_dlt(original):
    """Chave da `negociacao.dlt` (§7.4 e §13.1)."""
    if isinstance(original, dict):
        agregado = original.get("aggregateId")
        if isinstance(agregado, str) and 1 <= len(agregado) <= 128:
            return agregado
        event_id = original.get("eventId")
        # acima de 128 caracteres o aggregateId da DLT sairia do schema
        if event_id is not None and 1 <= len(str(event_id)) <= 128:
            return str(event_id)
    return "desconhecido"


def evento_dlt(topico_origem, original, erro, detalhe, pilha, tentativa, agora):
    chave = chave_dlt(original)
    correlacao = original.get("correlationId") if isinstance(original, dict) else None
    if not isinstance(correlacao, str) or not CORRELACAO.fullmatch(correlacao):
        correlacao = str(uuid4())
    payload = {"topicoOrigem": topico_origem, "originalEvent": original, "error": erro,
               "detalhe": (detalhe or erro)[:500], "stackTrace": pilha, "failedAtAttempt": tentativa}
    return chave, envelope(TOPICO_DLT, chave, payload, agora, correlacao)
