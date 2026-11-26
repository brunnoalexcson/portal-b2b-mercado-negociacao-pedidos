import asyncio
import logging
import time
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.errors import KafkaError
from psycopg.types.json import Jsonb

from app.jsonio import dumps, loads
from app.portas import RepositorioIndisponivel

log = logging.getLogger(__name__)
TOPICOS = {"leilao_iniciado", "modo_negociacao_definido", "pedido_atualizado", "pedido_temporario_solicitado",
           "lance_realizado", "negociacao_fechada", "negociacao.dlt"}


class Kafka:
    def __init__(self, repositorio, servidores, grupo):
        self.repositorio = repositorio
        self.servidores = servidores
        self.grupo = grupo
        self.handlers = {}
        self.ultima_verificacao = float("-inf")
        self.faltando = set()

    def publish(self, topic, key, message):
        # grava na outbox pela conexão da transação; o publicador envia depois do commit
        self.repositorio.conexao().execute(
            "INSERT INTO negociacao.outbox (event_id, topico, chave, envelope) VALUES (%s, %s, %s, %s)",
            (message["eventId"], topic, key, Jsonb(message, dumps=dumps)),
        )

    def subscribe(self, topic, handler):
        self.handlers[topic] = handler

    def disponivel(self):
        # chamado de thread do FastAPI: relógio monotônico, não o do laço de eventos
        # 8 s sem resposta do broker = barramento fora; a prontidão precisa cair em até 10 s
        return time.monotonic() - self.ultima_verificacao < 8

    async def run(self):
        while True:
            try:
                produtor = AIOKafkaProducer(bootstrap_servers=self.servidores, acks="all", enable_idempotence=True)
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
                # broker com falha: a mensagem sem commit volta a ser entregue a cada 5 s
                await asyncio.sleep(5)

    async def _consumir(self, produtor):
        consumidor = AIOKafkaConsumer(
            *self.handlers, bootstrap_servers=self.servidores, group_id=self.grupo,
            enable_auto_commit=False, auto_offset_reset="earliest", request_timeout_ms=5000,
        )
        try:
            await consumidor.start()
            while True:
                # confere o broker a cada 3 s, dentro da janela de 8 s da prontidão
                if time.monotonic() - self.ultima_verificacao >= 3:
                    # metadados sem resposta em 4 s contam como broker fora
                    topicos = await asyncio.wait_for(consumidor.topics(), timeout=4)
                    # tópico faltando não derruba a prontidão: a outbox reenvia até ele existir
                    if (faltando := TOPICOS - topicos) != self.faltando:
                        if faltando:
                            log.warning("Tópicos não provisionados: %s", ", ".join(sorted(faltando)))
                        self.faltando = faltando
                    self.ultima_verificacao = time.monotonic()
                if not self.handlers:
                    # sem tópico assinado o consumidor só confere o broker para a prontidão
                    await asyncio.sleep(1)
                    continue
                try:
                    mensagem = await asyncio.wait_for(consumidor.getone(), timeout=1)
                except TimeoutError:
                    continue
                await self._entregar(consumidor, produtor, mensagem)
        finally:
            await consumidor.stop()

    async def _entregar(self, consumidor, produtor, mensagem):
        particao = TopicPartition(mensagem.topic, mensagem.partition)
        try:
            dados = loads(mensagem.value.decode("utf-8"))
        except (ValueError, RecursionError, AttributeError):
            await self._dlt(produtor, mensagem)
        else:
            try:
                await asyncio.to_thread(self.handlers[mensagem.topic], dados)
            except RepositorioIndisponivel:
                log.warning("Banco indisponível; mensagem reentregue em 5 s: %s:%s:%s",
                            mensagem.topic, mensagem.partition, mensagem.offset)
                consumidor.seek(particao, mensagem.offset)
                await asyncio.sleep(5)
                return
        await consumidor.commit({particao: mensagem.offset + 1})

    async def _dlt(self, produtor, mensagem):
        # bytes que não são JSON: sem aggregateId nem eventId de origem, a chave é "desconhecido"
        origem = f"{mensagem.topic}:{mensagem.partition}:{mensagem.offset}"
        event_id = str(uuid5(NAMESPACE_URL, f"negociacao.dlt:{origem}"))
        falha = {
            "eventId": event_id, "eventType": "negociacao.dlt", "eventVersion": "1.0",
            "timestamp": datetime.now(timezone.utc), "source": "negociacao-service",
            "correlationId": event_id, "aggregateId": "desconhecido",
            "payload": {
                "topicoOrigem": mensagem.topic,
                # JSON só carrega Unicode: byte inválido vira \xNN, que ainda dá para recuperar
                "originalEvent": (mensagem.value or b"").decode("utf-8", errors="backslashreplace"),
                "error": "EVENTO_INVALIDO", "detalhe": "Mensagem não é JSON válido.",
                "stackTrace": "", "failedAtAttempt": 1,
            },
        }
        await asyncio.wait_for(
            produtor.send_and_wait("negociacao.dlt", dumps(falha).encode(), key=b"desconhecido"), timeout=5,
        )

    async def _publicar_outbox(self, produtor):
        laco = asyncio.get_running_loop()
        while True:
            try:
                enviou = await asyncio.to_thread(self._publicar_um, laco, produtor)
            except RepositorioIndisponivel:
                enviou = False
            if not enviou:
                # outbox vazia: espera curta antes de olhar de novo
                await asyncio.sleep(0.25)

    def _publicar_um(self, laco, produtor):
        with self.repositorio.transacao():
            conn = self.repositorio.conexao()
            # só sai o pendente mais antigo de cada chave: um reagendado segura os seguintes da mesma negociação
            evento = conn.execute("""
                SELECT event_id, topico, chave, envelope, tentativas FROM negociacao.outbox o
                WHERE confirmado_em IS NULL AND proximo_envio <= now()
                  AND NOT EXISTS (SELECT 1 FROM negociacao.outbox a WHERE a.chave = o.chave
                                  AND a.confirmado_em IS NULL AND a.sequencia < o.sequencia)
                ORDER BY sequencia LIMIT 1 FOR UPDATE SKIP LOCKED
            """).fetchone()
            if evento is None:
                return False
            # o registro fica bloqueado durante o envio: a outra réplica não envia o mesmo eventId junto
            envio = asyncio.run_coroutine_threadsafe(produtor.send_and_wait(
                evento["topico"], dumps(evento["envelope"]).encode(), key=evento["chave"].encode(),
            ), laco)
            try:
                envio.result(timeout=5)
            except (KafkaError, TimeoutError):
                envio.cancel()
                conn.execute("""
                    UPDATE negociacao.outbox
                    SET tentativas = tentativas + 1, proximo_envio = now() + %s * interval '1 second'
                    WHERE event_id = %s
                """, (min(30, 2 ** min(evento["tentativas"], 5)), evento["event_id"]))
                log.warning("Publicação pendente de confirmação", extra={"eventId": str(evento["event_id"])})
            else:
                conn.execute("UPDATE negociacao.outbox SET confirmado_em = now() WHERE event_id = %s",
                             (evento["event_id"],))
        return True
