"""Documentação do `/docs` (ESP-NEG-001 §7.2). Só descreve a API: nenhuma validação depende deste módulo."""

ID_EXEMPLO = "31000000-0000-4000-8000-000000000001"

DESCRICAO = """
Conduz os **leilões** (`LEILAO_DIRETO`) e as **negociações diretas** (`VENDA_DIRETA`, inclusive a compra imediata) do
Portal B2B. Não há rota de criação: as negociações são abertas pelos eventos do Mercado (ESP-NEG-001 v1.5).

**Token.** No ambiente integrado, faça `POST /api/v1/auth/login` e depois `POST /api/v1/auth/switch-context` no
Identity-Service, escolhendo a empresa e o escopo (`BUYER` ou `SUPPLIER`). Na demo local, use o emissor de teste em
`http://localhost:18107/docs` (`GET /tokens` com o sufixo que o `demo/popular.py` imprime). Clique em **Authorize** e
cole só o token, sem a palavra `Bearer`.

**Convenções.** Erros em `application/problem+json`, com o campo `codigo`. Valores com 2 casas decimais e datas ISO
8601 em UTC. Listagens com `page` e `size`. Nas escritas, `Idempotency-Key` opcional: o mesmo corpo devolve a mesma
resposta. `X-Correlation-Id` opcional, devolvido em toda resposta.
""".strip()

TAGS = [
    {"name": "Saúde", "description": "Rotas públicas de vida, prontidão e métricas."},
    {"name": "Negociações", "description": "Listar e detalhar leilões e negociações diretas."},
    {"name": "Leilão", "description": "Lances de um leilão."},
    {"name": "Negociação direta", "description": "Propostas, aceite e recusa entre comprador e fornecedor."},
]

BEARER = "JWT RS256 do Identity-Service com `view_mode = SINGLE`, `tenant_id` e `active_scope` (`BUYER` ou `SUPPLIER`)."
NEGOCIACAO_ID = "UUID da negociação; outro formato → `422`."

IDEMPOTENCIA = {
    "name": "Idempotency-Key", "in": "header", "required": False,
    # sem `example`: o Swagger o preencheria no "Try it out" e todo envio repetiria a mesma chave
    "description": "Opcional. Por 24 h, o mesmo corpo devolve a resposta original; outro corpo → `409`.",
    "schema": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,128}$"},
}
PAGINACAO = [
    {"name": "page", "in": "query", "required": False, "description": "Página, a partir de 1.",
     "schema": {"type": "integer", "minimum": 1, "default": 1}},
    {"name": "size", "in": "query", "required": False, "description": "Itens por página, de 1 a 100.",
     "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10}},
]

LEILAO = {
    "negociacaoId": ID_EXEMPLO, "modo": "LEILAO_DIRETO", "status": "ABERTO",
    "produtoId": "35000000-0000-4000-8000-000000000001", "dataInicio": "2026-11-20T12:00:00.000Z",
    "dataFim": "2026-11-20T15:00:00.000Z", "quantidadeTotal": 20.00, "precoInicial": 2550.00, "incrementoMinimo": 25.50,
    "melhorLance": {"lanceId": "33000000-0000-4000-8000-000000000002", "negociacaoId": ID_EXEMPLO,
                    "compradorId": "OCULTO", "valor": 2600.00, "sequencia": 2, "criadoEm": "2026-11-20T13:10:00.000Z"},
    "totalLances": 2, "compradorId": None,
    "ofertas": [
        {"negociacaoId": ID_EXEMPLO, "fornecimentoId": "34000000-0000-4000-8000-000000000001",
         "fornecedorId": "a1000000-0000-4000-8000-000000000001", "quantidade": 5.00, "precoUnitario": 120.00,
         "valorItem": 600.00, "participacao": 0.2353, "situacao": "CONFIRMADA"},
        {"negociacaoId": ID_EXEMPLO, "fornecimentoId": "34000000-0000-4000-8000-000000000002",
         "fornecedorId": "a1000000-0000-4000-8000-000000000002", "quantidade": 15.00, "precoUnitario": 130.00,
         "valorItem": 1950.00, "participacao": 0.7647, "situacao": "CONFIRMADA"},
    ],
    "criadoEm": "2026-11-20T12:00:00.000Z", "encerradoEm": None, "agora": "2026-11-20T13:20:00.000Z",
    "acoesPermitidas": ["DAR_LANCE"],
}
DIRETA_ID = "31000000-0000-4000-8000-000000000002"
DIRETA = {
    "negociacaoId": DIRETA_ID, "modo": "VENDA_DIRETA", "status": "ABERTO",
    "produtoId": "35000000-0000-4000-8000-000000000001", "fornecimentoId": "34000000-0000-4000-8000-000000000002",
    "fornecedorId": "a1000000-0000-4000-8000-000000000002", "compradorId": "b2000000-0000-4000-8000-000000000001",
    "quantidade": 10.00, "precoOferta": 130.00, "precoAtual": 130.00, "vez": "BUYER", "compraImediata": False,
    "expiraEm": "2026-11-22T12:00:00.000Z", "reservaSituacao": "PENDENTE", "totalPropostas": 1,
    "criadoEm": "2026-11-20T12:00:00.000Z", "encerradoEm": None, "agora": "2026-11-20T12:00:00.000Z",
    "acoesPermitidas": ["PROPOR", "RECUSAR"],
}
FECHADA = {**DIRETA, "precoAtual": 125.00, "vez": "SUPPLIER", "reservaSituacao": "CONFIRMADA", "totalPropostas": 2,
           "encerradoEm": "2026-11-21T13:00:00.000Z", "agora": "2026-11-21T13:00:00.000Z", "acoesPermitidas": []}
LANCE = {"lanceId": "33000000-0000-4000-8000-000000000001", "negociacaoId": ID_EXEMPLO,
         "compradorId": "b2000000-0000-4000-8000-000000000001", "valor": 2550.00, "sequencia": 1,
         "criadoEm": "2026-11-20T13:05:00.000Z"}
PROPOSTA = {"propostaId": "36000000-0000-4000-8000-000000000102", "negociacaoId": DIRETA_ID, "autor": "BUYER",
            "precoUnitario": 125.00, "sequencia": 2, "criadoEm": "2026-11-21T12:00:00.000Z"}
RESUMO = {"negociacaoId": DIRETA_ID, "modo": "VENDA_DIRETA", "status": "ABERTO",
          "produtoId": "35000000-0000-4000-8000-000000000001", "dataInicio": None, "dataFim": None,
          "expiraEm": "2026-11-22T12:00:00.000Z", "criadoEm": "2026-11-20T12:00:00.000Z"}

AUTENTICADA = ("TOKEN_INVALIDO", "DEPENDENCIA_INDISPONIVEL", "CONTEXTO_NAO_SELECIONADO", "ACESSO_NEGADO",
               "VALIDACAO_INVALIDA")
COM_ID = AUTENTICADA + ("NEGOCIACAO_NAO_ENCONTRADA",)
DA_DIRETA = COM_ID + ("MODO_INCOMPATIVEL", "IDEMPOTENCIA_CONFLITO", "NEGOCIACAO_ENCERRADA", "COMPRA_IMEDIATA")


def corpo(propriedades, exemplo, obrigatorios=()):
    return {"required": bool(obrigatorios), "content": {"application/json": {
        "schema": {"type": "object", "additionalProperties": False, "required": list(obrigatorios),
                   "properties": propriedades},
        "example": exemplo}}}


def sucesso(status, descricao, *exemplos):
    """`exemplos`: um valor, ou pares (nome, valor) quando a rota devolve formatos diferentes."""
    if len(exemplos) == 1:
        conteudo = {"example": exemplos[0]}
    else:
        conteudo = {"examples": {nome: {"value": valor} for nome, valor in zip(exemplos[::2], exemplos[1::2])}}
    return {status: {"description": descricao, "content": {"application/json": conteudo}}}


ROTAS = {
    ("get", "/health"): {
        "summary": "API-01 — Saúde", "tags": ["Saúde"],
        "description": "Rota pública. Responde se o processo está no ar.",
        "responses": sucesso("200", "No ar.", {"status": "UP", "service": "negociacao-service",
                                               "timestamp": "2026-11-20T12:00:00.000Z"}),
    },
    ("get", "/health/ready"): {
        "summary": "API-02 — Prontidão", "tags": ["Saúde"],
        "description": "Rota pública. `200` quando o banco e o Kafka respondem; senão `503`.",
        "responses": sucesso("200", "Pronto.", {"status": "READY", "service": "negociacao-service"}),
        "erros": ("DEPENDENCIA_INDISPONIVEL",),
    },
    ("get", "/metrics"): {
        "summary": "API-03 — Métricas", "tags": ["Saúde"],
        "description": "Rota pública. Métricas no formato Prometheus.",
        "responses": {"200": {"description": "Métricas.", "content": {"text/plain": {
            "example": 'http_requests_total{metodo="GET",rota="/api/v1/negociacoes",status="200"} 1.0\n'}}}},
    },
    ("get", "/api/v1/negociacoes"): {
        "summary": "API-04 — Listar negociações", "tags": ["Negociações"],
        "description": "`BUYER` ou `SUPPLIER`. Lista todos os leilões e as diretas de que a empresa participa, dos "
                       "mais recentes para os mais antigos.",
        "parameters": [*PAGINACAO,
                       {"name": "modo", "in": "query", "required": False, "description": "Filtra pelo modo.",
                        "schema": {"type": "string", "enum": ["LEILAO_DIRETO", "VENDA_DIRETA"]}},
                       {"name": "status", "in": "query", "required": False, "description": "Filtra pelo estado.",
                        "schema": {"type": "string", "enum": ["AGUARDANDO_RESERVAS", "ABERTO", "ENCERRADO",
                                                              "DESERTO", "RECUSADO", "EXPIRADO"]}}],
        "responses": sucesso("200", "Página de resumos.", {"items": [RESUMO], "page": 1, "size": 10, "total": 1}),
        "erros": AUTENTICADA + ("ERRO_INTERNO",),
    },
    ("get", "/api/v1/negociacoes/{negociacaoId}"): {
        "summary": "API-05 — Detalhar negociação", "tags": ["Negociações"],
        "description": "`BUYER` ou `SUPPLIER`. O formato depende do `modo`. No leilão, o comprador dos lances alheios "
                       "vem como `OCULTO`; a direta só é visível às duas partes.",
        "responses": sucesso("200", "Detalhe.", "leilao", LEILAO, "direta", DIRETA),
        "erros": COM_ID + ("ERRO_INTERNO",),
    },
    ("get", "/api/v1/negociacoes/{negociacaoId}/lances"): {
        "summary": "API-06 — Listar lances", "tags": ["Leilão"],
        "description": "`BUYER` ou `SUPPLIER`. Lances do leilão em ordem de chegada, com o sigilo do comprador.",
        "parameters": PAGINACAO,
        "responses": sucesso("200", "Página de lances.", {"items": [LANCE], "page": 1, "size": 10, "total": 1}),
        "erros": COM_ID + ("MODO_INCOMPATIVEL", "ERRO_INTERNO"),
    },
    ("post", "/api/v1/negociacoes/{negociacaoId}/lances"): {
        "summary": "API-07 — Dar lance", "tags": ["Leilão"],
        "description": "Só `BUYER`. Valor total pelo lote: no mínimo o preço inicial e, havendo lances, o melhor "
                       "lance mais o incremento. Quem fornece no leilão não dá lance nele.",
        "parameters": [IDEMPOTENCIA],
        "requestBody": corpo({"valor": {"type": "number", "description": "Valor total pelo lote."},
                              "demandaId": {"type": ["string", "null"], "description": "Demanda atendida (opcional)."}},
                             {"valor": 2600.00, "demandaId": "36000000-0000-4000-8000-000000000002"}, ["valor"]),
        "responses": sucesso("201", "Lance aceito.", {**LANCE, "valor": 2600.00}),
        "erros": COM_ID + ("MODO_INCOMPATIVEL", "IDEMPOTENCIA_CONFLITO", "CONFLITO_DE_INTERESSE", "LEILAO_NAO_ABERTO",
                           "LANCE_INSUFICIENTE", "ERRO_INTERNO"),
    },
    ("get", "/api/v1/negociacoes/{negociacaoId}/propostas"): {
        "summary": "API-08 — Listar propostas", "tags": ["Negociação direta"],
        "description": "Partes da direta. Propostas em ordem; a primeira é a do fornecedor, pelo preço da oferta.",
        "parameters": PAGINACAO,
        "responses": sucesso("200", "Página de propostas.", {"items": [PROPOSTA], "page": 1, "size": 10, "total": 1}),
        "erros": COM_ID + ("MODO_INCOMPATIVEL", "ERRO_INTERNO"),
    },
    ("post", "/api/v1/negociacoes/{negociacaoId}/propostas"): {
        "summary": "API-09 — Contraproposta", "tags": ["Negociação direta"],
        "description": "A parte da vez propõe um preço unitário, no máximo o preço da oferta. A vez passa à outra.",
        "parameters": [IDEMPOTENCIA],
        "requestBody": corpo({"precoUnitario": {"type": "number", "description": "Preço unitário proposto."}},
                             {"precoUnitario": 125.00}, ["precoUnitario"]),
        "responses": sucesso("201", "Proposta registrada.", PROPOSTA),
        "erros": DA_DIRETA + ("FORA_DA_VEZ", "PROPOSTA_ACIMA_DA_OFERTA", "ERRO_INTERNO"),
    },
    ("post", "/api/v1/negociacoes/{negociacaoId}/aceitar"): {
        "summary": "API-10 — Aceitar", "tags": ["Negociação direta"],
        "description": "A parte da vez aceita o preço atual, com a reserva já confirmada. Fecha a direta como "
                       "`ENCERRADO` e publica `negociacao_fechada`. Corpo vazio ou `{}`.",
        "parameters": [IDEMPOTENCIA],
        "requestBody": corpo({}, {}),
        "responses": sucesso("200", "Direta encerrada.", {**FECHADA, "status": "ENCERRADO"}),
        "erros": DA_DIRETA + ("FORA_DA_VEZ", "RESERVA_PENDENTE", "ERRO_INTERNO"),
    },
    ("post", "/api/v1/negociacoes/{negociacaoId}/recusar"): {
        "summary": "API-11 — Recusar", "tags": ["Negociação direta"],
        "description": "Qualquer das partes, a qualquer momento da direta aberta. Fecha como `RECUSADO` e publica "
                       "`negociacao_fechada`. Corpo vazio ou `{}`.",
        "parameters": [IDEMPOTENCIA],
        "requestBody": corpo({}, {}),
        "responses": sucesso("200", "Direta recusada.", {**FECHADA, "status": "RECUSADO"}),
        "erros": DA_DIRETA + ("ERRO_INTERNO",),
    },
}


def documentar(app, erros):
    """Completa o OpenAPI gerado pelo FastAPI com `ROTAS`; `erros` é o catálogo `codigo → (status, título, detalhe)`."""
    gerar = app.openapi

    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = gerar()
        schema["info"].update(description=DESCRICAO, version="1.5")
        schema["tags"] = TAGS
        # tira o HTTPValidationError do FastAPI: o serviço nunca responde nesse formato
        schema["components"].pop("schemas", None)
        schema["components"]["securitySchemes"]["HTTPBearer"]["description"] = BEARER
        for (metodo, caminho), doc in ROTAS.items():
            operacao = schema["paths"][caminho][metodo]
            gerados = [{**p, "description": NEGOCIACAO_ID, "example": ID_EXEMPLO} if p["name"] == "negociacaoId" else p
                       for p in operacao.get("parameters", [])]
            operacao.update({chave: valor for chave, valor in doc.items() if chave != "erros"})
            operacao["parameters"] = gerados + doc.get("parameters", [])
            operacao["responses"] = {**doc["responses"], **respostas_de_erro(doc.get("erros", ()), caminho, erros)}
        app.openapi_schema = schema
        return schema

    app.openapi = openapi


def respostas_de_erro(codigos, caminho, erros):
    """Uma resposta por status, com um exemplo `problem+json` completo para cada código (§10)."""
    respostas = {}
    for codigo in codigos:
        status, titulo, detalhe = erros[codigo]
        resposta = respostas.setdefault(str(status), {"description": titulo, "content": {"application/problem+json": {
            "examples": {}}}})
        resposta["description"] += f"{',' if resposta['description'] != titulo else ':'} `{codigo}`"
        resposta["content"]["application/problem+json"]["examples"][codigo] = {"value": {
            "type": "about:blank", "title": titulo, "status": status, "codigo": codigo, "detail": detalhe,
            "instance": caminho.replace("{negociacaoId}", ID_EXEMPLO),
            "correlationId": "7f3c2a10-5b1e-4c8d-9a2f-0e6b4d8c1a35", "timestamp": "2026-11-20T13:20:00.000Z"}}
    return respostas
