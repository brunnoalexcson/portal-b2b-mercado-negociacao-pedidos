"""Teste integrado local: Mercado, Negociação e Pedidos reais no kit. Faz o papel de Produtos, Fornecimento e
Demanda (eventos) e das empresas (HTTP). Uso: python roteiro_integrado.py direta | leilao | fechar
Roda dentro da rede portal-net, com a pasta desta validação montada em /integ (ver relatorio-integrado.md)."""
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import httpx
from aiokafka import AIOKafkaProducer

from app.jsonio import dumps, formato_json, loads

MERCADO, NEGOCIACAO, PEDIDOS = ("http://app1-mercado:8105/api/v1/mercado", "http://app2-negociacao:8106/api/v1",
                                "http://app1-pedidos:8107/api/v1")
IDENTITY, KAFKA = "http://identity-teste:8000", "rp-0:9092,rp-1:9092,rp-2:9092"
ESTADO = Path("/integ/estado.json")
http = httpx.Client(timeout=10)


def agora():
    return formato_json(datetime.now(timezone.utc))


def evento(topico, fonte, chave, payload):
    return topico, chave, {"eventId": str(uuid4()), "eventType": topico, "eventVersion": "1.0", "timestamp": agora(),
                           "source": fonte, "correlationId": f"integ-{chave}"[:128], "aggregateId": chave,
                           "payload": payload}


async def _publicar(eventos):
    produtor = AIOKafkaProducer(bootstrap_servers=KAFKA, acks="all")
    await produtor.start()
    try:
        for topico, chave, mensagem in eventos:
            await produtor.send_and_wait(topico, dumps(mensagem).encode(), key=chave.encode())
    finally:
        await produtor.stop()


def publicar(*eventos):
    asyncio.run(_publicar(eventos))


def token(empresa, escopo):
    r = http.post(f"{IDENTITY}/token", params={"empresa": empresa, "escopo": escopo, "minutos": 120})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def esperar(descricao, condicao, limite=60):
    fim = time.monotonic() + limite
    while time.monotonic() < fim:
        resultado = condicao()
        if resultado:
            return resultado
        time.sleep(1)
    raise SystemExit(f"FALHOU: tempo esgotado esperando {descricao}")


def get(url, cab, **params):
    r = http.get(url, headers=cab, params=params)
    return loads(r.text) if r.status_code == 200 else None


def chamar(metodo, url, cab, corpo=None, esperado=None):
    r = http.request(metodo, url, headers=cab, content=None if corpo is None else dumps(corpo))
    ok = esperado is None or r.status_code == esperado
    print(f"  {'ok ' if ok else 'ERRO'} {metodo} {url.split('/api/v1')[1]} → {r.status_code}"
          + ("" if ok else f" (esperado {esperado}): {r.text[:300]}"))
    if not ok:
        raise SystemExit("FALHOU")
    return loads(r.text) if r.text else None


def pedidos_da(negociacao_id, cab, minimo=1, **filtro):
    def ler():
        dados = get(f"{PEDIDOS}/pedidos", cab, negociacaoId=negociacao_id, **filtro)
        if not dados or len(dados["items"]) < minimo:
            return None
        # a listagem traz o resumo; reserva, produto e quantidade estão no detalhe
        return [get(f"{PEDIDOS}/pedidos/{p['pedidoId']}", cab) for p in dados["items"]]
    return ler


def reservar(e, pedidos_):
    """Faz o papel do Fornecimento: confirma a reserva de cada pedido temporário, com o saldo que sobra na oferta."""
    eventos = []
    for p in pedidos_:
        e["disp"][p["fornecimentoId"]] = str(D(e["disp"][p["fornecimentoId"]]) - D(str(p["quantidade"])))
        eventos.append(evento("estoque_atualizado", "fornecimentos-service", p["fornecimentoId"], {
            "fornecimentoId": p["fornecimentoId"], "produtoId": p["produtoId"], "fornecedorId": p["fornecedorId"],
            "operacao": "RESERVA", "quantidadeMovimentada": D(str(p["quantidade"])),
            "quantidadeDisponivel": D(e["disp"][p["fornecimentoId"]]), "quantidadeReservada": D(str(p["quantidade"])),
            "pedidoId": p["pedidoId"], "atualizadoEm": agora()}))
    publicar(*eventos)
    ESTADO.write_text(json.dumps(e))


def cenario():
    s = uuid4().hex[:6]
    e = {"s": s, "produto": f"milho-{s}", "ofx": f"oferta-x-{s}", "ofy": f"oferta-y-{s}",
         "fx": f"fornecedor-x-{s}", "fy": f"fornecedor-y-{s}", "ca": f"comprador-a-{s}", "cb": f"comprador-b-{s}",
         "da": f"demanda-a-{s}", "db": f"demanda-b-{s}"}
    e["disp"] = {e["ofx"]: "60.00", e["ofy"]: "40.00"}

    def oferta(oferta_id, fornecedor, preco, qtd, cidade):
        # união do que o Mercado e o Pedidos leem; o Fornecimento real publica os dois conjuntos
        return evento("fornecimento_criado", "fornecimentos-service", oferta_id, {
            "fornecimentoId": oferta_id, "fornecedorId": fornecedor, "produtoId": e["produto"], "unidade": "TON",
            "preco": D(preco), "quantidadeDisponivel": D(qtd), "origem": {"cidade": cidade, "uf": "GO"},
            "criadoEm": agora()})

    def demanda(demanda_id, comprador, qtd, maximo, cidade):
        return evento("demanda_criada", "demanda-service", demanda_id, {
            "demandaId": demanda_id, "compradorId": comprador, "produtoId": e["produto"], "unidade": "TON",
            "quantidade": D(qtd), "precoMaximo": None if maximo is None else D(maximo),
            "destino": {"cidade": cidade, "uf": "GO"}, "prazoDesejado": None, "tipo": "UNICA", "criadoEm": agora()})

    publicar(evento("produto_cadastrado", "produtos-service", e["produto"], {
        "produtoId": e["produto"], "nome": "Milho em grão", "unidade": "TON", "tipoTransporte": "GRANEL",
        "pesoUnitarioKg": None, "cadastradoEm": agora()}))
    time.sleep(2)
    publicar(oferta(e["ofx"], e["fx"], "8.00", "60.00", "Rio Verde"), oferta(e["ofy"], e["fy"], "7.50", "40.00", "Jataí"),
             demanda(e["da"], e["ca"], "50.00", "9.00", "Anápolis"), demanda(e["db"], e["cb"], "80.00", None, "Goiânia"))
    return e


def direta():
    e = cenario()
    ca, cb, fy = token(e["ca"], "BUYER"), token(e["cb"], "BUYER"), token(e["fy"], "SUPPLIER")
    print(f"Cenário {e['s']}: produto, ofertas X (8.00 x 60) e Y (7.50 x 40), demandas A (50) e B (80)")
    esperar("o Mercado ver as duas ofertas", lambda: len((get(f"{MERCADO}/produtos/{e['produto']}/ofertas", ca,
                                                              demandaId=e["da"]) or {}).get("items", [])) == 2)

    print("\n1. Direta com propostas: Comprador A abre no Mercado (demanda A, oferta Y, 10)")
    d = chamar("POST", f"{MERCADO}/negociacoes", ca, {"demandaId": e["da"], "fornecimentoId": e["ofy"],
                                                      "quantidade": 10, "compraImediata": False}, 201)["negociacaoId"]
    neg = esperar("a Negociação abrir a direta", lambda: get(f"{NEGOCIACAO}/negociacoes/{d}", ca))
    print(f"  Negociação {d}: {neg['status']}, reserva {neg['reservaSituacao']}, vez {neg['vez']}, "
          f"precoOferta {neg['precoOferta']}")
    ped = esperar("o Pedidos criar o pedido temporário", pedidos_da(d, fy))
    print(f"  Pedidos: {len(ped)} pedido {ped[0]['status']}, reserva {ped[0]['reservaSituacao']}, "
          f"comprador {ped[0].get('compradorId')}")
    chamar("POST", f"{NEGOCIACAO}/negociacoes/{d}/aceitar", fy, esperado=409)
    reservar(e, ped)
    esperar("a reserva confirmar na Negociação",
            lambda: (get(f"{NEGOCIACAO}/negociacoes/{d}", ca) or {}).get("reservaSituacao") == "CONFIRMADA")
    print("  Fornecimento confirmou a reserva → Pedidos → pedido_atualizado → Negociação: CONFIRMADA")
    chamar("POST", f"{NEGOCIACAO}/negociacoes/{d}/propostas", ca, {"precoUnitario": 7.00}, 201)
    fim = chamar("POST", f"{NEGOCIACAO}/negociacoes/{d}/aceitar", fy, esperado=200)
    print(f"  Negociação: {fim['status']} a {fim['precoAtual']}")
    vendido = esperar("o Pedidos concluir a venda", pedidos_da(d, fy, status="VENDA_CONCLUIDA"))[0]
    print(f"  Pedidos: {vendido['status']}, comprador {vendido['compradorId']}, valor {vendido.get('valorTotal')}")
    m = esperar("o Mercado registrar o fechamento",
                lambda: (lambda x: x if x and x.get("situacao") != "ABERTA" else None)(
                    get(f"{MERCADO}/negociacoes/{d}", ca)))
    print(f"  Mercado: negociação {m.get('situacao')}; demanda A: "
          f"{json.dumps(get(f'{MERCADO}/demandas/' + e['da'], ca), default=str)[:300]}")

    print("\n2. Compra imediata: Comprador B (demanda B, oferta X, 5)")
    c = chamar("POST", f"{MERCADO}/negociacoes", cb, {"demandaId": e["db"], "fornecimentoId": e["ofx"],
                                                      "quantidade": 5, "compraImediata": True}, 201)["negociacaoId"]
    fx = token(e["fx"], "SUPPLIER")
    ped = esperar("o pedido da compra imediata", pedidos_da(c, fx))
    reservar(e, ped)
    neg = esperar("a compra imediata fechar sozinha",
                  lambda: (lambda x: x if x and x["status"] == "ENCERRADO" else None)(
                      get(f"{NEGOCIACAO}/negociacoes/{c}", cb)))
    vendido = esperar("o Pedidos concluir a compra", pedidos_da(c, fx, status="VENDA_CONCLUIDA"))[0]
    print(f"  Negociação {c}: {neg['status']} a {neg['precoAtual']}; Pedidos: {vendido['status']}, "
          f"comprador {vendido['compradorId']}, valor {vendido.get('valorTotal')}")
    ESTADO.write_text(json.dumps(e))
    print("\nDIRETA OK")


def leilao():
    e = json.loads(ESTADO.read_text())
    ca, cb = token(e["ca"], "BUYER"), token(e["cb"], "BUYER")
    fx, fy = token(e["fx"], "SUPPLIER"), token(e["fy"], "SUPPLIER")
    print(f"\n3. Leilão (cenário {e['s']})")
    sug = esperar("a sugestão de leilão", lambda: next(
        (x for x in (get(f"{MERCADO}/sugestoes-leilao", fx) or {}).get("items", [])
         if x.get("produtoId") == e["produto"]), None))
    print(f"  Sugestão {sug['sugestaoId']}: {sug.get('situacao')}, excesso {sug.get('excessoDemanda')}")
    ofx = get(f"{MERCADO}/produtos/{e['produto']}/ofertas", ca, demandaId=e["da"])["items"]
    saldo = {o["fornecimentoId"]: o.get("quantidadeDisponivel") for o in ofx}
    print(f"  Saldo das ofertas no Mercado: {json.dumps(saldo, default=str)}")
    chamar("POST", f"{MERCADO}/sugestoes-leilao/{sug['sugestaoId']}/aceites", fx,
           {"fornecimentoId": e["ofx"], "quantidade": saldo[e["ofx"]], "duracaoLeilaoHoras": 1}, 201)
    r = chamar("POST", f"{MERCADO}/sugestoes-leilao/{sug['sugestaoId']}/aceites", fy,
               {"fornecimentoId": e["ofy"], "quantidade": saldo[e["ofy"]]}, 201)
    lid = r.get("negociacaoId")
    print(f"  Aceites: {r.get('situacao')}, leilão {lid}")
    neg = esperar("a Negociação abrir o leilão", lambda: get(f"{NEGOCIACAO}/negociacoes/{lid}", ca))
    print(f"  Negociação: {neg['status']}, precoInicial {neg['precoInicial']}, incremento {neg['incrementoMinimo']}, "
          f"dataFim {neg['dataFim']}, ofertas {[o['situacao'] for o in neg['ofertas']]}")
    ped = esperar("os 2 pedidos temporários", pedidos_da(lid, fx, minimo=1)) + \
        esperar("o pedido do Fornecedor Y", pedidos_da(lid, fy, minimo=1))
    reservar(e, ped)
    neg = esperar("o leilão abrir", lambda: (lambda x: x if x and x["status"] == "ABERTO" else None)(
        get(f"{NEGOCIACAO}/negociacoes/{lid}", ca)))
    print(f"  Reservas confirmadas: {neg['status']}, acoes {neg['acoesPermitidas']}")
    inicial, inc = D(str(neg["precoInicial"])), D(str(neg["incrementoMinimo"]))
    chamar("POST", f"{NEGOCIACAO}/negociacoes/{lid}/lances", ca, {"valor": inicial}, 201)
    chamar("POST", f"{NEGOCIACAO}/negociacoes/{lid}/lances", cb, {"valor": inicial + inc, "demandaId": e["db"]}, 201)
    e.update(leilao=lid, valor=str(inicial + inc), dataFim=neg["dataFim"])
    ESTADO.write_text(json.dumps(e))
    print(f"\nLEILÃO ABERTO: vencedor esperado Comprador B com {inicial + inc}; encerra em {neg['dataFim']}")


def fechar():
    e = json.loads(ESTADO.read_text())
    lid, cb, fx, fy = e["leilao"], token(e["cb"], "BUYER"), token(e["fx"], "SUPPLIER"), token(e["fy"], "SUPPLIER")
    neg = esperar("o encerramento do leilão", lambda: (lambda x: x if x and x["status"] != "ABERTO" else None)(
        get(f"{NEGOCIACAO}/negociacoes/{lid}", cb)), 120)
    print(f"\n4. Encerramento: {neg['status']}, comprador {neg['compradorId']}, melhor {neg['melhorLance']['valor']}")
    vendidos = esperar("o Pedidos concluir as 2 vendas", lambda: (lambda a, b: a + b if a and b else None)(
        pedidos_da(lid, fx, status="VENDA_CONCLUIDA")(), pedidos_da(lid, fy, status="VENDA_CONCLUIDA")()))
    total = sum(D(str(p["valorTotal"])) for p in vendidos)
    print(f"  Pedidos: {[(p['fornecimentoId'], p['compradorId'], p['valorTotal']) for p in vendidos]}; soma {total}"
          f" (lance {e['valor']})")
    m = get(f"{MERCADO}/negociacoes/{lid}", cb)
    print(f"  Mercado: leilão {m.get('situacao') if m else m}; demanda B: "
          f"{json.dumps(get(f'{MERCADO}/demandas/' + e['db'], cb), default=str)[:300]}")
    print("\nLEILÃO OK" if total == D(e["valor"]) else "\nFALHOU: soma do rateio diferente do lance")


{"direta": direta, "leilao": leilao, "fechar": fechar}[sys.argv[1]]()
