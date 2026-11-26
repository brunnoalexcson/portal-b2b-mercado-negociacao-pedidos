from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

import simplejson

UTC = timezone.utc


def utc(valor: datetime) -> datetime:
    """Instante em UTC; sem fuso, é tratado como UTC (§5.1, `clock.now()`)."""
    if valor.tzinfo is None or valor.utcoffset() is None:
        return valor.replace(tzinfo=UTC)
    return valor.astimezone(UTC)


def instante(valor: datetime) -> str:
    """Instante de saída (§4): UTC, milissegundos e sufixo Z."""
    return utc(valor).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ler_instante(valor) -> datetime:
    """Instante de entrada (§4): ISO-8601 com fuso, convertido para UTC; sem fuso é inválido."""
    if not isinstance(valor, str):
        raise ValueError("Instante deve ser texto")
    lido = datetime.fromisoformat(valor)
    if lido.tzinfo is None or lido.utcoffset() is None:
        raise ValueError("Instante sem fuso horário")
    return lido.astimezone(UTC)


def formato_json(valor):
    if isinstance(valor, datetime):
        return instante(valor)
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, UUID):
        return str(valor)
    raise TypeError(f"Tipo não serializável: {type(valor).__name__}")


def _constante_invalida(nome):
    raise ValueError(f"Constante JSON não permitida: {nome}")


def dumps(objeto) -> str:
    return simplejson.dumps(objeto, use_decimal=True, ensure_ascii=False, default=formato_json)


def loads(texto):
    return simplejson.loads(texto, use_decimal=True, parse_constant=_constante_invalida)


def normalizar(objeto):
    if isinstance(objeto, dict):
        return {chave: normalizar(valor) for chave, valor in objeto.items()}
    if isinstance(objeto, list):
        return [normalizar(valor) for valor in objeto]
    if isinstance(objeto, Decimal):
        return objeto.normalize()
    return objeto


def fingerprint(objeto) -> str:
    texto = simplejson.dumps(normalizar(objeto), use_decimal=True, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"), default=formato_json)
    return sha256(texto.encode("utf-8", "surrogatepass")).hexdigest()
