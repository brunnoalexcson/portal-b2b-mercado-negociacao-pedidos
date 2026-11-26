"""Eventos de teste no formato do §7.3 (PROF-05), para a stack de demonstração.

Uso, com a stack no ar:  docker compose -f compose.demo.yml run --rm semear
Publica produto, três ofertas, os pedidos temporários de uma negociação nova, as reservas e o fechamento
(P1 e P2 vendidos ao comprador COMP, P3 fora do lote). Imprime os IDs e um token de comprador para testar a API.
"""
import asyncio
import os
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from app.jsonio import dumps, loads

PROD = "35000000-0000-4000-8000-000000000001"
COMP = "b2000000-0000-4000-8000-000000000002"
OFERTAS = [
    ("34000000-0000-4000-8000-000000000001", "a1000000-0000-4000-8000-000000000001", "Rio Verde", 5.00, 120.00),
    ("34000000-0000-4000-8000-000000000002", "a1000000-0000-4000-8000-000000000002", "Jataí", 15.00, 125.00),
    ("34000000-0000-4000-8000-000000000003", "a1000000-0000-4000-8000-000000000003", "Mineiros", 10.00, 118.00),
]
FONTES = {"pedido_temporario_solicitado": "negociacao-service", "negociacao_fechada": "negociacao-service",
          "produto_cadastrado": "produtos-service", "fornecimento_criado": "fornecimentos-service",
          "estoque_atualizado": "fornecimentos-service", "solicitacao_frete_criada": "logistica-service",
          "frete_selecionado": "logistica-service", "entrega_concluida": "logistica-service"}


def evento(topico, chave, payload, correlacao):
    return {"eventId": str(uuid4()), "eventType": topico, "eventVersion": "1.0",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "source": FONTES[topico], "correlationId": correlacao, "aggregateId": chave, "payload": payload}


async def publicar(produtor, topico, chave, payload, correlacao="demo-pedidos"):
    mensagem = evento(topico, chave, payload, correlacao)
    await produtor.send_and_wait(topico, dumps(mensagem).encode(), key=chave.encode())
    return mensagem


async def esperar(consumidor, topico, condicao, prazo=30):
    async with asyncio.timeout(prazo):
        while True:
            mensagem = await consumidor.getone()
            if mensagem.topic == topico:
                dados = loads(mensagem.value)
                if condicao(dados["payload"]):
                    return dados


async def token(cliente, usuario, tenant=None, escopo="BUYER", admin=False):
    params = {"usuario": usuario, "escopo": escopo, "admin": str(admin).lower()}
    if tenant:
        params["tenant"] = tenant
    resposta = await cliente.post(os.environ["PEDIDOS_IDENTITY_URL"] + "/token", params=params)
    resposta.raise_for_status()
    return resposta.json()["access_token"]


async def abrir_kafka():
    servidores = os.environ["KAFKA_BOOTSTRAP_SERVERS"]
    produtor = AIOKafkaProducer(bootstrap_servers=servidores, acks="all")
    observador = AIOKafkaConsumer("pedido_criado", "pedido_atualizado", "pedidos.dlt", bootstrap_servers=servidores,
                                  group_id=f"demo-{uuid4()}", auto_offset_reset="latest")
    await produtor.start()
    await observador.start()
    await observador.getmany(timeout_ms=1000)  # garante a atribuição antes de publicar
    return produtor, observador


async def semear(produtor, observador, negociacao=None):
    """Leva uma negociação nova até VENDA_CONCLUIDA (P1, P2) e CANCELADO (P3). Devolve {i: pedidoId}."""
    negociacao = negociacao or str(uuid4())
    await publicar(produtor, "produto_cadastrado", PROD, {"produtoId": PROD, "nome": "Milho em grão",
                                                          "unidade": "TON", "tipoTransporte": "GRANEL",
                                                          "pesoUnitarioKg": None})
    for forn, emp, cidade, _, _ in OFERTAS:
        await publicar(produtor, "fornecimento_criado", forn, {"fornecimentoId": forn, "fornecedorId": emp,
                                                               "produtoId": PROD, "unidade": "TON",
                                                               "origem": {"cidade": cidade, "uf": "GO"}})
    pedidos = {}
    for i, (forn, emp, _, qtd, preco) in enumerate(OFERTAS, 1):
        await publicar(produtor, "pedido_temporario_solicitado", negociacao, {
            "negociacaoId": negociacao, "tipo": "LEILAO", "fornecimentoId": forn, "fornecedorId": emp,
            "produtoId": PROD, "quantidade": qtd, "precoUnitario": preco})
        criado = await esperar(observador, "pedido_criado",
                               lambda p, f=forn: p["negociacaoId"] == negociacao and p["fornecimentoId"] == f)
        pedidos[i] = criado["payload"]["pedidoId"]
    for i, (forn, emp, _, qtd, _) in enumerate(OFERTAS, 1):
        await publicar(produtor, "estoque_atualizado", forn, {
            "fornecimentoId": forn, "produtoId": PROD, "fornecedorId": emp, "operacao": "RESERVA",
            "quantidadeMovimentada": qtd, "quantidadeDisponivel": 100.0, "quantidadeReservada": qtd,
            "pedidoId": pedidos[i], "atualizadoEm": datetime.now(timezone.utc).isoformat()})
        await esperar(observador, "pedido_atualizado",
                      lambda p, pid=pedidos[i]: p["pedidoId"] == pid and p["reservaSituacao"] == "CONFIRMADA")
    await publicar(produtor, "negociacao_fechada", negociacao, {
        "negociacaoId": negociacao, "tipo": "LEILAO", "status": "ENCERRADO", "compradorId": COMP, "produtoId": PROD,
        "demandaId": None, "alocacoes": [
            {"fornecimentoId": OFERTAS[0][0], "fornecedorId": OFERTAS[0][1], "pedidoId": pedidos[1],
             "quantidade": 5.00, "valorUnitario": 130.00, "valorTotal": 650.00},
            {"fornecimentoId": OFERTAS[1][0], "fornecedorId": OFERTAS[1][1], "pedidoId": pedidos[2],
             "quantidade": 15.00, "valorUnitario": 130.00, "valorTotal": 1950.00}]})
    await esperar(observador, "pedido_atualizado",
                  lambda p: p["pedidoId"] == pedidos[3] and p["status"] == "CANCELADO")
    return negociacao, pedidos


async def main():
    produtor, observador = await abrir_kafka()
    try:
        negociacao, pedidos = await semear(produtor, observador)
    finally:
        await observador.stop()
        await produtor.stop()
    async with httpx.AsyncClient() as cliente:
        comprador = await token(cliente, "usr-comp", COMP, "BUYER")
    print(f"negociacaoId: {negociacao}")
    for i, pid in pedidos.items():
        print(f"P{i}: {pid}")
    print("Token do comprador (BUYER, 60 min):")
    print(comprador)


if __name__ == "__main__":
    asyncio.run(main())
