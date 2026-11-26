from datetime import datetime, timezone
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
