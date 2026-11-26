"""Validação das entradas (eventos e corpo HTTP) e montagem das saídas (eventos e respostas)."""
import re
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
CORRELACAO_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
DATA_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
UFS = frozenset("AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split())
UNIDADES = ("KG", "TON", "UN", "CX", "L")
TRANSPORTES = ("GRANEL", "REFRIGERADO", "CARGA_SECA", "PERIGOSO")
STATUS = ("CRIADO", "VENDA_CONCLUIDA", "AGUARDANDO_FRETE", "FRETE_SELECIONADO", "ENTREGUE", "CANCELADO")
MAX_DINHEIRO = Decimal("99999999999999.99")
MAX_QUANTIDADE = Decimal("9999999999.99")
MAX_PESO_UNITARIO = Decimal("999999999.999")
CENTAVO = Decimal("0.01")
GRAMA = Decimal("0.001")

ENVELOPE = ("eventId", "eventType", "eventVersion", "timestamp", "source", "correlationId", "aggregateId", "payload")

# tópico consumido → (produtor, campo do payload que é a chave)
CONSUMIDOS = {
    "pedido_temporario_solicitado": ("negociacao-service", "negociacaoId"),
    "negociacao_fechada": ("negociacao-service", "negociacaoId"),
    "produto_cadastrado": ("produtos-service", "produtoId"),
    "produto_atualizado": ("produtos-service", "produtoId"),
    "fornecimento_criado": ("fornecimentos-service", "fornecimentoId"),
    "estoque_atualizado": ("fornecimentos-service", "fornecimentoId"),
    "reserva_estoque_recusada": ("fornecimentos-service", "pedidoId"),
    "solicitacao_frete_criada": ("logistica-service", "solicitacaoFreteId"),
    "frete_selecionado": ("logistica-service", "pedidoId"),
    "entrega_concluida": ("logistica-service", "pedidoId"),
    "frete_indisponivel": ("logistica-service", "pedidoId"),
}
PUBLICADOS = ("pedido_criado", "pedido_atualizado", "pedidos.dlt")


class Invalido(ValueError):
    """Entrada fora do contrato. A mensagem cita só o campo, nunca o valor recebido."""


# ---------------------------------------------------------------- leitura de campos

def _obrigatorio(dados, campo, opcional):
    valor = dados.get(campo)
    if valor is None and not opcional:
        raise Invalido(f"{campo}: obrigatório")
    return valor


def texto(dados, campo, maximo=64, opcional=False):
    valor = _obrigatorio(dados, campo, opcional)
    if valor is None:
        return None
    if not isinstance(valor, str) or not valor.strip() or len(valor) > maximo or "\x00" in valor:
        raise Invalido(f"{campo}: texto inválido")
    return valor


def uuid_texto(dados, campo):
    valor = _obrigatorio(dados, campo, False)
    if not isinstance(valor, str) or not UUID_RE.fullmatch(valor):
        raise Invalido(f"{campo}: UUID inválido")
    return valor.lower()


def enum(dados, campo, valores):
    valor = _obrigatorio(dados, campo, False)
    if not isinstance(valor, str) or valor not in valores:
        raise Invalido(f"{campo}: valor fora do domínio")
    return valor


def numero(dados, campo, casas, maximo, minimo=Decimal(0), exclusivo=True, opcional=False):
    valor = _obrigatorio(dados, campo, opcional)
    if valor is None:
        return None
    if isinstance(valor, bool) or not isinstance(valor, (int, float, Decimal)):
        raise Invalido(f"{campo}: número esperado")
    decimal = Decimal(str(valor)) if isinstance(valor, float) else Decimal(valor)
    if not decimal.is_finite() or decimal.normalize().as_tuple().exponent < -casas:
        raise Invalido(f"{campo}: casas decimais demais")
    if decimal > maximo or decimal < minimo or (exclusivo and decimal == minimo):
        raise Invalido(f"{campo}: fora do limite")
    return decimal


def inteiro(dados, campo, minimo=1):
    valor = _obrigatorio(dados, campo, False)
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < minimo or valor > 2_147_483_647:
        raise Invalido(f"{campo}: inteiro inválido")
    return valor


def ler_instante(valor):
    if not isinstance(valor, str) or "T" not in valor:
        raise ValueError("instante")
    instante = datetime.fromisoformat(valor)
    if instante.tzinfo is None or instante.utcoffset() is None:
        raise ValueError("instante sem fuso")
    return instante.astimezone(timezone.utc)


def instante(dados, campo):
    try:
        return ler_instante(_obrigatorio(dados, campo, False))
    except ValueError as exc:
        raise Invalido(f"{campo}: instante inválido") from exc


def ler_data(valor):
    if not isinstance(valor, str) or not DATA_RE.fullmatch(valor):
        raise ValueError("data")
    return date.fromisoformat(valor)


def data(dados, campo, opcional=False):
    valor = _obrigatorio(dados, campo, opcional)
    if valor is None:
        return None
    try:
        return ler_data(valor)
    except ValueError as exc:
        raise Invalido(f"{campo}: data inválida") from exc


def local(dados, campo):
    valor = _obrigatorio(dados, campo, False)
    if not isinstance(valor, dict):
        raise Invalido(f"{campo}: objeto esperado")
    cidade = valor.get("cidade")
    if not isinstance(cidade, str) or "\x00" in cidade or not 1 <= len(cidade.strip()) <= 255:
        raise Invalido(f"{campo}.cidade: texto inválido")
    return {"cidade": cidade.strip(), "uf": enum(valor, "uf", UFS)}


# ---------------------------------------------------------------- eventos consumidos

def _solicitacao(p):
    s = {"negociacaoId": texto(p, "negociacaoId"), "tipo": enum(p, "tipo", ("LEILAO", "VENDA_DIRETA")),
         "fornecimentoId": texto(p, "fornecimentoId"), "fornecedorId": texto(p, "fornecedorId"),
         "produtoId": texto(p, "produtoId"),
         "quantidade": numero(p, "quantidade", 2, MAX_QUANTIDADE),
         "precoUnitario": numero(p, "precoUnitario", 2, MAX_DINHEIRO, exclusivo=False)}
    if (s["quantidade"] * s["precoUnitario"]).quantize(CENTAVO, ROUND_HALF_UP) > MAX_DINHEIRO:
        raise Invalido("valorTotal: acima do máximo (RN-04)")
    return s


def _alocacao(a):
    if not isinstance(a, dict):
        raise Invalido("alocacoes: objeto esperado")
    return {"fornecimentoId": texto(a, "fornecimentoId"), "fornecedorId": texto(a, "fornecedorId"),
            "pedidoId": uuid_texto(a, "pedidoId"),
            "quantidade": numero(a, "quantidade", 2, MAX_QUANTIDADE),
            "valorUnitario": numero(a, "valorUnitario", 2, MAX_DINHEIRO, exclusivo=False),
            "valorTotal": numero(a, "valorTotal", 2, MAX_DINHEIRO, exclusivo=False)}


def _fechamento(p):
    status = enum(p, "status", ("ENCERRADO", "DESERTO", "RECUSADO", "EXPIRADO"))
    alocacoes = p.get("alocacoes")
    if not isinstance(alocacoes, list):
        raise Invalido("alocacoes: lista esperada")
    if status == "ENCERRADO":
        comprador = texto(p, "compradorId")
        if not alocacoes:
            raise Invalido("alocacoes: vazia em ENCERRADO")
    else:
        comprador = None
        if p.get("compradorId") is not None or alocacoes:
            raise Invalido("compradorId/alocacoes: preenchidos sem venda")
    # demandaId é aceito e ignorado (§7.3).
    return {"negociacaoId": texto(p, "negociacaoId"), "tipo": enum(p, "tipo", ("LEILAO", "VENDA_DIRETA")),
            "status": status, "compradorId": comprador, "produtoId": texto(p, "produtoId"),
            "alocacoes": [_alocacao(a) for a in alocacoes]}


def _produto(p):
    return {"produtoId": texto(p, "produtoId"), "nome": texto(p, "nome", 255),
            "unidade": enum(p, "unidade", UNIDADES), "tipoTransporte": enum(p, "tipoTransporte", TRANSPORTES),
            "pesoUnitarioKg": numero(p, "pesoUnitarioKg", 3, MAX_PESO_UNITARIO, opcional=True)}


def _oferta(p):
    return {"fornecimentoId": texto(p, "fornecimentoId"), "fornecedorId": texto(p, "fornecedorId"),
            "produtoId": texto(p, "produtoId"), "unidade": enum(p, "unidade", UNIDADES),
            "origem": local(p, "origem")}


def _estoque(p):
    operacao = enum(p, "operacao", ("RESERVA", "LIBERACAO", "BAIXA", "REPOSICAO", "ENCERRAMENTO"))
    if operacao in ("RESERVA", "LIBERACAO", "BAIXA"):
        pedido = uuid_texto(p, "pedidoId")
    elif p.get("pedidoId") is not None:
        raise Invalido("pedidoId: deve ser nulo nesta operação")
    else:
        pedido = None
    return {"fornecimentoId": texto(p, "fornecimentoId"), "produtoId": texto(p, "produtoId"),
            "fornecedorId": texto(p, "fornecedorId"), "operacao": operacao,
            "quantidadeMovimentada": numero(p, "quantidadeMovimentada", 2, MAX_QUANTIDADE),
            "quantidadeDisponivel": numero(p, "quantidadeDisponivel", 2, MAX_QUANTIDADE, exclusivo=False),
            "quantidadeReservada": numero(p, "quantidadeReservada", 2, MAX_QUANTIDADE, exclusivo=False),
            "pedidoId": pedido, "atualizadoEm": instante(p, "atualizadoEm")}


def _recusa(p):
    return {"pedidoId": uuid_texto(p, "pedidoId"), "fornecimentoId": texto(p, "fornecimentoId"),
            "quantidadeSolicitada": numero(p, "quantidadeSolicitada", 2, MAX_QUANTIDADE),
            "quantidadeDisponivel": numero(p, "quantidadeDisponivel", 2, MAX_QUANTIDADE, exclusivo=False,
                                           opcional=True),
            "motivo": enum(p, "motivo", ("ESTOQUE_INSUFICIENTE", "FORNECIMENTO_INEXISTENTE")),
            "recusadoEm": instante(p, "recusadoEm")}


def _solicitacao_frete(p):
    enum(p, "status", ("ABERTA",))
    return {"solicitacaoFreteId": texto(p, "solicitacaoFreteId"), "pedidoId": uuid_texto(p, "pedidoId")}


def _frete_selecionado(p):
    enum(p, "status", ("FRETE_SELECIONADO",))
    prevista = data(p, "dataEntregaPrevista", opcional=True)
    return {"pedidoId": uuid_texto(p, "pedidoId"), "solicitacaoFreteId": texto(p, "solicitacaoFreteId"),
            "entregaId": texto(p, "entregaId"), "cotacaoId": texto(p, "cotacaoId"),
            "transportadoraId": texto(p, "transportadoraId"), "tentativa": inteiro(p, "tentativa"),
            "preco": numero(p, "preco", 2, MAX_DINHEIRO, exclusivo=False), "prazoDias": inteiro(p, "prazoDias"),
            "dataEntregaPrevista": prevista.isoformat() if prevista else None}


def _entrega(p):
    return {"pedidoId": uuid_texto(p, "pedidoId"), "entregaId": texto(p, "entregaId"),
            "transportadoraId": texto(p, "transportadoraId"), "entregueEm": instante(p, "entregueEm")}


def _indisponivel(p):
    return {"pedidoId": uuid_texto(p, "pedidoId"), "solicitacaoFreteId": texto(p, "solicitacaoFreteId"),
            "motivo": enum(p, "motivo", ("SEM_COTACAO", "SEM_TRANSPORTADORA_DISPONIVEL")),
            "ocorridoEm": instante(p, "ocorridoEm"), "detalhe": texto(p, "detalhe", 500, opcional=True)}


PAYLOADS = {
    "pedido_temporario_solicitado": _solicitacao, "negociacao_fechada": _fechamento,
    "produto_cadastrado": _produto, "produto_atualizado": _produto, "fornecimento_criado": _oferta,
    "estoque_atualizado": _estoque, "reserva_estoque_recusada": _recusa,
    "solicitacao_frete_criada": _solicitacao_frete, "frete_selecionado": _frete_selecionado,
    "entrega_concluida": _entrega, "frete_indisponivel": _indisponivel,
}


def validar_evento(topico, mensagem):
    """Valida o envelope e o payload (§7.3). Devolve o envelope com o payload normalizado."""
    if not isinstance(mensagem, dict):
        raise Invalido("mensagem: objeto esperado")
    faltando = [campo for campo in ENVELOPE if campo not in mensagem]
    if faltando:
        raise Invalido("envelope: faltam " + ", ".join(faltando))
    produtor, chave = CONSUMIDOS[topico]
    event_id = mensagem["eventId"]
    if not isinstance(event_id, str) or not UUID_RE.fullmatch(event_id):
        raise Invalido("eventId: UUID inválido")
    if mensagem["eventType"] != topico or mensagem["eventVersion"] != "1.0" or mensagem["source"] != produtor:
        raise Invalido("envelope: eventType, eventVersion ou source incompatível")
    correlacao = mensagem["correlationId"]
    if not isinstance(correlacao, str) or not CORRELACAO_RE.fullmatch(correlacao):
        raise Invalido("correlationId: formato inválido")
    momento = instante(mensagem, "timestamp")
    payload = mensagem["payload"]
    if not isinstance(payload, dict):
        raise Invalido("payload: objeto esperado")
    if mensagem["aggregateId"] != payload.get(chave):
        raise Invalido("aggregateId: diferente da chave do payload")
    return {"eventId": event_id.lower(), "source": produtor, "correlationId": correlacao,
            "timestamp": momento, "aggregateId": mensagem["aggregateId"], "payload": PAYLOADS[topico](payload)}


# ---------------------------------------------------------------- corpo da API-07

def validar_destino(corpo: bytes, loads):
    try:
        dados = loads(corpo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise Invalido("corpo: JSON inválido") from exc
    if not isinstance(dados, dict) or set(dados) - {"cidade", "uf", "dataEntregaDesejada"}:
        raise Invalido("corpo: campos fora do contrato")
    destino = local({"destino": dados}, "destino")
    return dados, destino, data(dados, "dataEntregaDesejada", opcional=True)


# ---------------------------------------------------------------- saídas

def instante_txt(valor):
    if valor is None:
        return None
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dinheiro(valor):
    return None if valor is None else Decimal(valor).quantize(CENTAVO, ROUND_HALF_UP)


def peso(valor):
    return Decimal(valor).quantize(GRAMA, ROUND_HALF_UP)


def frete_saida(frete):
    if frete is None:
        return None
    return {"solicitacaoFreteId": frete["solicitacaoFreteId"], "entregaId": frete["entregaId"],
            "cotacaoId": frete["cotacaoId"], "transportadoraId": frete["transportadoraId"],
            "tentativa": frete["tentativa"], "preco": dinheiro(frete["preco"]), "prazoDias": frete["prazoDias"],
            "dataEntregaPrevista": frete["dataEntregaPrevista"]}


def detalhe(p):
    """API-05: todos os atributos do §6.1, exceto precoUnitarioOferta, solicitacaoFreteVigenteId e correlationId."""
    return {
        "pedidoId": p["pedidoId"], "negociacaoId": p["negociacaoId"], "tipoNegociacao": p["tipoNegociacao"],
        "compradorId": p["compradorId"], "fornecedorId": p["fornecedorId"], "fornecimentoId": p["fornecimentoId"],
        "produtoId": p["produtoId"], "produtoNome": p["produtoNome"], "quantidade": dinheiro(p["quantidade"]),
        "unidade": p["unidade"], "valorUnitario": dinheiro(p["valorUnitario"]), "valorTotal": dinheiro(p["valorTotal"]),
        "pesoKg": peso(p["pesoKg"]), "tipoTransporte": p["tipoTransporte"], "origem": p["origem"],
        "destino": p["destino"],
        "dataEntregaDesejada": p["dataEntregaDesejada"].isoformat() if p["dataEntregaDesejada"] else None,
        "status": p["status"], "reservaSituacao": p["reservaSituacao"], "motivoCancelamento": p["motivoCancelamento"],
        "frete": frete_saida(p["frete"]), "ocorrenciaFrete": p["ocorrenciaFrete"], "versao": p["versao"],
        "criadoEm": instante_txt(p["criadoEm"]), "atualizadoEm": instante_txt(p["atualizadoEm"]),
        "vendidoEm": instante_txt(p["vendidoEm"]), "entregueEm": instante_txt(p["entregueEm"]),
    }


def item_lista(p):
    return {"pedidoId": p["pedidoId"], "negociacaoId": p["negociacaoId"], "fornecimentoId": p["fornecimentoId"],
            "compradorId": p["compradorId"], "fornecedorId": p["fornecedorId"], "status": p["status"],
            "valorTotal": dinheiro(p["valorTotal"]), "criadoEm": instante_txt(p["criadoEm"])}


def item_historico(h):
    return {"versao": h["versao"], "statusAnterior": h["statusAnterior"], "status": h["status"],
            "reservaSituacao": h["reservaSituacao"], "origem": h["origem"], "motivo": h["motivo"],
            "ocorridoEm": instante_txt(h["ocorridoEm"])}


def carga_pedido_criado(p):
    return {"pedidoId": p["pedidoId"], "negociacaoId": p["negociacaoId"], "compradorId": None,
            "fornecedorId": p["fornecedorId"], "produtoId": p["produtoId"], "fornecimentoId": p["fornecimentoId"],
            "produtoNome": p["produtoNome"], "quantidade": dinheiro(p["quantidade"]), "unidade": p["unidade"],
            "pesoKg": peso(p["pesoKg"]), "tipoTransporte": p["tipoTransporte"],
            "valorTotal": dinheiro(p["valorTotal"]), "origem": p["origem"], "destino": None, "status": "CRIADO"}


def carga_pedido_atualizado(p, status_anterior):
    carga = {"pedidoId": p["pedidoId"], "negociacaoId": p["negociacaoId"], "fornecimentoId": p["fornecimentoId"],
             "status": p["status"], "statusAnterior": status_anterior, "reservaSituacao": p["reservaSituacao"],
             "versao": p["versao"]}
    status = p["status"]
    if status == "CANCELADO":
        carga["motivo"] = p["motivoCancelamento"]
    elif status == "VENDA_CONCLUIDA":
        carga.update(compradorId=p["compradorId"], valorUnitario=dinheiro(p["valorUnitario"]),
                     valorTotal=dinheiro(p["valorTotal"]))
    elif status == "AGUARDANDO_FRETE":
        carga.update(compradorId=p["compradorId"], fornecedorId=p["fornecedorId"], produtoId=p["produtoId"],
                     produtoNome=p["produtoNome"], quantidade=dinheiro(p["quantidade"]), unidade=p["unidade"],
                     pesoKg=peso(p["pesoKg"]), tipoTransporte=p["tipoTransporte"],
                     valorTotal=dinheiro(p["valorTotal"]), origem=p["origem"], destino=p["destino"])
        if p["dataEntregaDesejada"] is not None:
            carga["dataEntregaDesejada"] = p["dataEntregaDesejada"].isoformat()
    elif status == "FRETE_SELECIONADO":
        carga["frete"] = frete_saida(p["frete"])
    elif status == "ENTREGUE":
        carga["entregueEm"] = instante_txt(p["entregueEm"])
    return carga


def envelope(topico, chave, payload, correlacao, agora, event_id=None):
    return {"eventId": event_id or str(uuid4()), "eventType": topico, "eventVersion": "1.0",
            "timestamp": instante_txt(agora), "source": "pedidos-service", "correlationId": correlacao,
            "aggregateId": chave, "payload": payload}
