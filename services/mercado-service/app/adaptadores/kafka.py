"""`EventBus` real: outbox transacional, publicador por polling, consumidor `aiokafka` e DLT direta (§5.1)."""
import asyncio
import logging
import threading
from typing import Callable

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.errors import KafkaError

from app import contratos
from app.adaptadores.postgres import RepositorioPostgres
from app.jsonio import dumps, loads
from app.portas import Clock, RepositorioIndisponivel

log = logging.getLogger("app.adaptadores.kafka")

TOPICOS_PUBLICADOS = ("modo_negociacao_definido", "leilao_iniciado", contratos.TOPICO_DLT)
ESPERA_REENTREGA = 5
TIMEOUT_ENVIO = 5

SQL_PROXIMO = """
    SELECT o.id, o.event_id, o.topico, o.chave, o.envelope, o.tentativas
    FROM mercado.outbox o
    WHERE o.confirmado_em IS NULL AND o.proximo_envio <= now()
      AND NOT EXISTS (SELECT 1 FROM mercado.outbox a
                      WHERE a.chave = o.chave AND a.confirmado_em IS NULL AND a.id < o.id)
    ORDER BY o.id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
"""


class FalhaDlt(Exception):
    """O envio direto para a `mercado.dlt` não foi confirmado pelo broker."""


class EventBusKafka:
    def __init__(self, repositorio: RepositorioPostgres, servidores: str, grupo: str, clock: Clock,
                 ao_dlt: Callable[[str], None] | None = None):
        self.repositorio = repositorio
        self.servidores = servidores
        self.grupo = grupo
        self.clock = clock
        self.ao_dlt = ao_dlt
        self.handlers: dict[str, Callable[[dict], None]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.produtor: AIOKafkaProducer | None = None
        self._local = threading.local()
        self._ultima_verificacao = float("-inf")
        self._pendentes = 0

    # ------------------------------------------------------------ porta EventBus

    def subscribe(self, topic: str, handler: Callable[[dict], None]) -> None:
        self.handlers[topic] = handler

    def publish(self, topic: str, key: str, message: dict) -> None:
        if self.repositorio.conexao_atual() is not None:
            self.repositorio.gravar_outbox(topic, key, message)
            return
        # Fora de transação só há publicações na DLT (§5.1): vão direto ao broker, com acks=all.
        try:
            if self.loop is None or self.produtor is None:
                raise FalhaDlt("Produtor Kafka não iniciado")
            futuro = asyncio.run_coroutine_threadsafe(self._enviar(topic, key, message), self.loop)
            futuro.result(timeout=TIMEOUT_ENVIO + 1)
        except Exception as exc:
            self._local.falha_dlt = True
            raise FalhaDlt(str(exc)) from exc

    def disponivel(self) -> bool:
        if self.loop is None:
            return False
        return self.loop.time() - self._ultima_verificacao < 8

    def pendentes(self) -> int:
        return self._pendentes

    # ------------------------------------------------------------ ciclo de vida

    async def run(self):
        self.loop = asyncio.get_running_loop()
        while True:
            produtor = AIOKafkaProducer(bootstrap_servers=self.servidores, acks="all", enable_idempotence=True,
                                        request_timeout_ms=TIMEOUT_ENVIO * 1000)
            try:
                await produtor.start()
                self.produtor = produtor
                async with asyncio.TaskGroup() as grupo:
                    grupo.create_task(self._consumir())
                    grupo.create_task(self._publicar_outbox())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Reiniciando Kafka: %s", type(exc).__name__)
                await asyncio.sleep(1)
            finally:
                self._ultima_verificacao = float("-inf")
                self.produtor = None
                await produtor.stop()

    async def _enviar(self, topico: str, chave: str, mensagem: dict):
        await asyncio.wait_for(self.produtor.send_and_wait(topico, dumps(mensagem).encode("utf-8", "surrogatepass"),
                                                           key=chave.encode("utf-8", "surrogatepass")),
                               timeout=TIMEOUT_ENVIO)

    # ------------------------------------------------------------ consumidor

    async def _consumir(self):
        topicos = tuple(self.handlers)
        consumidor = AIOKafkaConsumer(*topicos, bootstrap_servers=self.servidores, group_id=self.grupo,
                                      enable_auto_commit=False, auto_offset_reset="earliest",
                                      request_timeout_ms=TIMEOUT_ENVIO * 1000)
        await consumidor.start()
        try:
            while True:
                await self._verificar_topicos(consumidor, topicos)
                try:
                    mensagem = await asyncio.wait_for(consumidor.getone(), timeout=1)
                except TimeoutError:
                    continue
                while True:
                    try:
                        if await self._processar(mensagem):
                            await consumidor.commit({TopicPartition(mensagem.topic, mensagem.partition):
                                                     mensagem.offset + 1})
                            break
                        motivo = "DLT não confirmada"
                    except RepositorioIndisponivel:
                        motivo = "banco indisponível"
                    except (KafkaError, TimeoutError) as exc:
                        motivo = f"broker: {type(exc).__name__}"
                    log.warning("Mensagem será reentregue em %ss (%s)", ESPERA_REENTREGA, motivo)
                    await asyncio.sleep(ESPERA_REENTREGA)
                    await self._verificar_topicos(consumidor, topicos)
        finally:
            await consumidor.stop()

    async def _verificar_topicos(self, consumidor, topicos):
        agora = self.loop.time()
        if agora - self._ultima_verificacao < 3:
            return
        existentes = await asyncio.wait_for(consumidor.topics(), timeout=4)
        faltando = (set(topicos) | set(TOPICOS_PUBLICADOS)) - existentes
        if faltando:
            log.warning("Tópicos não provisionados: %s", ", ".join(sorted(faltando)))
            return
        self._ultima_verificacao = agora

    async def _processar(self, mensagem) -> bool:
        """True quando o offset pode ser confirmado."""
        try:
            dados = loads((mensagem.value or b"").decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            original = (mensagem.value or b"").decode("utf-8", errors="replace")
            chave, envelope = contratos.evento_dlt(mensagem.topic, original, "EVENTO_INVALIDO",
                                                   "Mensagem não é JSON em UTF-8.", "", 1, self.clock.now())
            await self._enviar(contratos.TOPICO_DLT, chave, envelope)
            if self.ao_dlt is not None:
                self.ao_dlt(mensagem.topic)
            return True
        handler = self.handlers[mensagem.topic]
        return not await asyncio.to_thread(self._executar_handler, handler, dados)

    def _executar_handler(self, handler, dados) -> bool:
        """Roda o handler fora do laço de eventos; devolve True se uma publicação na DLT falhou."""
        self._local.falha_dlt = False
        handler(dados)
        return self._local.falha_dlt

    # ------------------------------------------------------------ publicador da outbox

    async def _publicar_outbox(self):
        while True:
            enviou = await asyncio.to_thread(self._ciclo_outbox)
            if not enviou:
                await asyncio.sleep(1)

    def _ciclo_outbox(self) -> bool:
        try:
            with self.repositorio.pool.connection() as conexao:
                with conexao.transaction():
                    registro = conexao.execute(SQL_PROXIMO).fetchone()
                    if registro is None:
                        self._pendentes = conexao.execute(
                            "SELECT count(*) AS n FROM mercado.outbox WHERE confirmado_em IS NULL").fetchone()["n"]
                        return False
                    try:
                        futuro = asyncio.run_coroutine_threadsafe(
                            self._enviar(registro["topico"], registro["chave"], registro["envelope"]), self.loop)
                        futuro.result(timeout=TIMEOUT_ENVIO + 1)
                    except Exception as exc:
                        espera = min(30, 2 ** registro["tentativas"])
                        conexao.execute("UPDATE mercado.outbox SET tentativas = tentativas + 1, "
                                        "proximo_envio = now() + %s * interval '1 second' WHERE id = %s",
                                        (espera, registro["id"]))
                        log.warning("Envio da outbox falhou (%s); nova tentativa em %ss",
                                    type(exc).__name__, espera, extra={"eventId": str(registro["event_id"])})
                    else:
                        conexao.execute("UPDATE mercado.outbox SET confirmado_em = now() WHERE id = %s",
                                        (registro["id"],))
                    return True
        except Exception as exc:
            log.warning("Publicador da outbox aguardando o banco: %s", type(exc).__name__)
            return False
