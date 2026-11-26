"""Popula a demonstração fazendo o papel do Mercado e do Pedidos no Kafka.

    python demo/popular.py                       abre duas diretas (uma com compra imediata) e imprime IDs e tokens
    python demo/popular.py confirmar <negociacaoId>   responde a reserva da negociação como o Pedidos
    python demo/popular.py recusar <negociacaoId>     responde a reserva recusada

Roda na imagem de teste pelo Compose: serviço `popular` do compose.demo.yml."""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from app.jsonio import dumps, formato_json, loads

API = os.getenv("NEGOCIACAO_API_URL", "http://gateway:8080")
IDENTITY = os.getenv("NEGOCIACAO_IDENTITY_URL", "http://identity:8000")
KAFKA = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "broker:9092")
SUFIXO = uuid4().hex[:6]
COMPRADOR, OUTRO, FORNECEDOR = f"comprador-a-{SUFIXO}", f"comprador-b-{SUFIXO}", f"fornecedor-x-{SUFIXO}"


def evento(topico, fonte, chave, payload):
    return {"eventId": str(uuid4()), "eventType": topico, "eventVersion": "1.0",
            "timestamp": formato_json(datetime.now(timezone.utc)), "source": fonte,
            "correlationId": f"demo-{SUFIXO}", "aggregateId": chave, "payload": payload}


def direta(negociacao_id, compra_imediata):
    """modo_negociacao_definido como o Mercado publica (ESP-MRC-001 §7.4)."""
    return evento("modo_negociacao_definido", "mercado-service", negociacao_id, {
        "negociacaoId": negociacao_id, "modo": "VENDA_DIRETA", "produtoId": f"milho-{SUFIXO}", "unidade": "TON",
        "quantidadeTotal": Decimal("10.00"), "valorReferencia": Decimal("1300.00"),
        "demandas": [{"demandaId": f"demanda-a-{SUFIXO}", "demandaOrigemId": None, "compradorId": COMPRADOR,
                      "quantidade": Decimal("10.00"), "precoMaximo": Decimal("135.00"),
                      "destino": {"cidade": "Anápolis", "uf": "GO"}, "prazoDesejado": None}],
        "ofertas": [{"fornecimentoId": f"oferta-x-{SUFIXO}", "fornecedorId": FORNECEDOR,
                     "quantidade": Decimal("10.00"), "precoUnitario": Decimal("130.00"),
                     "valorItem": Decimal("1300.00"), "participacao": Decimal("1.0000"),
                     "origem": {"cidade": "Jataí", "uf": "GO"}}],
        "compraImediata": compra_imediata, "definidaEm": formato_json(datetime.now(timezone.utc)),
    })


async def publicar(mensagem):
    produtor = AIOKafkaProducer(bootstrap_servers=KAFKA, acks="all")
    await produtor.start()
    try:
        await produtor.send_and_wait(mensagem["eventType"], dumps(mensagem).encode(),
                                     key=mensagem["aggregateId"].encode())
    finally:
        await produtor.stop()


async def pedido_temporario(negociacao_id):
    """Lê o pedido_temporario_solicitado da negociação, como o Pedidos faria."""
    consumidor = AIOKafkaConsumer("pedido_temporario_solicitado", bootstrap_servers=KAFKA,
                                  group_id=f"demo-{uuid4().hex}", auto_offset_reset="earliest")
    await consumidor.start()
    try:
        async with asyncio.timeout(30):
            async for mensagem in consumidor:
                payload = loads(mensagem.value)["payload"]
                if payload["negociacaoId"] == negociacao_id:
                    return payload
    finally:
        await consumidor.stop()


def token(cliente, empresa, escopo):
    resposta = cliente.post(f"{IDENTITY}/token", params={"empresa": empresa, "escopo": escopo, "minutos": 120})
    resposta.raise_for_status()
    return resposta.json()["access_token"]


def abrir():
    ids = {"Direta com propostas": str(uuid4()), "Compra imediata": str(uuid4())}
    for nome, negociacao_id in ids.items():
        asyncio.run(publicar(direta(negociacao_id, nome == "Compra imediata")))
    with httpx.Client(timeout=5) as cliente:
        tokens = {"Comprador A (BUYER)": token(cliente, COMPRADOR, "BUYER"),
                  "Fornecedor X (SUPPLIER)": token(cliente, FORNECEDOR, "SUPPLIER"),
                  "Comprador B (BUYER, não participa)": token(cliente, OUTRO, "BUYER")}
        cabecalho = {"Authorization": f"Bearer {tokens['Comprador A (BUYER)']}"}
        limite = time.monotonic() + 30
        for nome, negociacao_id in ids.items():
            # o serviço consome o evento em segundo plano: espera a negociação aparecer na API
            while (resposta := cliente.get(f"{API}/api/v1/negociacoes/{negociacao_id}", headers=cabecalho)
                   ).status_code == 404 and time.monotonic() < limite:
                time.sleep(0.5)
            corpo = resposta.json()
            print(f"{nome}: {negociacao_id}\n  status {corpo.get('status')}, reserva {corpo.get('reservaSituacao')},"
                  f" compraImediata {corpo.get('compraImediata')}, acoesPermitidas {corpo.get('acoesPermitidas')}")
    print("\nTokens (2 h), para o Authorize do /docs:")
    for nome, valor in tokens.items():
        print(f"\n{nome}:\n{valor}")
    print("\nPara responder a reserva como o Pedidos:\n"
          "  docker compose -f compose.demo.yml run --rm --no-deps popular python demo/popular.py confirmar <id>")


def responder(situacao, negociacao_id):
    solicitado = asyncio.run(pedido_temporario(negociacao_id))
    pedido_id = str(uuid4())
    status = "CRIADO" if situacao == "CONFIRMADA" else "CANCELADO"
    mensagem = evento("pedido_atualizado", "pedidos-service", pedido_id, {
        "pedidoId": pedido_id, "negociacaoId": negociacao_id, "fornecimentoId": solicitado["fornecimentoId"],
        "status": status, "statusAnterior": "CRIADO", "reservaSituacao": situacao, "versao": 2})
    asyncio.run(publicar(mensagem))
    print(f"pedido_atualizado publicado: pedido {pedido_id}, reserva {situacao}, oferta {solicitado['fornecimentoId']}")


if __name__ == "__main__":
    match sys.argv[1:]:
        case []:
            abrir()
        case ["confirmar", negociacao_id]:
            responder("CONFIRMADA", negociacao_id)
        case ["recusar", negociacao_id]:
            responder("RECUSADA", negociacao_id)
        case _:
            sys.exit(__doc__)
