"""Fábrica usada só pela demonstração: a mesma aplicação de `criar_app_real`, com o esquema Bearer declarado no
OpenAPI para o botão Authorize do `/docs`. A autenticação continua sendo a do serviço (§7.1)."""
from app.main import criar_app_real


def criar():
    app = criar_app_real()
    gerar = app.openapi

    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = gerar()
        schema.setdefault("components", {})["securitySchemes"] = {
            "bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}}
        schema["security"] = [{"bearer": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi
    return app
