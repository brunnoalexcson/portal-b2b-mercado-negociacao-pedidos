from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

import simplejson


def formato_json(valor):
    if isinstance(valor, datetime):
        return valor.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(valor, UUID):
        return str(valor)
    raise TypeError(f"Tipo não serializável: {type(valor).__name__}")


def dumps(objeto):
    return simplejson.dumps(objeto, use_decimal=True, ensure_ascii=False, default=formato_json)


def loads(texto):
    return simplejson.loads(texto, use_decimal=True)


def _normalizar(objeto):
    if isinstance(objeto, dict):
        return {chave: _normalizar(valor) for chave, valor in objeto.items()}
    if isinstance(objeto, list):
        return [_normalizar(valor) for valor in objeto]
    if isinstance(objeto, int) and not isinstance(objeto, bool):
        objeto = Decimal(objeto)
    if isinstance(objeto, Decimal):
        return objeto.normalize()
    return objeto


def fingerprint(objeto):
    """Hash do JSON normalizado: 125, 125.0 e 125.00 e a ordem das chaves não mudam o resultado."""
    texto = simplejson.dumps(_normalizar(objeto), use_decimal=True, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"), default=formato_json)
    return sha256(texto.encode("utf-8", "surrogatepass")).hexdigest()
