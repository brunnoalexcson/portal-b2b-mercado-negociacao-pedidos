"""Dublês em memória das portas do §5.1 para os testes de aceitação."""
import copy
from contextlib import contextmanager
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric import rsa

from app.dominio import utc
from app.portas import RepositorioIndisponivel

KID = "teste-1"
CHAVE_PRIVADA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OUTRA_CHAVE_PRIVADA = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _copia(valor):
    return copy.deepcopy(valor)


class Relogio:
    """`Clock` ajustável; `sleep` registra as esperas sem bloquear."""

    def __init__(self, agora: datetime = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)):
        self.agora = agora
        self.esperas: list[float] = []

    def now(self) -> datetime:
        return self.agora

    def sleep(self, segundos: float) -> None:
        self.esperas.append(segundos)


class Barramento:
    """`EventBus` em memória. Acumula as publicações feitas durante uma transação do repositório e só as expõe em
    `publicados` no commit, descartando-as se a transação abortar."""

    def __init__(self, repositorio: "Repositorio | None" = None):
        self.handlers: dict[str, object] = {}
        self.publicados: list[tuple[str, str, dict]] = []
        self.pronto = True
        self._pendentes: list[tuple[str, str, dict]] = []
        self._repositorio: Repositorio | None = None
        if repositorio is not None:
            repositorio.vincular(self)

    def subscribe(self, topic: str, handler) -> None:
        self.handlers[topic] = handler

    def publish(self, topic: str, key: str, message: dict) -> None:
        registro = (topic, key, _copia(message))
        if self._repositorio is not None and self._repositorio.em_transacao:
            self._pendentes.append(registro)
        else:
            self.publicados.append(registro)

    def disponivel(self) -> bool:
        if isinstance(self.pronto, Exception):
            raise self.pronto
        return bool(self.pronto)

    def confirmar(self) -> None:
        self.publicados.extend(self._pendentes)
        self._pendentes.clear()

    def descartar(self) -> None:
        self._pendentes.clear()

    def entregar(self, topic: str, message: dict) -> None:
        self.handlers[topic](_copia(message))

    def de(self, topic: str) -> list[tuple[str, dict]]:
        return [(chave, mensagem) for topico, chave, mensagem in self.publicados if topico == topic]

    def do_topico(self, topic: str) -> list[dict]:
        return [mensagem for topico, _, mensagem in self.publicados if topico == topic]


class Chaves:
    """`ChavesPublicas` em memória com a chave RSA de teste (`kid = "teste-1"`)."""

    def __init__(self):
        self.chaves = {KID: CHAVE_PRIVADA.public_key()}
        self.falha: Exception | bool = False

    def obter(self, kid: str):
        if self.falha:
            if isinstance(self.falha, Exception):
                raise self.falha
            raise ConnectionError("JWKS indisponível")
        return self.chaves.get(kid)


class Repositorio:
    """`NegociacaoRepositorio` em memória com transação atômica e injeção de falhas."""

    def __init__(self, barramento: Barramento | None = None):
        self._dados = {
            "processados": {},
            "negociacoes": {},
            "ofertas": {},
            "lances": {},
            "propostas": {},
            "idempotencia": {},
        }
        self._antes = None
        self.pronto = True
        self.ouvintes: list[Barramento] = []
        self._falhas: dict[str, list[object]] = {}
        if barramento is not None:
            self.vincular(barramento)

    def vincular(self, barramento: Barramento) -> None:
        barramento._repositorio = self
        if barramento not in self.ouvintes:
            self.ouvintes.append(barramento)

    @property
    def em_transacao(self) -> bool:
        return self._antes is not None

    def falhar(self, metodo: str, excecao=ValueError("falha simulada"), vezes: int | None = None) -> None:
        """Configura `metodo` para lançar `excecao` nas próximas `vezes` chamadas (ou sempre, se `vezes` for `None`)."""
        if vezes == 0:
            self._falhas.pop(metodo, None)
        else:
            self._falhas[metodo] = [excecao, vezes]

    def falhar_processamento(self, vezes: int | None = None, excecao=ValueError("falha no processamento")) -> None:
        """Injeta falha em `evento_processado`, que roda na transação de processamento mas não na da DLT."""
        self.falhar("evento_processado", excecao, vezes=vezes)

    def limpar_falhas(self) -> None:
        self._falhas.clear()

    def _checar(self, metodo: str) -> None:
        config = self._falhas.get(metodo)
        if config is None:
            return
        excecao, restantes = config
        if restantes is not None:
            if restantes <= 1:
                self._falhas.pop(metodo, None)
            else:
                config[1] = restantes - 1
        if isinstance(excecao, type):
            raise excecao(f"falha simulada em {metodo}")
        raise type(excecao)(*excecao.args)

    def _exigir_transacao(self, metodo: str) -> None:
        self._checar(metodo)
        if self._antes is None:
            raise RuntimeError(f"{metodo} chamado fora de repositorio.transacao()")

    @contextmanager
    def transacao(self):
        self._checar("transacao")
        if self._antes is not None:
            raise RuntimeError("Transação aninhada")
        self._antes = _copia(self._dados)
        try:
            yield
        except BaseException:
            self._dados = self._antes
            self._antes = None
            for ouvinte in self.ouvintes:
                ouvinte.descartar()
            raise
        self._antes = None
        for ouvinte in self.ouvintes:
            ouvinte.confirmar()

    def disponivel(self) -> bool:
        self._checar("disponivel")
        if isinstance(self.pronto, Exception):
            raise self.pronto
        return bool(self.pronto)

    # consumo idempotente

    def evento_processado(self, source: str, event_id: str) -> bool:
        self._checar("evento_processado")
        return (source, event_id) in self._dados["processados"]

    def registrar_evento_processado(self, source: str, event_id: str, topico: str) -> None:
        self._exigir_transacao("registrar_evento_processado")
        self._dados["processados"][(source, event_id)] = topico

    # negociações

    def inserir_negociacao(self, negociacao: dict) -> None:
        self._exigir_transacao("inserir_negociacao")
        nid = negociacao["negociacaoId"]
        if nid in self._dados["negociacoes"]:
            raise ValueError(f"negociacaoId duplicado: {nid}")
        self._dados["negociacoes"][nid] = _copia(negociacao)

    def obter_negociacao(self, negociacao_id: str, bloquear: bool = False) -> dict | None:
        self._checar("obter_negociacao")
        return _copia(self._dados["negociacoes"].get(negociacao_id))

    def atualizar_negociacao(self, negociacao: dict) -> None:
        self._exigir_transacao("atualizar_negociacao")
        self._dados["negociacoes"][negociacao["negociacaoId"]] = _copia(negociacao)

    def listar_negociacoes(self, empresa: str, escopo: str, modo: str | None, status: str | None,
                           offset: int, limite: int) -> tuple[list[dict], int]:
        self._checar("listar_negociacoes")
        itens = [
            n for n in self._dados["negociacoes"].values()
            if (n["modo"] == "LEILAO_DIRETO"
                or (escopo == "BUYER" and n["compradorId"] == empresa)
                or (escopo == "SUPPLIER" and n["fornecedorId"] == empresa))
            and (modo is None or n["modo"] == modo)
            and (status is None or n["status"] == status)
        ]
        itens.sort(key=lambda n: (utc(n["criadoEm"]), n["negociacaoId"]), reverse=True)
        return _copia(itens[offset:offset + limite]), len(itens)

    def listar_vencidas(self, agora: datetime, limite: int) -> list[str]:
        self._checar("listar_vencidas")
        vencidas = []
        for n in self._dados["negociacoes"].values():
            if n["modo"] == "LEILAO_DIRETO" and n["status"] in ("AGUARDANDO_RESERVAS", "ABERTO") and n["dataFim"] <= agora:
                vencidas.append((n["dataFim"], n["negociacaoId"]))
            elif n["modo"] == "VENDA_DIRETA" and n["status"] == "ABERTO" and n["expiraEm"] <= agora:
                vencidas.append((n["expiraEm"], n["negociacaoId"]))
        vencidas.sort()
        return [nid for _, nid in vencidas[:limite]]

    # ofertas do leilão

    def inserir_oferta_leilao(self, oferta: dict) -> None:
        self._exigir_transacao("inserir_oferta_leilao")
        chave = (oferta["negociacaoId"], oferta["fornecimentoId"])
        self._dados["ofertas"][chave] = _copia(oferta)

    def listar_ofertas_leilao(self, negociacao_id: str) -> list[dict]:
        self._checar("listar_ofertas_leilao")
        ofertas = [o for o in self._dados["ofertas"].values() if o["negociacaoId"] == negociacao_id]
        ofertas.sort(key=lambda o: o["ordem"])
        return _copia(ofertas)

    def atualizar_oferta_leilao(self, oferta: dict) -> None:
        self._exigir_transacao("atualizar_oferta_leilao")
        chave = (oferta["negociacaoId"], oferta["fornecimentoId"])
        self._dados["ofertas"][chave] = _copia(oferta)

    # lances e propostas

    def inserir_lance(self, lance: dict) -> None:
        self._exigir_transacao("inserir_lance")
        self._dados["lances"].setdefault(lance["negociacaoId"], []).append(_copia(lance))

    def listar_lances(self, negociacao_id: str, offset: int, limite: int) -> tuple[list[dict], int]:
        self._checar("listar_lances")
        itens = sorted(self._dados["lances"].get(negociacao_id, []), key=lambda l: l["sequencia"])
        return _copia(itens[offset:offset + limite]), len(itens)

    def inserir_proposta(self, proposta: dict) -> None:
        self._exigir_transacao("inserir_proposta")
        self._dados["propostas"].setdefault(proposta["negociacaoId"], []).append(_copia(proposta))

    def listar_propostas(self, negociacao_id: str, offset: int, limite: int) -> tuple[list[dict], int]:
        self._checar("listar_propostas")
        itens = sorted(self._dados["propostas"].get(negociacao_id, []), key=lambda p: p["sequencia"])
        return _copia(itens[offset:offset + limite]), len(itens)

    # idempotência HTTP

    def obter_idempotencia(self, escopo: str) -> dict | None:
        self._checar("obter_idempotencia")
        return _copia(self._dados["idempotencia"].get(escopo))

    def salvar_idempotencia(self, escopo: str, registro: dict) -> None:
        self._exigir_transacao("salvar_idempotencia")
        self._dados["idempotencia"][escopo] = _copia(registro)
