import logging
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.portas import RepositorioIndisponivel

DOZE = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
CHAVES_DO_ERRO = {"type", "title", "status", "codigo", "detail", "instance", "correlationId", "timestamp"}


class Relogio:
    def __init__(self, agora):
        self.agora = agora

    def now(self):
        return self.agora

    def sleep(self, segundos):
        """Os testes não esperam."""


class Dependencia:
    """Repositório ou barramento: só a prontidão importa nestes testes."""

    def __init__(self, disponivel=True):
        self.estado = disponivel

    def disponivel(self):
        if isinstance(self.estado, Exception):
            raise self.estado
        return self.estado


def cliente(repositorio=True, barramento=True, agora=DOZE):
    app = create_app(Dependencia(barramento), Relogio(agora), Dependencia(repositorio), chaves_publicas=None)
    return TestClient(app, raise_server_exceptions=False)


def test_health_responde_sem_token_com_o_relogio():
    resposta = cliente().get("/health")

    assert resposta.status_code == 200
    assert resposta.json() == {"status": "UP", "service": "negociacao-service",
                               "timestamp": "2026-11-20T12:00:00.000Z"}


def test_relogio_sem_fuso_vale_como_utc():
    resposta = cliente(agora=datetime(2026, 11, 20, 12)).get("/health")

    assert resposta.json()["timestamp"] == "2026-11-20T12:00:00.000Z"


def test_prontidao_com_repositorio_e_barramento_disponiveis():
    resposta = cliente().get("/health/ready")

    assert resposta.status_code == 200
    assert resposta.json() == {"status": "READY", "service": "negociacao-service"}


@pytest.mark.parametrize("repositorio, barramento", [
    (False, True), (True, False), (RepositorioIndisponivel(), True),
])
def test_prontidao_responde_503_se_uma_dependencia_falha(repositorio, barramento):
    resposta = cliente(repositorio, barramento).get("/health/ready", headers={"X-Correlation-Id": "teste-neg-50"})

    assert resposta.status_code == 503
    assert resposta.headers["content-type"] == "application/problem+json"
    assert resposta.headers["x-correlation-id"] == "teste-neg-50"
    corpo = resposta.json()
    assert set(corpo) == CHAVES_DO_ERRO
    assert corpo["codigo"] == "DEPENDENCIA_INDISPONIVEL"
    assert (corpo["status"], corpo["title"]) == (503, "Serviço indisponível")
    assert corpo["detail"] == "Uma dependência necessária está indisponível."
    assert (corpo["instance"], corpo["correlationId"]) == ("/health/ready", "teste-neg-50")
    assert corpo["timestamp"] == "2026-11-20T12:00:00.000Z"


def test_falha_inesperada_vira_500_e_entra_nas_metricas():
    http = cliente(repositorio=RuntimeError("bug"))

    resposta = http.get("/health/ready")

    assert resposta.status_code == 500
    assert resposta.headers["content-type"] == "application/problem+json"
    assert resposta.json()["codigo"] == "ERRO_INTERNO"
    assert 'http_requests_total{metodo="GET",rota="/health/ready",status="500"} 1.0' in http.get("/metrics").text


@pytest.mark.parametrize("recebido", [None, "", "com espaco", "x" * 129])
def test_correlacao_ausente_ou_fora_do_formato_gera_uuid(recebido):
    headers = {} if recebido is None else {"X-Correlation-Id": recebido}

    resposta = cliente().get("/health", headers=headers)

    assert UUID(resposta.headers["x-correlation-id"]).version == 4


def test_metricas_usam_o_modelo_da_rota():
    http = cliente()
    http.get("/health")
    http.get("/nao-existe/31000000-0000-4000-8000-000000000001")

    resposta = http.get("/metrics")

    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'http_requests_total{metodo="GET",rota="/health",status="200"} 1.0' in resposta.text
    assert 'http_requests_total{metodo="GET",rota="(sem rota)",status="404"} 1.0' in resposta.text
    assert 'http_request_duration_seconds_count{metodo="GET",rota="/health"} 1.0' in resposta.text
    assert "31000000" not in resposta.text


def test_log_de_saude_sai_em_debug_e_o_resto_em_info(caplog):
    caplog.set_level(logging.DEBUG, logger="app.http")
    http = cliente()

    http.get("/health")
    http.get("/health/ready")
    http.get("/nao-existe")

    niveis = {registro.getMessage(): registro.levelno for registro in caplog.records}
    assert niveis == {"GET /health 200": logging.DEBUG, "GET /health/ready 200": logging.DEBUG,
                      "GET /nao-existe 404": logging.INFO}


def test_duas_aplicacoes_nao_somam_metricas():
    cliente().get("/health")

    assert 'rota="/health"' not in cliente().get("/metrics").text
