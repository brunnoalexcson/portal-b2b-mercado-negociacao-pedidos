import re
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

from psycopg import Error as ErroPostgres
from psycopg import OperationalError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb, set_json_loads
from psycopg_pool import ConnectionPool, PoolTimeout

from app.jsonio import dumps, loads
from app.portas import RepositorioIndisponivel


def configurar(conexao):
    # JSONB lido com Decimal, para o valor monetário não virar float
    set_json_loads(loads, conexao)


class Postgres:
    def __init__(self, dsn):
        self.pool = ConnectionPool(
            dsn, min_size=1, max_size=5, timeout=2, open=True, name="negociacao", configure=configurar,
            kwargs={"row_factory": dict_row, "connect_timeout": 3},
        )
        self._conexao = ContextVar("conexao_negociacao", default=None)

    @contextmanager
    def transacao(self):
        try:
            # o pool faz commit ao sair sem exceção e rollback com exceção
            with self.pool.connection() as conn:
                marca = self._conexao.set(conn)
                try:
                    yield
                finally:
                    self._conexao.reset(marca)
        except (OperationalError, PoolTimeout) as exc:
            raise RepositorioIndisponivel() from exc

    def conexao(self):
        conn = self._conexao.get()
        if conn is None:
            raise RuntimeError("Uso do banco fora de repositorio.transacao()")
        return conn

    def disponivel(self):
        try:
            with self.pool.connection() as conn:
                return conn.execute("SELECT 1 FROM negociacao.alembic_version").fetchone() is not None
        except (ErroPostgres, PoolTimeout):
            return False

    def fechar(self):
        self.pool.close()

    # consumo idempotente

    def evento_processado(self, source, event_id):
        return self.conexao().execute(
            "SELECT 1 FROM negociacao.processed_events WHERE source = %s AND event_id = %s", (source, event_id),
        ).fetchone() is not None

    def registrar_evento_processado(self, source, event_id, topico):
        self.conexao().execute(
            "INSERT INTO negociacao.processed_events (source, event_id, topico) VALUES (%s, %s, %s)",
            (source, event_id, topico),
        )

    # negociações

    def inserir_negociacao(self, negociacao):
        self._inserir("processo_negociacao", NEGOCIACAO, negociacao)

    def obter_negociacao(self, negociacao_id, bloquear=False):
        linha = self.conexao().execute(
            "SELECT * FROM negociacao.processo_negociacao WHERE id = %s" + (" FOR UPDATE" if bloquear else ""),
            (negociacao_id,),
        ).fetchone()
        return None if linha is None else _campos(NEGOCIACAO, linha)

    def atualizar_negociacao(self, negociacao):
        colunas = [coluna for campo, coluna in NEGOCIACAO.items() if campo != "negociacaoId"]
        self.conexao().execute(
            f"UPDATE negociacao.processo_negociacao SET {', '.join(f'{c} = %s' for c in colunas)} WHERE id = %s",
            [negociacao[campo] for campo in NEGOCIACAO if campo != "negociacaoId"] + [negociacao["negociacaoId"]],
        )

    def listar_negociacoes(self, empresa, escopo, modo, status, offset, limite):
        filtro = """
            FROM negociacao.processo_negociacao
            WHERE (modo = 'LEILAO_DIRETO' OR (%(escopo)s::text = 'BUYER' AND comprador_id = %(empresa)s)
                   OR (%(escopo)s::text = 'SUPPLIER' AND fornecedor_id = %(empresa)s))
              AND (%(modo)s::text IS NULL OR modo = %(modo)s)
              AND (%(status)s::text IS NULL OR status = %(status)s)
        """
        parametros = {"empresa": empresa, "escopo": escopo, "modo": modo, "status": status,
                      "offset": offset, "limite": limite}
        conn = self.conexao()
        linhas = conn.execute(f"SELECT * {filtro} ORDER BY criado_em DESC, id DESC OFFSET %(offset)s LIMIT %(limite)s",
                              parametros).fetchall()
        total = conn.execute(f"SELECT count(*) AS total {filtro}", parametros).fetchone()["total"]
        return [_campos(NEGOCIACAO, linha) for linha in linhas], total

    def listar_vencidas(self, agora, limite):
        linhas = self.conexao().execute("""
            SELECT id FROM negociacao.processo_negociacao
            WHERE (modo = 'LEILAO_DIRETO' AND status IN ('AGUARDANDO_RESERVAS', 'ABERTO') AND data_fim <= %(agora)s)
               OR (modo = 'VENDA_DIRETA' AND status = 'ABERTO' AND expira_em <= %(agora)s)
            ORDER BY coalesce(data_fim, expira_em), id
            LIMIT %(limite)s
        """, {"agora": agora, "limite": limite}).fetchall()
        return [str(linha["id"]) for linha in linhas]

    # ofertas do leilão

    def inserir_oferta_leilao(self, oferta):
        self._inserir("oferta_leilao", OFERTA_LEILAO, oferta)

    def listar_ofertas_leilao(self, negociacao_id):
        linhas = self.conexao().execute(
            "SELECT * FROM negociacao.oferta_leilao WHERE negociacao_id = %s ORDER BY ordem", (negociacao_id,),
        ).fetchall()
        return [_campos(OFERTA_LEILAO, linha) for linha in linhas]

    def atualizar_oferta_leilao(self, oferta):
        chave = ("negociacaoId", "fornecimentoId")
        campos = [campo for campo in OFERTA_LEILAO if campo not in chave]
        self.conexao().execute(
            f"UPDATE negociacao.oferta_leilao SET {', '.join(f'{OFERTA_LEILAO[c]} = %s' for c in campos)} "
            "WHERE negociacao_id = %s AND fornecimento_id = %s",
            [oferta[campo] for campo in campos] + [oferta[campo] for campo in chave],
        )

    # propostas

    def inserir_proposta(self, proposta):
        self._inserir("proposta", PROPOSTA, proposta)

    def listar_propostas(self, negociacao_id, offset, limite):
        conn = self.conexao()
        linhas = conn.execute(
            "SELECT * FROM negociacao.proposta WHERE negociacao_id = %s ORDER BY sequencia OFFSET %s LIMIT %s",
            (negociacao_id, offset, limite),
        ).fetchall()
        total = conn.execute("SELECT count(*) AS total FROM negociacao.proposta WHERE negociacao_id = %s",
                             (negociacao_id,)).fetchone()["total"]
        return [_campos(PROPOSTA, linha) for linha in linhas], total

    # idempotência HTTP

    def obter_idempotencia(self, escopo):
        linha = self.conexao().execute(
            "SELECT * FROM negociacao.idempotency_keys WHERE escopo = %s", (escopo,),
        ).fetchone()
        return None if linha is None else _campos(IDEMPOTENCIA, linha)

    def salvar_idempotencia(self, escopo, registro):
        self.conexao().execute(
            "INSERT INTO negociacao.idempotency_keys (escopo, hash_corpo, status, corpo, location, criado_em) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (escopo) DO UPDATE SET hash_corpo = EXCLUDED.hash_corpo, "
            "status = EXCLUDED.status, corpo = EXCLUDED.corpo, location = EXCLUDED.location, "
            "criado_em = EXCLUDED.criado_em",
            (escopo, registro["hashCorpo"], registro["status"], Jsonb(registro["corpo"], dumps=dumps),
             registro["location"], registro["criadoEm"]),
        )

    def _inserir(self, tabela, colunas, registro):
        self.conexao().execute(
            f"INSERT INTO negociacao.{tabela} ({', '.join(colunas.values())}) "
            f"VALUES ({', '.join(['%s'] * len(colunas))})",
            [registro[campo] for campo in colunas],
        )


def _colunas(campos, especiais):
    """Atributo do §6.1 (camelCase) → coluna (snake_case)."""
    return {campo: especiais.get(campo) or re.sub(r"([A-Z])", r"_\1", campo).lower() for campo in campos.split()}


NEGOCIACAO = _colunas("""
    negociacaoId modo status produtoId dataInicio dataFim quantidadeTotal precoInicial incrementoMinimo
    incrementoMinimoPercentual melhorLanceId totalLances compradorId fornecimentoId fornecedorId quantidade
    precoOferta precoAtual vez compraImediata expiraEm reservaSituacao pedidoId totalPropostas correlationId
    criadoEm encerradoEm
""", {"negociacaoId": "id", "precoInicial": "valor_reserva"})
PROPOSTA = _colunas("propostaId negociacaoId autor empresaId precoUnitario sequencia criadoEm",
                    {"propostaId": "id"})
IDEMPOTENCIA = _colunas("hashCorpo status corpo location criadoEm", {})

OFERTA_LEILAO = _colunas("""
    negociacaoId fornecimentoId fornecedorId quantidade precoUnitario valorItem participacao ordem situacao pedidoId
""", {})


def _campos(colunas, linha):
    # colunas uuid voltam como UUID; os IDs circulam como texto
    return {campo: str(linha[coluna]) if isinstance(linha[coluna], UUID) else linha[coluna]
            for campo, coluna in colunas.items()}
