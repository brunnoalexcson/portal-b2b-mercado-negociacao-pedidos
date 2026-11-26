"""Popula a demonstração fazendo o papel do Mercado e do Pedidos no Kafka.

    python demo/popular.py                       abre duas diretas (uma com compra imediata) e imprime IDs e tokens
    python demo/popular.py confirmar <negociacaoId>   responde a reserva da negociação como o Pedidos
    python demo/popular.py recusar <negociacaoId>     responde a reserva recusada
    python demo/popular.py leilao                     abre um leilão com 2 ofertas e imprime IDs e tokens
    python demo/popular.py confirmar|recusar <negociacaoId> <fornecimentoId>   responde a reserva de uma oferta

Roda na imagem de teste pelo Compose: serviço `popular` do compose.demo.yml."""
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
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


async def pedido_temporario(negociacao_id, fornecimento_id=None):
    """Lê o pedido_temporario_solicitado da negociação, como o Pedidos faria."""
    consumidor = AIOKafkaConsumer("pedido_temporario_solicitado", bootstrap_servers=KAFKA,
                                  group_id=f"demo-{uuid4().hex}", auto_offset_reset="earliest")
    await consumidor.start()
    try:
        async with asyncio.timeout(30):
            async for mensagem in consumidor:
                payload = loads(mensagem.value)["payload"]
                if payload["negociacaoId"] == negociacao_id and fornecimento_id in (None, payload["fornecimentoId"]):
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


def responder(situacao, negociacao_id, fornecimento_id=None):
    solicitado = asyncio.run(pedido_temporario(negociacao_id, fornecimento_id))
    pedido_id = str(uuid4())
    status = "CRIADO" if situacao == "CONFIRMADA" else "CANCELADO"
    mensagem = evento("pedido_atualizado", "pedidos-service", pedido_id, {
        "pedidoId": pedido_id, "negociacaoId": negociacao_id, "fornecimentoId": solicitado["fornecimentoId"],
        "status": status, "statusAnterior": "CRIADO", "reservaSituacao": situacao, "versao": 2})
    asyncio.run(publicar(mensagem))
    print(f"pedido_atualizado publicado: pedido {pedido_id}, reserva {situacao}, oferta {solicitado['fornecimentoId']}")


# ---------------------------------------------------------------- leilão

FORNECEDOR_A, FORNECEDOR_B = f"fornecedor-a-{SUFIXO}", f"fornecedor-b-{SUFIXO}"


def leilao(negociacao_id):
    """leilao_iniciado como o Mercado publica (formato de docs/negociacao/contratos/leilao_iniciado.exemplo.json)."""
    agora = datetime.now(timezone.utc)
    itens = [(f"oferta-a-{SUFIXO}", FORNECEDOR_A, "5.00", "120.00", "600.00", "0.2353", "Rio Verde"),
             (f"oferta-b-{SUFIXO}", FORNECEDOR_B, "15.00", "130.00", "1950.00", "0.7647", "Jataí")]
    return evento("leilao_iniciado", "mercado-service", negociacao_id, {
        "negociacaoId": negociacao_id, "sugestaoId": str(uuid4()), "produtoId": f"milho-{SUFIXO}", "unidade": "TON",
        "quantidadeTotal": Decimal("20.00"), "precoInicial": Decimal("2550.00"), "incrementoMinimo": Decimal("25.50"),
        "incrementoMinimoPercentual": Decimal("1.00"),
        "itens": [{"fornecimentoId": oferta, "fornecedorId": fornecedor, "quantidade": Decimal(quantidade),
                   "precoUnitario": Decimal(preco), "valorItem": Decimal(valor), "participacao": Decimal(parte),
                   "origem": {"cidade": cidade, "uf": "GO"}}
                  for oferta, fornecedor, quantidade, preco, valor, parte, cidade in itens],
        "iniciaEm": formato_json(agora), "encerraEm": formato_json(agora + timedelta(hours=2)),
    })


def abrir_leilao():
    negociacao_id = str(uuid4())
    asyncio.run(publicar(leilao(negociacao_id)))
    with httpx.Client(timeout=5) as cliente:
        tokens = {"Comprador A (BUYER)": token(cliente, COMPRADOR, "BUYER"),
                  "Fornecedor A (SUPPLIER, do leilão)": token(cliente, FORNECEDOR_A, "SUPPLIER"),
                  "Fornecedor X (SUPPLIER, fora do leilão)": token(cliente, FORNECEDOR, "SUPPLIER")}
        cabecalho = {"Authorization": f"Bearer {tokens['Comprador A (BUYER)']}"}
        limite = time.monotonic() + 30
        # o serviço consome o evento em segundo plano: espera o leilão aparecer na API
        while (resposta := cliente.get(f"{API}/api/v1/negociacoes/{negociacao_id}", headers=cabecalho)
               ).status_code == 404 and time.monotonic() < limite:
            time.sleep(0.5)
        # loads com Decimal, para imprimir 2550.00 como a API devolve
        corpo = loads(resposta.text)
    print(f"Leilão: {negociacao_id}\n  status {corpo.get('status')}, precoInicial {corpo.get('precoInicial')},"
          f" acoesPermitidas {corpo.get('acoesPermitidas')}")
    for oferta in corpo.get("ofertas", []):
        print(f"  oferta {oferta['fornecimentoId']} de {oferta['fornecedorId']}: {oferta['valorItem']},"
              f" participação {oferta['participacao']}, {oferta['situacao']}")
    print("\nTokens (2 h), para o Authorize do /docs:")
    for nome, valor in tokens.items():
        print(f"\n{nome}:\n{valor}")
    print("\nPara responder a reserva de uma oferta como o Pedidos:\n  docker compose -f compose.demo.yml run --rm"
          " --no-deps popular python demo/popular.py confirmar <negociacaoId> <fornecimentoId>")


if __name__ == "__main__":
    match sys.argv[1:]:
        case []:
            abrir()
        case ["confirmar", negociacao_id]:
            responder("CONFIRMADA", negociacao_id)
        case ["recusar", negociacao_id]:
            responder("RECUSADA", negociacao_id)
        case ["leilao"]:
            abrir_leilao()
        case ["confirmar", negociacao_id, fornecimento_id]:
            responder("CONFIRMADA", negociacao_id, fornecimento_id)
        case ["recusar", negociacao_id, fornecimento_id]:
            responder("RECUSADA", negociacao_id, fornecimento_id)
        case _:
            sys.exit(__doc__)
