"""Emissor de tokens de teste para a demonstração (`compose.demo.yml`): JWKS, `POST /token` e `GET /tokens`. Não é o Identity."""
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


def emitir(empresa, escopo="BUYER", usuario="usr-demo", view_mode="SINGLE", minutos=5):
    agora = datetime.now(timezone.utc)
    claims = {"iss": "identity-service", "aud": "b2b-platform", "sub": usuario,
              "exp": int((agora + timedelta(minutes=minutos)).timestamp()), "tenant_id": empresa,
              "active_scope": escopo, "view_mode": view_mode}
    return jwt.encode(claims, chave, algorithm="RS256", headers={"kid": KID})


@app.post("/token", summary="Token para uma empresa e um escopo")
def token(empresa: str, escopo: Literal["BUYER", "SUPPLIER", "TENANT_ADMIN", "CARRIER"] = "BUYER",
          usuario: str = "usr-demo", view_mode: str = "SINGLE", minutos: int = 5):
    """`view_mode` diferente de `SINGLE` simula o login sem empresa escolhida. Validade de 1 a 120 minutos."""
    return {"access_token": emitir(empresa, escopo, usuario, view_mode, min(max(minutos, 1), 120))}


@app.get("/tokens", summary="Tokens prontos para as empresas do demo/popular.py")
def tokens(sufixo: str):
    """Use o sufixo que o `popular` imprime. Copie o `token` e cole em Authorize, no Swagger da Negociação
    (localhost:18106/docs), sem a palavra Bearer. Valem 2 h."""
    perfis = {
        "comprador-a": (f"comprador-a-{sufixo}", "BUYER", "Comprador das diretas e do leilão"),
        "comprador-b": (f"comprador-b-{sufixo}", "BUYER", "Não participa das diretas (404); dá lances no leilão"),
        "fornecedor-x": (f"fornecedor-x-{sufixo}", "SUPPLIER", "Fornecedor das diretas; fora do leilão"),
        "fornecedor-a": (f"fornecedor-a-{sufixo}", "SUPPLIER", "Dono da oferta A do leilão"),
        "fornecedor-a-comprando": (f"fornecedor-a-{sufixo}", "BUYER",
                                   "Fornecedor A com escopo de compra: lance no próprio leilão → 403"),
        "fornecedor-b": (f"fornecedor-b-{sufixo}", "SUPPLIER", "Dono da oferta B do leilão"),
        "transportadora": (f"transportadora-{sufixo}", "CARRIER", "Escopo sem acesso à Negociação → 403"),
    }
    resposta = {nome: {"empresa": empresa, "escopo": escopo, "descricao": descricao,
                       "token": emitir(empresa, escopo, minutos=120)}
                for nome, (empresa, escopo, descricao) in perfis.items()}
    resposta["sem-contexto"] = {"empresa": f"comprador-a-{sufixo}", "escopo": "BUYER",
                                "descricao": "Login sem empresa escolhida (view_mode NONE) → 403",
                                "token": emitir(f"comprador-a-{sufixo}", view_mode="NONE", minutos=120)}
    resposta["expirado"] = {"empresa": f"comprador-a-{sufixo}", "escopo": "BUYER",
                            "descricao": "Token vencido → 401", "token": emitir(f"comprador-a-{sufixo}", minutos=-1)}
    return resposta
