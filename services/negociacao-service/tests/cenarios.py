"""Massa de teste do §8.0, leitura dos contratos e helpers de reserva e autenticação."""
import copy
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import jwt
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from app.http import ERROS
from app.jsonio import dumps, formato_json, loads
from app.main import create_app
from tests.dubles import CHAVE_PRIVADA, KID, Barramento, Chaves, Relogio, Repositorio

D = Decimal
AUSENTE = object()

FORN_A = "a1000000-0000-4000-8000-000000000001"
FORN_B = "a1000000-0000-4000-8000-000000000002"
FORN_C = "a1000000-0000-4000-8000-000000000003"
COMP_X = "b2000000-0000-4000-8000-000000000001"
COMP_Y = "b2000000-0000-4000-8000-000000000002"

P = "35000000-0000-4000-8000-000000000001"

OF_A = "34000000-0000-4000-8000-000000000001"
OF_B = "34000000-0000-4000-8000-000000000002"
OF_D = "34000000-0000-4000-8000-000000000006"
OF_E = "34000000-0000-4000-8000-000000000005"

PT_A = "32000000-0000-4000-8000-000000000001"
PT_B = "32000000-0000-4000-8000-000000000002"
PT_D = "32000000-0000-4000-8000-000000000003"

DEM_X = "36000000-0000-4000-8000-000000000001"
DEM_Y = "36000000-0000-4000-8000-000000000002"

L1 = "31000000-0000-4000-8000-000000000001"
D1 = "31000000-0000-4000-8000-000000000002"

CORR_L1 = "teste-neg-01"
CORR_D1 = "teste-neg-02"
SUB_PADRAO = "c3000000-0000-4000-8000-000000000001"
DOZE = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

RAIZ = Path(__file__).resolve().parents[1]
CONTRATOS = Path(os.environ.get("NEGOCIACAO_CONTRATOS_DIR", RAIZ.parents[1] / "docs" / "negociacao" / "contratos"))


def t(hora: str) -> datetime:
    """Converte `"12:00:00"`, `"12:00"` (dia 2026-11-20 UTC) ou um instante ISO completo para `datetime` UTC."""
    if "T" in hora:
        return datetime.fromisoformat(hora).astimezone(timezone.utc)
    if len(hora) == 5:
        hora += ":00"
    return datetime.fromisoformat(f"2026-11-20T{hora}+00:00")


def ler_contrato(nome_arquivo: str) -> dict:
    return loads((CONTRATOS / nome_arquivo).read_text(encoding="utf-8"))


def exemplo(topico: str) -> dict:
    return ler_contrato(f"{topico}.exemplo.json")


def schema(topico: str) -> dict:
    return json.loads((CONTRATOS / f"{topico}.schema.json").read_text(encoding="utf-8"))


def evento_d1(**alteracoes_payload) -> dict:
    """Envelope de `modo_negociacao_definido` da direta D1 lido do exemplo oficial."""
    envelope = copy.deepcopy(exemplo("modo_negociacao_definido"))
    envelope["payload"].update(alteracoes_payload)
    return envelope


def _formatos() -> FormatChecker:
    formatos = FormatChecker()

    @formatos.checks("date-time", raises=ValueError)
    def _instante(valor):
        if not isinstance(valor, str):
            return True
        if "T" not in valor.upper():
            return False
        lido = datetime.fromisoformat(valor)
        return lido.utcoffset() is not None

    return formatos


def validar_schema(topico: str, mensagem: dict) -> None:
    instancia = json.loads(dumps(mensagem))
    Draft202012Validator(schema(topico), format_checker=_formatos()).validate(instancia)


def token(escopo: str | None, empresa: str | None = COMP_X, *, agora: datetime = DOZE, chave=CHAVE_PRIVADA,
          algoritmo: str = "RS256", kid: str | None = KID, cabecalho: dict | None = None, **claims) -> str:
    """Gera o JWT RS256 do §8.0 com `exp` = `agora` + 900 s."""
    dados = {
        "iss": "identity-service",
        "aud": "b2b-platform",
        "sub": SUB_PADRAO,
        "exp": int(agora.timestamp()) + 900,
        "view_mode": "SINGLE",
        "tenant_id": empresa,
        "active_scope": escopo,
    }
    dados.update(claims)
    dados = {k: v for k, v in dados.items() if v is not AUSENTE}
    headers = {"kid": kid} if cabecalho is None and kid is not None else cabecalho
    return jwt.encode(dados, chave, algorithm=algoritmo, headers=headers)


def evento_pedido_atualizado(negociacao_id: str, fornecimento_id: str, pedido_id: str, situacao: str, *,
                             status: str | None = None, event_id: str | None = None,
                             correlation_id: str = CORR_D1, timestamp: str = "2026-11-20T12:01:00.000Z") -> dict:
    if status is None:
        status = "CRIADO" if situacao != "RECUSADA" else "CANCELADO"
    return {
        "eventId": event_id or str(uuid4()),
        "eventType": "pedido_atualizado",
        "eventVersion": "1.0",
        "timestamp": timestamp,
        "source": "pedidos-service",
        "correlationId": correlation_id,
        "aggregateId": pedido_id,
        "payload": {
            "pedidoId": pedido_id,
            "negociacaoId": negociacao_id,
            "fornecimentoId": fornecimento_id,
            "status": status,
            "statusAnterior": "CRIADO",
            "reservaSituacao": situacao,
            "versao": 2,
        },
    }


def confirmar_reserva(fornecimento_id: str = OF_B, pedido_id: str = PT_D, negociacao_id: str = D1, **opcoes) -> dict:
    """Monta o `pedido_atualizado` de confirmação de reserva (§8.0)."""
    return evento_pedido_atualizado(negociacao_id, fornecimento_id, pedido_id, "CONFIRMADA", status="CRIADO", **opcoes)


def recusar_reserva(fornecimento_id: str = OF_B, pedido_id: str = PT_D, negociacao_id: str = D1, **opcoes) -> dict:
    """Monta o `pedido_atualizado` de recusa de reserva (§8.0)."""
    return evento_pedido_atualizado(negociacao_id, fornecimento_id, pedido_id, "RECUSADA", status="CANCELADO", **opcoes)


def validar_erro_rfc7807(resposta) -> dict:
    """Verifica a CA-52 em uma resposta 4xx ou 5xx e devolve o corpo lido."""
    assert resposta.headers["content-type"] == "application/problem+json"
    corpo = loads(resposta.text)
    assert set(corpo) == {"type", "title", "status", "codigo", "detail", "instance", "correlationId", "timestamp"}
    assert corpo["type"] == "about:blank"
    status_esperado, titulo_esperado, detalhe_esperado = ERROS[corpo["codigo"]]
    assert resposta.status_code == corpo["status"] == status_esperado
    assert corpo["title"] == titulo_esperado
    assert corpo["detail"] == detalhe_esperado
    assert corpo["correlationId"] == resposta.headers["x-correlation-id"]
    return corpo


class Ambiente:
    """Agrupa os dublês do §5.1, a aplicação FastAPI e o `TestClient`, registrando todas as respostas de erro."""

    def __init__(self, hora: str = "12:00:00"):
        self.relogio = Relogio(t(hora))
        self.repo = Repositorio()
        self.bus = Barramento(self.repo)
        self.chaves = Chaves()
        self.app = create_app(self.bus, self.relogio, self.repo, self.chaves)
        self.cliente = TestClient(self.app, raise_server_exceptions=False)
        self.respostas_erro = []

    def em(self, hora: str) -> None:
        self.relogio.agora = t(hora)

    def token(self, escopo: str | None, empresa: str | None = COMP_X, **claims) -> str:
        return token(escopo, empresa, agora=self.relogio.now(), **claims)

    def entregar(self, topico: str, mensagem: dict) -> None:
        self.bus.entregar(topico, mensagem)

    def abrir_d1(self, **alteracoes_payload) -> dict:
        mensagem = evento_d1(**alteracoes_payload)
        self.entregar("modo_negociacao_definido", mensagem)
        return mensagem

    def confirmar_reserva(self, fornecimento_id: str = OF_B, pedido_id: str = PT_D, negociacao_id: str = D1,
                          **opcoes) -> dict:
        opcoes.setdefault("timestamp", formato_json(self.relogio.now()))
        mensagem = confirmar_reserva(fornecimento_id, pedido_id, negociacao_id, **opcoes)
        self.entregar("pedido_atualizado", mensagem)
        return mensagem

    def recusar_reserva(self, fornecimento_id: str = OF_B, pedido_id: str = PT_D, negociacao_id: str = D1,
                        **opcoes) -> dict:
        opcoes.setdefault("timestamp", formato_json(self.relogio.now()))
        mensagem = recusar_reserva(fornecimento_id, pedido_id, negociacao_id, **opcoes)
        self.entregar("pedido_atualizado", mensagem)
        return mensagem

    def get(self, caminho: str, escopo: str | None = "BUYER", empresa: str | None = COMP_X, *,
            jwt_token: str | None = None, sem_auth: bool = False, headers: dict | None = None):
        cabecalhos = {}
        if not sem_auth:
            valor_token = jwt_token if jwt_token is not None else self.token(escopo, empresa)
            cabecalhos["Authorization"] = f"Bearer {valor_token}"
        if headers:
            cabecalhos.update(headers)
        resposta = self.cliente.get(caminho, headers=cabecalhos)
        if resposta.status_code >= 400:
            validar_erro_rfc7807(resposta)
            self.respostas_erro.append(resposta)
        return resposta
