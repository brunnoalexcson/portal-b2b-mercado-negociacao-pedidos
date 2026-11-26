"""Emissor de JWT RS256 da demonstração: faz o papel do Identity-Service (JWKS + tokens de teste)."""
from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from jwt.algorithms import RSAAlgorithm

from app.jsonio import loads

app = FastAPI(title="Identity de teste — Portal B2B (demonstração)")
chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
jwk = loads(RSAAlgorithm.to_jwk(chave.public_key()))
jwk.update(kid="pedidos-demo", alg="RS256", use="sig")

COMP = "b2000000-0000-4000-8000-000000000002"
F1 = "a1000000-0000-4000-8000-000000000001"
OUTRA = "c3000000-0000-4000-8000-000000000003"
PERFIS = {
    "comprador": ("usr-comp", COMP, "BUYER", "Comprador COMP: vê e informa o destino dos pedidos que comprou"),
    "fornecedor_f1": ("usr-f1", F1, "SUPPLIER", "Fornecedor F1 (Rio Verde): vê os pedidos da sua oferta"),
    "outro_comprador": ("usr-outra-b", OUTRA, "BUYER", "Outra empresa: não vê os pedidos do COMP (404)"),
    "transportadora": ("usr-carrier", OUTRA, "CARRIER", "CARRIER: sem acesso a Pedidos (403 ACESSO_NEGADO)"),
    "sem_contexto": ("usr-sem", None, "BUYER", "Login sem empresa escolhida (403 CONTEXTO_NAO_SELECIONADO)"),
}


def emitir(usuario, tenant=None, escopo="BUYER", admin=False, minutos=60):
    agora = datetime.now(timezone.utc)
    claims = {"iss": "identity-service", "aud": "b2b-platform", "sub": usuario,
              "iat": agora, "exp": agora + timedelta(minutes=minutos), "active_scope": escopo, "tenant_id": tenant}
    if admin:
        claims["platform_role"] = "PLATFORM_ADMIN"
    return jwt.encode(claims, chave, algorithm="RS256", headers={"kid": "pedidos-demo"})


@app.get("/.well-known/jwks.json")
def jwks():
    return {"keys": [jwk]}


@app.post("/token")
def token(usuario: str, tenant: str | None = None,
          escopo: Literal["BUYER", "SUPPLIER", "TENANT_ADMIN", "CARRIER"] = "BUYER", admin: bool = False):
    return {"access_token": emitir(usuario, tenant, escopo, admin)}


@app.get("/tokens", summary="Tokens prontos para testar a API de Pedidos no Swagger")
def tokens():
    """Copie o valor de `token` e cole em Authorize, no Swagger do Pedidos (localhost:18107/docs)."""
    perfis = {nome: {"descricao": desc, "token": emitir(usuario, tenant, escopo)}
              for nome, (usuario, tenant, escopo, desc) in PERFIS.items()}
    perfis["suporte"] = {"descricao": "PLATFORM_ADMIN: vê todos os pedidos, só leitura (PUT → 403)",
                         "token": emitir("usr-admin", admin=True)}
    perfis["expirado"] = {"descricao": "Token vencido (401 TOKEN_INVALIDO)",
                          "token": emitir("usr-comp", COMP, "BUYER", minutos=-1)}
    return {"validade": "60 minutos", "perfis": perfis}
