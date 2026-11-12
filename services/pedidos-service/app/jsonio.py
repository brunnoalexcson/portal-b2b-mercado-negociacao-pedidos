from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

import simplejson


def validar_texto(valor):
    if "\x00" in valor:
        raise ValueError("Texto contém caractere nulo")
    valor.encode("utf-8")
    return valor


def validar_textos_json(objeto):
    if isinstance(objeto, str):
        validar_texto(objeto)
    elif isinstance(objeto, dict):
        for chave, valor in objeto.items():
            validar_texto(chave)
            validar_textos_json(valor)
    elif isinstance(objeto, list):
        for valor in objeto:
            validar_textos_json(valor)


def formato_json(valor):
    if isinstance(valor, datetime):
        return valor.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, UUID):
        return str(valor)
    raise TypeError(f"Tipo não serializável: {type(valor).__name__}")


def dumps(objeto):
    return simplejson.dumps(objeto, use_decimal=True, ensure_ascii=False, default=formato_json)


def loads(texto):
    return simplejson.loads(texto, use_decimal=True)


def normalizar(objeto):
    if isinstance(objeto, dict):
        return {chave: normalizar(valor) for chave, valor in objeto.items()}
    if isinstance(objeto, list):
        return [normalizar(valor) for valor in objeto]
    if isinstance(objeto, Decimal):
        return objeto.normalize()
    return objeto


def fingerprint(objeto):
    texto = simplejson.dumps(normalizar(objeto), use_decimal=True, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"), default=formato_json)
    return sha256(texto.encode()).hexdigest()
