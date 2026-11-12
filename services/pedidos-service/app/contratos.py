import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, Field

Id = Annotated[str, Field(strict=True, min_length=1, max_length=64, pattern=r"\S")]
Unidade = Literal["KG", "TON", "UN", "CX", "L"]
TipoTransporte = Literal["GRANEL", "REFRIGERADO", "CARGA_SECA", "PERIGOSO"]
UF = Literal["AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
             "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"]


def numero_json(valor):
    if isinstance(valor, bool) or not isinstance(valor, (int, Decimal)):
        raise ValueError("Esperado um número JSON")
    return valor


Quantidade = Annotated[Decimal, BeforeValidator(numero_json), Field(gt=0, max_digits=12, decimal_places=2)]
Preco = Annotated[Decimal, BeforeValidator(numero_json), Field(gt=0, max_digits=16, decimal_places=2)]
PesoUnitario = Annotated[Decimal, BeforeValidator(numero_json), Field(gt=0, max_digits=12, decimal_places=3)]


class Local(BaseModel):
    cidade: Annotated[str, Field(strict=True, min_length=1, max_length=120, pattern=r"\S")]
    uf: UF


class Produto(BaseModel):
    produtoId: Id
    nome: Annotated[str, Field(strict=True, min_length=1, max_length=255, pattern=r"\S")]
    unidade: Unidade
    tipoTransporte: TipoTransporte
    pesoUnitarioKg: PesoUnitario | None = None


class Fornecimento(BaseModel):
    fornecimentoId: Id
    fornecedorId: Id
    produtoId: Id
    unidade: Unidade
    origem: Local


class Fechamento(BaseModel):
    negociacaoId: Id
    status: Literal["ENCERRADO"]
    compradorId: Id
    fornecedorId: Id
    produtoId: Id
    fornecimentoId: Id
    demandaId: Id | None = None
    quantidade: Quantidade
    valorUnitario: Preco
    destino: Local | None = None
    dataEntregaDesejada: date | None = None
    reservaId: Id | None = None


class Deserto(BaseModel):
    negociacaoId: Id
    status: Literal["DESERTO"]


class Envelope(BaseModel):
    eventId: UUID
    eventType: str
    eventVersion: Literal["1.0"]
    timestamp: datetime
    source: str
    correlationId: Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
    aggregateId: Id
    payload: dict


TOPICOS = {
    "negociacao_fechada": ("negociacao-service", "negociacaoId", Fechamento),
    "produto_cadastrado": ("produtos-service", "produtoId", Produto),
    "produto_atualizado": ("produtos-service", "produtoId", Produto),
    "fornecimento_criado": ("fornecimentos-service", "fornecimentoId", Fornecimento),
}
CORRELACAO_HTTP = re.compile(r"[A-Za-z0-9._-]{1,128}")


def validar_evento(topico, chave, dados):
    origem, id_campo, modelo = TOPICOS[topico]
    evento = Envelope.model_validate(dados)
    if (evento.eventType != topico or evento.source != origem or evento.aggregateId != chave
            or evento.timestamp.utcoffset() != timedelta(0)):
        raise ValueError("Envelope incompatível com tópico, origem, chave ou data UTC")
    if topico == "negociacao_fechada" and evento.payload.get("status") == "DESERTO":
        modelo = Deserto
        if any(evento.payload.get(campo) is not None for campo in ("fornecimentoId", "vencedorId")):
            raise ValueError("Negociação deserta contém vencedor")
    payload = modelo.model_validate(evento.payload)
    if getattr(payload, id_campo) != chave:
        raise ValueError("A chave difere do identificador do payload")
    return evento, payload
