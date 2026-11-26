from contextlib import contextmanager
from contextvars import ContextVar

from psycopg import Error as ErroPostgres
from psycopg import OperationalError
from psycopg.rows import dict_row
from psycopg.types.json import set_json_loads
from psycopg_pool import ConnectionPool, PoolTimeout

from app.jsonio import loads
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
