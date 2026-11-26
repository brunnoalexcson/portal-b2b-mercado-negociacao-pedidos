import os
import time

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

url = make_url(os.environ["MIGRATION_DATABASE_URL"]).set(drivername="postgresql+psycopg")
options = {"version_table_schema": "mercado"}

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, **options)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url)
    for tentativa in range(30):
        try:
            connection = engine.connect()
            break
        except OperationalError:
            if tentativa == 29:
                raise
            time.sleep(2)
    with connection:
        context.configure(connection=connection, **options)
        with context.begin_transaction():
            context.run_migrations()
