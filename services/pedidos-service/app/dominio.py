"""Regras puras de Pedidos: peso, valor, pendência, fechamento, visibilidade e frete."""
from datetime import timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

TERMINAIS = ("CANCELADO", "ENTREGUE")
EM_FRETE = ("AGUARDANDO_FRETE", "FRETE_SELECIONADO")
CAMPOS_SOLICITACAO = ("tipo", "fornecedorId", "produtoId", "quantidade", "precoUnitario")
CAMPOS_SELECAO = ("solicitacaoFreteId", "tentativa", "entregaId", "cotacaoId", "transportadoraId", "preco",
                  "prazoDias", "dataEntregaPrevista")


def hoje(agora):
    """Data em Brasília (UTC−03:00, sem horário de verão), §4."""
    return (agora.astimezone(timezone.utc) - timedelta(hours=3)).date()


def peso_kg(quantidade, unidade, peso_unitario):
    """RN-03. None quando falta o peso unitário."""
    if unidade == "KG":
        return quantidade
    if unidade == "TON":
        return quantidade * 1000
    if peso_unitario is None:
        return None
    return (quantidade * peso_unitario).quantize(Decimal("0.001"), ROUND_HALF_UP)


def valor_total(quantidade, preco_unitario):
    """RN-04."""
    return (quantidade * preco_unitario).quantize(Decimal("0.01"), ROUND_HALF_UP)


def mesma_solicitacao(a, b):
    """RN-02: repetição de pedido_temporario_solicitado."""
    return all(a[campo] == b[campo] for campo in CAMPOS_SOLICITACAO)


def solicitacao_do_pedido(pedido):
    return {"tipo": pedido["tipoNegociacao"], "fornecedorId": pedido["fornecedorId"],
            "produtoId": pedido["produtoId"], "quantidade": pedido["quantidade"],
            "precoUnitario": pedido["precoUnitarioOferta"]}


def diverge(solicitacao, produto, oferta):
    """RN-06 passo 3: projeção presente que contradiz a solicitação."""
    if oferta is not None and (oferta["fornecedorId"] != solicitacao["fornecedorId"]
                               or oferta["produtoId"] != solicitacao["produtoId"]):
        return True
    return oferta is not None and produto is not None and oferta["unidade"] != produto["unidade"]


def causa_pendencia(produto, oferta):
    """RN-06 passo 4."""
    if produto is None:
        return "PRODUTO_AUSENTE"
    if oferta is None:
        return "OFERTA_AUSENTE"
    if produto["unidade"] not in ("KG", "TON") and produto["pesoUnitarioKg"] is None:
        return "PESO_AUSENTE"
    return None


def normalizar_alocacoes(alocacoes):
    return sorted(({**a, "pedidoId": a["pedidoId"].lower()} for a in alocacoes), key=lambda a: a["pedidoId"])


def mesmo_fechamento(registro, novo):
    """RN-12: mesmo status, comprador e alocações (ordenadas por pedidoId, números por valor)."""
    return (registro["status"] == novo["status"] and registro["compradorId"] == novo["compradorId"]
            and normalizar_alocacoes(registro["alocacoes"]) == normalizar_alocacoes(novo["alocacoes"]))


def fechamento_compativel(fechamento, alocacoes, pedidos):
    """RN-08: toda alocação aponta para um pedido CRIADO com reserva CONFIRMADA e os mesmos dados."""
    ids = [a["pedidoId"] for a in alocacoes]
    ofertas = [a["fornecimentoId"] for a in alocacoes]
    if len(set(ids)) != len(ids) or len(set(ofertas)) != len(ofertas):
        return False
    for alocacao in alocacoes:
        pedido = pedidos.get(alocacao["pedidoId"])
        if (pedido is None or pedido["status"] != "CRIADO" or pedido["reservaSituacao"] != "CONFIRMADA"
                or pedido["fornecimentoId"] != alocacao["fornecimentoId"]
                or pedido["fornecedorId"] != alocacao["fornecedorId"]
                or pedido["quantidade"] != alocacao["quantidade"]
                or pedido["produtoId"] != fechamento["produtoId"]
                or pedido["tipoNegociacao"] != fechamento["tipo"]):
            return False
    return True


def visivel(pedido, ator):
    """RN-17."""
    if ator.suporte:
        return True
    if ator.escopo == "BUYER":
        return pedido["compradorId"] == ator.empresa
    if ator.escopo == "SUPPLIER":
        return pedido["fornecedorId"] == ator.empresa
    if ator.escopo == "TENANT_ADMIN":
        return ator.empresa in (pedido["compradorId"], pedido["fornecedorId"])
    return False


def mesma_selecao(a, b):
    return all(a[campo] == b[campo] for campo in CAMPOS_SELECAO)


def selecao_aplicada(solicitacao):
    if solicitacao is None:
        return None
    return next((s for s in solicitacao["selecoes"] if s["situacao"] == "APLICADA"), None)


def par_substituido(par, solicitacoes):
    """RN-21 passo 2: par de uma seleção SUBSTITUIDA ou de uma solicitação SUBSTITUIDA."""
    return any((s["situacao"] == "SUBSTITUIDA" or sel["situacao"] == "SUBSTITUIDA")
               and (sel["entregaId"], sel["transportadoraId"]) == par
               for s in solicitacoes for sel in s["selecoes"])
