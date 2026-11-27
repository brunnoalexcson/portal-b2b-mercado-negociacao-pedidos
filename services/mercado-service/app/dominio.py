"""Regras do Mercado: aptidão, balanço, sugestão, lote e participação (funções puras) e o serviço que as aplica
sobre as portas do §5.1, uma transação por mensagem, requisição ou registro de prazo."""
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Callable
from uuid import uuid4

from app import contratos
from app.contratos import CTX, MAX_QUANTIDADE, MAX_VALOR, Q2, Q4, EventoInvalido, q2, q4
from app.jsonio import utc
from app.portas import Clock, ConfigMercado, EventBus, MercadoRepositorio, RepositorioIndisponivel

log = logging.getLogger("app.dominio")

ZERO = Decimal("0.00")
UM = Decimal("1")
MAX_ITENS_LOTE = 1000
TOPICOS_CONSUMIDOS = tuple(contratos.PRODUTORES)
TOPICOS_COM_UNIDADE = ("fornecimento_criado", "fornecimento_atualizado", "demanda_criada", "demanda_recorrente_gerada")

AGUARDANDO, ATENDIDA, CANCELADA = "AGUARDANDO", "ATENDIDA", "CANCELADA"
ABERTA, EM_FORMACAO, CONVERTIDA, ENCERRADA = "ABERTA", "EM_FORMACAO", "CONVERTIDA", "ENCERRADA"
CONCLUIDA, SEM_ACORDO = "CONCLUIDA", "SEM_ACORDO"
VENDA_DIRETA, LEILAO_DIRETO = "VENDA_DIRETA", "LEILAO_DIRETO"
SEM_DEMANDA, SEM_OFERTA, OFERTA_SUFICIENTE, DEMANDA_EXCEDENTE = (
    "SEM_DEMANDA", "SEM_OFERTA", "OFERTA_SUFICIENTE", "DEMANDA_EXCEDENTE")


class ErroMercado(Exception):
    """Erro de negócio do catálogo do §10, identificado pelo `codigo`."""

    def __init__(self, codigo: str):
        super().__init__(codigo)
        self.codigo = codigo


def validar_config(config: ConfigMercado) -> ConfigMercado:
    """Valores inválidos impedem a subida do serviço (§5)."""
    for nome in ("excesso_demanda_minimo_percentual", "leilao_incremento_minimo_percentual"):
        valor = getattr(config, nome)
        if not isinstance(valor, Decimal) or not valor.is_finite() or valor < 0:
            raise ValueError(f"Configuração inválida: {nome}")
    for nome in ("leilao_janela_adesao_minutos", "leilao_duracao_horas"):
        valor = getattr(config, nome)
        if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
            raise ValueError(f"Configuração inválida: {nome}")
    return config


# ---------------------------------------------------------------- regras puras

def valor_item(quantidade: Decimal, preco: Decimal) -> Decimal:
    """`quantidade × precoUnitario`, 2 casas, ROUND_HALF_UP (RN-14, RN-22)."""
    return CTX.multiply(quantidade, preco).quantize(Q2, rounding=ROUND_HALF_UP, context=CTX)


def negociavel(oferta: dict) -> bool:
    return oferta["preco"] > 0 and oferta["quantidadeDisponivel"] > 0


def disponivel_livre(oferta: dict, comprometido: dict[str, Decimal]) -> Decimal:
    return max(ZERO, oferta["quantidadeDisponivel"] - comprometido.get(oferta["fornecimentoId"], ZERO))


def saldo_negociavel(demanda: dict) -> Decimal:
    """`saldo − emNegociacao`, nunca negativo (decisão da etapa 1)."""
    return max(ZERO, demanda["saldo"] - demanda["emNegociacao"])


def prazo_vencido_em(demanda: dict, agora: datetime) -> bool:
    return demanda["prazoDesejado"] is not None and demanda["prazoDesejado"] < agora


def conta_no_balanco(demanda: dict) -> bool:
    """Demanda aberta para o balanço: aguardando e sem a flag `prazoVencido` (RN-06, RN-10)."""
    return demanda["situacao"] == AGUARDANDO and not demanda["prazoVencido"]


def aberta_agora(demanda: dict, agora: datetime) -> bool:
    """Demanda aberta para a API-07 e a aptidão: compara o prazo com `agora` (decisão da etapa 1)."""
    return conta_no_balanco(demanda) and not prazo_vencido_em(demanda, agora)


def situacao_balanco(oferta_total: Decimal, demanda_total: Decimal) -> str:
    if demanda_total == 0:
        return SEM_DEMANDA
    if oferta_total == 0:
        return SEM_OFERTA
    if oferta_total >= demanda_total:
        return OFERTA_SUFICIENTE
    return DEMANDA_EXCEDENTE


def gatilho_sugestao(oferta_total: Decimal, demanda_total: Decimal, excesso_minimo_percentual: Decimal) -> bool:
    limite = CTX.multiply(oferta_total, CTX.add(UM, CTX.divide(excesso_minimo_percentual, Decimal(100))))
    return oferta_total > 0 and demanda_total > limite


def oferta_apta(oferta: dict, livre: Decimal, demanda: dict, agora: datetime) -> bool:
    """RN-07."""
    return (oferta["produtoId"] == demanda["produtoId"] and negociavel(oferta) and livre > 0
            and (demanda["precoMaximo"] is None or oferta["preco"] <= demanda["precoMaximo"])
            and oferta["fornecedorId"] != demanda["compradorId"] and aberta_agora(demanda, agora))


def chave_vitrine(oferta: dict, livre: Decimal):
    """RN-11: preço crescente, disponível livre decrescente, `criadaEm`, `fornecimentoId`."""
    return (oferta["preco"], -livre, oferta["criadaEm"], oferta["fornecimentoId"])


def incremento_minimo(preco_inicial: Decimal, percentual: Decimal) -> Decimal:
    """RN-22: `max(0.01, precoInicial × percentual / 100)`, 2 casas, ROUND_HALF_UP."""
    bruto = CTX.divide(CTX.multiply(preco_inicial, percentual), Decimal(100))
    return max(Q2, bruto.quantize(Q2, rounding=ROUND_HALF_UP, context=CTX))


def participacoes(valores: list[Decimal]) -> list[Decimal]:
    """RN-23: maior resto, piso de 0.0001 e soma 1.0000."""
    total = sum(valores, ZERO)
    exatos = [CTX.divide(valor, total) for valor in valores]
    truncados = [exato.quantize(Q4, rounding=ROUND_DOWN, context=CTX) for exato in exatos]
    partes = [max(Q4, truncado) for truncado in truncados]
    restos = [exato - truncado for exato, truncado in zip(exatos, truncados)]
    diferenca = UM - sum(partes, ZERO)
    if diferenca > 0:
        unidades = int(diferenca / Q4)
        for indice in sorted(range(len(partes)), key=lambda i: (-restos[i], i))[:unidades]:
            partes[indice] += Q4
    elif diferenca < 0:
        falta = -diferenca
        for indice in sorted(range(len(partes)), key=lambda i: (-partes[i], i)):
            if falta <= 0:
                break
            retirada = min(falta, partes[indice] - Q4)
            partes[indice] -= retirada
            falta -= retirada
    return [q4(parte) for parte in partes]


def montar_lote(aceites: list[dict], ofertas: dict[str, dict | None], produto_id: str | None = None) -> list[dict]:
    """RN-21: itens na ordem dos aceites, com `min(aceite, disponível)`; saem os que zeram, os de oferta não
    negociável, os de `valorItem` 0.00 e os que estourariam os limites do lote."""
    itens, soma_quantidade, soma_valor = [], ZERO, ZERO
    for aceite in aceites:
        oferta = ofertas.get(aceite["fornecimentoId"])
        if oferta is None or not negociavel(oferta):
            continue
        if produto_id is not None and oferta["produtoId"] != produto_id:
            continue
        quantidade = min(aceite["quantidade"], oferta["quantidadeDisponivel"])
        if quantidade <= 0:
            continue
        valor = valor_item(quantidade, oferta["preco"])
        if valor <= 0:
            continue
        if (len(itens) >= MAX_ITENS_LOTE or soma_quantidade + quantidade > MAX_QUANTIDADE
                or soma_valor + valor > MAX_VALOR):
            continue
        itens.append({
            "fornecimentoId": oferta["fornecimentoId"], "fornecedorId": oferta["fornecedorId"],
            "quantidade": quantidade, "precoUnitario": oferta["preco"], "valorItem": valor,
            "participacao": None, "origem": oferta["origem"],
        })
        soma_quantidade += quantidade
        soma_valor += valor
    if itens:
        for item, parte in zip(itens, participacoes([item["valorItem"] for item in itens])):
            item["participacao"] = parte
    return itens


def paginar(itens: list, pagina: int, tamanho: int) -> dict:
    inicio = (pagina - 1) * tamanho
    return {"items": itens[inicio:inicio + tamanho], "page": pagina, "size": tamanho, "total": len(itens)}


# ---------------------------------------------------------------- serviço

@dataclass(frozen=True)
class Idempotencia:
    escopo: str
    hash_corpo: str
    status: int
    location: str | None = None


@dataclass
class _Operacao:
    agora: datetime
    correlacao: str | None
    eventos: list = field(default_factory=list)
    bloqueados: set = field(default_factory=set)
    ids_bloqueados: set = field(default_factory=set)


class Mercado:
    def __init__(self, repositorio: MercadoRepositorio, event_bus: EventBus, clock: Clock,
                 config: ConfigMercado = ConfigMercado(),
                 ao_consumir: Callable[[str, str], None] | None = None):
        self.repo = repositorio
        self.bus = event_bus
        self.clock = clock
        self.config = validar_config(config)
        self.ao_consumir = ao_consumir

    def agora(self) -> datetime:
        return utc(self.clock.now())

    # ------------------------------------------------------------ apoio

    def _bloquear(self, op: _Operacao, *produtos: str):
        novos = sorted(set(produtos) - op.bloqueados)
        for produto in novos:
            self.repo.bloquear_produto(produto)
            op.bloqueados.add(produto)
        if novos:
            op.agora = self.agora()   # DG-69: o prazo é conferido com o instante de depois da espera

    def _bloquear_ids(self, op: _Operacao, *ids: str):
        """IDs de demanda ou modelo (RN-08, RN-09), sempre antes dos produtos, em ordem fixa."""
        novos = sorted(set(ids) - op.ids_bloqueados)
        for demanda_id in novos:
            self.repo.bloquear_id_demanda(demanda_id)
            op.ids_bloqueados.add(demanda_id)
        if novos:
            op.agora = self.agora()   # DG-69

    def _publicar(self, op: _Operacao):
        for topico, chave, mensagem in op.eventos:
            self.bus.publish(topico, chave, mensagem)

    def _comprometido(self, sugestao: dict | None) -> dict[str, Decimal]:
        """Quantidade comprometida por oferta: aceites de sugestão `EM_FORMACAO` (RN-04)."""
        comprometido: dict[str, Decimal] = {}
        if sugestao is not None and sugestao["situacao"] == EM_FORMACAO:
            for aceite in self.repo.listar_aceites(sugestao["sugestaoId"]):
                atual = comprometido.get(aceite["fornecimentoId"], ZERO)
                comprometido[aceite["fornecimentoId"]] = atual + aceite["quantidade"]
        return comprometido

    def _totais(self, produto_id: str, sugestao: dict | None) -> tuple[Decimal, Decimal]:
        comprometido = self._comprometido(sugestao)
        oferta_total = sum((disponivel_livre(oferta, comprometido)
                            for oferta in self.repo.listar_ofertas_do_produto(produto_id) if negociavel(oferta)), ZERO)
        demanda_total = sum((saldo_negociavel(demanda)
                             for demanda in self.repo.listar_demandas_abertas(produto_id)
                             if conta_no_balanco(demanda)), ZERO)
        return oferta_total, demanda_total

    def _elegiveis(self, produto_id: str, sugestao: dict | None) -> set[str]:
        """Fornecedores elegíveis (§4): oferta negociável com disponível livre > 0 e sem leilão aberto do produto."""
        comprometido = self._comprometido(sugestao)
        candidatos = {oferta["fornecedorId"] for oferta in self.repo.listar_ofertas_do_produto(produto_id)
                      if negociavel(oferta) and disponivel_livre(oferta, comprometido) > 0}
        return {fornecedor for fornecedor in candidatos
                if not self.repo.listar_leiloes_abertos_do_fornecedor(produto_id, fornecedor)}

    def _aceites_ordenados(self, sugestao_id: str) -> list[dict]:
        return sorted(self.repo.listar_aceites(sugestao_id), key=lambda a: (a["aceitoEm"], a["fornecimentoId"]))

    def _produto_conhecido(self, produto_id: str) -> tuple[dict | None, dict | None]:
        produto = self.repo.obter_produto(produto_id)
        situacao = self.repo.obter_situacao_balanco(produto_id)
        if produto is None and situacao is None:
            raise ErroMercado("PRODUTO_NAO_ENCONTRADO")
        return produto, situacao

    # ------------------------------------------------------------ balanço, sugestão e lote (RN-06, RN-16, RN-21)

    def _recalcular(self, op: _Operacao, produto_id: str):
        repo = self.repo
        sugestao = repo.obter_sugestao_ativa(produto_id)
        oferta_total, demanda_total = self._totais(produto_id, sugestao)
        situacao = situacao_balanco(oferta_total, demanda_total)
        anterior = repo.obter_situacao_balanco(produto_id)
        if anterior is None or anterior["situacao"] != situacao:
            repo.salvar_situacao_balanco(produto_id, situacao, op.agora)
        dispara = gatilho_sugestao(oferta_total, demanda_total, self.config.excesso_demanda_minimo_percentual)
        excesso = max(ZERO, demanda_total - oferta_total)
        if sugestao is None:
            if dispara:
                repo.salvar_sugestao({
                    "sugestaoId": str(uuid4()), "produtoId": produto_id, "situacao": ABERTA,
                    "excessoDemanda": excesso, "janelaEncerraEm": None, "duracaoLeilaoHoras": None,
                    "negociacaoId": None,
                    "correlationId": None, "abertaEm": op.agora, "encerradaEm": None,
                })
            return
        sugestao["excessoDemanda"] = excesso
        if sugestao["situacao"] == ABERTA and not dispara:
            sugestao["situacao"] = ENCERRADA
            sugestao["encerradaEm"] = op.agora
        repo.salvar_sugestao(sugestao)
        if sugestao["situacao"] == EM_FORMACAO and not dispara:
            self._fechar_lote(op, sugestao)

    def _fechar_lote(self, op: _Operacao, sugestao: dict):
        """T1 = agora (RN-21). Publica `modo_negociacao_definido` e `leilao_iniciado`, nessa ordem."""
        repo = self.repo
        produto_id = sugestao["produtoId"]
        correlacao = op.correlacao or sugestao["correlationId"] or str(uuid4())
        aceites = self._aceites_ordenados(sugestao["sugestaoId"])
        ofertas = {aceite["fornecimentoId"]: repo.obter_oferta(aceite["fornecimentoId"]) for aceite in aceites}
        itens = montar_lote(aceites, ofertas, produto_id)
        if not itens:
            sugestao.update(situacao=ENCERRADA, encerradaEm=op.agora)
            repo.salvar_sugestao(sugestao)
            log.info("Lote fechado sem itens", extra={"correlationId": correlacao, "produtoId": produto_id})
            self._recalcular(op, produto_id)
            return
        produto = repo.obter_produto(produto_id)
        unidade = produto["unidade"] if produto else ofertas[itens[0]["fornecimentoId"]]["unidade"]
        preco_inicial = sum((item["valorItem"] for item in itens), ZERO)
        negociacao = {
            "negociacaoId": str(uuid4()), "modo": LEILAO_DIRETO, "produtoId": produto_id, "unidade": unidade,
            "situacao": ABERTA, "demandaId": None, "compradorId": None, "compraImediata": False, "itens": itens,
            "quantidadeTotal": sum((item["quantidade"] for item in itens), ZERO), "valorReferencia": preco_inicial,
            "precoInicial": preco_inicial,
            "incrementoMinimo": incremento_minimo(preco_inicial, self.config.leilao_incremento_minimo_percentual),
            "sugestaoId": sugestao["sugestaoId"], "iniciaEm": op.agora,
            "encerraEm": op.agora + timedelta(hours=self._duracao_leilao(sugestao)),
            "definidaEm": op.agora, "encerradaEm": None, "correlationId": correlacao,
        }
        repo.inserir_negociacao(negociacao)
        sugestao.update(situacao=CONVERTIDA, negociacaoId=negociacao["negociacaoId"], encerradaEm=op.agora)
        repo.salvar_sugestao(sugestao)
        chave = negociacao["negociacaoId"]
        op.eventos.append(("modo_negociacao_definido", chave,
                           contratos.evento_modo_negociacao_definido(negociacao, None, op.agora, correlacao)))
        op.eventos.append(("leilao_iniciado", chave, contratos.evento_leilao_iniciado(
            negociacao, self.config.leilao_incremento_minimo_percentual, op.agora, correlacao)))
        log.info("Leilão iniciado", extra={"correlationId": correlacao, "produtoId": produto_id,
                                           "negociacaoId": chave})
        self._recalcular(op, produto_id)

    # ------------------------------------------------------------ consumo (RN-31, RN-32)

    def handler(self, topico: str) -> Callable[[dict], None]:
        def tratar(mensagem: dict) -> None:
            self.consumir(topico, mensagem)
        tratar.__name__ = f"tratar_{topico}"
        return tratar

    def consumir(self, topico: str, mensagem) -> str:
        try:
            evento = contratos.validar_evento(topico, mensagem)
        except EventoInvalido as erro:
            return self._dlt(topico, mensagem, "EVENTO_INVALIDO", erro.detalhe, "", 1)
        except (TypeError, ValueError, RecursionError):
            return self._dlt(topico, mensagem, "EVENTO_INVALIDO", "Mensagem fora do contrato.", "", 1)
        pilha, detalhe = "", ""
        for tentativa in (1, 2, 3):
            try:
                with self.repo.transacao():
                    op = _Operacao(self.agora(), evento.correlation_id)
                    resultado = self._aplicar(op, evento)
                    self._publicar(op)
            except RepositorioIndisponivel:
                raise
            except EventoInvalido as erro:
                return self._dlt(topico, mensagem, "EVENTO_INVALIDO", erro.detalhe, "", 1)
            except Exception as exc:
                pilha = traceback.format_exc()
                detalhe = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.warning("Falha ao processar mensagem (execução %s de 3)", tentativa,
                            extra={"correlationId": evento.correlation_id, "eventId": evento.event_id})
                if tentativa < 3:
                    self.clock.sleep(tentativa)
            else:
                self._contar(topico, resultado)
                return resultado
        return self._dlt(topico, mensagem, "PROCESSAMENTO_FALHOU", detalhe, pilha, 3)

    def _contar(self, topico: str, resultado: str):
        if self.ao_consumir is not None:
            self.ao_consumir(topico, resultado)

    def _dlt(self, topico: str, mensagem, erro: str, detalhe: str, pilha: str, tentativa: int) -> str:
        chave, envelope = contratos.evento_dlt(topico, mensagem, erro, detalhe, pilha, tentativa, self.agora())
        log.warning("Mensagem enviada para mercado.dlt: %s", erro,
                    extra={"correlationId": envelope["correlationId"],
                           "eventId": mensagem.get("eventId") if isinstance(mensagem, dict) else None})
        try:
            self.bus.publish(contratos.TOPICO_DLT, chave, envelope)
        except Exception:
            log.exception("Falha ao publicar em mercado.dlt", extra={"correlationId": envelope["correlationId"]})
        self._contar(topico, "dlt")
        return "dlt"

    def _bloquear_antes_da_unidade(self, op: _Operacao, topico: str, p: dict):
        """RN-03 e DG-69: a unidade é comparada com o produto já bloqueado, na ordem da DG-68 (IDs antes dos
        produtos). O modelo recorrente não bloqueia o produto (§13.1)."""
        if topico == "demanda_criada":
            self._bloquear_ids(op, p["demandaId"])
            if p["tipo"] == "RECORRENTE":
                return
        elif topico == "demanda_recorrente_gerada":
            self._bloquear_ids(op, p["demandaId"], p["ocorrenciaId"])
        produtos = {p["produtoId"]}
        if topico == "fornecimento_atualizado":
            atual = self.repo.obter_oferta(p["fornecimentoId"])
            if atual is not None:
                produtos.add(atual["produtoId"])
        self._bloquear(op, *produtos)

    def _aplicar(self, op: _Operacao, evento: contratos.Evento) -> str:
        payload = evento.payload
        if evento.topico in TOPICOS_COM_UNIDADE:
            self._bloquear_antes_da_unidade(op, evento.topico, payload)
            produto = self.repo.obter_produto(payload["produtoId"])
            if produto is not None and produto["unidade"] != payload["unidade"]:
                raise EventoInvalido(contratos.UNIDADE_DIFERENTE)
        if self.repo.evento_processado(evento.source, evento.event_id):
            return "duplicado"
        efeitos = {
            "produto_cadastrado": self._produto, "produto_atualizado": self._produto,
            "fornecimento_criado": lambda o, p: self._oferta(o, p, criacao=True),
            "fornecimento_atualizado": lambda o, p: self._oferta(o, p, criacao=False),
            "estoque_atualizado": self._estoque, "demanda_criada": self._demanda_criada,
            "demanda_recorrente_gerada": self._demanda_recorrente, "demanda_cancelada": self._demanda_cancelada,
            "negociacao_fechada": self._negociacao_fechada,
        }
        resultado = efeitos[evento.topico](op, payload)
        self.repo.registrar_evento_processado(evento.source, evento.event_id, evento.topico)
        return resultado

    def _produto(self, op: _Operacao, p: dict) -> str:
        """RN-02. O bloqueio do produto serializa cadastro e atualização entre as réplicas."""
        self._bloquear(op, p["produtoId"])
        atual = self.repo.obter_produto(p["produtoId"])
        if atual is not None and p["instante"] <= atual["atualizadoEm"]:
            return "ignorado"
        self.repo.salvar_produto({"produtoId": p["produtoId"], "nome": p["nome"], "unidade": p["unidade"],
                                  "atualizadoEm": p["instante"]})
        return "aplicado"

    def _oferta(self, op: _Operacao, p: dict, criacao: bool) -> str:
        """RN-01, para `fornecimento_criado` e `fornecimento_atualizado`."""
        repo, instante = self.repo, p["instante"]
        atual = repo.obter_oferta(p["fornecimentoId"])
        if atual is None:
            if not criacao:
                return "ignorado"
            self._bloquear(op, p["produtoId"])
            atual = repo.obter_oferta(p["fornecimentoId"])
            if atual is None:
                repo.salvar_oferta({
                    "fornecimentoId": p["fornecimentoId"], "fornecedorId": p["fornecedorId"],
                    "produtoId": p["produtoId"], "unidade": p["unidade"], "preco": p["preco"],
                    "quantidadeDisponivel": p["quantidadeDisponivel"], "origem": p["origem"],
                    "criadaEm": instante, "dadosAtualizadosEm": instante, "estoqueAtualizadoEm": instante,
                })
                self._recalcular(op, p["produtoId"])
                return "aplicado"
        if instante <= atual["dadosAtualizadosEm"] and instante <= atual["estoqueAtualizadoEm"]:
            return "ignorado"
        self._bloquear(op, atual["produtoId"], p["produtoId"])
        atual = repo.obter_oferta(p["fornecimentoId"])
        self._bloquear(op, atual["produtoId"])
        anterior = atual["produtoId"]
        mudou = False
        if instante > atual["dadosAtualizadosEm"]:
            atual.update(fornecedorId=p["fornecedorId"], produtoId=p["produtoId"], unidade=p["unidade"],
                         preco=p["preco"], origem=p["origem"], dadosAtualizadosEm=instante)
            mudou = True
        if instante > atual["estoqueAtualizadoEm"]:
            atual.update(quantidadeDisponivel=p["quantidadeDisponivel"], estoqueAtualizadoEm=instante)
            mudou = True
        if not mudou:
            return "ignorado"
        repo.salvar_oferta(atual)
        if anterior != atual["produtoId"]:
            self._retirar_aceites_da_oferta(anterior, atual["fornecimentoId"])
        for produto in sorted({anterior, atual["produtoId"]}):
            self._recalcular(op, produto)
        return "aplicado"

    def _retirar_aceites_da_oferta(self, produto_id: str, fornecimento_id: str):
        """DG-67: oferta que muda de produto sai da sugestão em formação do produto anterior, como na API-13."""
        repo = self.repo
        sugestao = repo.obter_sugestao_ativa(produto_id)
        if sugestao is None or sugestao["situacao"] != EM_FORMACAO:
            return
        if not any(aceite["fornecimentoId"] == fornecimento_id for aceite in repo.listar_aceites(sugestao["sugestaoId"])):
            return
        repo.remover_aceite(sugestao["sugestaoId"], fornecimento_id)
        if not repo.listar_aceites(sugestao["sugestaoId"]):
            sugestao.update(situacao=ABERTA, janelaEncerraEm=None, duracaoLeilaoHoras=None, correlationId=None)
            repo.salvar_sugestao(sugestao)

    def _estoque(self, op: _Operacao, p: dict) -> str:
        """RN-01, para `estoque_atualizado`."""
        repo = self.repo
        atual = repo.obter_oferta(p["fornecimentoId"])
        if atual is None or p["instante"] <= atual["estoqueAtualizadoEm"]:
            return "ignorado"
        self._bloquear(op, atual["produtoId"])
        atual = repo.obter_oferta(p["fornecimentoId"])
        self._bloquear(op, atual["produtoId"])
        if p["instante"] <= atual["estoqueAtualizadoEm"]:
            return "ignorado"
        atual.update(quantidadeDisponivel=p["quantidadeDisponivel"], estoqueAtualizadoEm=p["instante"])
        repo.salvar_oferta(atual)
        self._recalcular(op, atual["produtoId"])
        return "aplicado"

    def _id_em_uso(self, demanda_id: str) -> bool:
        return self.repo.obter_demanda(demanda_id) is not None or self.repo.obter_modelo(demanda_id) is not None

    def _cancelada_antes(self, demanda_id: str, comprador_id: str) -> bool:
        antecipado = self.repo.obter_cancelamento_antecipado(demanda_id)
        return antecipado is not None and antecipado["compradorId"] == comprador_id

    def _criar_demanda(self, op: _Operacao, demanda_id: str, origem_id: str | None, p: dict):
        self._bloquear(op, p["produtoId"])
        if self._id_em_uso(demanda_id):
            return "ignorado"
        cancelada = self._cancelada_antes(demanda_id, p["compradorId"])
        self.repo.salvar_demanda({
            "demandaId": demanda_id, "demandaOrigemId": origem_id, "compradorId": p["compradorId"],
            "produtoId": p["produtoId"], "unidade": p["unidade"], "quantidade": p["quantidade"],
            "precoMaximo": p["precoMaximo"], "destino": p["destino"], "prazoDesejado": p["prazoDesejado"],
            "saldo": p["quantidade"], "emNegociacao": ZERO, "situacao": CANCELADA if cancelada else AGUARDANDO,
            "prazoVencido": False, "recebidaEm": op.agora, "encerradaEm": op.agora if cancelada else None,
        })
        self._recalcular(op, p["produtoId"])
        return "aplicado"

    def _demanda_criada(self, op: _Operacao, p: dict) -> str:
        """RN-08."""
        self._bloquear_ids(op, p["demandaId"])
        if self._id_em_uso(p["demandaId"]):
            return "ignorado"
        if p["tipo"] == "RECORRENTE":
            self.repo.salvar_modelo({
                "demandaId": p["demandaId"], "compradorId": p["compradorId"], "produtoId": p["produtoId"],
                "recebidoEm": op.agora, "cancelado": self._cancelada_antes(p["demandaId"], p["compradorId"]),
            })
            return "aplicado"
        return self._criar_demanda(op, p["demandaId"], None, p)

    def _demanda_recorrente(self, op: _Operacao, p: dict) -> str:
        """RN-08: ocorrência com `demandaId = ocorrenciaId` e `demandaOrigemId = demandaId` do evento."""
        self._bloquear_ids(op, p["demandaId"], p["ocorrenciaId"])
        if self._id_em_uso(p["ocorrenciaId"]):
            return "ignorado"
        modelo = self.repo.obter_modelo(p["demandaId"])
        if modelo is not None and modelo["cancelado"]:
            return "ignorado"
        if self._cancelada_antes(p["demandaId"], p["compradorId"]):
            return "ignorado"
        return self._criar_demanda(op, p["ocorrenciaId"], p["demandaId"], p)

    def _cancelar_demandas(self, op: _Operacao, ids: list[str], comprador_id: str | None):
        repo = self.repo
        demandas = [demanda for demanda in (repo.obter_demanda(i) for i in ids) if demanda is not None]
        produtos = sorted({demanda["produtoId"] for demanda in demandas})
        self._bloquear(op, *produtos)
        for demanda_id in ids:
            demanda = repo.obter_demanda(demanda_id, bloquear=True)
            if demanda is None or demanda["situacao"] != AGUARDANDO:
                continue
            if comprador_id is not None and demanda["compradorId"] != comprador_id:
                continue
            demanda.update(situacao=CANCELADA, encerradaEm=op.agora)
            repo.salvar_demanda(demanda)
        for produto in produtos:
            self._recalcular(op, produto)

    def _demanda_cancelada(self, op: _Operacao, p: dict) -> str:
        """RN-09. O bloqueio do ID serializa com a criação da demanda, do modelo e das ocorrências (RN-08)."""
        repo, demanda_id, comprador = self.repo, p["demandaId"], p["compradorId"]
        self._bloquear_ids(op, demanda_id)
        demanda = repo.obter_demanda(demanda_id)
        if demanda is not None:
            if demanda["situacao"] != AGUARDANDO or demanda["compradorId"] != comprador:
                return "ignorado"
            self._cancelar_demandas(op, [demanda_id], comprador)
            return "aplicado"
        modelo = repo.obter_modelo(demanda_id)
        if modelo is not None:
            if modelo["cancelado"] or modelo["compradorId"] != comprador:
                return "ignorado"
            modelo["cancelado"] = True
            repo.salvar_modelo(modelo)
            ocorrencias = [d["demandaId"] for d in repo.listar_demandas_por_origem(demanda_id)
                           if d["situacao"] == AGUARDANDO]
            self._cancelar_demandas(op, ocorrencias, None)
            return "aplicado"
        if repo.obter_cancelamento_antecipado(demanda_id) is not None:
            return "ignorado"
        repo.salvar_cancelamento_antecipado({"demandaId": demanda_id, "compradorId": comprador,
                                             "canceladoEm": p["canceladoEm"], "recebidoEm": op.agora})
        ocorrencias = [d["demandaId"] for d in repo.listar_demandas_por_origem(demanda_id)
                       if d["situacao"] == AGUARDANDO and d["compradorId"] == comprador]
        self._cancelar_demandas(op, ocorrencias, comprador)
        return "aplicado"

    def _atender_se_zerou(self, op: _Operacao, demanda: dict):
        if demanda["situacao"] == AGUARDANDO and demanda["saldo"] == 0:
            demanda.update(situacao=ATENDIDA, encerradaEm=op.agora)

    def _negociacao_fechada(self, op: _Operacao, p: dict) -> str:
        """RN-25 a RN-27."""
        repo = self.repo
        negociacao = repo.obter_negociacao(p["negociacaoId"])
        if negociacao is None or negociacao["situacao"] != ABERTA:
            return "ignorado"
        self._bloquear(op, negociacao["produtoId"])
        negociacao = repo.obter_negociacao(p["negociacaoId"], bloquear=True)
        if negociacao["situacao"] != ABERTA:
            return "ignorado"
        encerrado = p["status"] == "ENCERRADO"
        negociacao.update(situacao=CONCLUIDA if encerrado else SEM_ACORDO, encerradaEm=op.agora)
        acordado = sum((alocacao["quantidade"] for alocacao in p["alocacoes"]), ZERO)
        if negociacao["modo"] == VENDA_DIRETA:
            demanda = repo.obter_demanda(negociacao["demandaId"], bloquear=True)
            if demanda is not None:
                quantidade = negociacao["itens"][0]["quantidade"]
                demanda["emNegociacao"] = max(ZERO, demanda["emNegociacao"] - quantidade)
                if encerrado:
                    demanda["saldo"] = max(ZERO, demanda["saldo"] - acordado)
                self._atender_se_zerou(op, demanda)
                repo.salvar_demanda(demanda)
        elif encerrado and p["demandaId"] is not None:
            demanda = repo.obter_demanda(p["demandaId"], bloquear=True)
            if (demanda is not None and demanda["situacao"] == AGUARDANDO
                    and demanda["compradorId"] == p["compradorId"]
                    and demanda["produtoId"] == negociacao["produtoId"]):
                demanda["saldo"] -= min(saldo_negociavel(demanda), acordado)
                self._atender_se_zerou(op, demanda)
                repo.salvar_demanda(demanda)
        repo.atualizar_negociacao(negociacao)
        self._recalcular(op, negociacao["produtoId"])
        return "aplicado"

    # ------------------------------------------------------------ prazos (RN-10, RN-21 a, RN-33)

    def executar_prazos(self) -> int:
        agora = self.agora()
        mudancas = 0
        with self.repo.transacao():
            janelas = self.repo.listar_janelas_vencidas(agora, 100)
        for sugestao_id in janelas:
            with self.repo.transacao():
                op = _Operacao(agora, None)
                sugestao = self.repo.obter_sugestao(sugestao_id)
                if sugestao is None:
                    continue
                self._bloquear(op, sugestao["produtoId"])
                sugestao = self.repo.obter_sugestao(sugestao_id, bloquear=True)
                if (sugestao["situacao"] != EM_FORMACAO or sugestao["janelaEncerraEm"] is None
                        or sugestao["janelaEncerraEm"] > agora):
                    continue
                self._fechar_lote(op, sugestao)
                self._publicar(op)
                mudancas += 1
        with self.repo.transacao():
            vencidas = self.repo.listar_demandas_vencidas(agora, 100)
        for demanda_id in vencidas:
            with self.repo.transacao():
                op = _Operacao(agora, None)
                demanda = self.repo.obter_demanda(demanda_id)
                if demanda is None:
                    continue
                self._bloquear(op, demanda["produtoId"])
                demanda = self.repo.obter_demanda(demanda_id, bloquear=True)
                if (demanda["situacao"] != AGUARDANDO or demanda["prazoVencido"]
                        or not prazo_vencido_em(demanda, agora)):
                    continue
                demanda["prazoVencido"] = True
                self.repo.salvar_demanda(demanda)
                self._recalcular(op, demanda["produtoId"])
                self._publicar(op)
                mudancas += 1
        return mudancas

    # ------------------------------------------------------------ idempotência (RN-30)

    def _replay(self, idempotencia: Idempotencia | None, op: _Operacao):
        if idempotencia is None:
            return None
        registro = self.repo.obter_idempotencia(idempotencia.escopo)
        op.agora = self.agora()   # DG-69: obter_idempotencia espera o bloqueio da chave entre as réplicas
        if registro is None or registro["criadoEm"] + timedelta(hours=24) <= op.agora:
            return None
        if registro["hashCorpo"] != idempotencia.hash_corpo:
            raise ErroMercado("IDEMPOTENCIA_CONFLITO")
        return registro["corpo"]

    def _memorizar(self, idempotencia: Idempotencia | None, corpo: dict, location: str | None, agora: datetime):
        if idempotencia is not None:
            self.repo.salvar_idempotencia(idempotencia.escopo, {
                "hashCorpo": idempotencia.hash_corpo, "status": idempotencia.status, "corpo": corpo,
                "location": location, "criadoEm": agora,
            })

    # ------------------------------------------------------------ projeções

    @staticmethod
    def _item_saida(item: dict) -> dict:
        return {
            "fornecimentoId": item["fornecimentoId"], "fornecedorId": item["fornecedorId"],
            "quantidade": q2(item["quantidade"]), "precoUnitario": q2(item["precoUnitario"]),
            "valorItem": q2(item["valorItem"]), "participacao": q4(item["participacao"]),
            "origem": item["origem"],
        }

    def _detalhe_negociacao(self, negociacao: dict, agora: datetime) -> dict:
        contexto = None
        if negociacao["modo"] == LEILAO_DIRETO and negociacao["situacao"] == ABERTA:
            produto_id = negociacao["produtoId"]
            sugestao = self.repo.obter_sugestao_ativa(produto_id)
            situacao = situacao_balanco(*self._totais(produto_id, sugestao))
            gravada = self.repo.obter_situacao_balanco(produto_id)
            contexto = {"situacao": situacao, "desde": gravada["situacaoDesde"] if gravada else None}
        opcional = lambda valor: None if valor is None else q2(valor)  # noqa: E731
        return {
            "negociacaoId": negociacao["negociacaoId"], "modo": negociacao["modo"],
            "produtoId": negociacao["produtoId"], "unidade": negociacao["unidade"],
            "situacao": negociacao["situacao"], "demandaId": negociacao["demandaId"],
            "compradorId": negociacao["compradorId"], "compraImediata": negociacao["compraImediata"],
            "quantidadeTotal": q2(negociacao["quantidadeTotal"]),
            "valorReferencia": q2(negociacao["valorReferencia"]),
            "precoInicial": opcional(negociacao["precoInicial"]),
            "incrementoMinimo": opcional(negociacao["incrementoMinimo"]),
            "sugestaoId": negociacao["sugestaoId"], "iniciaEm": negociacao["iniciaEm"],
            "encerraEm": negociacao["encerraEm"], "itens": [self._item_saida(item) for item in negociacao["itens"]],
            "definidaEm": negociacao["definidaEm"], "encerradaEm": negociacao["encerradaEm"],
            "contextoProduto": contexto, "agora": agora,
        }

    def _duracao_leilao(self, sugestao: dict) -> int:
        """DG-70: a duração informada no primeiro aceite; sem ela, `LEILAO_DURACAO_HORAS`."""
        return sugestao.get("duracaoLeilaoHoras") or self.config.leilao_duracao_horas

    def _projecao_sugestao(self, sugestao: dict, empresa: str, agora: datetime) -> dict:
        aceites = self._aceites_ordenados(sugestao["sugestaoId"])
        produto = self.repo.obter_produto(sugestao["produtoId"])
        meus = [aceite for aceite in aceites if aceite["fornecedorId"] == empresa]
        return {
            "sugestaoId": sugestao["sugestaoId"], "produtoId": sugestao["produtoId"],
            "unidade": produto["unidade"] if produto else None, "situacao": sugestao["situacao"],
            "excessoDemanda": q2(sugestao["excessoDemanda"]),
            "quantidadeNoLote": q2(sum((aceite["quantidade"] for aceite in aceites), ZERO)),
            "fornecedoresNoLote": len({aceite["fornecedorId"] for aceite in aceites}),
            "janelaEncerraEm": sugestao["janelaEncerraEm"],
            "duracaoLeilaoHoras": self._duracao_leilao(sugestao),
            "minhaParticipacao": ({"fornecimentoId": meus[0]["fornecimentoId"], "quantidade": q2(meus[0]["quantidade"])}
                                  if meus else None),
            "negociacaoId": sugestao["negociacaoId"], "agora": agora,
        }

    @staticmethod
    def _detalhe_demanda(demanda: dict) -> dict:
        return {
            "demandaId": demanda["demandaId"], "demandaOrigemId": demanda["demandaOrigemId"],
            "produtoId": demanda["produtoId"], "unidade": demanda["unidade"],
            "quantidade": q2(demanda["quantidade"]), "saldo": q2(demanda["saldo"]),
            "emNegociacao": q2(demanda["emNegociacao"]), "saldoNegociavel": q2(saldo_negociavel(demanda)),
            "precoMaximo": None if demanda["precoMaximo"] is None else q2(demanda["precoMaximo"]),
            "destino": demanda["destino"], "prazoDesejado": demanda["prazoDesejado"],
            "situacao": demanda["situacao"], "prazoVencido": demanda["prazoVencido"],
            "recebidaEm": demanda["recebidaEm"], "encerradaEm": demanda["encerradaEm"],
        }

    # ------------------------------------------------------------ consultas (API-04, 05, 06, 08, 09, 10)

    def balanco(self, produto_id: str) -> dict:
        with self.repo.transacao():
            agora = self.agora()
            produto, gravada = self._produto_conhecido(produto_id)
            sugestao = self.repo.obter_sugestao_ativa(produto_id)
            oferta_total, demanda_total = self._totais(produto_id, sugestao)
            situacao = situacao_balanco(oferta_total, demanda_total)
            excesso = demanda_total - oferta_total if situacao == DEMANDA_EXCEDENTE else ZERO
            return {
                "produtoId": produto_id, "unidade": produto["unidade"] if produto else None,
                "ofertaTotal": q2(oferta_total), "demandaTotal": q2(demanda_total), "situacao": situacao,
                "situacaoDesde": gravada["situacaoDesde"] if gravada else None, "excessoDemanda": q2(excesso),
                "sugestaoId": sugestao["sugestaoId"] if sugestao else None, "agora": agora,
            }

    def vitrine(self, produto_id: str, empresa: str, demanda_id: str | None, pagina: int, tamanho: int) -> dict:
        with self.repo.transacao():
            agora = self.agora()
            self._produto_conhecido(produto_id)
            demanda = None
            if demanda_id is not None:
                demanda = self.repo.obter_demanda(demanda_id)
                if demanda is None or demanda["compradorId"] != empresa:
                    raise ErroMercado("DEMANDA_NAO_ENCONTRADA")
                if demanda["produtoId"] != produto_id:
                    raise ErroMercado("PRODUTO_DIFERENTE")
            comprometido = self._comprometido(self.repo.obter_sugestao_ativa(produto_id))
            listadas = []
            for oferta in self.repo.listar_ofertas_do_produto(produto_id):
                livre = disponivel_livre(oferta, comprometido)
                if negociavel(oferta) and livre > 0:
                    listadas.append((oferta, livre))
            listadas.sort(key=lambda par: chave_vitrine(*par))
            itens = [{
                "fornecimentoId": oferta["fornecimentoId"], "fornecedorId": oferta["fornecedorId"],
                "preco": q2(oferta["preco"]), "quantidadeDisponivel": q2(livre), "origem": oferta["origem"],
                "apta": None if demanda is None else oferta_apta(oferta, livre, demanda, agora),
            } for oferta, livre in listadas]
            return paginar(itens, pagina, tamanho)

    def demanda(self, empresa: str, demanda_id: str) -> dict:
        with self.repo.transacao():
            demanda = self.repo.obter_demanda(demanda_id)
            if demanda is None or demanda["compradorId"] != empresa:
                raise ErroMercado("DEMANDA_NAO_ENCONTRADA")
            return self._detalhe_demanda(demanda)

    def negociacoes(self, empresa: str, escopo: str, modo: str | None, situacao: str | None,
                    pagina: int, tamanho: int) -> dict:
        with self.repo.transacao():
            itens, total = self.repo.listar_negociacoes(empresa, escopo, modo, situacao,
                                                        (pagina - 1) * tamanho, tamanho)
            return {"items": [{
                "negociacaoId": n["negociacaoId"], "modo": n["modo"], "produtoId": n["produtoId"],
                "situacao": n["situacao"], "quantidadeTotal": q2(n["quantidadeTotal"]),
                "valorReferencia": q2(n["valorReferencia"]), "definidaEm": n["definidaEm"],
            } for n in itens], "page": pagina, "size": tamanho, "total": total}

    @staticmethod
    def _visivel(negociacao: dict, empresa: str, escopo: str) -> bool:
        """RN-12."""
        if negociacao["modo"] == LEILAO_DIRETO:
            return True
        if escopo == "BUYER":
            return negociacao["compradorId"] == empresa
        return escopo == "SUPPLIER" and negociacao["itens"][0]["fornecedorId"] == empresa

    def negociacao(self, empresa: str, escopo: str, negociacao_id: str) -> dict:
        with self.repo.transacao():
            agora = self.agora()
            negociacao = self.repo.obter_negociacao(negociacao_id)
            if negociacao is None or not self._visivel(negociacao, empresa, escopo):
                raise ErroMercado("NEGOCIACAO_NAO_ENCONTRADA")
            return self._detalhe_negociacao(negociacao, agora)

    def sugestoes(self, empresa: str, pagina: int, tamanho: int) -> dict:
        with self.repo.transacao():
            agora = self.agora()
            visiveis = []
            for sugestao in self.repo.listar_sugestoes_ativas():
                tem_aceite = any(aceite["fornecedorId"] == empresa
                                 for aceite in self.repo.listar_aceites(sugestao["sugestaoId"]))
                if tem_aceite or empresa in self._elegiveis(sugestao["produtoId"], sugestao):
                    visiveis.append(self._projecao_sugestao(sugestao, empresa, agora))
            return paginar(visiveis, pagina, tamanho)

    # ------------------------------------------------------------ negociação direta (API-07, RN-14, RN-15, RN-35)

    def abrir_direta(self, empresa: str, demanda_id: str, fornecimento_id: str, quantidade: Decimal,
                     compra_imediata: bool, correlacao: str, idempotencia: Idempotencia | None = None) -> dict:
        repo = self.repo
        with repo.transacao():
            op = _Operacao(self.agora(), correlacao)
            memorizada = self._replay(idempotencia, op)
            if memorizada is not None:
                return memorizada
            demanda = repo.obter_demanda(demanda_id)
            if demanda is None or demanda["compradorId"] != empresa:
                raise ErroMercado("DEMANDA_NAO_ENCONTRADA")
            self._bloquear(op, demanda["produtoId"])
            demanda = repo.obter_demanda(demanda_id, bloquear=True)
            if not aberta_agora(demanda, op.agora):
                raise ErroMercado("DEMANDA_ENCERRADA")
            oferta = repo.obter_oferta(fornecimento_id)
            if oferta is None or not negociavel(oferta):
                raise ErroMercado("OFERTA_INDISPONIVEL")
            if oferta["produtoId"] != demanda["produtoId"]:
                raise ErroMercado("PRODUTO_DIFERENTE")
            if oferta["fornecedorId"] == demanda["compradorId"]:
                raise ErroMercado("CONFLITO_DE_INTERESSE")
            if demanda["precoMaximo"] is not None and oferta["preco"] > demanda["precoMaximo"]:
                raise ErroMercado("PRECO_ACIMA_DO_MAXIMO")
            if repo.obter_direta_aberta(demanda_id, fornecimento_id) is not None:
                raise ErroMercado("NEGOCIACAO_JA_ABERTA")
            if quantidade > saldo_negociavel(demanda):
                raise ErroMercado("QUANTIDADE_ACIMA_DO_SALDO")
            produto_id = demanda["produtoId"]
            if quantidade > disponivel_livre(oferta, self._comprometido(repo.obter_sugestao_ativa(produto_id))):
                raise ErroMercado("QUANTIDADE_INDISPONIVEL")
            valor = valor_item(quantidade, oferta["preco"])
            if valor <= 0 or valor > MAX_VALOR:
                raise ErroMercado("VALIDACAO_INVALIDA")
            produto = repo.obter_produto(produto_id)
            negociacao = {
                "negociacaoId": str(uuid4()), "modo": VENDA_DIRETA, "produtoId": produto_id,
                "unidade": produto["unidade"] if produto else oferta["unidade"], "situacao": ABERTA,
                "demandaId": demanda_id,
                "compradorId": demanda["compradorId"], "compraImediata": compra_imediata,
                "itens": [{
                    "fornecimentoId": fornecimento_id, "fornecedorId": oferta["fornecedorId"],
                    "quantidade": quantidade, "precoUnitario": oferta["preco"], "valorItem": valor,
                    "participacao": q4(UM), "origem": oferta["origem"],
                }],
                "quantidadeTotal": quantidade, "valorReferencia": valor, "precoInicial": None,
                "incrementoMinimo": None, "sugestaoId": None, "iniciaEm": None, "encerraEm": None,
                "definidaEm": op.agora, "encerradaEm": None, "correlationId": correlacao,
            }
            repo.inserir_negociacao(negociacao)
            demanda["emNegociacao"] += quantidade
            repo.salvar_demanda(demanda)
            op.eventos.append(("modo_negociacao_definido", negociacao["negociacaoId"],
                               contratos.evento_modo_negociacao_definido(negociacao, demanda, op.agora, correlacao)))
            self._recalcular(op, produto_id)
            corpo = self._detalhe_negociacao(negociacao, op.agora)
            self._memorizar(idempotencia, corpo, f"/api/v1/mercado/negociacoes/{negociacao['negociacaoId']}",
                            op.agora)
            self._publicar(op)
            log.info("Negociação direta aberta", extra={"correlationId": correlacao, "produtoId": produto_id,
                                                        "demandaId": demanda_id,
                                                        "negociacaoId": negociacao["negociacaoId"]})
            return corpo

    # ------------------------------------------------------------ aceites (API-11, 12, 13, RN-17 a RN-21)

    @staticmethod
    def _janela_aberta(sugestao: dict, agora: datetime) -> bool:
        return sugestao["situacao"] == EM_FORMACAO and agora < sugestao["janelaEncerraEm"]

    def aceitar(self, empresa: str, sugestao_id: str, fornecimento_id: str, quantidade: Decimal,
                correlacao: str, idempotencia: Idempotencia | None = None,
                duracao_horas: int | None = None) -> dict:
        repo = self.repo
        with repo.transacao():
            op = _Operacao(self.agora(), correlacao)
            sugestao = repo.obter_sugestao(sugestao_id)
            if sugestao is None:
                raise ErroMercado("SUGESTAO_NAO_ENCONTRADA")
            memorizada = self._replay(idempotencia, op)
            if memorizada is not None:
                return memorizada
            produto_id = sugestao["produtoId"]
            self._bloquear(op, produto_id)
            sugestao = repo.obter_sugestao(sugestao_id, bloquear=True)
            if not (sugestao["situacao"] == ABERTA or self._janela_aberta(sugestao, op.agora)):
                raise ErroMercado("SUGESTAO_ENCERRADA")
            oferta = repo.obter_oferta(fornecimento_id)
            if oferta is None or not negociavel(oferta):
                raise ErroMercado("OFERTA_INDISPONIVEL")
            if oferta["fornecedorId"] != empresa:
                raise ErroMercado("OFERTA_DE_OUTRA_EMPRESA")
            if oferta["produtoId"] != produto_id:
                raise ErroMercado("PRODUTO_DIFERENTE")
            if repo.listar_leiloes_abertos_do_fornecedor(produto_id, empresa):
                raise ErroMercado("FORNECEDOR_EM_LEILAO")
            if any(aceite["fornecimentoId"] == fornecimento_id for aceite in repo.listar_aceites(sugestao_id)):
                raise ErroMercado("ACEITE_JA_REGISTRADO")
            if quantidade > disponivel_livre(oferta, self._comprometido(sugestao)):
                raise ErroMercado("QUANTIDADE_INDISPONIVEL")
            repo.salvar_aceite({"sugestaoId": sugestao_id, "fornecimentoId": fornecimento_id,
                                "fornecedorId": empresa, "quantidade": quantidade, "aceitoEm": op.agora})
            if sugestao["situacao"] == ABERTA:
                # DG-70: só o aceite que abre a janela define a duração do leilão; nos seguintes, ela é ignorada.
                sugestao.update(situacao=EM_FORMACAO, correlationId=correlacao, duracaoLeilaoHoras=duracao_horas,
                                janelaEncerraEm=op.agora + timedelta(minutes=self.config.leilao_janela_adesao_minutos))
                repo.salvar_sugestao(sugestao)
            self._recalcular(op, produto_id)
            sugestao = repo.obter_sugestao(sugestao_id)
            if sugestao["situacao"] == EM_FORMACAO:
                no_lote = {aceite["fornecedorId"] for aceite in repo.listar_aceites(sugestao_id)}
                if self._elegiveis(produto_id, sugestao) <= no_lote:
                    self._fechar_lote(op, sugestao)
                    sugestao = repo.obter_sugestao(sugestao_id)
            corpo = self._projecao_sugestao(sugestao, empresa, op.agora)
            self._memorizar(idempotencia, corpo, None, op.agora)
            self._publicar(op)
            return corpo

    def _aceite_da_empresa(self, sugestao_id: str, fornecimento_id: str, empresa: str) -> dict:
        for aceite in self.repo.listar_aceites(sugestao_id):
            if aceite["fornecimentoId"] == fornecimento_id and aceite["fornecedorId"] == empresa:
                return aceite
        raise ErroMercado("ACEITE_NAO_ENCONTRADO")

    def _preparar_mudanca(self, op: _Operacao, empresa: str, sugestao_id: str, fornecimento_id: str):
        repo = self.repo
        sugestao = repo.obter_sugestao(sugestao_id)
        if sugestao is None:
            raise ErroMercado("SUGESTAO_NAO_ENCONTRADA")
        self._aceite_da_empresa(sugestao_id, fornecimento_id, empresa)
        self._bloquear(op, sugestao["produtoId"])
        sugestao = repo.obter_sugestao(sugestao_id, bloquear=True)
        aceite = self._aceite_da_empresa(sugestao_id, fornecimento_id, empresa)
        if not self._janela_aberta(sugestao, op.agora):
            raise ErroMercado("SUGESTAO_ENCERRADA")
        return sugestao, aceite

    def alterar_aceite(self, empresa: str, sugestao_id: str, fornecimento_id: str, quantidade: Decimal,
                       correlacao: str) -> dict:
        repo = self.repo
        with repo.transacao():
            op = _Operacao(self.agora(), correlacao)
            sugestao, aceite = self._preparar_mudanca(op, empresa, sugestao_id, fornecimento_id)
            oferta = repo.obter_oferta(fornecimento_id)
            livre = disponivel_livre(oferta, self._comprometido(sugestao)) if oferta is not None else ZERO
            if quantidade > livre + aceite["quantidade"]:
                raise ErroMercado("QUANTIDADE_INDISPONIVEL")
            aceite["quantidade"] = quantidade
            repo.salvar_aceite(aceite)
            self._recalcular(op, sugestao["produtoId"])
            corpo = self._projecao_sugestao(repo.obter_sugestao(sugestao_id), empresa, op.agora)
            self._publicar(op)
            return corpo

    def remover_aceite(self, empresa: str, sugestao_id: str, fornecimento_id: str, correlacao: str) -> None:
        repo = self.repo
        with repo.transacao():
            op = _Operacao(self.agora(), correlacao)
            sugestao, _ = self._preparar_mudanca(op, empresa, sugestao_id, fornecimento_id)
            repo.remover_aceite(sugestao_id, fornecimento_id)
            if not repo.listar_aceites(sugestao_id):
                sugestao.update(situacao=ABERTA, janelaEncerraEm=None, duracaoLeilaoHoras=None, correlationId=None)
                repo.salvar_sugestao(sugestao)
            self._recalcular(op, sugestao["produtoId"])
            self._publicar(op)
