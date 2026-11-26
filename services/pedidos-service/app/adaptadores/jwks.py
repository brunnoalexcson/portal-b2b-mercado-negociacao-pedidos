import jwt
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError


class ChavesJwks:
    """ChavesPublicas real: JWKS do Identity-Service com cache de 1 h."""

    def __init__(self, url):
        self.cliente = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=2)

    def obter(self, kid):
        try:
            return self.cliente.get_signing_key(kid).key
        except PyJWKClientConnectionError:
            raise  # rede → 503
        except PyJWKClientError:
            return None  # kid desconhecido → 401
