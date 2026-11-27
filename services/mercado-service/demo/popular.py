"""Popula a demonstração: publica no Kafka 1 produto, 2 ofertas e 2 demandas (demanda > oferta) e imprime os IDs,
corpos prontos para o /docs e um token de 2 h para cada empresa.

Roda na imagem de teste pelo Compose: serviço `popular` do compose.demo.yml."""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
from aiokafka import AIOKafkaProducer

from app.jsonio import dumps, instante

API = os.getenv("MERCADO_API_URL", "http://gateway:8080")
IDENTITY = os.getenv("MERCADO_IDENTITY_URL", "http://identity:8000")
KAFKA = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "broker:9092")
D = Decimal

SUFIXO = uuid4().hex[:6]
EMPRESAS = {
    "Comprador A": (f"comprador-a-{SUFIXO}", "BUYER"),
    "Comprador B": (f"comprador-b-{SUFIXO}", "BUYER"),
    "Fornecedor X": (f"fornecedor-x-{SUFIXO}", "SUPPLIER"),
    "Fornecedor Y": (f"fornecedor-y-{SUFIXO}", "SUPPLIER"),
}
COMP_A, COMP_B = EMPRESAS["Comprador A"][0], EMPRESAS["Comprador B"][0]
FORN_X, FORN_Y = EMPRESAS["Fornecedor X"][0], EMPRESAS["Fornecedor Y"][0]
PRODUTO = f"milho-{SUFIXO}"
OFERTA_X, OFERTA_Y = f"oferta-x-{SUFIXO}", f"oferta-y-{SUFIXO}"
DEMANDA_A, DEMANDA_B = f"demanda-a-{SUFIXO}", f"demanda-b-{SUFIXO}"


def agora() -> str:
    return instante(datetime.now(timezone.utc))


def evento(topico, fonte, agregado, payload):
    return topico, agregado, {
        "eventId": str(uuid4()), "eventType": topico, "eventVersion": "1.0", "timestamp": agora(),
        "source": fonte, "correlationId": f"demo-{SUFIXO}", "aggregateId": agregado, "payload": payload}


def oferta(oferta_id, fornecedor, preco, quantidade, cidade):
    return evento("fornecimento_criado", "fornecimentos-service", oferta_id, {
        "fornecimentoId": oferta_id, "fornecedorId": fornecedor, "produtoId": PRODUTO, "unidade": "TON",
        "preco": D(preco), "quantidadeDisponivel": D(quantidade), "origem": {"cidade": cidade, "uf": "GO"},
        "criadoEm": agora()})


def demanda(demanda_id, comprador, quantidade, preco_maximo, cidade):
    return evento("demanda_criada", "demanda-service", demanda_id, {
        "demandaId": demanda_id, "compradorId": comprador, "produtoId": PRODUTO, "unidade": "TON",
        "quantidade": D(quantidade), "precoMaximo": None if preco_maximo is None else D(preco_maximo),
        "destino": {"cidade": cidade, "uf": "GO"}, "prazoDesejado": None, "tipo": "UNICA", "criadoEm": agora()})


EVENTOS = [
    evento("produto_cadastrado", "produtos-service", PRODUTO, {
        "produtoId": PRODUTO, "nome": "Milho em grão", "unidade": "TON", "cadastradoEm": agora()}),
    oferta(OFERTA_X, FORN_X, "8.00", "60.00", "Rio Verde"),
    oferta(OFERTA_Y, FORN_Y, "7.50", "40.00", "Jataí"),
    demanda(DEMANDA_A, COMP_A, "50.00", "9.00", "Anápolis"),
    demanda(DEMANDA_B, COMP_B, "80.00", None, "Goiânia"),
]


async def publicar():
    produtor = AIOKafkaProducer(bootstrap_servers=KAFKA, acks="all")
    await produtor.start()
    try:
        for topico, chave, mensagem in EVENTOS:
            await produtor.send_and_wait(topico, dumps(mensagem).encode(), key=chave.encode())
    finally:
        await produtor.stop()


def token(empresa, escopo, minutos=120):
    resposta = httpx.post(f"{IDENTITY}/token", params={"empresa": empresa, "escopo": escopo, "minutos": minutos},
                          timeout=5)
    resposta.raise_for_status()
    return resposta.json()["access_token"]


def esperar(condicao, limite, descricao):
    fim = time.monotonic() + limite
    while time.monotonic() < fim:
        try:
            resultado = condicao()
            if resultado:
                return resultado
        except httpx.HTTPError:
            pass
        time.sleep(1)
    sys.exit(f"Tempo esgotado esperando {descricao}.")


def main():
    esperar(lambda: httpx.get(f"{API}/health/ready", timeout=3).status_code == 200, 120, "o /health/ready")
    asyncio.run(publicar())
    leitura = token(COMP_A, "BUYER", 5)

    def balanco_com_sugestao():
        dados = httpx.get(f"{API}/api/v1/mercado/produtos/{PRODUTO}/balanco", timeout=5,
                          headers={"Authorization": f"Bearer {leitura}"}).json()
        return dados if dados.get("sugestaoId") and dados.get("demandaTotal") == 130 else None

    balanco = esperar(balanco_com_sugestao, 60, "a sugestão de leilão")
    tokens = {nome: token(empresa, escopo) for nome, (empresa, escopo) in EMPRESAS.items()}

    linha = "=" * 78
    print(f"\n{linha}\nCENÁRIO DA DEMONSTRAÇÃO (sufixo {SUFIXO})\n{linha}")
    print(f"Produto ............ {PRODUTO}  (Milho em grão, TON)")
    print(f"Oferta X ........... {OFERTA_X}  ({FORN_X}, 8.00 x 60.00, Rio Verde/GO)")
    print(f"Oferta Y ........... {OFERTA_Y}  ({FORN_Y}, 7.50 x 40.00, Jataí/GO)")
    print(f"Demanda A .......... {DEMANDA_A}  ({COMP_A}, 50.00, máximo 9.00)")
    print(f"Demanda B .......... {DEMANDA_B}  ({COMP_B}, 80.00, sem máximo)")
    print(f"Balanço ............ oferta {balanco['ofertaTotal']} x demanda {balanco['demandaTotal']} "
          f"-> {balanco['situacao']}")
    print(f"Sugestão de leilão . {balanco['sugestaoId']}")
    print(f"\n{linha}\nCORPOS PARA O /docs\n{linha}")
    print(f"Direta (Comprador A): {{\"demandaId\": \"{DEMANDA_A}\", \"fornecimentoId\": \"{OFERTA_Y}\", "
          f"\"quantidade\": 10, \"compraImediata\": true}}")
    print(f"Aceite (Fornecedor X): {{\"fornecimentoId\": \"{OFERTA_X}\", \"quantidade\": 60, "
          f"\"duracaoLeilaoHoras\": 48}}")
    print(f"Aceite (Fornecedor Y): {{\"fornecimentoId\": \"{OFERTA_Y}\", \"quantidade\": 40}}")
    print(f"\n{linha}\nTOKENS (válidos por 2 h; cole no Authorize do /docs, sem a palavra Bearer)\n{linha}")
    for nome, (empresa, escopo) in EMPRESAS.items():
        print(f"\n{nome} ({empresa}, {escopo}):\n{tokens[nome]}")
    print()


if __name__ == "__main__":
    main()
