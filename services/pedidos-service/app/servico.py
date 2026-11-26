"""Casos de uso de Pedidos sobre as portas do §5.1: consumo dos 11 tópicos, reconciliação, API-07 e consultas."""
import logging
from datetime import timedelta, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from app import contratos, dominio
from app.contratos import Invalido
from app.jsonio import dumps
from app.portas import RepositorioIndisponivel

log = logging.getLogger("pedidos")
LIMITE_DLT = 64 * 1024
SENSIVEIS = {"token", "authorization", "senha", "password", "accesstoken", "refreshtoken", "secret", "apikey",
             "clientsecret"}


class ErroHttp(Exception):
    def __init__(self, codigo):
        super().__init__(codigo)
        self.codigo = codigo


class Rejeicao(Exception):
    """Mensagem válida que vai para a DLT sem ser registrada como processada (RN-25)."""

    def __init__(self, erro):
        super().__init__(erro)
        self.erro = erro


class Metricas:
    def __init__(self):
        self.registry = CollectorRegistry()
        r = self.registry
        self.http = Counter("pedidos_http_requisicoes_total", "Requisições HTTP", ["rota", "metodo", "status"],
                            registry=r)
        self.latencia = Histogram("pedidos_http_latencia_segundos", "Latência HTTP", ["rota", "metodo"], registry=r)
        self.eventos = Counter("pedidos_eventos_consumidos_total", "Eventos consumidos", ["topico", "resultado"],
                               registry=r)
        self.pendencias = Gauge("pedidos_pendencias", "Solicitações pendentes", registry=r)
        self.pendencias_antigas = Gauge("pedidos_pendencias_antigas", "Pendências com mais de 24 h", registry=r)
        self.outbox_pendentes = Gauge("pedidos_outbox_pendentes", "Registros da outbox sem confirmação", registry=r)
        self.outbox_idade = Gauge("pedidos_outbox_idade_segundos", "Idade do registro pendente mais antigo",
                                  registry=r)


def _redigir(dados):
    if isinstance(dados, dict):
        return {k: "[omitido]" if isinstance(k, str) and k.replace("_", "").replace("-", "").lower() in SENSIVEIS
                else _redigir(v) for k, v in dados.items()}
    if isinstance(dados, list):
        return [_redigir(v) for v in dados]
    return dados


def original_dlt(mensagem):
    """originalEvent da DLT para um objeto já lido (§7.4)."""
    original = _redigir(mensagem)
    try:
        texto = dumps(original)
    except (TypeError, ValueError, RecursionError):
        texto = str(original)
        original = None
    bruto = texto.encode("utf-8", errors="replace")
    if original is None or len(bruto) > LIMITE_DLT:
        return bruto[:LIMITE_DLT].decode("utf-8", errors="ignore")
    return original


def descrever(exc):
    return f"{type(exc).__name__}: {exc}"[:1000]


class Contexto:
    """Uma transação: relógio fixo, origem da mudança e eventos a publicar no fim."""

    def __init__(self, agora, topico, event_id=None, correlacao=None, usuario=None, empresa=None, chave=None):
        self.agora = agora
        self.topico = topico
        self.event_id = event_id
        self.correlacao = correlacao
        self.usuario = usuario
        self.empresa = empresa
        self.chave = chave
        self.timestamp = None  # timestamp do envelope consumido (RN-01)
        self.saida = []
        self.resultado = "ignorado"


class Servico:
    def __init__(self, repositorio, event_bus, clock, metricas):
        self.repo = repositorio
        self.bus = event_bus
        self.clock = clock
        self.metricas = metricas

    def agora(self):
        agora = self.clock.now()
        if agora.tzinfo is None:
            agora = agora.replace(tzinfo=timezone.utc)
        return agora.astimezone(timezone.utc)

    # ------------------------------------------------------------ infraestrutura comum

    def _publicar(self, ctx):
        for topico, chave, mensagem in ctx.saida:
            self.bus.publish(topico, chave, mensagem)

    def _auditar(self, ctx, acao, pedido_id=None, negociacao_id=None, **detalhe):
        self.repo.registrar_auditoria({
            "acao": acao, "instante": ctx.agora, "detalhe": {"topico": ctx.topico, "id": ctx.chave, **detalhe},
            "pedidoId": pedido_id, "negociacaoId": negociacao_id, "usuario": ctx.usuario, "empresa": ctx.empresa,
            "eventId": ctx.event_id, "correlationId": ctx.correlacao,
        })

    def _historico(self, ctx, pedido, anterior, origem, event_id, correlacao):
        self.repo.registrar_historico({
            "pedidoId": pedido["pedidoId"], "versao": pedido["versao"], "statusAnterior": anterior,
            "status": pedido["status"], "reservaSituacao": pedido["reservaSituacao"], "origem": origem,
            "motivo": pedido["motivoCancelamento"] if pedido["status"] == "CANCELADO" and anterior != "CANCELADO"
            else None,
            "eventId": event_id, "usuario": None if event_id else ctx.usuario, "correlationId": correlacao,
            "ocorridoEm": ctx.agora,
        })

    def _mudar(self, ctx, pedido, origem=None, event_id=None, correlacao=None, **campos):
        """RN-28: versão nova, histórico e exatamente um pedido_atualizado."""
        origem = origem or ctx.topico
        event_id = event_id if event_id is not None else ctx.event_id
        correlacao = correlacao or ctx.correlacao
        anterior = pedido["status"]
        novo = {**pedido, **campos, "versao": pedido["versao"] + 1, "atualizadoEm": ctx.agora}
        self.repo.atualizar_pedido(novo)
        self._historico(ctx, novo, anterior, origem, event_id, correlacao)
        carga = contratos.carga_pedido_atualizado(novo, anterior)
        ctx.saida.append(("pedido_atualizado", novo["pedidoId"],
                          contratos.envelope("pedido_atualizado", novo["pedidoId"], carga, correlacao, ctx.agora)))
        ctx.resultado = "aplicado"
        return novo

    def _dlt(self, topico, mensagem, erro, stack, tentativa):
        """Publica em pedidos.dlt fora da transação (§7.4, regras do dublê)."""
        dados = mensagem if isinstance(mensagem, dict) else {}
        recebido = dados.get("eventId")
        nome = f"pedidos.dlt:{topico}:{recebido if isinstance(recebido, str) else 'desconhecido'}"
        agregado = dados.get("aggregateId")
        chave = agregado if isinstance(agregado, str) and 1 <= len(agregado) <= 128 else "desconhecido"
        correlacao = dados.get("correlationId")
        if not isinstance(correlacao, str) or not contratos.CORRELACAO_RE.fullmatch(correlacao):
            correlacao = str(uuid4())
        carga = {"originalEvent": original_dlt(mensagem), "error": erro, "stackTrace": stack,
                 "failedAtAttempt": tentativa}
        self.bus.publish("pedidos.dlt", chave, contratos.envelope(
            "pedidos.dlt", chave, carga, correlacao, self.agora(), event_id=str(uuid5(NAMESPACE_URL, nome))))
        self.metricas.eventos.labels(topico, "dlt").inc()
        log.warning("Mensagem enviada para pedidos.dlt", extra={"codigo": erro, "correlationId": correlacao,
                                                                 "eventId": recebido if isinstance(recebido, str)
                                                                 else None})

    # ------------------------------------------------------------ consumo

    def handler(self, topico):
        aplicar = getattr(self, "_aplicar_" + topico)

        def tratar(mensagem):
            try:
                evento = contratos.validar_evento(topico, mensagem)
            except Invalido as exc:
                self._dlt(topico, mensagem, "EVENTO_INVALIDO", descrever(exc), 1)
                return
            for tentativa in (1, 2, 3):
                try:
                    resultado = self._processar(topico, evento, aplicar)
                    break
                except RepositorioIndisponivel:
                    raise
                except Rejeicao as exc:
                    self._dlt(topico, mensagem, exc.erro, None, 1)
                    return
                except Exception as exc:  # RN-26: 3 execuções no total
                    log.warning("Falha ao processar %s (tentativa %s): %s", topico, tentativa, descrever(exc),
                                extra={"eventId": evento["eventId"], "correlationId": evento["correlationId"]})
                    if tentativa == 3:
                        self._dlt(topico, mensagem, "TENTATIVAS_ESGOTADAS", descrever(exc), 3)
                        return
                    self.clock.sleep(tentativa)
            self.metricas.eventos.labels(topico, resultado).inc()
            if resultado == "aplicado" and topico in ("produto_cadastrado", "produto_atualizado"):
                self._reavaliar_pendentes(produto_id=evento["payload"]["produtoId"])
            elif resultado == "aplicado" and topico == "fornecimento_criado":
                self._reavaliar_pendentes(fornecimento_id=evento["payload"]["fornecimentoId"])

        return tratar

    def _processar(self, topico, evento, aplicar):
        with self.repo.transacao():
            if self.repo.evento_processado(evento["source"], evento["eventId"]):
                return "duplicado"
            ctx = Contexto(self.agora(), topico, evento["eventId"], evento["correlationId"],
                           chave=evento["aggregateId"])
            ctx.timestamp = evento["timestamp"]
            aplicar(ctx, evento["payload"])
            self.repo.registrar_evento_processado(evento["source"], evento["eventId"], topico)
            self._publicar(ctx)
            return ctx.resultado

    # RF-01 ------------------------------------------------------------

    def _aplicar_produto_cadastrado(self, ctx, p):
        atual = self.repo.obter_produto(p["produtoId"])
        novo = {**p, "timestamp": ctx.timestamp}
        if atual is None or ctx.timestamp > atual["timestamp"]:
            self.repo.salvar_produto(novo)
            ctx.resultado = "aplicado"
        elif ctx.timestamp == atual["timestamp"] and any(
                atual[c] != p[c] for c in ("nome", "unidade", "tipoTransporte", "pesoUnitarioKg")):
            self._auditar(ctx, "EVENTO_CONFLITANTE", produtoId=p["produtoId"])

    _aplicar_produto_atualizado = _aplicar_produto_cadastrado

    def _aplicar_fornecimento_criado(self, ctx, p):
        atual = self.repo.obter_oferta(p["fornecimentoId"])
        if atual is None:
            self.repo.salvar_oferta(p)
            ctx.resultado = "aplicado"
        elif any(atual[c] != p[c] for c in ("fornecedorId", "produtoId", "unidade", "origem")):
            self._auditar(ctx, "EVENTO_CONFLITANTE", fornecimentoId=p["fornecimentoId"])

    # RF-02 ------------------------------------------------------------

    def _aplicar_pedido_temporario_solicitado(self, ctx, s):
        negociacao, oferta = s["negociacaoId"], s["fornecimentoId"]
        self.repo.bloquear_negociacao(negociacao)
        pedido = self.repo.obter_pedido_da_oferta(negociacao, oferta)
        pendente = None if pedido else self.repo.obter_pendente(negociacao, oferta)
        if pedido or pendente:
            anterior = dominio.solicitacao_do_pedido(pedido) if pedido else pendente["payload"]
            if not dominio.mesma_solicitacao(anterior, s):
                self._auditar(ctx, "EVENTO_CONFLITANTE", pedido["pedidoId"] if pedido else None, negociacao,
                              fornecimentoId=oferta)
            return
        situacao = self._avaliar(ctx, s, ctx.event_id, ctx.correlacao)
        if situacao == "DESCARTADA":
            self._auditar(ctx, "SOLICITACAO_DESCARTADA", None, negociacao, fornecimentoId=oferta)
        elif situacao == "DIVERGENTE":
            raise Rejeicao("CONTRATO_DIVERGENTE")
        elif situacao != "CRIADO":
            self.repo.salvar_pendente({"negociacaoId": negociacao, "fornecimentoId": oferta, "payload": s,
                                       "eventId": ctx.event_id, "correlationId": ctx.correlacao,
                                       "causa": situacao, "recebidoEm": ctx.agora})
            ctx.resultado = "aplicado"

    def _avaliar(self, ctx, s, event_id, correlacao):
        """RN-06, passos 2 a 5. Devolve DESCARTADA, DIVERGENTE, a causa da pendência ou CRIADO."""
        if self.repo.obter_negociacao_encerrada(s["negociacaoId"]):
            return "DESCARTADA"
        produto = self.repo.obter_produto(s["produtoId"])
        oferta = self.repo.obter_oferta(s["fornecimentoId"])
        if dominio.diverge(s, produto, oferta):
            return "DIVERGENTE"
        causa = dominio.causa_pendencia(produto, oferta)
        if causa:
            return causa
        self._criar(ctx, s, produto, oferta, event_id, correlacao)
        return "CRIADO"

    def _criar(self, ctx, s, produto, oferta, event_id, correlacao):
        """RN-05."""
        pedido = {
            "pedidoId": str(uuid4()), "negociacaoId": s["negociacaoId"], "fornecimentoId": s["fornecimentoId"],
            "tipoNegociacao": s["tipo"], "produtoId": s["produtoId"], "fornecedorId": s["fornecedorId"],
            "compradorId": None, "quantidade": s["quantidade"], "unidade": produto["unidade"],
            "precoUnitarioOferta": s["precoUnitario"], "valorUnitario": s["precoUnitario"],
            "valorTotal": dominio.valor_total(s["quantidade"], s["precoUnitario"]),
            "produtoNome": produto["nome"], "tipoTransporte": produto["tipoTransporte"],
            "pesoKg": dominio.peso_kg(s["quantidade"], produto["unidade"], produto["pesoUnitarioKg"]),
            "origem": oferta["origem"], "destino": None, "dataEntregaDesejada": None, "status": "CRIADO",
            "reservaSituacao": "PENDENTE", "motivoCancelamento": None, "solicitacaoFreteVigenteId": None,
            "frete": None, "ocorrenciaFrete": None, "versao": 1, "criadoEm": ctx.agora, "atualizadoEm": ctx.agora,
            "vendidoEm": None, "entregueEm": None, "correlationId": correlacao,
        }
        self.repo.inserir_pedido(pedido)
        self._historico(ctx, pedido, None, "pedido_temporario_solicitado", event_id, correlacao)
        self.repo.registrar_auditoria({
            "acao": "PEDIDO_CRIADO", "instante": ctx.agora,
            "detalhe": {"topico": "pedido_temporario_solicitado", "id": s["negociacaoId"],
                        "fornecimentoId": s["fornecimentoId"]},
            "pedidoId": pedido["pedidoId"], "negociacaoId": s["negociacaoId"], "usuario": None, "empresa": None,
            "eventId": event_id, "correlationId": correlacao,
        })
        ctx.saida.append(("pedido_criado", pedido["pedidoId"], contratos.envelope(
            "pedido_criado", pedido["pedidoId"], contratos.carga_pedido_criado(pedido), correlacao, ctx.agora)))
        ctx.resultado = "aplicado"

    # RF-03 ------------------------------------------------------------

    def _aplicar_estoque_atualizado(self, ctx, p):
        operacao = p["operacao"]
        if operacao in ("REPOSICAO", "ENCERRAMENTO"):
            return
        pedido = self.repo.obter_pedido(p["pedidoId"], bloquear=True)
        if pedido is None:
            return
        if operacao in ("LIBERACAO", "BAIXA"):
            self._auditar(ctx, "ESTOQUE_REGISTRADO", pedido["pedidoId"], pedido["negociacaoId"], operacao=operacao)
            ctx.resultado = "aplicado"
            return
        igual = (p["fornecimentoId"] == pedido["fornecimentoId"]
                 and p["quantidadeMovimentada"] == pedido["quantidade"])
        if pedido["status"] == "CANCELADO" or (pedido["status"] == "ENTREGUE" and igual):
            return
        if pedido["status"] == "ENTREGUE":
            self._auditar(ctx, "EVENTO_FORA_DE_ESTADO", pedido["pedidoId"], pedido["negociacaoId"])
        elif not igual:
            self._auditar(ctx, "EVENTO_CONFLITANTE", pedido["pedidoId"], pedido["negociacaoId"])
        elif pedido["status"] == "CRIADO" and pedido["reservaSituacao"] == "PENDENTE":
            self._mudar(ctx, pedido, reservaSituacao="CONFIRMADA")
            self._auditar(ctx, "RESERVA_CONFIRMADA", pedido["pedidoId"], pedido["negociacaoId"])

    def _aplicar_reserva_estoque_recusada(self, ctx, p):
        pedido = self.repo.obter_pedido(p["pedidoId"], bloquear=True)
        if pedido is None:
            return
        igual = (p["fornecimentoId"] == pedido["fornecimentoId"]
                 and p["quantidadeSolicitada"] == pedido["quantidade"])
        if igual and pedido["reservaSituacao"] == "RECUSADA" and pedido["motivoCancelamento"] == p["motivo"]:
            return
        if pedido["status"] in dominio.TERMINAIS:
            self._auditar(ctx, "EVENTO_FORA_DE_ESTADO", pedido["pedidoId"], pedido["negociacaoId"])
        elif not igual or pedido["reservaSituacao"] != "PENDENTE" or pedido["status"] != "CRIADO":
            self._auditar(ctx, "EVENTO_CONFLITANTE", pedido["pedidoId"], pedido["negociacaoId"])
        else:
            self._mudar(ctx, pedido, status="CANCELADO", reservaSituacao="RECUSADA",
                        motivoCancelamento=p["motivo"])
            self._auditar(ctx, "PEDIDO_CANCELADO", pedido["pedidoId"], pedido["negociacaoId"], motivo=p["motivo"])

    # RF-04 ------------------------------------------------------------

    def _aplicar_negociacao_fechada(self, ctx, f):
        negociacao = f["negociacaoId"]
        self.repo.bloquear_negociacao(negociacao)
        registro = {"negociacaoId": negociacao, "status": f["status"], "compradorId": f["compradorId"],
                    "alocacoes": dominio.normalizar_alocacoes(f["alocacoes"]), "recebidoEm": ctx.agora}
        anterior = self.repo.obter_negociacao_encerrada(negociacao)
        if anterior is not None:
            if not dominio.mesmo_fechamento(anterior, registro):
                self._auditar(ctx, "EVENTO_CONFLITANTE", None, negociacao, status=f["status"])
            return
        ids = sorted(p["pedidoId"] for p in self.repo.listar_pedidos_da_negociacao(negociacao))
        pedidos = {i: self.repo.obter_pedido(i, bloquear=True) for i in ids}
        if f["status"] == "ENCERRADO" and not dominio.fechamento_compativel(f, registro["alocacoes"], pedidos):
            raise Rejeicao("FECHAMENTO_INCOMPATIVEL")
        alocacoes = {a["pedidoId"]: a for a in registro["alocacoes"]}
        for pedido_id in ids:
            pedido = pedidos[pedido_id]
            if pedido["status"] in dominio.TERMINAIS:
                continue
            alocacao = alocacoes.get(pedido_id)
            if alocacao:  # RN-09
                self._mudar(ctx, pedido, status="VENDA_CONCLUIDA", compradorId=f["compradorId"],
                            valorUnitario=alocacao["valorUnitario"], valorTotal=alocacao["valorTotal"],
                            vendidoEm=ctx.agora)
                self._auditar(ctx, "VENDA_CONCLUIDA", pedido_id, negociacao)
            else:  # RN-10
                motivo = "FORA_DO_LOTE" if f["status"] == "ENCERRADO" else "NEGOCIACAO_SEM_VENDA"
                reserva = ("ENCERRADA" if pedido["reservaSituacao"] in ("PENDENTE", "CONFIRMADA")
                           else pedido["reservaSituacao"])
                self._mudar(ctx, pedido, status="CANCELADO", reservaSituacao=reserva, motivoCancelamento=motivo)
                self._auditar(ctx, "PEDIDO_CANCELADO", pedido_id, negociacao, motivo=motivo)
        self.repo.salvar_negociacao_encerrada(registro)
        ctx.resultado = "aplicado"

    # RF-06 ------------------------------------------------------------

    def _pedido_de_frete(self, ctx, p):
        pedido = self.repo.obter_pedido(p["pedidoId"], bloquear=True)
        if pedido is None:  # RN-18: sem DLT
            self._auditar(ctx, "EVENTO_NAO_CORRELACIONADO", pedidoId=p["pedidoId"])
        return pedido

    def _fora_de_estado(self, ctx, pedido):
        if pedido["status"] in dominio.EM_FRETE:
            return False
        self._auditar(ctx, "EVENTO_FORA_DE_ESTADO", pedido["pedidoId"], pedido["negociacaoId"])
        return True

    def _tornar_vigente(self, ctx, pedido, solicitacao_id):
        """RN-19. Devolve (pedido, solicitação) ou None se outra vigente ainda vale."""
        vigente_id = pedido["solicitacaoFreteVigenteId"]
        if vigente_id:
            vigente = self.repo.obter_solicitacao(vigente_id)
            if not vigente["indisponivel"]:
                self._auditar(ctx, "EVENTO_CONFLITANTE", pedido["pedidoId"], pedido["negociacaoId"],
                              solicitacaoFreteId=solicitacao_id)
                return None
            self.repo.salvar_solicitacao({**vigente, "situacao": "SUBSTITUIDA"})
        # DECISÃO: anteriorId encadeia as solicitações do pedido, para a RN-21 achar seleções antigas.
        nova = {"solicitacaoFreteId": solicitacao_id, "pedidoId": pedido["pedidoId"], "situacao": "VIGENTE",
                "indisponivel": False, "motivoIndisponivel": None, "anteriorId": vigente_id, "selecoes": []}
        self.repo.salvar_solicitacao(nova)
        pedido = {**pedido, "solicitacaoFreteVigenteId": solicitacao_id, "ocorrenciaFrete": None,
                  "atualizadoEm": ctx.agora}
        self.repo.atualizar_pedido(pedido)
        ctx.resultado = "aplicado"
        return pedido, nova

    def _cadeia(self, pedido):
        solicitacoes, atual = [], pedido["solicitacaoFreteVigenteId"]
        while atual:
            solicitacao = self.repo.obter_solicitacao(atual)
            if solicitacao is None:
                break
            solicitacoes.append(solicitacao)
            atual = solicitacao.get("anteriorId")
        return solicitacoes

    def _aplicar_solicitacao_frete_criada(self, ctx, p):
        pedido = self._pedido_de_frete(ctx, p)
        if pedido is None:
            return
        solicitacao = self.repo.obter_solicitacao(p["solicitacaoFreteId"])
        if solicitacao is not None and solicitacao["pedidoId"] == pedido["pedidoId"]:
            return
        if self._fora_de_estado(ctx, pedido):
            return
        if solicitacao is not None:
            self._auditar(ctx, "EVENTO_CONFLITANTE", pedido["pedidoId"], pedido["negociacaoId"],
                          solicitacaoFreteId=p["solicitacaoFreteId"])
            return
        self._tornar_vigente(ctx, pedido, p["solicitacaoFreteId"])

    def _aplicar_frete_selecionado(self, ctx, p):
        pedido = self._pedido_de_frete(ctx, p)
        if pedido is None:
            return
        selecao = {campo: p[campo] for campo in dominio.CAMPOS_SELECAO}
        solicitacao = self.repo.obter_solicitacao(p["solicitacaoFreteId"])
        if (solicitacao is not None and solicitacao["pedidoId"] == pedido["pedidoId"]
                and any(dominio.mesma_selecao(s, selecao) for s in solicitacao["selecoes"])):
            return
        if self._fora_de_estado(ctx, pedido):
            return
        if solicitacao is None:
            vigente = self._tornar_vigente(ctx, pedido, p["solicitacaoFreteId"])
            if vigente is None:
                return
            pedido, solicitacao = vigente
        elif solicitacao["pedidoId"] != pedido["pedidoId"]:
            self._auditar(ctx, "EVENTO_CONFLITANTE", pedido["pedidoId"], pedido["negociacaoId"])
            return
        elif solicitacao["situacao"] == "SUBSTITUIDA":
            self._auditar(ctx, "EVENTO_FORA_DE_ESTADO", pedido["pedidoId"], pedido["negociacaoId"])
            return
        aplicada = dominio.selecao_aplicada(solicitacao)
        if aplicada and p["tentativa"] < aplicada["tentativa"]:
            return
        if aplicada and p["tentativa"] == aplicada["tentativa"]:
            self._auditar(ctx, "EVENTO_CONFLITANTE", pedido["pedidoId"], pedido["negociacaoId"])
            return
        selecoes = [{**s, "situacao": "SUBSTITUIDA"} if s["situacao"] == "APLICADA" else s
                    for s in solicitacao["selecoes"]]
        self.repo.salvar_solicitacao({**solicitacao, "selecoes": selecoes + [{**selecao, "situacao": "APLICADA"}]})
        pedido = self._mudar(ctx, pedido, status="FRETE_SELECIONADO", frete=selecao)
        self._auditar(ctx, "FRETE_APLICADO", pedido["pedidoId"], pedido["negociacaoId"],
                      solicitacaoFreteId=selecao["solicitacaoFreteId"], tentativa=selecao["tentativa"])
        par = (selecao["entregaId"], selecao["transportadoraId"])
        for entrega in self.repo.listar_entregas_pendentes(pedido["pedidoId"], 100):
            if (entrega["entregaId"], entrega["transportadoraId"]) == par:
                self._entregar(ctx, pedido, entrega, entrega["eventId"], entrega["correlationId"])
                self.repo.remover_entrega_pendente(pedido["pedidoId"], entrega["entregaId"])
                break

    def _entregar(self, ctx, pedido, entrega, event_id, correlacao):
        novo = self._mudar(ctx, pedido, origem="entrega_concluida", event_id=event_id, correlacao=correlacao,
                           status="ENTREGUE", entregueEm=entrega["entregueEm"])
        self._auditar(ctx, "ENTREGA_APLICADA", pedido["pedidoId"], pedido["negociacaoId"],
                      entregaId=entrega["entregaId"])
        return novo

    def _aplicar_entrega_concluida(self, ctx, p):
        pedido = self._pedido_de_frete(ctx, p)
        if pedido is None:
            return
        par = (p["entregaId"], p["transportadoraId"])
        frete = pedido["frete"]
        if (pedido["status"] == "ENTREGUE" and frete and (frete["entregaId"], frete["transportadoraId"]) == par
                and pedido["entregueEm"] == p["entregueEm"]):
            return
        guardadas = self.repo.listar_entregas_pendentes(pedido["pedidoId"], 100)
        if any((e["entregaId"], e["transportadoraId"], e["entregueEm"]) == (*par, p["entregueEm"])
               for e in guardadas):
            return
        if self._fora_de_estado(ctx, pedido):
            return
        solicitacoes = self._cadeia(pedido)
        vigente = dominio.selecao_aplicada(solicitacoes[0] if solicitacoes else None)
        if vigente and (vigente["entregaId"], vigente["transportadoraId"]) == par:
            self._entregar(ctx, pedido, p, ctx.event_id, ctx.correlacao)
        elif dominio.par_substituido(par, solicitacoes):
            self._auditar(ctx, "ENTREGA_DESCARTADA", pedido["pedidoId"], pedido["negociacaoId"],
                          entregaId=p["entregaId"])
        elif vigente and vigente["entregaId"] == p["entregaId"]:
            self._auditar(ctx, "EVENTO_CONFLITANTE", pedido["pedidoId"], pedido["negociacaoId"],
                          entregaId=p["entregaId"])
        else:
            self.repo.salvar_entrega_pendente({**p, "eventId": ctx.event_id, "correlationId": ctx.correlacao,
                                               "recebidoEm": ctx.agora})
            ctx.resultado = "aplicado"

    def _aplicar_frete_indisponivel(self, ctx, p):
        pedido = self._pedido_de_frete(ctx, p)
        if pedido is None:
            return
        solicitacao = self.repo.obter_solicitacao(p["solicitacaoFreteId"])
        propria = solicitacao is not None and solicitacao["pedidoId"] == pedido["pedidoId"]
        if propria and solicitacao["indisponivel"] and solicitacao["motivoIndisponivel"] == p["motivo"]:
            return
        if self._fora_de_estado(ctx, pedido):
            return
        if not propria:
            self._auditar(ctx, "EVENTO_NAO_CORRELACIONADO", pedido["pedidoId"], pedido["negociacaoId"],
                          solicitacaoFreteId=p["solicitacaoFreteId"])
            return
        if solicitacao["situacao"] == "SUBSTITUIDA":
            self._auditar(ctx, "EVENTO_FORA_DE_ESTADO", pedido["pedidoId"], pedido["negociacaoId"])
            return
        self.repo.salvar_solicitacao({**solicitacao, "indisponivel": True, "motivoIndisponivel": p["motivo"]})
        self.repo.atualizar_pedido({**pedido, "atualizadoEm": ctx.agora, "ocorrenciaFrete": {
            "motivo": p["motivo"], "ocorridoEm": contratos.instante_txt(p["ocorridoEm"])}})
        self._auditar(ctx, "FRETE_INDISPONIVEL", pedido["pedidoId"], pedido["negociacaoId"], motivo=p["motivo"],
                      solicitacaoFreteId=p["solicitacaoFreteId"], detalhe=p["detalhe"])
        ctx.resultado = "aplicado"

    # ------------------------------------------------------------ reconciliação

    def _reavaliar_pendentes(self, produto_id=None, fornecimento_id=None):
        """RN-01: reavaliação depois da projeção. Falha fica para o reconciliar."""
        try:
            for pendente in self.repo.listar_pendentes(100, produto_id=produto_id, fornecimento_id=fornecimento_id):
                self._reavaliar(pendente)
        except Exception as exc:
            log.warning("Reavaliação de pendências adiada: %s", descrever(exc))

    def _reavaliar(self, guardada):
        """RN-06 na reavaliação, numa transação. Devolve 1 se criou o pedido."""
        negociacao, oferta = guardada["negociacaoId"], guardada["fornecimentoId"]
        with self.repo.transacao():
            ctx = Contexto(self.agora(), "pedido_temporario_solicitado", guardada["eventId"],
                           guardada["correlationId"], chave=negociacao)
            self.repo.bloquear_negociacao(negociacao)
            pendente = self.repo.obter_pendente(negociacao, oferta)
            if pendente is None:
                return 0
            situacao = self._avaliar(ctx, pendente["payload"], pendente["eventId"], pendente["correlationId"])
            if situacao in ("DESCARTADA", "DIVERGENTE", "CRIADO"):
                self.repo.remover_pendente(negociacao, oferta)
            if situacao == "DESCARTADA":
                self._auditar(ctx, "SOLICITACAO_DESCARTADA", None, negociacao, fornecimentoId=oferta)
            elif situacao == "DIVERGENTE":
                self._auditar(ctx, "SOLICITACAO_INVALIDA", None, negociacao, fornecimentoId=oferta)
            elif situacao != "CRIADO" and situacao != pendente["causa"]:
                self.repo.salvar_pendente({**pendente, "causa": situacao})
            self._publicar(ctx)
            return 1 if situacao == "CRIADO" else 0

    def _reavaliar_entrega(self, guardada):
        with self.repo.transacao():
            ctx = Contexto(self.agora(), "entrega_concluida", guardada["eventId"], guardada["correlationId"],
                           chave=guardada["pedidoId"])
            pedido = self.repo.obter_pedido(guardada["pedidoId"], bloquear=True)
            if pedido is None or pedido["status"] in dominio.TERMINAIS:
                self.repo.remover_entrega_pendente(guardada["pedidoId"], guardada["entregaId"])
                return 0
            vigente = dominio.selecao_aplicada(self.repo.obter_solicitacao(pedido["solicitacaoFreteVigenteId"])
                                               if pedido["solicitacaoFreteVigenteId"] else None)
            if not vigente or (vigente["entregaId"], vigente["transportadoraId"]) != (
                    guardada["entregaId"], guardada["transportadoraId"]):
                return 0
            self._entregar(ctx, pedido, guardada, guardada["eventId"], guardada["correlationId"])
            self.repo.remover_entrega_pendente(guardada["pedidoId"], guardada["entregaId"])
            self._publicar(ctx)
            return 1

    def reconciliar(self):
        total = 0
        agora = self.agora()
        pendentes = self.repo.listar_pendentes(100)
        antigas = [p for p in pendentes if agora - p["recebidoEm"] > timedelta(hours=24)]
        # shortcut: os gauges contam até 100 pendências (o limite da leitura); trocar por COUNT se passar disso.
        self.metricas.pendencias.set(len(pendentes))
        self.metricas.pendencias_antigas.set(len(antigas))
        for pendente in antigas:
            log.warning("Solicitação pendente há mais de 24 h",
                        extra={"codigo": "PENDENCIA_ANTIGA", "negociacaoId": pendente["negociacaoId"],
                               "eventId": pendente["eventId"], "correlationId": pendente["correlationId"]})
        for pendente in pendentes:
            try:
                total += self._reavaliar(pendente)
            except RepositorioIndisponivel:
                raise
            except Exception as exc:
                log.warning("Pendência não reavaliada: %s", descrever(exc))
        for entrega in self.repo.listar_entregas_pendentes(None, 100):
            try:
                total += self._reavaliar_entrega(entrega)
            except RepositorioIndisponivel:
                raise
            except Exception as exc:
                log.warning("Entrega guardada não reavaliada: %s", descrever(exc))
        return total

    # ------------------------------------------------------------ API

    def listar(self, ator, status, negociacao_id, pagina, tamanho):
        empresa, escopo = (None, None) if ator.suporte else (ator.empresa, ator.escopo)
        itens, total = self.repo.listar_pedidos(empresa, escopo, status, negociacao_id, (pagina - 1) * tamanho,
                                                tamanho)
        return {"items": [contratos.item_lista(p) for p in itens], "page": pagina, "size": tamanho, "total": total}

    def _visivel(self, ator, pedido_id):
        pedido = self.repo.obter_pedido(pedido_id)
        if pedido is None or not dominio.visivel(pedido, ator):
            raise ErroHttp("PEDIDO_NAO_ENCONTRADO")
        return pedido

    def detalhe(self, ator, pedido_id):
        return contratos.detalhe(self._visivel(ator, pedido_id))

    def historico(self, ator, pedido_id, pagina, tamanho):
        self._visivel(ator, pedido_id)
        itens, total = self.repo.listar_historico(pedido_id, (pagina - 1) * tamanho, tamanho)
        return {"items": [contratos.item_historico(h) for h in itens], "page": pagina, "size": tamanho,
                "total": total}

    def informar_destino(self, ator, pedido_id, destino, data_desejada, escopo_idempotencia, hash_corpo,
                         correlacao):
        """API-07 (RN-14, RN-15, RN-24), numa transação."""
        with self.repo.transacao():
            ctx = Contexto(self.agora(), "http_destino", correlacao=correlacao, usuario=ator.usuario,
                           empresa=ator.empresa, chave=pedido_id)
            pedido = self.repo.obter_pedido(pedido_id, bloquear=True)
            if pedido is None or pedido["compradorId"] != ator.empresa:
                raise ErroHttp("PEDIDO_NAO_ENCONTRADO")
            if escopo_idempotencia:
                memorizada = self.repo.obter_idempotencia(escopo_idempotencia)
                if memorizada and memorizada["expiraEm"] > ctx.agora:
                    if memorizada["hash"] != hash_corpo:
                        raise ErroHttp("IDEMPOTENCIA_CONFLITO")
                    return memorizada["status"], memorizada["corpo"]
            if pedido["status"] in ("AGUARDANDO_FRETE", "FRETE_SELECIONADO", "ENTREGUE"):
                if pedido["destino"] != destino or pedido["dataEntregaDesejada"] != data_desejada:
                    raise ErroHttp("TRANSICAO_INVALIDA")
            elif pedido["status"] == "VENDA_CONCLUIDA":
                if data_desejada is not None and data_desejada < dominio.hoje(ctx.agora):
                    raise ErroHttp("VALIDACAO_INVALIDA")
                pedido = self._mudar(ctx, pedido, origem="http_destino", event_id=None,
                                     status="AGUARDANDO_FRETE", destino=destino, dataEntregaDesejada=data_desejada)
                self._auditar(ctx, "DESTINO_INFORMADO", pedido["pedidoId"], pedido["negociacaoId"])
            else:
                raise ErroHttp("TRANSICAO_INVALIDA")
            corpo = contratos.detalhe(pedido)
            if escopo_idempotencia:
                self.repo.salvar_idempotencia(escopo_idempotencia, {
                    "hash": hash_corpo, "status": 200, "corpo": corpo, "expiraEm": ctx.agora + timedelta(hours=24)})
            self._publicar(ctx)
            return 200, corpo
