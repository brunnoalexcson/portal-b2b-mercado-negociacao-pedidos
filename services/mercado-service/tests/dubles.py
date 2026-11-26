"""Dublês em memória das portas do §5.1, usados pela suíte de aceitação."""
import copy
from contextlib import contextmanager
from datetime import datetime

from app.jsonio import utc


def _copia(valor):
    return copy.deepcopy(valor)


class RepositorioMemoria:
    """`MercadoRepositorio` em memória. Cada transação trabalha sobre uma cópia; uma exceção desfaz tudo e
    descarta as publicações pendentes do `EventBusMemoria` ligado a ele."""

    def __init__(self):
        self._dados = {nome: {} for nome in (
            "produtos", "ofertas", "demandas", "modelos", "cancelamentos", "balanco", "sugestoes", "aceites",
            "negociacoes", "idempotencia", "processados")}
        self._antes = None
        self.falhas: dict[str, object] = {}
        self.bloqueios: list[str] = []
        self.ouvintes: list = []
        self.pronto = True

    # ------------------------------------------------------------ apoio aos testes

    def falhar(self, metodo: str, excecao):
        """Faz `metodo` lançar `excecao` (classe ou instância) em toda chamada."""
        self.falhas[metodo] = excecao

    def _checar(self, metodo: str):
        excecao = self.falhas.get(metodo)
        if excecao is None:
            return
        if isinstance(excecao, type):
            raise excecao(f"falha simulada em {metodo}")
        raise type(excecao)(*excecao.args)

    def _escrita(self, metodo: str):
        self._checar(metodo)
        if self._antes is None:
            raise RuntimeError(f"{metodo} fora de transação")

    @property
    def em_transacao(self) -> bool:
        return self._antes is not None

    def tabela(self, nome: str) -> dict:
        return self._dados[nome]

    # ------------------------------------------------------------ porta

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
        return self.pronto

    def evento_processado(self, source, event_id):
        self._checar("evento_processado")
        return (source, event_id) in self._dados["processados"]

    def registrar_evento_processado(self, source, event_id, topico):
        self._escrita("registrar_evento_processado")
        self._dados["processados"][(source, event_id)] = {"topico": topico}

    def obter_produto(self, produto_id):
        self._checar("obter_produto")
        return _copia(self._dados["produtos"].get(produto_id))

    def salvar_produto(self, produto):
        self._escrita("salvar_produto")
        self._dados["produtos"][produto["produtoId"]] = _copia(produto)

    def obter_oferta(self, fornecimento_id):
        self._checar("obter_oferta")
        return _copia(self._dados["ofertas"].get(fornecimento_id))

    def salvar_oferta(self, oferta):
        self._escrita("salvar_oferta")
        self._dados["ofertas"][oferta["fornecimentoId"]] = _copia(oferta)

    def listar_ofertas_do_produto(self, produto_id):
        self._checar("listar_ofertas_do_produto")
        return [_copia(o) for o in self._dados["ofertas"].values() if o["produtoId"] == produto_id]

    def obter_demanda(self, demanda_id, bloquear=False):
        self._checar("obter_demanda")
        return _copia(self._dados["demandas"].get(demanda_id))

    def salvar_demanda(self, demanda):
        self._escrita("salvar_demanda")
        self._dados["demandas"][demanda["demandaId"]] = _copia(demanda)

    def obter_modelo(self, demanda_id):
        self._checar("obter_modelo")
        return _copia(self._dados["modelos"].get(demanda_id))

    def salvar_modelo(self, modelo):
        self._escrita("salvar_modelo")
        self._dados["modelos"][modelo["demandaId"]] = _copia(modelo)

    def obter_cancelamento_antecipado(self, demanda_id):
        self._checar("obter_cancelamento_antecipado")
        return _copia(self._dados["cancelamentos"].get(demanda_id))

    def salvar_cancelamento_antecipado(self, cancelamento):
        self._escrita("salvar_cancelamento_antecipado")
        self._dados["cancelamentos"][cancelamento["demandaId"]] = _copia(cancelamento)

    def listar_demandas_abertas(self, produto_id):
        self._checar("listar_demandas_abertas")
        demandas = [d for d in self._dados["demandas"].values()
                    if d["produtoId"] == produto_id and d["situacao"] == "AGUARDANDO"]
        return [_copia(d) for d in sorted(demandas, key=lambda d: (d["recebidaEm"], d["demandaId"]))]

    def listar_demandas_por_origem(self, demanda_origem_id):
        self._checar("listar_demandas_por_origem")
        return [_copia(d) for d in self._dados["demandas"].values() if d["demandaOrigemId"] == demanda_origem_id]

    def listar_demandas_vencidas(self, agora: datetime, limite):
        self._checar("listar_demandas_vencidas")
        vencidas = [d for d in self._dados["demandas"].values()
                    if d["situacao"] == "AGUARDANDO" and not d["prazoVencido"]
                    and d["prazoDesejado"] is not None and d["prazoDesejado"] < agora]
        vencidas.sort(key=lambda d: (d["prazoDesejado"], d["demandaId"]))
        return [d["demandaId"] for d in vencidas[:limite]]

    def bloquear_produto(self, produto_id):
        self._escrita("bloquear_produto")
        self.bloqueios.append(produto_id)

    def obter_situacao_balanco(self, produto_id):
        self._checar("obter_situacao_balanco")
        return _copia(self._dados["balanco"].get(produto_id))

    def salvar_situacao_balanco(self, produto_id, situacao, situacao_desde):
        self._escrita("salvar_situacao_balanco")
        self._dados["balanco"][produto_id] = {"situacao": situacao, "situacaoDesde": situacao_desde}

    def obter_sugestao(self, sugestao_id, bloquear=False):
        self._checar("obter_sugestao")
        return _copia(self._dados["sugestoes"].get(sugestao_id))

    def obter_sugestao_ativa(self, produto_id):
        self._checar("obter_sugestao_ativa")
        for sugestao in self._dados["sugestoes"].values():
            if sugestao["produtoId"] == produto_id and sugestao["situacao"] in ("ABERTA", "EM_FORMACAO"):
                return _copia(sugestao)
        return None

    def salvar_sugestao(self, sugestao):
        self._escrita("salvar_sugestao")
        self._dados["sugestoes"][sugestao["sugestaoId"]] = _copia(sugestao)

    def listar_sugestoes_ativas(self):
        self._checar("listar_sugestoes_ativas")
        ativas = [s for s in self._dados["sugestoes"].values() if s["situacao"] in ("ABERTA", "EM_FORMACAO")]
        return [_copia(s) for s in sorted(ativas, key=lambda s: (s["abertaEm"], s["sugestaoId"]))]

    def listar_janelas_vencidas(self, agora, limite):
        self._checar("listar_janelas_vencidas")
        vencidas = [s for s in self._dados["sugestoes"].values()
                    if s["situacao"] == "EM_FORMACAO" and s["janelaEncerraEm"] <= agora]
        vencidas.sort(key=lambda s: (s["janelaEncerraEm"], s["sugestaoId"]))
        return [s["sugestaoId"] for s in vencidas[:limite]]

    def listar_aceites(self, sugestao_id):
        self._checar("listar_aceites")
        return [_copia(a) for (sugestao, _), a in self._dados["aceites"].items() if sugestao == sugestao_id]

    def salvar_aceite(self, aceite):
        self._escrita("salvar_aceite")
        self._dados["aceites"][(aceite["sugestaoId"], aceite["fornecimentoId"])] = _copia(aceite)

    def remover_aceite(self, sugestao_id, fornecimento_id):
        self._escrita("remover_aceite")
        self._dados["aceites"].pop((sugestao_id, fornecimento_id), None)

    def inserir_negociacao(self, negociacao):
        self._escrita("inserir_negociacao")
        if negociacao["negociacaoId"] in self._dados["negociacoes"]:
            raise ValueError("negociacaoId duplicado")
        self._dados["negociacoes"][negociacao["negociacaoId"]] = _copia(negociacao)

    def obter_negociacao(self, negociacao_id, bloquear=False):
        self._checar("obter_negociacao")
        return _copia(self._dados["negociacoes"].get(negociacao_id))

    def atualizar_negociacao(self, negociacao):
        self._escrita("atualizar_negociacao")
        self._dados["negociacoes"][negociacao["negociacaoId"]] = _copia(negociacao)

    def listar_negociacoes(self, empresa, escopo, modo, situacao, offset, limite):
        self._checar("listar_negociacoes")
        visiveis = [n for n in self._dados["negociacoes"].values()
                    if (n["modo"] == "LEILAO_DIRETO"
                        or (escopo == "BUYER" and n["compradorId"] == empresa)
                        or (escopo == "SUPPLIER" and n["itens"][0]["fornecedorId"] == empresa))
                    and (modo is None or n["modo"] == modo) and (situacao is None or n["situacao"] == situacao)]
        visiveis.sort(key=lambda n: (n["definidaEm"], n["negociacaoId"]), reverse=True)
        return [_copia(n) for n in visiveis[offset:offset + limite]], len(visiveis)

    def obter_direta_aberta(self, demanda_id, fornecimento_id):
        self._checar("obter_direta_aberta")
        for n in self._dados["negociacoes"].values():
            if (n["modo"] == "VENDA_DIRETA" and n["situacao"] == "ABERTA" and n["demandaId"] == demanda_id
                    and n["itens"][0]["fornecimentoId"] == fornecimento_id):
                return _copia(n)
        return None

    def listar_leiloes_abertos_do_fornecedor(self, produto_id, fornecedor_id):
        self._checar("listar_leiloes_abertos_do_fornecedor")
        return [_copia(n) for n in self._dados["negociacoes"].values()
                if n["modo"] == "LEILAO_DIRETO" and n["situacao"] == "ABERTA" and n["produtoId"] == produto_id
                and any(item["fornecedorId"] == fornecedor_id for item in n["itens"])]

    def obter_idempotencia(self, escopo):
        self._checar("obter_idempotencia")
        return _copia(self._dados["idempotencia"].get(escopo))

    def salvar_idempotencia(self, escopo, registro):
        self._escrita("salvar_idempotencia")
        self._dados["idempotencia"][escopo] = _copia(registro)


class EventBusMemoria:
    """`EventBus` em memória. Publicações dentro de uma transação só aparecem em `publicados` no commit (como a
    outbox); as de fora (DLT) aparecem na hora. Nunca chama handlers de forma síncrona."""

    def __init__(self, repositorio: RepositorioMemoria | None = None):
        self.publicados: list[tuple[str, str, dict]] = []
        self.assinaturas: dict[str, object] = {}
        self.pronto = True
        self._pendentes: list[tuple[str, str, dict]] = []
        self._repositorio = repositorio
        if repositorio is not None:
            repositorio.ouvintes.append(self)

    def publish(self, topic, key, message):
        registro = (topic, key, copy.deepcopy(message))
        if self._repositorio is not None and self._repositorio.em_transacao:
            self._pendentes.append(registro)
        else:
            self.publicados.append(registro)

    def confirmar(self):
        self.publicados.extend(self._pendentes)
        self._pendentes.clear()

    def descartar(self):
        self._pendentes.clear()

    def subscribe(self, topic, handler):
        self.assinaturas[topic] = handler

    def disponivel(self):
        return self.pronto

    def entregar(self, topic, message):
        self.assinaturas[topic](message)

    def do_topico(self, topic) -> list[dict]:
        return [mensagem for topico, _, mensagem in self.publicados if topico == topic]


class RelogioFalso:
    """`Clock` controlado pelo teste; `sleep` só registra o valor."""

    def __init__(self, agora: datetime):
        self.agora = utc(agora)
        self.esperas: list[float] = []

    def now(self) -> datetime:
        return self.agora

    def sleep(self, segundos: float) -> None:
        self.esperas.append(segundos)
