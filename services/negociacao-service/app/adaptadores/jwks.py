import jwt
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError


class ChavesJwks:
    def __init__(self, url):
        self.cliente = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=2)

    def obter(self, kid):
        try:
            return self.cliente.get_signing_key(kid).key
        except PyJWKClientConnectionError:
            # subclasse de PyJWKClientError: falha de rede vira 503, não kid desconhecido
            raise
        except PyJWKClientError:
            return None
