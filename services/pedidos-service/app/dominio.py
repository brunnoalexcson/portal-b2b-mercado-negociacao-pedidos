from decimal import Decimal, ROUND_HALF_UP

from app.contratos import Fechamento, Fornecimento, Produto


def montar_carga(resultado: Fechamento, produto: Produto, oferta: Fornecimento) -> dict | None:
    if (produto.produtoId != resultado.produtoId or oferta.produtoId != produto.produtoId
            or oferta.fornecimentoId != resultado.fornecimentoId or oferta.fornecedorId != resultado.fornecedorId
            or produto.unidade != oferta.unidade or resultado.destino is None):
        return None

    if produto.unidade == "KG":
        peso = resultado.quantidade
    elif produto.unidade == "TON":
        peso = resultado.quantidade * 1000
    elif produto.pesoUnitarioKg is None:
        return None
    else:
        peso = resultado.quantidade * produto.pesoUnitarioKg

    valor = (resultado.quantidade * resultado.valorUnitario).quantize(Decimal("0.01"), ROUND_HALF_UP)
    if valor >= Decimal("100000000000000"):
        return None

    return {
        "negociacaoId": resultado.negociacaoId, "compradorId": resultado.compradorId,
        "fornecedorId": resultado.fornecedorId, "produtoId": resultado.produtoId,
        "fornecimentoId": resultado.fornecimentoId, "produtoNome": produto.nome,
        "quantidade": resultado.quantidade, "unidade": produto.unidade,
        "pesoKg": peso, "tipoTransporte": produto.tipoTransporte, "valorTotal": valor,
        "origem": oferta.origem.model_dump(), "destino": resultado.destino.model_dump(),
        "dataEntregaDesejada": resultado.dataEntregaDesejada,
    }
