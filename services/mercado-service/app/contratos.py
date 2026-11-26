"""Contratos de entrada e saída: eventos consumidos (§7.3) e eventos publicados (§7.4)."""
import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation
from uuid import UUID, uuid4

from app.jsonio import instante, ler_instante

FONTE = "mercado-service"
VERSAO = "1.0"
TOPICO_DLT = "mercado.dlt"

MAX_QUANTIDADE = Decimal("9999999999.99")
MAX_VALOR = Decimal("99999999999999.99")
Q2 = Decimal("0.01")
Q4 = Decimal("0.0001")
CTX = Context(prec=60, rounding=ROUND_HALF_UP)

UNIDADES = ("KG", "TON", "UN", "CX", "L")
CAMPOS_ENVELOPE = ("eventId", "eventType", "eventVersion", "timestamp", "source", "correlationId",
                   "aggregateId", "payload")
PRODUTORES = {
    "produto_cadastrado": "produtos-service",
    "produto_atualizado": "produtos-service",
    "fornecimento_criado": "fornecimentos-service",
    "fornecimento_atualizado": "fornecimentos-service",
    "estoque_atualizado": "fornecimentos-service",
    "demanda_criada": "demanda-service",
    "demanda_recorrente_gerada": "demanda-service",
    "demanda_cancelada": "demanda-service",
    "negociacao_fechada": "negociacao-service",
}
CORRELACAO = re.compile(r"[A-Za-z0-9._-]{1,128}")
UF = re.compile(r"[A-Z]{2}")
UUID_TEXTO = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
OPERACOES_ESTOQUE = ("RESERVA", "LIBERACAO", "BAIXA", "REPOSICAO", "ENCERRAMENTO")
UNIDADE_DIFERENTE = "Unidade diferente da do produto."


def q2(valor: Decimal) -> Decimal:
    return valor.quantize(Q2, rounding=ROUND_HALF_UP, context=CTX)


def q4(valor: Decimal) -> Decimal:
    return valor.quantize(Q4, rounding=ROUND_HALF_UP, context=CTX)


def formatar_percentual(valor: Decimal) -> Decimal:
    """No mínimo 2 casas, mantendo as casas extras da configuração (decisão da etapa 1)."""
    if valor.as_tuple().exponent > -2:
        return q2(valor)
    return valor


class EventoInvalido(ValueError):
    """Mensagem consumida inválida (RN-32); `detalhe` vai para a `mercado.dlt`."""

    def __init__(self, detalhe: str):
        super().__init__(detalhe)
        self.detalhe = detalhe


@dataclass(frozen=True)
class Evento:
    topico: str
    event_id: str
    source: str
    correlation_id: str
    payload: dict


_AUSENTE = object()


def _ausente(campo):
    raise EventoInvalido(f"Campo obrigatório ausente: {campo}.")


def _invalido(campo):
    raise EventoInvalido(f"Campo inválido: {campo}.")


class _Payload:
    def __init__(self, dados: dict, caminho: str):
        self.dados = dados
        self.caminho = caminho

    def campo(self, nome):
        return f"{self.caminho}.{nome}"

    def obrigatorio(self, nome):
        if nome not in self.dados:
            _ausente(self.campo(nome))
        return self.dados[nome]

    def opcional(self, nome):
        return self.dados.get(nome, _AUSENTE)

    def id(self, nome):
        return validar_id(self.obrigatorio(nome), self.campo(nome))

    def texto(self, nome):
        valor = self.obrigatorio(nome)
        if not isinstance(valor, str) or not valor.strip():
            _invalido(self.campo(nome))
        return valor

    def unidade(self, nome="unidade"):
        valor = self.obrigatorio(nome)
        if not isinstance(valor, str) or valor not in UNIDADES:
            _invalido(self.campo(nome))
        return valor

    def enum(self, nome, valores):
        valor = self.obrigatorio(nome)
        if not isinstance(valor, str) or valor not in valores:
            _invalido(self.campo(nome))
        return valor

    def instante(self, nome):
        return validar_instante(self.obrigatorio(nome), self.campo(nome))

    def quantidade(self, nome, positiva=True):
        return validar_numero(self.obrigatorio(nome), self.campo(nome), positivo=positiva, maximo=MAX_QUANTIDADE)

    def valor(self, nome, positivo=False):
        return validar_numero(self.obrigatorio(nome), self.campo(nome), positivo=positivo, maximo=MAX_VALOR)

    def local(self, nome, obrigatorio):
        valor = self.obrigatorio(nome) if obrigatorio else self.opcional(nome)
        if valor is _AUSENTE or (valor is None and not obrigatorio):
            return None
        return validar_local(valor, self.campo(nome))


def validar_id(valor, campo):
    if not isinstance(valor, str) or not valor.strip() or len(valor) > 64:
        _invalido(campo)
    return valor


def validar_instante(valor, campo) -> datetime:
    try:
        return ler_instante(valor)
    except (ValueError, TypeError):
        _invalido(campo)


def validar_numero(valor, campo, *, positivo: bool, maximo: Decimal) -> Decimal:
    """Número JSON (nunca texto nem booleano), com no máximo 2 casas e dentro do limite do §4."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float, Decimal)):
        _invalido(campo)
    if isinstance(valor, float):
        if not math.isfinite(valor):
            _invalido(campo)
        valor = Decimal(repr(valor))
    elif isinstance(valor, int):
        valor = Decimal(valor)
    if not valor.is_finite():
        _invalido(campo)
    if (positivo and valor <= 0) or valor < 0 or valor > maximo:
        _invalido(campo)
    try:
        if valor != valor.quantize(Q2, context=CTX):
            _invalido(campo)
    except InvalidOperation:
        _invalido(campo)
    return q2(valor) + Decimal("0.00")


def validar_local(valor, campo) -> dict:
    if not isinstance(valor, dict):
        _invalido(campo)
    if "cidade" not in valor:
        _ausente(f"{campo}.cidade")
    if "uf" not in valor:
        _ausente(f"{campo}.uf")
    cidade, uf = valor["cidade"], valor["uf"]
    if not isinstance(cidade, str) or not cidade.strip():
        _invalido(f"{campo}.cidade")
    if not isinstance(uf, str) or not UF.fullmatch(uf):
        _invalido(f"{campo}.uf")
    return {"cidade": cidade, "uf": uf}


def _id_opcional(p: _Payload, nome, anulavel):
    valor = p.opcional(nome)
    if valor is _AUSENTE or (valor is None and anulavel):
        return None
    if not isinstance(valor, str) or not 1 <= len(valor) <= 64:
        _invalido(p.campo(nome))
    return valor


def _preco_maximo(p: _Payload):
    valor = p.opcional("precoMaximo")
    if valor is _AUSENTE or valor is None:
        return None
    return validar_numero(valor, p.campo("precoMaximo"), positivo=True, maximo=MAX_VALOR)


def _prazo(p: _Payload):
    valor = p.opcional("prazoDesejado")
    if valor is _AUSENTE or valor is None:
        return None
    return validar_instante(valor, p.campo("prazoDesejado"))


def _produto(p: _Payload, campo_instante):
    return {"produtoId": p.id("produtoId"), "nome": p.texto("nome"), "unidade": p.unidade(),
            "instante": p.instante(campo_instante)}


def _fornecimento(p: _Payload, campo_instante):
    return {
        "fornecimentoId": p.id("fornecimentoId"), "fornecedorId": p.id("fornecedorId"),
        "produtoId": p.id("produtoId"), "unidade": p.unidade(), "preco": p.valor("preco"),
        "quantidadeDisponivel": p.quantidade("quantidadeDisponivel", positiva=False),
        "origem": p.local("origem", obrigatorio=False), "instante": p.instante(campo_instante),
    }


def _estoque(p: _Payload):
    _id_opcional(p, "produtoId", anulavel=False)
    _id_opcional(p, "fornecedorId", anulavel=False)
    operacao = p.opcional("operacao")
    if operacao is not _AUSENTE and (not isinstance(operacao, str) or operacao not in OPERACOES_ESTOQUE):
        _invalido(p.campo("operacao"))
    return {"fornecimentoId": p.id("fornecimentoId"),
            "quantidadeDisponivel": p.quantidade("quantidadeDisponivel", positiva=False),
            "instante": p.instante("atualizadoEm")}


def _demanda_criada(p: _Payload):
    return {
        "demandaId": p.id("demandaId"), "compradorId": p.id("compradorId"), "produtoId": p.id("produtoId"),
        "unidade": p.unidade(), "quantidade": p.quantidade("quantidade"),
        "destino": p.local("destino", obrigatorio=True), "tipo": p.enum("tipo", ("UNICA", "RECORRENTE")),
        "instante": p.instante("criadoEm"), "precoMaximo": _preco_maximo(p), "prazoDesejado": _prazo(p),
    }


def _demanda_recorrente(p: _Payload):
    return {
        "demandaId": p.id("demandaId"), "ocorrenciaId": p.id("ocorrenciaId"), "compradorId": p.id("compradorId"),
        "produtoId": p.id("produtoId"), "unidade": p.unidade(), "quantidade": p.quantidade("quantidade"),
        "destino": p.local("destino", obrigatorio=True), "instante": p.instante("geradoEm"),
        "precoMaximo": _preco_maximo(p), "prazoDesejado": _prazo(p),
    }


def _demanda_cancelada(p: _Payload):
    motivo = p.opcional("motivo")
    if motivo is not _AUSENTE and motivo is not None and not isinstance(motivo, str):
        _invalido(p.campo("motivo"))
    return {"demandaId": p.id("demandaId"), "compradorId": p.id("compradorId"),
            "canceladoEm": p.instante("canceladoEm")}


def _negociacao_fechada(p: _Payload):
    negociacao = p.obrigatorio("negociacaoId")
    if not isinstance(negociacao, str) or not UUID_TEXTO.fullmatch(negociacao):
        _invalido(p.campo("negociacaoId"))
    tipo = p.enum("tipo", ("LEILAO", "VENDA_DIRETA"))
    status = p.enum("status", ("ENCERRADO", "DESERTO", "RECUSADO", "EXPIRADO"))
    produto = p.id("produtoId")
    alocacoes = p.obrigatorio("alocacoes")
    if not isinstance(alocacoes, list):
        _invalido(p.campo("alocacoes"))
    itens = []
    for indice, alocacao in enumerate(alocacoes):
        caminho = f"{p.campo('alocacoes')}[{indice}]"
        if not isinstance(alocacao, dict):
            _invalido(caminho)
        item = _Payload(alocacao, caminho)
        itens.append({"fornecimentoId": item.id("fornecimentoId"), "quantidade": item.quantidade("quantidade")})
    return {"negociacaoId": str(UUID(negociacao)), "tipo": tipo, "status": status, "produtoId": produto,
            "compradorId": _id_opcional(p, "compradorId", anulavel=True),
            "demandaId": _id_opcional(p, "demandaId", anulavel=True), "alocacoes": itens}


VALIDADORES = {
    "produto_cadastrado": lambda p: _produto(p, "cadastradoEm"),
    "produto_atualizado": lambda p: _produto(p, "atualizadoEm"),
    "fornecimento_criado": lambda p: _fornecimento(p, "criadoEm"),
    "fornecimento_atualizado": lambda p: _fornecimento(p, "atualizadoEm"),
    "estoque_atualizado": _estoque,
    "demanda_criada": _demanda_criada,
    "demanda_recorrente_gerada": _demanda_recorrente,
    "demanda_cancelada": _demanda_cancelada,
    "negociacao_fechada": _negociacao_fechada,
}


def validar_evento(topico: str, mensagem) -> Evento:
    """Validação comum do §7.3 e do schema do tópico. A unidade (RN-03) depende do repositório e é
    verificada pelo domínio."""
    if not isinstance(mensagem, dict):
        raise EventoInvalido("Mensagem não é um objeto JSON.")
    for campo in CAMPOS_ENVELOPE:
        if campo not in mensagem:
            _ausente(campo)
    extras = [str(campo) for campo in mensagem if campo not in CAMPOS_ENVELOPE]
    if extras:
        raise EventoInvalido(f"Campo fora do envelope: {sorted(extras)[0]}.")
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
    validar_instante(mensagem["timestamp"], "timestamp")
    agregado = mensagem["aggregateId"]
    if not isinstance(agregado, str) or not agregado.strip() or len(agregado) > 128:
        _invalido("aggregateId")
    if not isinstance(mensagem["payload"], dict):
        _invalido("payload")
    payload = VALIDADORES[topico](_Payload(mensagem["payload"], "payload"))
    return Evento(topico, event_id, mensagem["source"], correlacao, payload)


# ---------------------------------------------------------------- eventos publicados (§7.4)

def envelope(topico: str, chave: str, payload: dict, agora: datetime, correlacao: str) -> dict:
    return {
        "eventId": str(uuid4()), "eventType": topico, "eventVersion": VERSAO, "timestamp": instante(agora),
        "source": FONTE, "correlationId": correlacao, "aggregateId": chave, "payload": payload,
    }


def _local_saida(local):
    return None if local is None else {"cidade": local["cidade"], "uf": local["uf"]}


def item_evento(item: dict) -> dict:
    return {
        "fornecimentoId": item["fornecimentoId"], "fornecedorId": item["fornecedorId"],
        "quantidade": q2(item["quantidade"]), "precoUnitario": q2(item["precoUnitario"]),
        "valorItem": q2(item["valorItem"]), "participacao": q4(item["participacao"]),
        "origem": _local_saida(item["origem"]),
    }


def evento_modo_negociacao_definido(negociacao: dict, demanda: dict | None, agora: datetime,
                                    correlacao: str) -> dict:
    demandas = []
    if demanda is not None:
        demandas.append({
            "demandaId": demanda["demandaId"], "demandaOrigemId": demanda["demandaOrigemId"],
            "compradorId": demanda["compradorId"], "quantidade": q2(negociacao["itens"][0]["quantidade"]),
            "precoMaximo": None if demanda["precoMaximo"] is None else q2(demanda["precoMaximo"]),
            "destino": _local_saida(demanda["destino"]),
            "prazoDesejado": None if demanda["prazoDesejado"] is None else instante(demanda["prazoDesejado"]),
        })
    payload = {
        "negociacaoId": negociacao["negociacaoId"], "modo": negociacao["modo"],
        "compraImediata": negociacao["compraImediata"], "produtoId": negociacao["produtoId"],
        "unidade": negociacao["unidade"], "quantidadeTotal": q2(negociacao["quantidadeTotal"]),
        "valorReferencia": q2(negociacao["valorReferencia"]), "demandas": demandas,
        "ofertas": [item_evento(item) for item in negociacao["itens"]],
        "definidaEm": instante(negociacao["definidaEm"]),
    }
    return envelope("modo_negociacao_definido", negociacao["negociacaoId"], payload, agora, correlacao)


def evento_leilao_iniciado(negociacao: dict, percentual: Decimal, agora: datetime, correlacao: str) -> dict:
    payload = {
        "negociacaoId": negociacao["negociacaoId"], "sugestaoId": negociacao["sugestaoId"],
        "produtoId": negociacao["produtoId"], "unidade": negociacao["unidade"],
        "quantidadeTotal": q2(negociacao["quantidadeTotal"]), "precoInicial": q2(negociacao["precoInicial"]),
        "incrementoMinimo": q2(negociacao["incrementoMinimo"]),
        "incrementoMinimoPercentual": formatar_percentual(percentual),
        "itens": [item_evento(item) for item in negociacao["itens"]],
        "iniciaEm": instante(negociacao["iniciaEm"]), "encerraEm": instante(negociacao["encerraEm"]),
    }
    return envelope("leilao_iniciado", negociacao["negociacaoId"], payload, agora, correlacao)


def chave_dlt(original) -> str:
    """Chave da `mercado.dlt` (§7.4, §13.1 e decisão da etapa 1 para `str(eventId)` acima de 128)."""
    if isinstance(original, dict):
        agregado = original.get("aggregateId")
        if isinstance(agregado, str) and 1 <= len(agregado) <= 128:
            return agregado
        event_id = original.get("eventId")
        if event_id is not None:
            texto = str(event_id)
            if texto and len(texto) <= 128:
                return texto
    return "desconhecido"


def evento_dlt(topico_origem: str, original, erro: str, detalhe: str, pilha: str, tentativa: int,
               agora: datetime) -> tuple[str, dict]:
    chave = chave_dlt(original)
    correlacao = original.get("correlationId") if isinstance(original, dict) else None
    if not isinstance(correlacao, str) or not CORRELACAO.fullmatch(correlacao):
        correlacao = str(uuid4())
    payload = {
        "topicoOrigem": topico_origem, "originalEvent": original, "error": erro,
        "detalhe": (detalhe or erro)[:500], "stackTrace": pilha, "failedAtAttempt": tentativa,
    }
    return chave, envelope(TOPICO_DLT, chave, payload, agora, correlacao)
