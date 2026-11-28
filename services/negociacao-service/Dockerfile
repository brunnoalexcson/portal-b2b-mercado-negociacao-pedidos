# syntax=docker/dockerfile:1
FROM python:3.12-slim AS deps
WORKDIR /srv/negociacao
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Contratos de docs/negociacao/contratos, fora do contexto deste serviço. Sobrescrito no build com
# --build-context contratos=<repo>/docs/negociacao/contratos (ou additional_contexts no Compose).
FROM scratch AS contratos

FROM deps AS test
COPY requirements-test.txt .
RUN pip install --no-cache-dir -r requirements-test.txt
COPY app app
COPY tests tests
COPY alembic.ini .
COPY migracoes migracoes
COPY --from=contratos / /srv/contratos/
ENV NEGOCIACAO_CONTRATOS_DIR=/srv/contratos
# o build falha se algum teste falhar
RUN python -m pytest -q -p no:cacheprovider
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM deps AS runtime
COPY app app
COPY alembic.ini .
COPY migracoes migracoes
USER 65532:65532
EXPOSE 8106
# Com MIGRATION_DATABASE_URL (role portal_ddl), migra antes de subir; sem ela, só sobe (a migração fica com a Infra).
CMD ["sh", "-c", "if [ -n \"$MIGRATION_DATABASE_URL\" ]; then python -m alembic upgrade head || exit 1; fi; exec uvicorn app.main:criar_app_real --factory --host 0.0.0.0 --port ${PORT:-8106}"]
