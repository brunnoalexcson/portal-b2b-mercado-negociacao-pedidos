FROM python:3.12-slim AS deps
WORKDIR /srv/pedidos
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM deps AS test
COPY requirements-test.txt .
RUN pip install --no-cache-dir -r requirements-test.txt
COPY app app
COPY tests tests
COPY alembic.ini .
COPY migracoes migracoes

FROM deps AS runtime
COPY app app
COPY alembic.ini .
COPY migracoes migracoes
USER 65532:65532
EXPOSE 8107
# Com MIGRATION_DATABASE_URL (role portal_ddl), migra antes de subir; sem ela, só sobe (a Infra migra à parte).
CMD ["sh", "-c", "if [ -n \"$MIGRATION_DATABASE_URL\" ]; then python -m alembic upgrade head || exit 1; fi; exec uvicorn app.main:criar_app_real --factory --host 0.0.0.0 --port ${PORT:-8107}"]
