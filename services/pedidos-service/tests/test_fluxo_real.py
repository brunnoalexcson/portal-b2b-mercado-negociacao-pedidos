"""Fluxo completo na stack de demonstração (Postgres, Redpanda, duas réplicas e Nginx)."""
import asyncio
import os

import httpx
import pytest

from tests.demo_eventos import COMP, abrir_kafka, esperar, publicar, semear, token

pytestmark = pytest.mark.skipif(os.getenv("PEDIDOS_DEMO") != "1", reason="Requer compose.demo.yml")


def test_fluxo_do_pedido_temporario_ate_a_entrega():
    asyncio.run(_fluxo())


async def _fluxo():
    api = os.environ["PEDIDOS_API_URL"] + "/api/v1/pedidos"
    produtor, observador = await abrir_kafka()
    try:
        negociacao, p = await semear(produtor, observador)
        async with httpx.AsyncClient(timeout=10) as cliente:
            comprador = {"Authorization": "Bearer " + await token(cliente, "usr-comp", COMP, "BUYER")}
            fornecedor = {"Authorization": "Bearer " + await token(
                cliente, "usr-f1", "a1000000-0000-4000-8000-000000000001", "SUPPLIER")}
            suporte = {"Authorization": "Bearer " + await token(cliente, "usr-admin", admin=True)}

            assert (await cliente.get(api)).status_code == 401
            lista = (await cliente.get(api, params={"negociacaoId": negociacao}, headers=comprador)).json()
            assert lista["total"] == 2
            assert (await cliente.get(api, params={"negociacaoId": negociacao}, headers=suporte)).json()["total"] == 3
            assert (await cliente.get(f"{api}/{p[3]}", headers=comprador)).status_code == 404
            assert (await cliente.get(f"{api}/{p[1]}", headers=fornecedor)).json()["status"] == "VENDA_CONCLUIDA"

            r = await cliente.put(f"{api}/{p[1]}/destino", headers={**comprador, "Idempotency-Key": "e2e-1",
                                                                     "X-Correlation-Id": "e2e-destino"},
                                  json={"cidade": "Anápolis", "uf": "GO"})
            assert r.status_code == 200 and r.json()["status"] == "AGUARDANDO_FRETE"
            assert r.headers["x-correlation-id"] == "e2e-destino"
            atualizado = await esperar(observador, "pedido_atualizado",
                                       lambda c: c["pedidoId"] == p[1] and c["status"] == "AGUARDANDO_FRETE")
            assert atualizado["correlationId"] == "e2e-destino" and atualizado["payload"]["destino"]["uf"] == "GO"

            await publicar(produtor, "solicitacao_frete_criada", "sol-e2e-" + p[1][:8], {
                "solicitacaoFreteId": "sol-e2e-" + p[1][:8], "pedidoId": p[1], "status": "ABERTA"})
            await publicar(produtor, "frete_selecionado", p[1], {
                "pedidoId": p[1], "solicitacaoFreteId": "sol-e2e-" + p[1][:8], "entregaId": "ent-e2e",
                "cotacaoId": "cot-e2e", "transportadoraId": "tra-e2e", "tentativa": 1, "preco": 150.00,
                "prazoDias": 2, "status": "FRETE_SELECIONADO"})
            await esperar(observador, "pedido_atualizado",
                          lambda c: c["pedidoId"] == p[1] and c["status"] == "FRETE_SELECIONADO")
            await publicar(produtor, "entrega_concluida", p[1], {
                "pedidoId": p[1], "entregaId": "ent-e2e", "transportadoraId": "tra-e2e",
                "entregueEm": "2026-11-23T16:00:00.000Z"})
            await esperar(observador, "pedido_atualizado",
                          lambda c: c["pedidoId"] == p[1] and c["status"] == "ENTREGUE")

            historico = (await cliente.get(f"{api}/{p[1]}/historico", headers=comprador)).json()
            assert [h["status"] for h in historico["items"]] == [
                "CRIADO", "CRIADO", "VENDA_CONCLUIDA", "AGUARDANDO_FRETE", "FRETE_SELECIONADO", "ENTREGUE"]

            await produtor.send_and_wait("frete_selecionado", b"{nao e json", key=p[1].encode())
            dlt = await esperar(observador, "pedidos.dlt", lambda c: c["error"] == "EVENTO_INVALIDO")
            assert dlt["aggregateId"] == p[1] and dlt["payload"]["failedAtAttempt"] == 1

            metricas = (await cliente.get(os.environ["PEDIDOS_API_URL"] + "/metrics")).text
            assert "pedidos_eventos_consumidos_total" in metricas
            assert (await cliente.get(os.environ["PEDIDOS_API_URL"] + "/health/ready")).json() == {"status": "ready"}
    finally:
        await observador.stop()
        await produtor.stop()
