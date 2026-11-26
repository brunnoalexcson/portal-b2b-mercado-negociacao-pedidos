"""EventBus real: outbox transacional, publicador, consumidor com offset manual e DLT."""
import asyncio
import logging
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.errors import KafkaError

from app.contratos import CONSUMIDOS, CORRELACAO_RE, PUBLICADOS, envelope
from app.jsonio import dumps, loads
from app.portas import RepositorioIndisponivel

log = logging.getLogger("pedidos")
NECESSARIOS = set(CONSUMIDOS) | set(PUBLICADOS)
LIMITE_DLT = 64 * 1024
SENSIVEL_TEXTO = re.compile(r"authorization|bearer|token|senha|password|secret|api_key|api-key|apikey|\\u",
                            re.IGNORECASE)
ESPERAS = (1, 2, 4, 8, 16)
_dlt_pendente = ContextVar("pedidos_dlt_pendente", default=None)


def original_bruto(valor):
    """originalEvent de bytes que não são JSON UTF-8 (§7.4)."""
    texto = (valor or b"").decode("utf-8", errors="replace")
    if SENSIVEL_TEXTO.search(texto):
        return None
    return texto.encode("utf-8")[:LIMITE_DLT].decode("utf-8", errors="ignore")


class KafkaEventBus:
    def __init__(self, repositorio, servidores, grupo):
        self.repo = repositorio
        self.servidores = servidores
        self.grupo = grupo
        self.handlers = {}
        self.metricas = None
        self._verificado_em = float("-inf")

    # ------------------------------------------------------------ porta EventBus

    def subscribe(self, topic, handler):
        self.handlers[topic] = handler

    def publish(self, topic, key, message):
        if topic == "pedidos.dlt":
            pendentes = _dlt_pendente.get()
            if pendentes is None:
                log.error("DLT fora do consumidor descartada", extra={"correlationId": message.get("correlationId")})
                return
            pendentes.append(message)
            return
        self.repo.gravar_outbox(topic, key, message)

    def disponivel(self):
        return time.monotonic() - self._verificado_em < 8

    # ------------------------------------------------------------ ciclo de vida

    async def executar(self):
        while True:
            produtor = AIOKafkaProducer(bootstrap_servers=self.servidores, acks="all", enable_idempotence=True,
                                        request_timeout_ms=5000)
            try:
                await produtor.start()
                async with asyncio.TaskGroup() as grupo:
                    grupo.create_task(self._consumir(produtor))
                    grupo.create_task(self._publicar_outbox(produtor))
                    grupo.create_task(self._acompanhar_outbox())
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # TaskGroup agrupa as falhas
                causas = exc.exceptions if isinstance(exc, BaseExceptionGroup) else (exc,)
                log.warning("Reiniciando Kafka: %s",
                            "; ".join(f"{type(c).__name__}: {str(c)[:200]}" for c in causas))
                await asyncio.sleep(5)
            finally:
                self._verificado_em = float("-inf")
                try:
                    await produtor.stop()
                except Exception:
                    log.debug("Produtor já parado")

    # ------------------------------------------------------------ consumidor

    async def _consumir(self, produtor):
        consumidor = AIOKafkaConsumer(*self.handlers, bootstrap_servers=self.servidores, group_id=self.grupo,
                                      enable_auto_commit=False, auto_offset_reset="earliest",
                                      request_timeout_ms=5000)
        await consumidor.start()
        try:
            while True:
                if time.monotonic() - self._verificado_em >= 3:
                    topicos = await asyncio.wait_for(consumidor.topics(), timeout=2)
                    if not NECESSARIOS <= topicos:
                        raise RuntimeError("Tópicos de Pedidos não provisionados: "
                                           + ", ".join(sorted(NECESSARIOS - topicos)))
                    self._verificado_em = time.monotonic()
                lotes = await consumidor.getmany(timeout_ms=1000, max_records=1)
                for particao, mensagens in lotes.items():
                    for mensagem in mensagens:
                        if not await self._tratar(produtor, consumidor, mensagem):
                            consumidor.seek(particao, mensagem.offset)
                            await asyncio.sleep(5)
                            break
        finally:
            await consumidor.stop()

    async def _tratar(self, produtor, consumidor, mensagem):
        """Devolve False quando a mensagem deve ser reentregue (banco indisponível)."""
        origem = f"{mensagem.topic}:{mensagem.partition}:{mensagem.offset}"
        try:
            chave = mensagem.key.decode("utf-8") if mensagem.key is not None else None
        except UnicodeDecodeError:
            chave = None
        chave = chave if chave and len(chave) <= 128 else origem
        try:
            dados = loads(mensagem.value.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, AttributeError, RecursionError) as exc:
            falha = envelope("pedidos.dlt", chave, {
                "originalEvent": original_bruto(mensagem.value), "error": "EVENTO_INVALIDO",
                "stackTrace": f"{type(exc).__name__}: conteúdo não é JSON UTF-8", "failedAtAttempt": 1},
                str(uuid4()), datetime.now(timezone.utc))
            await self._enviar_dlt(produtor, falha, chave, origem)
        else:
            pendentes = []
            marca = _dlt_pendente.set(pendentes)
            try:
                await asyncio.to_thread(self.handlers[mensagem.topic], dados)
            except RepositorioIndisponivel:
                log.warning("Banco indisponível; mensagem será reentregue em 5 s", extra={"codigo": origem})
                return False
            finally:
                _dlt_pendente.reset(marca)
            for falha in pendentes:
                await self._enviar_dlt(produtor, falha, chave, origem)
        await consumidor.commit({TopicPartition(mensagem.topic, mensagem.partition): mensagem.offset + 1})
        return True

    async def _enviar_dlt(self, produtor, falha, chave, origem):
        falha = {**falha, "eventId": str(uuid5(NAMESPACE_URL, f"pedidos.dlt:{origem}")), "aggregateId": chave}
        if not isinstance(falha.get("correlationId"), str) or not CORRELACAO_RE.fullmatch(falha["correlationId"]):
            falha["correlationId"] = str(uuid4())
        await asyncio.wait_for(produtor.send_and_wait("pedidos.dlt", dumps(falha).encode("utf-8"),
                                                      key=chave.encode("utf-8")), timeout=5)

    # ------------------------------------------------------------ publicador da outbox

    async def _publicar_outbox(self, produtor):
        laco = asyncio.get_running_loop()
        while True:
            try:
                enviado = await asyncio.to_thread(self._enviar_um, produtor, laco)
            except RepositorioIndisponivel:
                enviado = False
            if not enviado:
                await asyncio.sleep(1)

    def _enviar_um(self, produtor, laco):
        """Roda numa thread: segura o registro (FOR UPDATE SKIP LOCKED) enquanto o envio acontece no laço."""
        with self.repo.transacao():
            registro = self.repo.proximo_outbox()
            if registro is None:
                return False
            envio = asyncio.run_coroutine_threadsafe(asyncio.wait_for(produtor.send_and_wait(
                registro["topico"], dumps(registro["envelope"]).encode("utf-8"),
                key=registro["chave"].encode("utf-8")), timeout=5), laco)
            try:
                envio.result(timeout=6)
            except (KafkaError, TimeoutError, asyncio.TimeoutError) as exc:
                espera = ESPERAS[registro["tentativas"]] if registro["tentativas"] < len(ESPERAS) else 30
                self.repo.reagendar_outbox(registro["event_id"], espera)
                log.warning("Envio da outbox falhou (%s); nova tentativa em %s s", type(exc).__name__, espera,
                            extra={"eventId": str(registro["event_id"])})
            else:
                self.repo.confirmar_outbox(registro["event_id"])
            return True

    async def _acompanhar_outbox(self):
        while True:
            await asyncio.sleep(5)
            try:
                pendentes, idade = await asyncio.to_thread(self.repo.estado_outbox)
            except Exception:
                continue
            if self.metricas:
                self.metricas.outbox_pendentes.set(pendentes)
                self.metricas.outbox_idade.set(idade)
            if idade > 60:
                log.warning("Outbox com registro esperando há mais de 60 s", extra={"codigo": "OUTBOX_ATRASADA"})
