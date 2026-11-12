from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb, set_json_loads
from psycopg_pool import AsyncConnectionPool

from app.contratos import Deserto, Fechamento, Fornecimento, Produto
from app.dominio import montar_carga
from app.jsonio import dumps, fingerprint, loads


class ErroPedido(Exception):
    def __init__(self, codigo):
        self.codigo = codigo
        super().__init__(codigo)


def jsonb(dados):
    return Jsonb(dados, dumps=dumps)


CAMPOS_RESUMO = {
    "pedidoId": "id", "negociacaoId": "negociacao_id", "compradorId": "comprador_id",
    "fornecedorId": "fornecedor_id", "status": "status", "valorTotal": "valor_total", "criadoEm": "criado_em",
}
CAMPOS_DETALHE = {
    **CAMPOS_RESUMO, "fornecimentoId": "fornecimento_id", "produtoId": "produto_id",
    "produtoNome": "produto_nome", "quantidade": "quantidade", "unidade": "unidade",
    "valorUnitario": "valor_unitario", "pesoKg": "peso_kg", "tipoTransporte": "tipo_transporte",
    "origem": "origem", "destino": "destino", "dataEntregaDesejada": "data_entrega_desejada",
    "reservaSituacao": "reserva_situacao", "versao": "versao", "atualizadoEm": "atualizado_em",
}
CAMPOS_HISTORICO = {
    "versao": "versao", "statusAnterior": "status_anterior", "status": "status",
    "origem": "origem", "motivo": "motivo", "ocorridoEm": "ocorrido_em",
}


def selecionar(linha, campos):
    return {chave: linha[coluna] for chave, coluna in campos.items()}


def detalhe(linha):
    return {**selecionar(linha, CAMPOS_DETALHE), "frete": None, "entregueEm": None}


def filtro_visibilidade(ator):
    if ator.admin:
        return "TRUE", ()
    if ator.escopo == "BUYER":
        return "comprador_id = %s", (ator.tenant,)
    if ator.escopo == "SUPPLIER":
        return "fornecedor_id = %s", (ator.tenant,)
    return "(comprador_id = %s OR fornecedor_id = %s)", (ator.tenant, ator.tenant)


async def configurar(conexao):
    set_json_loads(loads, conexao)


class Postgres:
    def __init__(self, dsn):
        self.pool = AsyncConnectionPool(
            dsn, open=False, min_size=1, max_size=5, timeout=2,
            kwargs={"row_factory": dict_row, "connect_timeout": 3}, configure=configurar,
            name="pedidos",
        )

    async def pronto(self):
        async with self.pool.connection() as conn:
            versao = await (await conn.execute("SELECT version_num FROM pedidos.alembic_version")).fetchone()
        return bool(versao and versao["version_num"] == "0001")

    async def receber(self, evento, payload, particao, offset):
        async with self.pool.connection() as conn:
            resumo = fingerprint(evento.model_dump())
            novo = await conn.execute("""
                INSERT INTO pedidos.processed_events (source,event_id,fingerprint,topico,particao,"offset",resultado)
                VALUES (%s,%s,%s,%s,%s,%s,'RECEBIDO') ON CONFLICT DO NOTHING
            """, (evento.source, evento.eventId, resumo, evento.eventType, particao, offset))
            if not novo.rowcount:
                anterior = await (await conn.execute("""
                    SELECT fingerprint FROM pedidos.processed_events WHERE source=%s AND event_id=%s
                """, (evento.source, evento.eventId))).fetchone()
                if anterior["fingerprint"] != resumo:
                    await self._auditar(conn, evento.eventType, evento.correlationId, "CONFLITO_EVENTO",
                                       event_id=evento.eventId)
                return

            if isinstance(payload, (Fechamento, Deserto)):
                efeito = await self._fechamento(conn, evento, payload)
            else:
                efeito = await self._projecao(conn, evento, payload)
            await conn.execute("""
                UPDATE pedidos.processed_events SET resultado=%s WHERE source=%s AND event_id=%s
            """, (efeito, evento.source, evento.eventId))

    async def _projecao(self, conn, evento, payload):
        tabela = "produto_referencia" if isinstance(payload, Produto) else "fornecimento_referencia"
        dados = payload.model_dump()
        insercao = await conn.execute(f"""
            INSERT INTO pedidos.{tabela} AS atual (id,dados,event_id,atualizado_em)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET dados=EXCLUDED.dados, event_id=EXCLUDED.event_id,
                atualizado_em=EXCLUDED.atualizado_em
            WHERE atual.atualizado_em < EXCLUDED.atualizado_em
        """, (evento.aggregateId, jsonb(dados), evento.eventId, evento.timestamp))
        if insercao.rowcount:
            return "PROJETADO"
        anterior = await (await conn.execute(
            f"SELECT dados, atualizado_em FROM pedidos.{tabela} WHERE id=%s", (evento.aggregateId,)
        )).fetchone()
        if anterior["atualizado_em"] == evento.timestamp and fingerprint(anterior["dados"]) != fingerprint(dados):
            await self._auditar(conn, evento.eventType, evento.correlationId, "CONFLITO_PROJECAO",
                               event_id=evento.eventId)
            return "CONFLITO_PROJECAO"
        return "PROJECAO_IGNORADA"

    async def _fechamento(self, conn, evento, payload):
        dados = payload.model_dump()
        resumo = fingerprint(dados)
        novo = await conn.execute("""
            INSERT INTO pedidos.resultado_negociacao
                (negociacao_id,status,dados,fingerprint,event_id,correlation_id)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, (payload.negociacaoId, payload.status, jsonb(dados), resumo, evento.eventId, evento.correlationId))
        if not novo.rowcount:
            anterior = await (await conn.execute("""
                SELECT fingerprint FROM pedidos.resultado_negociacao WHERE negociacao_id=%s
            """, (payload.negociacaoId,))).fetchone()
            if anterior["fingerprint"] != resumo:
                await self._auditar(conn, evento.eventType, evento.correlationId, "CONFLITO_RESULTADO",
                                   negociacao_id=payload.negociacaoId, event_id=evento.eventId)
                return "CONFLITO_RESULTADO"
            return "RESULTADO_REPETIDO"
        if isinstance(payload, Deserto):
            return "DESERTO"
        pedido = await self._criar(conn, payload, evento.eventType, evento.correlationId, event_id=evento.eventId)
        return "CRIADO" if pedido else "DADOS_DO_PEDIDO_PENDENTES"

    async def _criar(self, conn, resultado, origem, correlacao, event_id=None, usuario=None):
        produto = await (await conn.execute(
            "SELECT dados FROM pedidos.produto_referencia WHERE id=%s", (resultado.produtoId,)
        )).fetchone()
        oferta = await (await conn.execute(
            "SELECT dados FROM pedidos.fornecimento_referencia WHERE id=%s", (resultado.fornecimentoId,)
        )).fetchone()
        carga = None
        if produto and oferta:
            carga = montar_carga(resultado, Produto.model_validate(produto["dados"]),
                                 Fornecimento.model_validate(oferta["dados"]))
        if carga is None:
            await conn.execute("""
                UPDATE pedidos.resultado_negociacao SET pendente=true, reavaliar_em=now()+interval '1 second'
                WHERE negociacao_id=%s
            """, (resultado.negociacaoId,))
            return None

        dados = dict(carga, pedidoId=uuid4(), valorUnitario=resultado.valorUnitario,
                     origem=jsonb(carga["origem"]), destino=jsonb(carga["destino"]))
        pedido = await (await conn.execute("""
            INSERT INTO pedidos.pedido (id,negociacao_id,comprador_id,fornecedor_id,fornecimento_id,produto_id,
                produto_nome,quantidade,unidade,valor_unitario,valor_total,peso_kg,tipo_transporte,
                origem,destino,data_entrega_desejada)
            VALUES (%(pedidoId)s,%(negociacaoId)s,%(compradorId)s,%(fornecedorId)s,%(fornecimentoId)s,%(produtoId)s,
                %(produtoNome)s,%(quantidade)s,%(unidade)s,%(valorUnitario)s,%(valorTotal)s,%(pesoKg)s,%(tipoTransporte)s,
                %(origem)s,%(destino)s,%(dataEntregaDesejada)s) RETURNING *
        """, dados)).fetchone()
        await conn.execute("""
            INSERT INTO pedidos.pedido_historico (pedido_id,versao,status,origem) VALUES (%s,1,'CRIADO',%s)
        """, (pedido["id"], origem))
        await self._auditar(conn, origem, correlacao, "CRIADO", pedido["id"], resultado.negociacaoId, event_id, usuario)

        payload = dict(carga, pedidoId=str(pedido["id"]), status="CRIADO")
        if payload["dataEntregaDesejada"] is None:
            del payload["dataEntregaDesejada"]
        envelope = {
            "eventId": uuid4(), "eventType": "pedido_criado", "eventVersion": "1.0",
            "timestamp": pedido["criado_em"], "source": "pedidos-service", "correlationId": correlacao,
            "aggregateId": str(pedido["id"]), "payload": payload,
        }
        await conn.execute("""
            INSERT INTO pedidos.outbox (event_id,pedido_id,versao,topico,chave,envelope)
            VALUES (%s,%s,1,'pedido_criado',%s,%s)
        """, (envelope["eventId"], pedido["id"], envelope["aggregateId"], jsonb(envelope)))
        await conn.execute("UPDATE pedidos.resultado_negociacao SET pendente=false WHERE negociacao_id=%s",
                           (resultado.negociacaoId,))
        return detalhe(pedido)

    async def _auditar(self, conn, origem, correlacao, resultado, pedido_id=None, negociacao_id=None,
                       event_id=None, usuario=None):
        await conn.execute("""
            INSERT INTO pedidos.audit_log (pedido_id,negociacao_id,usuario_id,origem,event_id,correlation_id,resultado)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (pedido_id, negociacao_id, usuario, origem, event_id, correlacao, resultado))

    async def reavaliar(self):
        async with self.pool.connection() as conn:
            resultado = await (await conn.execute("""
                SELECT * FROM pedidos.resultado_negociacao
                WHERE pendente AND reavaliar_em<=now()
                ORDER BY reavaliar_em, negociacao_id LIMIT 1 FOR UPDATE SKIP LOCKED
            """)).fetchone()
            if resultado is None:
                return False
            await self._criar(conn, Fechamento.model_validate(resultado["dados"]), "negociacao_fechada",
                              resultado["correlation_id"], event_id=resultado["event_id"])
            return True

    async def criar_http(self, negociacao, ator, correlacao, chave):
        async with self.pool.connection() as conn:
            if chave:
                await conn.execute("""
                    DELETE FROM pedidos.idempotency_keys WHERE usuario_id=%s AND chave=%s AND expira_em<=now()
                """, (ator.usuario, chave))
                inserido = await conn.execute("""
                    INSERT INTO pedidos.idempotency_keys (usuario_id,chave,negociacao_id)
                    VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
                """, (ator.usuario, chave, negociacao))
                if not inserido.rowcount:
                    anterior = await (await conn.execute("""
                        SELECT negociacao_id,status,resposta FROM pedidos.idempotency_keys
                        WHERE usuario_id=%s AND chave=%s
                    """, (ator.usuario, chave))).fetchone()
                    if anterior["negociacao_id"] != negociacao:
                        raise ErroPedido("IDEMPOTENCIA_CONFLITO")
                    return anterior["status"], anterior["resposta"]

            resultado = await (await conn.execute("""
                SELECT * FROM pedidos.resultado_negociacao WHERE negociacao_id=%s FOR UPDATE
            """, (negociacao,))).fetchone()
            if resultado is None or resultado["status"] != "ENCERRADO":
                raise ErroPedido("RESULTADO_INDISPONIVEL")

            existente = await (await conn.execute(
                "SELECT * FROM pedidos.pedido WHERE negociacao_id=%s", (negociacao,)
            )).fetchone()
            if existente:
                status, resposta = 200, detalhe(existente)
            else:
                resposta = await self._criar(conn, Fechamento.model_validate(resultado["dados"]), "HTTP",
                                             correlacao, usuario=ator.usuario)
                if resposta is None:
                    raise ErroPedido("DADOS_DO_PEDIDO_PENDENTES")
                status = 201

            if chave:
                await conn.execute("""
                    UPDATE pedidos.idempotency_keys SET status=%s,resposta=%s WHERE usuario_id=%s AND chave=%s
                """, (status, jsonb(resposta), ator.usuario, chave))
            return status, resposta

    async def consultar(self, pedido_id, ator, pagina=None, tamanho=None):
        condicao, params = filtro_visibilidade(ator)
        async with self.pool.connection() as conn:
            pedido = await (await conn.execute(
                f"SELECT * FROM pedidos.pedido WHERE id=%s AND {condicao}", (pedido_id, *params)
            )).fetchone()
            if pedido is None:
                raise ErroPedido("PEDIDO_NAO_ENCONTRADO")
            if pagina is None:
                return detalhe(pedido)
            return await self._pagina(conn, "pedido_historico", "pedido_id=%s", (pedido_id,), "versao ASC",
                                      CAMPOS_HISTORICO, pagina, tamanho)

    async def listar(self, ator, pagina, tamanho, status, negociacao):
        condicao, params = filtro_visibilidade(ator)
        if status is not None:
            condicao += " AND status=%s"
            params += (status,)
        if negociacao is not None:
            condicao += " AND negociacao_id=%s"
            params += (negociacao,)
        async with self.pool.connection() as conn:
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return await self._pagina(conn, "pedido", condicao, params, "criado_em DESC, id DESC",
                                      CAMPOS_RESUMO, pagina, tamanho)

    async def _pagina(self, conn, tabela, condicao, params, ordem, campos, pagina, tamanho):
        contagem = await (await conn.execute(
            f"SELECT count(*) AS total FROM pedidos.{tabela} WHERE {condicao}", params
        )).fetchone()
        total, inicio = contagem["total"], (pagina - 1) * tamanho
        linhas = []
        if inicio < total:
            linhas = await (await conn.execute(
                f"SELECT * FROM pedidos.{tabela} WHERE {condicao} ORDER BY {ordem} LIMIT %s OFFSET %s",
                (*params, tamanho, inicio),
            )).fetchall()
        return {"items": [selecionar(linha, campos) for linha in linhas], "page": pagina,
                "size": tamanho, "total": total}
