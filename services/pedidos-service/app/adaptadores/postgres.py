"""PedidosRepositorio real: psycopg 3, pool ≤ 5, role svc_pedidos, schema pedidos."""
import re
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from functools import wraps
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb, set_json_loads
from psycopg_pool import ConnectionPool, PoolTimeout

from app.jsonio import dumps, loads
from app.portas import RepositorioIndisponivel

VERSAO_ESQUEMA = "0001"
INDISPONIVEL = {"57P01", "57P02", "57P03"}
_conexao = ContextVar("pedidos_conexao", default=None)

PEDIDO = ("pedidoId", "negociacaoId", "fornecimentoId", "tipoNegociacao", "produtoId", "fornecedorId",
          "compradorId", "quantidade", "unidade", "precoUnitarioOferta", "valorUnitario", "valorTotal",
          "produtoNome", "tipoTransporte", "pesoKg", "origem", "destino", "dataEntregaDesejada", "status",
          "reservaSituacao", "motivoCancelamento", "solicitacaoFreteVigenteId", "frete", "ocorrenciaFrete",
          "versao", "criadoEm", "atualizadoEm", "vendidoEm", "entregueEm", "correlationId")
JSON_PEDIDO = {"origem", "destino", "frete", "ocorrenciaFrete"}


def coluna(campo):
    return re.sub(r"([A-Z])", r"_\1", campo).lower()


def _texto(valor):
    return str(valor) if isinstance(valor, UUID) else valor


def _linha(linha, campos):
    return None if linha is None else {campo: _texto(linha[coluna(campo)]) for campo in campos}


def falha_de_conexao(exc):
    if isinstance(exc, PoolTimeout):
        return True
    if isinstance(exc, psycopg.OperationalError):
        estado = exc.sqlstate
        # DECISÃO: OperationalError sem SQLSTATE é a conexão perdida no cliente; trata como classe 08.
        return estado is None or estado.startswith("08") or estado in INDISPONIVEL
    return False


def traduzir(metodo):
    @wraps(metodo)
    def envolvido(*args, **kwargs):
        try:
            return metodo(*args, **kwargs)
        except Exception as exc:
            if falha_de_conexao(exc):
                raise RepositorioIndisponivel(type(exc).__name__) from exc
            raise
    return envolvido


def _configurar(conn):
    set_json_loads(loads, conn)


def jsonb(valor):
    return None if valor is None else Jsonb(valor, dumps=dumps)


class PostgresRepositorio:
    def __init__(self, dsn):
        self.pool = ConnectionPool(dsn, open=False, min_size=1, max_size=5, timeout=2, name="pedidos",
                                   kwargs={"row_factory": dict_row, "connect_timeout": 3}, configure=_configurar)

    def abrir(self):
        self.pool.open(wait=False)

    def fechar(self):
        self.pool.close()

    # ------------------------------------------------------------ conexão e transação

    @contextmanager
    def transacao(self):
        if _conexao.get() is not None:
            yield
            return
        try:
            with self.pool.connection() as conn, conn.transaction():
                marca = _conexao.set(conn)
                try:
                    yield
                finally:
                    _conexao.reset(marca)
        except RepositorioIndisponivel:
            raise
        except Exception as exc:
            if falha_de_conexao(exc):
                raise RepositorioIndisponivel(type(exc).__name__) from exc
            raise

    @contextmanager
    def _cursor(self):
        atual = _conexao.get()
        if atual is not None:
            yield atual
        else:
            with self.pool.connection() as conn:
                yield conn

    def conexao_atual(self):
        """Usada pela outbox: grava na mesma transação da mudança."""
        conn = _conexao.get()
        if conn is None:
            raise RuntimeError("publish fora de transação")
        return conn

    def _um(self, sql, parametros=()):
        with self._cursor() as conn:
            return conn.execute(sql, parametros).fetchone()

    def _todos(self, sql, parametros=()):
        with self._cursor() as conn:
            return conn.execute(sql, parametros).fetchall()

    def _executar(self, sql, parametros=()):
        with self._cursor() as conn:
            conn.execute(sql, parametros)

    def disponivel(self):
        try:
            with self.pool.connection(timeout=2) as conn:
                linha = conn.execute("SELECT version_num FROM pedidos.alembic_version").fetchone()
            return bool(linha and linha["version_num"] == VERSAO_ESQUEMA)
        except Exception:
            return False

    # ------------------------------------------------------------ consumo idempotente

    @traduzir
    def evento_processado(self, source, event_id):
        return self._um("SELECT 1 FROM pedidos.processed_events WHERE source=%s AND event_id=%s",
                        (source, event_id)) is not None

    @traduzir
    def registrar_evento_processado(self, source, event_id, topico):
        self._executar("INSERT INTO pedidos.processed_events (source, event_id, topico) VALUES (%s,%s,%s)",
                       (source, event_id, topico))

    # ------------------------------------------------------------ projeções

    @traduzir
    def obter_produto(self, produto_id):
        linha = self._um("SELECT * FROM pedidos.produto WHERE produto_id=%s", (produto_id,))
        if linha is None:
            return None
        return {"produtoId": linha["produto_id"], "nome": linha["nome"], "unidade": linha["unidade"],
                "tipoTransporte": linha["tipo_transporte"], "pesoUnitarioKg": linha["peso_unitario_kg"],
                "timestamp": linha["evento_em"]}

    @traduzir
    def salvar_produto(self, produto):
        self._executar("""
            INSERT INTO pedidos.produto (produto_id, nome, unidade, tipo_transporte, peso_unitario_kg, evento_em)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (produto_id) DO UPDATE SET nome=EXCLUDED.nome, unidade=EXCLUDED.unidade,
              tipo_transporte=EXCLUDED.tipo_transporte, peso_unitario_kg=EXCLUDED.peso_unitario_kg,
              evento_em=EXCLUDED.evento_em
        """, (produto["produtoId"], produto["nome"], produto["unidade"], produto["tipoTransporte"],
              produto["pesoUnitarioKg"], produto["timestamp"]))

    @traduzir
    def obter_oferta(self, fornecimento_id):
        linha = self._um("SELECT * FROM pedidos.oferta WHERE fornecimento_id=%s", (fornecimento_id,))
        if linha is None:
            return None
        return {"fornecimentoId": linha["fornecimento_id"], "fornecedorId": linha["fornecedor_id"],
                "produtoId": linha["produto_id"], "unidade": linha["unidade"], "origem": linha["origem"]}

    @traduzir
    def salvar_oferta(self, oferta):
        self._executar("""
            INSERT INTO pedidos.oferta (fornecimento_id, fornecedor_id, produto_id, unidade, origem)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (fornecimento_id) DO UPDATE SET fornecedor_id=EXCLUDED.fornecedor_id,
              produto_id=EXCLUDED.produto_id, unidade=EXCLUDED.unidade, origem=EXCLUDED.origem
        """, (oferta["fornecimentoId"], oferta["fornecedorId"], oferta["produtoId"], oferta["unidade"],
              jsonb(oferta["origem"])))

    # ------------------------------------------------------------ negociação

    @traduzir
    def bloquear_negociacao(self, negociacao_id):
        self._executar("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (negociacao_id,))

    @traduzir
    def obter_negociacao_encerrada(self, negociacao_id):
        linha = self._um("SELECT * FROM pedidos.negociacao_encerrada WHERE negociacao_id=%s", (negociacao_id,))
        if linha is None:
            return None
        return {"negociacaoId": linha["negociacao_id"], "status": linha["status"],
                "compradorId": linha["comprador_id"], "alocacoes": linha["alocacoes"],
                "recebidoEm": linha["recebido_em"]}

    @traduzir
    def salvar_negociacao_encerrada(self, registro):
        self._executar("""
            INSERT INTO pedidos.negociacao_encerrada (negociacao_id, status, comprador_id, alocacoes, recebido_em)
            VALUES (%s,%s,%s,%s,%s)
        """, (registro["negociacaoId"], registro["status"], registro["compradorId"], jsonb(registro["alocacoes"]),
              registro["recebidoEm"]))

    # ------------------------------------------------------------ pendências

    @staticmethod
    def _pendente(linha):
        if linha is None:
            return None
        # O JSON devolve 5 (inteiro) para uma quantidade 5.00; as regras de valor e peso precisam de Decimal.
        payload = {**linha["payload"], **{c: Decimal(str(linha["payload"][c])) for c in ("quantidade", "precoUnitario")}}
        return {"negociacaoId": linha["negociacao_id"], "fornecimentoId": linha["fornecimento_id"],
                "payload": payload, "eventId": str(linha["event_id"]),
                "correlationId": linha["correlation_id"], "causa": linha["causa"], "recebidoEm": linha["recebido_em"]}

    @traduzir
    def obter_pendente(self, negociacao_id, fornecimento_id):
        return self._pendente(self._um("""
            SELECT * FROM pedidos.solicitacao_pendente WHERE negociacao_id=%s AND fornecimento_id=%s
        """, (negociacao_id, fornecimento_id)))

    @traduzir
    def salvar_pendente(self, pendente):
        self._executar("""
            INSERT INTO pedidos.solicitacao_pendente
              (negociacao_id, fornecimento_id, produto_id, payload, event_id, correlation_id, causa, recebido_em)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (negociacao_id, fornecimento_id) DO UPDATE SET causa=EXCLUDED.causa
        """, (pendente["negociacaoId"], pendente["fornecimentoId"], pendente["payload"]["produtoId"],
              jsonb(pendente["payload"]), pendente["eventId"], pendente["correlationId"], pendente["causa"],
              pendente["recebidoEm"]))

    @traduzir
    def remover_pendente(self, negociacao_id, fornecimento_id):
        self._executar("DELETE FROM pedidos.solicitacao_pendente WHERE negociacao_id=%s AND fornecimento_id=%s",
                       (negociacao_id, fornecimento_id))

    @traduzir
    def listar_pendentes(self, limite, produto_id=None, fornecimento_id=None):
        return [self._pendente(linha) for linha in self._todos("""
            SELECT * FROM pedidos.solicitacao_pendente
            WHERE (%(p)s::text IS NULL OR produto_id=%(p)s) AND (%(f)s::text IS NULL OR fornecimento_id=%(f)s)
            ORDER BY recebido_em, negociacao_id, fornecimento_id LIMIT %(l)s
        """, {"p": produto_id, "f": fornecimento_id, "l": limite})]

    # ------------------------------------------------------------ pedidos

    @traduzir
    def obter_pedido(self, pedido_id, bloquear=False):
        sql = "SELECT * FROM pedidos.pedido WHERE pedido_id=%s" + (" FOR UPDATE" if bloquear else "")
        return _linha(self._um(sql, (pedido_id,)), PEDIDO)

    @traduzir
    def obter_pedido_da_oferta(self, negociacao_id, fornecimento_id):
        return _linha(self._um("SELECT * FROM pedidos.pedido WHERE negociacao_id=%s AND fornecimento_id=%s",
                               (negociacao_id, fornecimento_id)), PEDIDO)

    @traduzir
    def listar_pedidos_da_negociacao(self, negociacao_id):
        return [_linha(linha, PEDIDO) for linha in self._todos(
            "SELECT * FROM pedidos.pedido WHERE negociacao_id=%s ORDER BY pedido_id", (negociacao_id,))]

    @staticmethod
    def _valores(pedido):
        return [jsonb(pedido[c]) if c in JSON_PEDIDO else pedido[c] for c in PEDIDO]

    @traduzir
    def inserir_pedido(self, pedido):
        colunas = ", ".join(coluna(c) for c in PEDIDO)
        self._executar(f"INSERT INTO pedidos.pedido ({colunas}) VALUES ({', '.join(['%s'] * len(PEDIDO))})",
                       self._valores(pedido))

    @traduzir
    def atualizar_pedido(self, pedido):
        valores = self._valores(pedido)
        atribuicoes = ", ".join(f"{coluna(c)}=%s" for c in PEDIDO[1:])
        self._executar(f"UPDATE pedidos.pedido SET {atribuicoes} WHERE pedido_id=%s", valores[1:] + valores[:1])

    @traduzir
    def listar_pedidos(self, empresa, escopo, status, negociacao_id, offset, limite):
        filtros, parametros = [], []
        if escopo == "BUYER":
            filtros.append("comprador_id=%s")
            parametros.append(empresa)
        elif escopo == "SUPPLIER":
            filtros.append("fornecedor_id=%s")
            parametros.append(empresa)
        elif escopo is not None:
            filtros.append("(comprador_id=%s OR fornecedor_id=%s)")
            parametros += [empresa, empresa]
        if status:
            filtros.append("status=%s")
            parametros.append(status)
        if negociacao_id:
            filtros.append("negociacao_id=%s")
            parametros.append(negociacao_id)
        onde = " WHERE " + " AND ".join(filtros) if filtros else ""
        total = self._um(f"SELECT count(*) AS n FROM pedidos.pedido{onde}", parametros)["n"]
        linhas = self._todos(f"""SELECT * FROM pedidos.pedido{onde}
                                 ORDER BY criado_em DESC, pedido_id DESC OFFSET %s LIMIT %s""",
                             parametros + [offset, limite])
        return [_linha(linha, PEDIDO) for linha in linhas], total

    # ------------------------------------------------------------ histórico e auditoria

    @traduzir
    def registrar_historico(self, item):
        self._executar("""
            INSERT INTO pedidos.pedido_historico (pedido_id, versao, status_anterior, status, reserva_situacao,
              origem, motivo, event_id, usuario, correlation_id, ocorrido_em)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (item["pedidoId"], item["versao"], item["statusAnterior"], item["status"], item["reservaSituacao"],
              item["origem"], item["motivo"], item["eventId"], item["usuario"], item["correlationId"],
              item["ocorridoEm"]))

    @traduzir
    def listar_historico(self, pedido_id, offset, limite):
        total = self._um("SELECT count(*) AS n FROM pedidos.pedido_historico WHERE pedido_id=%s",
                         (pedido_id,))["n"]
        linhas = self._todos("""SELECT * FROM pedidos.pedido_historico WHERE pedido_id=%s
                                ORDER BY versao OFFSET %s LIMIT %s""", (pedido_id, offset, limite))
        campos = ("pedidoId", "versao", "statusAnterior", "status", "reservaSituacao", "origem", "motivo",
                  "eventId", "usuario", "correlationId", "ocorridoEm")
        return [_linha(linha, campos) for linha in linhas], total

    @traduzir
    def registrar_auditoria(self, registro):
        self._executar("""
            INSERT INTO pedidos.audit_log (acao, instante, detalhe, pedido_id, negociacao_id, usuario, empresa,
              event_id, correlation_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (registro["acao"], registro["instante"], jsonb(registro["detalhe"]), registro.get("pedidoId"),
              registro.get("negociacaoId"), registro.get("usuario"), registro.get("empresa"),
              registro.get("eventId"), registro.get("correlationId")))

    # ------------------------------------------------------------ frete

    @traduzir
    def obter_solicitacao(self, solicitacao_frete_id):
        linha = self._um("SELECT * FROM pedidos.solicitacao_frete WHERE solicitacao_frete_id=%s",
                         (solicitacao_frete_id,))
        if linha is None:
            return None
        return {"solicitacaoFreteId": linha["solicitacao_frete_id"], "pedidoId": str(linha["pedido_id"]),
                "situacao": linha["situacao"], "indisponivel": linha["indisponivel"],
                "motivoIndisponivel": linha["motivo_indisponivel"], "anteriorId": linha["anterior_id"],
                "selecoes": linha["selecoes"]}

    @traduzir
    def salvar_solicitacao(self, s):
        self._executar("""
            INSERT INTO pedidos.solicitacao_frete (solicitacao_frete_id, pedido_id, situacao, indisponivel,
              motivo_indisponivel, anterior_id, selecoes)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (solicitacao_frete_id) DO UPDATE SET situacao=EXCLUDED.situacao,
              indisponivel=EXCLUDED.indisponivel, motivo_indisponivel=EXCLUDED.motivo_indisponivel,
              selecoes=EXCLUDED.selecoes
        """, (s["solicitacaoFreteId"], s["pedidoId"], s["situacao"], s["indisponivel"], s["motivoIndisponivel"],
              s.get("anteriorId"), jsonb(s["selecoes"])))

    @traduzir
    def salvar_entrega_pendente(self, e):
        self._executar("""
            INSERT INTO pedidos.entrega_pendente (pedido_id, entrega_id, transportadora_id, entregue_em, event_id,
              correlation_id, recebido_em)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (pedido_id, entrega_id) DO UPDATE SET transportadora_id=EXCLUDED.transportadora_id,
              entregue_em=EXCLUDED.entregue_em, event_id=EXCLUDED.event_id, correlation_id=EXCLUDED.correlation_id
        """, (e["pedidoId"], e["entregaId"], e["transportadoraId"], e["entregueEm"], e["eventId"],
              e["correlationId"], e["recebidoEm"]))

    @traduzir
    def listar_entregas_pendentes(self, pedido_id, limite):
        campos = ("pedidoId", "entregaId", "transportadoraId", "entregueEm", "eventId", "correlationId",
                  "recebidoEm")
        return [_linha(linha, campos) for linha in self._todos("""
            SELECT * FROM pedidos.entrega_pendente WHERE (%(p)s::uuid IS NULL OR pedido_id=%(p)s)
            ORDER BY recebido_em, pedido_id, entrega_id LIMIT %(l)s
        """, {"p": pedido_id, "l": limite})]

    @traduzir
    def remover_entrega_pendente(self, pedido_id, entrega_id):
        self._executar("DELETE FROM pedidos.entrega_pendente WHERE pedido_id=%s AND entrega_id=%s",
                       (pedido_id, entrega_id))

    # ------------------------------------------------------------ idempotência HTTP

    @traduzir
    def obter_idempotencia(self, escopo):
        linha = self._um("SELECT * FROM pedidos.idempotency_keys WHERE escopo=%s FOR UPDATE", (escopo,))
        if linha is None:
            return None
        return {"hash": linha["hash"], "status": linha["status"], "corpo": linha["corpo"],
                "expiraEm": linha["expira_em"]}

    @traduzir
    def salvar_idempotencia(self, escopo, registro):
        self._executar("""
            INSERT INTO pedidos.idempotency_keys (escopo, hash, status, corpo, expira_em) VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (escopo) DO UPDATE SET hash=EXCLUDED.hash, status=EXCLUDED.status, corpo=EXCLUDED.corpo,
              expira_em=EXCLUDED.expira_em
        """, (escopo, registro["hash"], registro["status"], jsonb(registro["corpo"]), registro["expiraEm"]))

    # ------------------------------------------------------------ outbox (usada pelo adaptador Kafka)

    @traduzir
    def gravar_outbox(self, topico, chave, envelope):
        versao = envelope["payload"].get("versao", 1)
        self.conexao_atual().execute("""
            INSERT INTO pedidos.outbox (event_id, topico, chave, envelope, pedido_id, versao)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (envelope["eventId"], topico, chave, jsonb(envelope), chave, versao))

    @traduzir
    def proximo_outbox(self):
        """Registro pendente mais antigo cuja versão anterior do mesmo pedido já foi confirmada."""
        return self._um("""
            SELECT o.event_id, o.topico, o.chave, o.envelope, o.tentativas FROM pedidos.outbox o
            WHERE o.confirmado_em IS NULL AND o.proximo_envio <= now()
              AND NOT EXISTS (SELECT 1 FROM pedidos.outbox a WHERE a.pedido_id = o.pedido_id
                              AND a.versao < o.versao AND a.confirmado_em IS NULL)
            ORDER BY o.criado_em, o.versao LIMIT 1 FOR UPDATE SKIP LOCKED
        """)

    @traduzir
    def confirmar_outbox(self, event_id):
        self._executar("UPDATE pedidos.outbox SET confirmado_em=now() WHERE event_id=%s", (event_id,))

    @traduzir
    def reagendar_outbox(self, event_id, segundos):
        self._executar("""UPDATE pedidos.outbox SET tentativas=tentativas+1,
                          proximo_envio=now() + %s * interval '1 second' WHERE event_id=%s""", (segundos, event_id))

    @traduzir
    def estado_outbox(self):
        linha = self._um("""SELECT count(*) AS n, COALESCE(EXTRACT(EPOCH FROM now() - min(criado_em)), 0) AS idade
                            FROM pedidos.outbox WHERE confirmado_em IS NULL""")
        return linha["n"], float(linha["idade"])
