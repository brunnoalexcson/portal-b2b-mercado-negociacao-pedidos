FROM python:3.12-slim AS deps
WORKDIR /srv/negociacao
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
CMD ["python", "-m", "pytest", "-q"]

FROM deps AS runtime
COPY app app
COPY alembic.ini .
COPY migracoes migracoes
USER 65532:65532
EXPOSE 8106
CMD ["sh", "-c", "exec uvicorn app.main:criar_app_real --factory --host 0.0.0.0 --port ${APP_PORT:-8106}"]
