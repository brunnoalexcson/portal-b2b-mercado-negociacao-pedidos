"""Dublês em memória das portas do §5.1, usados pela suíte de aceitação."""
import copy
from contextlib import contextmanager
from datetime import datetime, timezone

from app.portas import RepositorioIndisponivel


class Relogio:
    def __init__(self, agora=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)):
        self.agora = agora
        self.esperas = []

    def now(self):
        return self.agora

    def sleep(self, segundos):
        self.esperas.append(segundos)


class Barramento:
    def __init__(self):
        self.handlers = {}
        self.publicados = []
        self.pronto = True

    def subscribe(self, topic, handler):
        self.handlers[topic] = handler

    def publish(self, topic, key, message):
        self.publicados.append((topic, key, copy.deepcopy(message)))

    def disponivel(self):
        return self.pronto

    def de(self, topico):
        return [(k, m) for t, k, m in self.publicados if t == topico]


class Chaves:
    def __init__(self, chaves):
        self.chaves = chaves
        self.falhar = False

    def obter(self, kid):
        if self.falhar:
            raise ConnectionError("JWKS fora do ar")
        return self.chaves.get(kid)


class Repositorio:
    TABELAS = ("eventos", "produtos", "ofertas", "encerradas", "pendentes", "pedidos", "historico", "auditoria",
               "solicitacoes", "entregas", "idempotencia")

    def __init__(self):
        self.eventos = set()
        self.produtos, self.ofertas, self.encerradas, self.pendentes, self.pedidos = {}, {}, {}, {}, {}
        self.historico, self.auditoria = [], []
        self.solicitacoes, self.entregas, self.idempotencia = {}, {}, {}
        self.falhar_transacao = False
        self._em_transacao = False

    @contextmanager
    def transacao(self):
        if self.falhar_transacao:
            raise RepositorioIndisponivel("banco fora do ar")
        copia = {t: copy.deepcopy(getattr(self, t)) for t in self.TABELAS}
        self._em_transacao = True
        try:
            yield
        except BaseException:
            for t, valor in copia.items():
                setattr(self, t, valor)
            raise
        finally:
            self._em_transacao = False

    def disponivel(self):
        return True

    def evento_processado(self, source, event_id):
        return (source, event_id) in self.eventos

    def registrar_evento_processado(self, source, event_id, topico):
        self.eventos.add((source, event_id))

    def obter_produto(self, produto_id):
        return copy.deepcopy(self.produtos.get(produto_id))

    def salvar_produto(self, produto):
        self.produtos[produto["produtoId"]] = copy.deepcopy(produto)

    def obter_oferta(self, fornecimento_id):
        return copy.deepcopy(self.ofertas.get(fornecimento_id))

    def salvar_oferta(self, oferta):
        self.ofertas[oferta["fornecimentoId"]] = copy.deepcopy(oferta)

    def bloquear_negociacao(self, negociacao_id):
        return None

    def obter_negociacao_encerrada(self, negociacao_id):
        return copy.deepcopy(self.encerradas.get(negociacao_id))

    def salvar_negociacao_encerrada(self, registro):
        self.encerradas[registro["negociacaoId"]] = copy.deepcopy(registro)

    def obter_pendente(self, negociacao_id, fornecimento_id):
        return copy.deepcopy(self.pendentes.get((negociacao_id, fornecimento_id)))

    def salvar_pendente(self, pendente):
        self.pendentes[(pendente["negociacaoId"], pendente["fornecimentoId"])] = copy.deepcopy(pendente)

    def remover_pendente(self, negociacao_id, fornecimento_id):
        self.pendentes.pop((negociacao_id, fornecimento_id), None)

    def listar_pendentes(self, limite, produto_id=None, fornecimento_id=None):
        itens = [p for p in self.pendentes.values()
                 if (produto_id is None or p["payload"]["produtoId"] == produto_id)
                 and (fornecimento_id is None or p["fornecimentoId"] == fornecimento_id)]
        return copy.deepcopy(sorted(itens, key=lambda p: p["recebidoEm"])[:limite])

    def obter_pedido(self, pedido_id, bloquear=False):
        return copy.deepcopy(self.pedidos.get(pedido_id))

    def obter_pedido_da_oferta(self, negociacao_id, fornecimento_id):
        return next((copy.deepcopy(p) for p in self.pedidos.values()
                     if p["negociacaoId"] == negociacao_id and p["fornecimentoId"] == fornecimento_id), None)

    def listar_pedidos_da_negociacao(self, negociacao_id):
        return [copy.deepcopy(p) for p in self.pedidos.values() if p["negociacaoId"] == negociacao_id]

    def inserir_pedido(self, pedido):
        self.pedidos[pedido["pedidoId"]] = copy.deepcopy(pedido)

    def atualizar_pedido(self, pedido):
        self.pedidos[pedido["pedidoId"]] = copy.deepcopy(pedido)

    def listar_pedidos(self, empresa, escopo, status, negociacao_id, offset, limite):
        def visivel(p):
            if escopo is None:
                return True
            if escopo == "BUYER":
                return p["compradorId"] == empresa
            if escopo == "SUPPLIER":
                return p["fornecedorId"] == empresa
            return empresa in (p["compradorId"], p["fornecedorId"])
        itens = [p for p in self.pedidos.values() if visivel(p) and (status is None or p["status"] == status)
                 and (negociacao_id is None or p["negociacaoId"] == negociacao_id)]
        itens.sort(key=lambda p: (p["criadoEm"], p["pedidoId"]), reverse=True)
        return copy.deepcopy(itens[offset:offset + limite]), len(itens)

    def registrar_historico(self, item):
        self.historico.append(copy.deepcopy(item))

    def listar_historico(self, pedido_id, offset, limite):
        itens = sorted((h for h in self.historico if h["pedidoId"] == pedido_id), key=lambda h: h["versao"])
        return copy.deepcopy(itens[offset:offset + limite]), len(itens)

    def registrar_auditoria(self, registro):
        self.auditoria.append(copy.deepcopy(registro))

    def obter_solicitacao(self, solicitacao_frete_id):
        return copy.deepcopy(self.solicitacoes.get(solicitacao_frete_id))

    def salvar_solicitacao(self, solicitacao):
        self.solicitacoes[solicitacao["solicitacaoFreteId"]] = copy.deepcopy(solicitacao)

    def salvar_entrega_pendente(self, entrega):
        self.entregas[(entrega["pedidoId"], entrega["entregaId"])] = copy.deepcopy(entrega)

    def listar_entregas_pendentes(self, pedido_id, limite):
        itens = [e for e in self.entregas.values() if pedido_id is None or e["pedidoId"] == pedido_id]
        return copy.deepcopy(sorted(itens, key=lambda e: e["recebidoEm"])[:limite])

    def remover_entrega_pendente(self, pedido_id, entrega_id):
        self.entregas.pop((pedido_id, entrega_id), None)

    def obter_idempotencia(self, escopo):
        return copy.deepcopy(self.idempotencia.get(escopo))

    def salvar_idempotencia(self, escopo, registro):
        self.idempotencia[escopo] = copy.deepcopy(registro)

    def acoes(self, acao):
        return [a for a in self.auditoria if a["acao"] == acao]
