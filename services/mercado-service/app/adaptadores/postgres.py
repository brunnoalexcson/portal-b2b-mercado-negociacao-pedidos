"""`MercadoRepositorio` real: psycopg 3, pool de até 5 conexões, role `svc_mercado`, schema `mercado` (§5.1)."""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json, Jsonb, set_json_loads
from psycopg_pool import ConnectionPool, PoolTimeout

from app.jsonio import dumps, loads
from app.portas import RepositorioIndisponivel

log = logging.getLogger("app.adaptadores.postgres")

VERSAO_MIGRACAO = "0001"
SQLSTATE_INDISPONIVEL = {"57P01", "57P02", "57P03"}
JSONB = {"origem", "destino"}

PRODUTO = [("produtoId", "produto_id"), ("nome", "nome"), ("unidade", "unidade"), ("atualizadoEm", "atualizado_em")]
OFERTA = [("fornecimentoId", "fornecimento_id"), ("fornecedorId", "fornecedor_id"), ("produtoId", "produto_id"),
          ("unidade", "unidade"), ("preco", "preco"), ("quantidadeDisponivel", "quantidade_disponivel"),
          ("origem", "origem"), ("criadaEm", "criada_em"), ("dadosAtualizadosEm", "dados_atualizados_em"),
          ("estoqueAtualizadoEm", "estoque_atualizado_em")]
DEMANDA = [("demandaId", "demanda_id"), ("demandaOrigemId", "demanda_origem_id"), ("compradorId", "comprador_id"),
           ("produtoId", "produto_id"), ("unidade", "unidade"), ("quantidade", "quantidade"),
           ("precoMaximo", "preco_maximo"), ("destino", "destino"), ("prazoDesejado", "prazo_desejado"),
           ("saldo", "saldo"), ("emNegociacao", "em_negociacao"), ("situacao", "situacao"),
           ("prazoVencido", "prazo_vencido"), ("recebidaEm", "recebida_em"), ("encerradaEm", "encerrada_em")]
MODELO = [("demandaId", "demanda_id"), ("compradorId", "comprador_id"), ("produtoId", "produto_id"),
          ("recebidoEm", "recebido_em"), ("cancelado", "cancelado")]
CANCELAMENTO = [("demandaId", "demanda_id"), ("compradorId", "comprador_id"), ("canceladoEm", "cancelado_em"),
                ("recebidoEm", "recebido_em")]
SUGESTAO = [("sugestaoId", "sugestao_id"), ("produtoId", "produto_id"), ("situacao", "situacao"),
            ("excessoDemanda", "excesso_demanda"), ("janelaEncerraEm", "janela_encerra_em"),
            ("negociacaoId", "negociacao_id"), ("correlationId", "correlation_id"), ("abertaEm", "aberta_em"),
            ("encerradaEm", "encerrada_em")]
ACEITE = [("sugestaoId", "sugestao_id"), ("fornecimentoId", "fornecimento_id"), ("fornecedorId", "fornecedor_id"),
          ("quantidade", "quantidade"), ("aceitoEm", "aceito_em")]
NEGOCIACAO = [("negociacaoId", "negociacao_id"), ("modo", "modo"), ("produtoId", "produto_id"),
              ("unidade", "unidade"), ("situacao", "situacao"), ("demandaId", "demanda_id"),
              ("compradorId", "comprador_id"), ("compraImediata", "compra_imediata"),
              ("quantidadeTotal", "quantidade_total"), ("valorReferencia", "valor_referencia"),
              ("precoInicial", "preco_inicial"), ("incrementoMinimo", "incremento_minimo"),
              ("sugestaoId", "sugestao_id"), ("iniciaEm", "inicia_em"), ("encerraEm", "encerra_em"),
              ("definidaEm", "definida_em"), ("encerradaEm", "encerrada_em"), ("correlationId", "correlation_id")]
ITEM = [("fornecimentoId", "fornecimento_id"), ("fornecedorId", "fornecedor_id"), ("quantidade", "quantidade"),
        ("precoUnitario", "preco_unitario"), ("valorItem", "valor_item"), ("participacao", "participacao"),
        ("origem", "origem")]


def indisponivel(exc: BaseException) -> bool:
    """Falhas de conexão (§5.1): SQLSTATE da classe 08, 57P01 a 57P03, `PoolTimeout` e erro operacional sem
    SQLSTATE (conexão perdida no meio do comando)."""
    if isinstance(exc, PoolTimeout):
        return True
    if isinstance(exc, psycopg.Error):
        estado = exc.sqlstate
        if estado is None:
            return isinstance(exc, psycopg.OperationalError)
        return estado.startswith("08") or estado in SQLSTATE_INDISPONIVEL
    return False


def _valor(coluna, valor):
    if coluna in JSONB and valor is not None:
        return Jsonb(valor, dumps=dumps)
    return valor


def _dict(linha, campos) -> dict:
    dados = {}
    for camel, coluna in campos:
        valor = linha[coluna]
        dados[camel] = str(valor) if isinstance(valor, UUID) else valor
    return dados


def _configurar(conexao):
    set_json_loads(loads, conexao)


class RepositorioPostgres:
    def __init__(self, dsn: str):
        self.pool = ConnectionPool(
            dsn, open=False, min_size=1, max_size=5, timeout=2, name="mercado", configure=_configurar,
            kwargs={"row_factory": dict_row, "connect_timeout": 3, "options": "-c TimeZone=UTC"},
        )
        self._conexao: ContextVar = ContextVar("mercado_conexao", default=None)

    # ------------------------------------------------------------ transação e saúde

    def conexao_atual(self):
        return self._conexao.get()

    @contextmanager
    def transacao(self):
        if self._conexao.get() is not None:
            raise RuntimeError("Transação aninhada")
        try:
            with self.pool.connection() as conexao:
                marca = self._conexao.set(conexao)
                try:
                    with conexao.transaction():
                        yield
                finally:
                    self._conexao.reset(marca)
        except (psycopg.Error, PoolTimeout) as exc:
            if indisponivel(exc):
                raise RepositorioIndisponivel(str(exc)) from exc
            raise

    def disponivel(self) -> bool:
        try:
            with self.pool.connection(timeout=2) as conexao:
                linha = conexao.execute("SELECT version_num FROM mercado.alembic_version").fetchone()
            return bool(linha and linha["version_num"] == VERSAO_MIGRACAO)
        except Exception:
            return False

    def _sql(self, comando, parametros=()):
        conexao = self._conexao.get()
        if conexao is None:
            raise RuntimeError("Repositório usado fora de transação")
        try:
            return conexao.execute(comando, parametros)
        except (psycopg.Error, PoolTimeout) as exc:
            if indisponivel(exc):
                raise RepositorioIndisponivel(str(exc)) from exc
            raise

    def _um(self, comando, parametros, campos):
        linha = self._sql(comando, parametros).fetchone()
        return None if linha is None else _dict(linha, campos)

    def _varios(self, comando, parametros, campos):
        return [_dict(linha, campos) for linha in self._sql(comando, parametros).fetchall()]

    def _upsert(self, tabela, chave, campos, dados):
        colunas = [coluna for _, coluna in campos]
        atualizar = ", ".join(f"{c}=EXCLUDED.{c}" for c in colunas if c not in chave)
        self._sql(
            f"INSERT INTO mercado.{tabela} ({', '.join(colunas)}) VALUES ({', '.join(['%s'] * len(colunas))}) "
            f"ON CONFLICT ({', '.join(chave)}) DO UPDATE SET {atualizar}",
            [_valor(coluna, dados[camel]) for camel, coluna in campos],
        )

    # ------------------------------------------------------------ consumo idempotente

    def evento_processado(self, source, event_id):
        return self._sql("SELECT 1 FROM mercado.processed_events WHERE source=%s AND event_id=%s",
                         (source, event_id)).fetchone() is not None

    def registrar_evento_processado(self, source, event_id, topico):
        self._sql("INSERT INTO mercado.processed_events (source, event_id, topico) VALUES (%s, %s, %s)",
                  (source, event_id, topico))

    # ------------------------------------------------------------ cópias locais

    def obter_produto(self, produto_id):
        return self._um("SELECT * FROM mercado.produto WHERE produto_id=%s", (produto_id,), PRODUTO)

    def salvar_produto(self, produto):
        self._upsert("produto", ("produto_id",), PRODUTO, produto)

    def obter_oferta(self, fornecimento_id):
        return self._um("SELECT * FROM mercado.oferta WHERE fornecimento_id=%s", (fornecimento_id,), OFERTA)

    def salvar_oferta(self, oferta):
        self._upsert("oferta", ("fornecimento_id",), OFERTA, oferta)

    def listar_ofertas_do_produto(self, produto_id):
        return self._varios("SELECT * FROM mercado.oferta WHERE produto_id=%s ORDER BY fornecimento_id",
                            (produto_id,), OFERTA)

    def obter_demanda(self, demanda_id, bloquear=False):
        sufixo = " FOR UPDATE" if bloquear else ""
        return self._um(f"SELECT * FROM mercado.demanda WHERE demanda_id=%s{sufixo}", (demanda_id,), DEMANDA)

    def salvar_demanda(self, demanda):
        self._upsert("demanda", ("demanda_id",), DEMANDA, demanda)

    def obter_modelo(self, demanda_id):
        return self._um("SELECT * FROM mercado.demanda_modelo WHERE demanda_id=%s", (demanda_id,), MODELO)

    def salvar_modelo(self, modelo):
        self._upsert("demanda_modelo", ("demanda_id",), MODELO, modelo)

    def obter_cancelamento_antecipado(self, demanda_id):
        return self._um("SELECT * FROM mercado.cancelamento_antecipado WHERE demanda_id=%s", (demanda_id,),
                        CANCELAMENTO)

    def salvar_cancelamento_antecipado(self, cancelamento):
        self._upsert("cancelamento_antecipado", ("demanda_id",), CANCELAMENTO, cancelamento)

    def listar_demandas_abertas(self, produto_id):
        return self._varios("SELECT * FROM mercado.demanda WHERE produto_id=%s AND situacao='AGUARDANDO' "
                            "ORDER BY recebida_em, demanda_id", (produto_id,), DEMANDA)

    def listar_demandas_por_origem(self, demanda_origem_id):
        return self._varios("SELECT * FROM mercado.demanda WHERE demanda_origem_id=%s ORDER BY demanda_id",
                            (demanda_origem_id,), DEMANDA)

    def listar_demandas_vencidas(self, agora, limite):
        linhas = self._sql("SELECT demanda_id FROM mercado.demanda WHERE situacao='AGUARDANDO' AND NOT prazo_vencido "
                           "AND prazo_desejado IS NOT NULL AND prazo_desejado < %s "
                           "ORDER BY prazo_desejado, demanda_id LIMIT %s", (agora, limite)).fetchall()
        return [linha["demanda_id"] for linha in linhas]

    # ------------------------------------------------------------ produto como unidade de concorrência

    def bloquear_produto(self, produto_id):
        self._sql("INSERT INTO mercado.produto_lock (produto_id) VALUES (%s) ON CONFLICT DO NOTHING", (produto_id,))
        self._sql("SELECT produto_id FROM mercado.produto_lock WHERE produto_id=%s FOR UPDATE", (produto_id,))

    def bloquear_id_demanda(self, demanda_id):
        # Serializa criação e cancelamento do mesmo ID de demanda ou modelo entre as réplicas (RN-08, RN-09).
        self._sql("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("mercado.demanda:" + demanda_id,))

    def obter_situacao_balanco(self, produto_id):
        linha = self._sql("SELECT situacao, situacao_desde FROM mercado.produto_lock WHERE produto_id=%s",
                          (produto_id,)).fetchone()
        if linha is None or linha["situacao"] is None:
            return None
        return {"situacao": linha["situacao"], "situacaoDesde": linha["situacao_desde"]}

    def salvar_situacao_balanco(self, produto_id, situacao, situacao_desde):
        self._sql("INSERT INTO mercado.produto_lock (produto_id, situacao, situacao_desde) VALUES (%s, %s, %s) "
                  "ON CONFLICT (produto_id) DO UPDATE SET situacao=EXCLUDED.situacao, "
                  "situacao_desde=EXCLUDED.situacao_desde", (produto_id, situacao, situacao_desde))

    # ------------------------------------------------------------ sugestões e aceites

    def obter_sugestao(self, sugestao_id, bloquear=False):
        sufixo = " FOR UPDATE" if bloquear else ""
        return self._um(f"SELECT * FROM mercado.sugestao_leilao WHERE sugestao_id=%s{sufixo}", (sugestao_id,),
                        SUGESTAO)

    def obter_sugestao_ativa(self, produto_id):
        return self._um("SELECT * FROM mercado.sugestao_leilao WHERE produto_id=%s "
                        "AND situacao IN ('ABERTA', 'EM_FORMACAO')", (produto_id,), SUGESTAO)

    def salvar_sugestao(self, sugestao):
        self._upsert("sugestao_leilao", ("sugestao_id",), SUGESTAO, sugestao)

    def listar_sugestoes_ativas(self):
        return self._varios("SELECT * FROM mercado.sugestao_leilao WHERE situacao IN ('ABERTA', 'EM_FORMACAO') "
                            "ORDER BY aberta_em, sugestao_id", (), SUGESTAO)

    def listar_janelas_vencidas(self, agora, limite):
        linhas = self._sql("SELECT sugestao_id FROM mercado.sugestao_leilao WHERE situacao='EM_FORMACAO' "
                           "AND janela_encerra_em <= %s ORDER BY janela_encerra_em, sugestao_id LIMIT %s",
                           (agora, limite)).fetchall()
        return [str(linha["sugestao_id"]) for linha in linhas]

    def listar_aceites(self, sugestao_id):
        return self._varios("SELECT * FROM mercado.aceite_leilao WHERE sugestao_id=%s "
                            "ORDER BY aceito_em, fornecimento_id", (sugestao_id,), ACEITE)

    def salvar_aceite(self, aceite):
        self._upsert("aceite_leilao", ("sugestao_id", "fornecimento_id"), ACEITE, aceite)

    def remover_aceite(self, sugestao_id, fornecimento_id):
        self._sql("DELETE FROM mercado.aceite_leilao WHERE sugestao_id=%s AND fornecimento_id=%s",
                  (sugestao_id, fornecimento_id))

    # ------------------------------------------------------------ negociações

    def _com_itens(self, negociacoes: list[dict]) -> list[dict]:
        if not negociacoes:
            return negociacoes
        linhas = self._sql("SELECT * FROM mercado.negociacao_item WHERE negociacao_id = ANY(%s::uuid[]) "
                           "ORDER BY negociacao_id, ordem", ([n["negociacaoId"] for n in negociacoes],)).fetchall()
        itens: dict[str, list] = {}
        for linha in linhas:
            itens.setdefault(str(linha["negociacao_id"]), []).append(_dict(linha, ITEM))
        for negociacao in negociacoes:
            negociacao["itens"] = itens.get(negociacao["negociacaoId"], [])
        return negociacoes

    def inserir_negociacao(self, negociacao):
        colunas = [coluna for _, coluna in NEGOCIACAO]
        self._sql(f"INSERT INTO mercado.negociacao ({', '.join(colunas)}) "
                  f"VALUES ({', '.join(['%s'] * len(colunas))})",
                  [negociacao[camel] for camel, _ in NEGOCIACAO])
        for ordem, item in enumerate(negociacao["itens"]):
            self._sql("INSERT INTO mercado.negociacao_item (negociacao_id, ordem, fornecimento_id, fornecedor_id, "
                      "quantidade, preco_unitario, valor_item, participacao, origem) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                      (negociacao["negociacaoId"], ordem, item["fornecimentoId"], item["fornecedorId"],
                       item["quantidade"], item["precoUnitario"], item["valorItem"], item["participacao"],
                       _valor("origem", item["origem"])))

    def obter_negociacao(self, negociacao_id, bloquear=False):
        sufixo = " FOR UPDATE" if bloquear else ""
        negociacao = self._um(f"SELECT * FROM mercado.negociacao WHERE negociacao_id=%s{sufixo}", (negociacao_id,),
                              NEGOCIACAO)
        return None if negociacao is None else self._com_itens([negociacao])[0]

    def atualizar_negociacao(self, negociacao):
        alterar = [(camel, coluna) for camel, coluna in NEGOCIACAO if coluna != "negociacao_id"]
        self._sql(f"UPDATE mercado.negociacao SET {', '.join(f'{c}=%s' for _, c in alterar)} "
                  "WHERE negociacao_id=%s", [negociacao[camel] for camel, _ in alterar] + [negociacao["negociacaoId"]])

    _VISIVEL = ("(n.modo='LEILAO_DIRETO' OR (%(escopo)s='BUYER' AND n.comprador_id=%(empresa)s) "
                "OR (%(escopo)s='SUPPLIER' AND n.modo='VENDA_DIRETA' AND EXISTS (SELECT 1 FROM mercado.negociacao_item i "
                "WHERE i.negociacao_id=n.negociacao_id AND i.fornecedor_id=%(empresa)s))) "
                "AND (%(modo)s::text IS NULL OR n.modo=%(modo)s) AND (%(situacao)s::text IS NULL OR n.situacao=%(situacao)s)")

    def listar_negociacoes(self, empresa, escopo, modo, situacao, offset, limite):
        parametros = {"empresa": empresa, "escopo": escopo, "modo": modo, "situacao": situacao,
                      "offset": offset, "limite": limite}
        total = self._sql(f"SELECT count(*) AS total FROM mercado.negociacao n WHERE {self._VISIVEL}",
                          parametros).fetchone()["total"]
        itens = self._varios(f"SELECT n.* FROM mercado.negociacao n WHERE {self._VISIVEL} "
                             "ORDER BY n.definida_em DESC, n.negociacao_id DESC LIMIT %(limite)s OFFSET %(offset)s",
                             parametros, NEGOCIACAO)
        return self._com_itens(itens), total

    def obter_direta_aberta(self, demanda_id, fornecimento_id):
        negociacao = self._um("SELECT n.* FROM mercado.negociacao n JOIN mercado.negociacao_item i "
                              "ON i.negociacao_id=n.negociacao_id WHERE n.modo='VENDA_DIRETA' AND n.situacao='ABERTA' "
                              "AND n.demanda_id=%s AND i.fornecimento_id=%s LIMIT 1",
                              (demanda_id, fornecimento_id), NEGOCIACAO)
        return None if negociacao is None else self._com_itens([negociacao])[0]

    def listar_leiloes_abertos_do_fornecedor(self, produto_id, fornecedor_id):
        return self._com_itens(self._varios(
            "SELECT n.* FROM mercado.negociacao n WHERE n.modo='LEILAO_DIRETO' AND n.situacao='ABERTA' "
            "AND n.produto_id=%s AND EXISTS (SELECT 1 FROM mercado.negociacao_item i "
            "WHERE i.negociacao_id=n.negociacao_id AND i.fornecedor_id=%s) ORDER BY n.definida_em",
            (produto_id, fornecedor_id), NEGOCIACAO))

    # ------------------------------------------------------------ idempotência HTTP

    def obter_idempotencia(self, escopo):
        # Serializa requisições com a mesma chave entre as réplicas até o fim da transação.
        self._sql("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (escopo,))
        linha = self._sql("SELECT * FROM mercado.idempotency_keys WHERE escopo=%s", (escopo,)).fetchone()
        if linha is None:
            return None
        return {"hashCorpo": linha["hash_corpo"], "status": linha["status"], "corpo": linha["corpo"],
                "location": linha["location"], "criadoEm": linha["criado_em"]}

    def salvar_idempotencia(self, escopo, registro):
        self._sql("INSERT INTO mercado.idempotency_keys (escopo, hash_corpo, status, corpo, location, criado_em) "
                  "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (escopo) DO UPDATE SET hash_corpo=EXCLUDED.hash_corpo, "
                  "status=EXCLUDED.status, corpo=EXCLUDED.corpo, location=EXCLUDED.location, "
                  "criado_em=EXCLUDED.criado_em",
                  (escopo, registro["hashCorpo"], registro["status"], Json(registro["corpo"], dumps=dumps),
                   registro["location"], registro["criadoEm"]))

    # ------------------------------------------------------------ outbox (usado pelo adaptador Kafka)

    def gravar_outbox(self, topico: str, chave: str, mensagem: dict):
        self._sql("INSERT INTO mercado.outbox (event_id, topico, chave, envelope) VALUES (%s, %s, %s, %s)",
                  (mensagem["eventId"], topico, chave, Json(mensagem, dumps=dumps)))
