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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8107"]
