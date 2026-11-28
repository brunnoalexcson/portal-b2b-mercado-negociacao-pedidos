import os

from alembic import context
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

url = make_url(os.environ["MIGRATION_DATABASE_URL"]).set(drivername="postgresql+psycopg")
options = {"version_table_schema": "negociacao"}
if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, **options)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url).connect() as connection:
        context.configure(connection=connection, **options)
        with context.begin_transaction():
            # app1 e app2 sobem juntas e as duas migram: a trava serializa, e a segunda já encontra a versão nova
            connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('negociacao-migracao'))"))
            context.run_migrations()
