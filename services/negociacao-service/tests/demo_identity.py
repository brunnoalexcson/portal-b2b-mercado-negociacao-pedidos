"""Emissor de tokens de teste para a demonstração (`compose.demo.yml`): JWKS e `POST /token`. Não é o Identity."""
from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from jwt.algorithms import RSAAlgorithm

from app.jsonio import loads

KID = "negociacao-demo"
app = FastAPI(title="Identity de teste da Negociação")
chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
jwk = loads(RSAAlgorithm.to_jwk(chave.public_key()))
jwk.update(kid=KID, alg="RS256", use="sig")


@app.get("/.well-known/jwks.json")
def jwks():
    return {"keys": [jwk]}


@app.post("/token")
def token(empresa: str, escopo: Literal["BUYER", "SUPPLIER", "TENANT_ADMIN", "CARRIER"] = "BUYER",
          usuario: str = "usr-demo", view_mode: str = "SINGLE", minutos: int = 5):
    agora = datetime.now(timezone.utc)
    validade = min(max(minutos, 1), 120)
    claims = {"iss": "identity-service", "aud": "b2b-platform", "sub": usuario,
              "exp": int((agora + timedelta(minutes=validade)).timestamp()), "tenant_id": empresa,
              "active_scope": escopo, "view_mode": view_mode}
    return {"access_token": jwt.encode(claims, chave, algorithm="RS256", headers={"kid": KID})}
