"""`ChavesPublicas` real: JWKS do Identity-Service com cache de 1 h (§5.1)."""
import jwt
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError


class ChavesJwks:
    def __init__(self, url: str):
        self.cliente = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=2)

    def obter(self, kid: str) -> object | None:
        """Chave pública RSA do `kid`; `None` se o kid não está no conjunto. Falha de rede sobe (→ 503)."""
        try:
            return self.cliente.get_signing_key(kid).key
        except PyJWKClientConnectionError:
            raise
        except PyJWKClientError:
            return None
