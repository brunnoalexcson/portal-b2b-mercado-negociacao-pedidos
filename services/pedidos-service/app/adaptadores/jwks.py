from dataclasses import dataclass

import jwt
from jwt.exceptions import InvalidTokenError, PyJWKClientConnectionError, PyJWKClientError

from app.adaptadores.postgres import ErroPedido


@dataclass(frozen=True)
class Ator:
    usuario: str
    tenant: str | None
    escopo: str | None
    admin: bool


class Identidade:
    def __init__(self, url, emissor, audiencia):
        self.chaves = jwt.PyJWKClient(url, timeout=2, lifespan=300)
        self.emissor = emissor
        self.audiencia = audiencia

    def autenticar(self, token):
        if not token:
            raise ErroPedido("TOKEN_INVALIDO")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or not header["kid"]:
                raise ErroPedido("TOKEN_INVALIDO")
            chave = self.chaves.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, chave.key, algorithms=["RS256"], issuer=self.emissor,
                                audience=self.audiencia, options={"require": ["exp", "sub"]})
        except PyJWKClientConnectionError as exc:
            raise ErroPedido("DEPENDENCIA_INDISPONIVEL") from exc
        except (InvalidTokenError, PyJWKClientError, ValueError) as exc:
            raise ErroPedido("TOKEN_INVALIDO") from exc

        usuario, tenant, escopo = claims["sub"], claims.get("tenant_id"), claims.get("active_scope")
        if not isinstance(usuario, str) or not usuario.strip() or len(usuario) > 128:
            raise ErroPedido("TOKEN_INVALIDO")
        if claims.get("platform_role") == "PLATFORM_ADMIN":
            return Ator(usuario, None, None, True)
        if not isinstance(tenant, str) or not tenant.strip() or len(tenant) > 64 or not isinstance(escopo, str):
            raise ErroPedido("CONTEXTO_NAO_SELECIONADO")
        if escopo not in ("BUYER", "SUPPLIER", "TENANT_ADMIN"):
            raise ErroPedido("ACESSO_NEGADO")
        return Ator(usuario, tenant, escopo, False)
