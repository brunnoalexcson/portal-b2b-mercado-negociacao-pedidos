from types import SimpleNamespace

import pytest
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

from app.adaptadores.jwks import ChavesJwks


class ClienteFalso:
    def __init__(self, erro=None):
        self.erro = erro

    def get_signing_key(self, kid):
        if self.erro:
            raise self.erro
        return SimpleNamespace(key=f"chave-{kid}")


def chaves(cliente):
    # o PyJWKClient só busca o JWKS no primeiro uso, então o endereço não é acessado
    resultado = ChavesJwks("http://jwks.invalido/.well-known/jwks.json")
    resultado.cliente = cliente
    return resultado


def test_kid_conhecido_devolve_a_chave():
    assert chaves(ClienteFalso()).obter("k1") == "chave-k1"


def test_kid_desconhecido_devolve_none():
    assert chaves(ClienteFalso(PyJWKClientError("sem chave"))).obter("k2") is None


def test_falha_de_rede_propaga_a_excecao():
    with pytest.raises(PyJWKClientConnectionError):
        chaves(ClienteFalso(PyJWKClientConnectionError("timeout"))).obter("k1")
