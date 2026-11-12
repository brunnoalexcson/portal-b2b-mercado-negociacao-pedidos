import asyncio
import logging
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.errors import KafkaError

from app.contratos import TOPICOS, validar_evento
from app.jsonio import dumps, loads

log = logging.getLogger(__name__)
TOPICOS_NECESSARIOS = set(TOPICOS) | {"pedido_criado", "pedidos.dlt"}
SENSIVEIS = {"token", "authorization", "senha", "password", "accesstoken", "refreshtoken", "secret"}


def redigir(dados):
    if isinstance(dados, dict):
        return {k: "[omitido]" if k.lower().replace("_", "").replace("-", "") in SENSIVEIS
                else redigir(v) for k, v in dados.items()}
    if isinstance(dados, list):
        return [redigir(v) for v in dados]
    return dados


class Kafka:
    def __init__(self, banco, servidores, grupo):
        self.banco = banco
        self.servidores = servidores
        self.grupo = grupo
        self.ultima_verificacao = float("-inf")

    def pronto(self):
        return asyncio.get_running_loop().time() - self.ultima_verificacao < 8

    async def run(self):
        while True:
            try:
                produtor = AIOKafkaProducer(bootstrap_servers=self.servidores, enable_idempotence=True)
                try:
                    await produtor.start()
                    async with asyncio.TaskGroup() as grupo:
                        grupo.create_task(self._consumir(produtor))
                        grupo.create_task(self._publicar_outbox(produtor))
                finally:
                    self.ultima_verificacao = float("-inf")
                    await produtor.stop()
            except Exception as exc:
                log.warning("Reiniciando consumidor e publicador Kafka: %s", type(exc).__name__)
                await asyncio.sleep(1)

    async def _consumir(self, produtor):
        consumidor = AIOKafkaConsumer(
            *TOPICOS, bootstrap_servers=self.servidores, group_id=self.grupo,
            enable_auto_commit=False, auto_offset_reset="earliest", request_timeout_ms=5000,
        )
        try:
            await consumidor.start()
            while True:
                agora = asyncio.get_running_loop().time()
                if agora - self.ultima_verificacao >= 3:
                    topicos = await asyncio.wait_for(consumidor.topics(), timeout=4)
                    if not TOPICOS_NECESSARIOS <= topicos:
                        raise RuntimeError("Tópicos de Pedidos não provisionados")
                    self.ultima_verificacao = agora
                try:
                    mensagem = await asyncio.wait_for(consumidor.getone(), timeout=1)
                except TimeoutError:
                    continue
                chave, dados = None, None
                try:
                    if mensagem.key is not None:
                        chave = mensagem.key.decode("utf-8")
                    dados = loads(mensagem.value.decode("utf-8") if mensagem.value else "")
                    evento, payload = validar_evento(mensagem.topic, chave, dados)
                except (ValueError, RecursionError):
                    await self._dlt(produtor, mensagem, chave, dados)
                else:
                    await self.banco.receber(evento, payload, mensagem.partition, mensagem.offset)
                particao = TopicPartition(mensagem.topic, mensagem.partition)
                await consumidor.commit({particao: mensagem.offset + 1})
        finally:
            self.ultima_verificacao = float("-inf")
            await consumidor.stop()

    async def _dlt(self, produtor, mensagem, chave, dados):
        origem = f"{mensagem.topic}:{mensagem.partition}:{mensagem.offset}"
        event_id = str(uuid5(NAMESPACE_URL, f"pedidos.dlt:{origem}"))
        chave = chave if chave and len(chave) <= 128 else origem
        correlacao = dados.get("correlationId") if isinstance(dados, dict) else None
        if not isinstance(correlacao, str) or not correlacao.strip() or len(correlacao) > 128:
            correlacao = event_id
        try:
            original = redigir(dados)
            if len(dumps(original).encode()) > 64 * 1024:
                original = {"referencia": origem, "conteudo": "excedeu o limite da DLT"}
        except RecursionError:
            original = None
        falha = {
            "eventId": event_id, "eventType": "pedidos.dlt", "eventVersion": "1.0",
            "timestamp": datetime.now(timezone.utc), "source": "pedidos-service",
            "correlationId": correlacao, "aggregateId": chave,
            "payload": {"originalEvent": original, "error": "EVENTO_INVALIDO",
                        "stackTrace": "", "failedAtAttempt": 1, "referencia": origem},
        }
        await asyncio.wait_for(
            produtor.send_and_wait("pedidos.dlt", dumps(falha).encode(), key=chave.encode()), timeout=5
        )

    async def _publicar_outbox(self, produtor):
        while True:
            async with self.banco.pool.connection() as conn:
                evento = await (await conn.execute("""
                    SELECT event_id, topico, chave, envelope, tentativas
                    FROM pedidos.outbox
                    WHERE confirmado_em IS NULL AND proximo_envio<=now()
                    ORDER BY proximo_envio, event_id LIMIT 1 FOR UPDATE SKIP LOCKED
                """)).fetchone()
                if evento:
                    await self._enviar(conn, produtor, evento)
            if evento is None:
                await asyncio.sleep(0.25)

    async def _enviar(self, conn, produtor, evento):
        try:
            await asyncio.wait_for(
                produtor.send_and_wait(evento["topico"], dumps(evento["envelope"]).encode(),
                                       key=evento["chave"].encode()), timeout=5
            )
        except (KafkaError, TimeoutError):
            atraso = min(30, 2 ** min(evento["tentativas"], 5))
            await conn.execute("""
                UPDATE pedidos.outbox SET tentativas=tentativas+1, proximo_envio=now()+%s*interval '1 second'
                WHERE event_id=%s
            """, (atraso, evento["event_id"]))
            log.warning("Publicação pendente de confirmação: eventId=%s", evento["event_id"])
        else:
            await conn.execute("UPDATE pedidos.outbox SET confirmado_em=now() WHERE event_id=%s",
                               (evento["event_id"],))
