"""Conversões do adaptador Postgres que a suíte em memória não exercita."""
from decimal import Decimal

from app.adaptadores.postgres import PostgresRepositorio
from app.dominio import valor_total
from app.jsonio import dumps, loads


def test_pendente_volta_do_jsonb_com_decimal():
    # Quantidade inteira (5) volta do JSON como int; sem a conversão, valor_total quebrava na reavaliação.
    payload = loads(dumps({"quantidade": Decimal(5), "precoUnitario": Decimal("120.0"), "produtoId": "p"}))
    linha = {"negociacao_id": "n", "fornecimento_id": "f", "payload": payload, "event_id": "e",
             "correlation_id": "c", "causa": "PRODUTO_AUSENTE", "recebido_em": None}
    lido = PostgresRepositorio._pendente(linha)["payload"]
    assert isinstance(lido["quantidade"], Decimal) and isinstance(lido["precoUnitario"], Decimal)
    assert valor_total(lido["quantidade"], lido["precoUnitario"]) == Decimal("600.00")
