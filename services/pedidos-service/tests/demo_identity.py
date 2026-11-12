from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from jwt.algorithms import RSAAlgorithm

from app.jsonio import loads

app = FastAPI()
chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
jwk = loads(RSAAlgorithm.to_jwk(chave.public_key()))
jwk.update(kid="pedidos-demo", alg="RS256", use="sig")


@app.get("/.well-known/jwks.json")
def jwks():
    return {"keys": [jwk]}


@app.post("/token")
def token(usuario: str, tenant: str | None = None,
          escopo: Literal["BUYER", "SUPPLIER", "TENANT_ADMIN", "CARRIER"] = "BUYER", admin: bool = False):
    agora = datetime.now(timezone.utc)
    claims = {"iss": "identity-service", "aud": "b2b-platform", "sub": usuario,
              "iat": agora, "exp": agora + timedelta(minutes=5), "active_scope": escopo, "tenant_id": tenant}
    if admin:
        claims["platform_role"] = "PLATFORM_ADMIN"
    return {"access_token": jwt.encode(claims, chave, algorithm="RS256", headers={"kid": "pedidos-demo"})}
