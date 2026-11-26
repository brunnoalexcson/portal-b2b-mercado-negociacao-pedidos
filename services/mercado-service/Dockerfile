# syntax=docker/dockerfile:1
FROM python:3.12-slim AS deps
WORKDIR /srv/mercado
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Contratos de docs/mercado/contratos, fora do contexto deste serviço. Sobrescrito no build com
# --build-context contratos=<repo>/docs/mercado/contratos (ou additional_contexts no Compose).
FROM scratch AS contratos

FROM deps AS test
COPY requirements-test.txt .
RUN pip install --no-cache-dir -r requirements-test.txt
COPY app app
COPY tests tests
COPY alembic.ini .
COPY migracoes migracoes
COPY --from=contratos / /srv/contratos/
ENV MERCADO_CONTRATOS_DIR=/srv/contratos
RUN python -m pytest -q -p no:cacheprovider

FROM deps AS runtime
COPY app app
COPY alembic.ini .
COPY migracoes migracoes
USER 65532:65532
EXPOSE 8105
CMD ["uvicorn", "app.main:criar_app_real", "--factory", "--host", "0.0.0.0", "--port", "8105"]
