import os

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

url = make_url(os.environ["MIGRATION_DATABASE_URL"]).set(drivername="postgresql+psycopg")
options = {"version_table_schema": "pedidos"}
if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, **options)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url).connect() as connection:
        context.configure(connection=connection, **options)
        with context.begin_transaction():
            context.run_migrations()
