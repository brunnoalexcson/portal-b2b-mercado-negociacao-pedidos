# Mercado (Matching Engine) do Portal B2B

Implementação da [ESP-MRC-001 v0.11](../../docs/mercado/requisitos.md). O serviço mantém cópias locais de produtos, ofertas e demandas, calcula o balanço por produto e define o modo de negociação. Quando o comprador pede, abre uma negociação direta; quando a demanda supera a oferta, forma um leilão em lote a partir da sugestão aceita pelos fornecedores. Publica `modo_negociacao_definido`, `leilao_iniciado` e `mercado.dlt`.

## Estrutura

- `app/dominio.py`: regras puras (aptidão, balanço, sugestão, lote, participação) e o serviço `Mercado`, que as aplica sobre as portas de `app/portas.py`.
- `app/contratos.py`: validação dos eventos consumidos e montagem dos publicados. `app/http.py`: rotas, JWT, RFC 7807, idempotência e métricas.
- `app/main.py`: `create_app`, `executar_prazos` e `criar_app_real`.
- `app/adaptadores/`: PostgreSQL (psycopg 3, pool ≤ 5), Kafka (outbox, publicador, consumidor, DLT) e JWKS.
- `migracoes/versions/0001_mercado.py`: schema `mercado`, executado com a role `portal_ddl`.

## Docker no WSL 2 sem Docker Desktop

Os comandos abaixo foram validados com o Docker Engine instalado no Ubuntu do WSL 2, sem Docker Desktop, executados no PowerShell a partir da raiz do repositório. Nesse arranjo, o WSL desliga quando nenhum terminal dele está aberto, e os containers da demonstração param junto. Há duas formas de evitar isso:

- rodar cada sequência num único `wsl sh -c '...'`, como nos blocos abaixo, para que a subida, os testes e as consultas aconteçam na mesma sessão do WSL; ou
- deixar o app Ubuntu aberto durante toda a demonstração e usar `wsl docker ...` em comandos separados.

## Testes

A suíte de aceitação usa dublês das portas e roda sem infraestrutura. Os testes de schema leem `docs/mercado/contratos/` ou a pasta de `MERCADO_CONTRATOS_DIR`. Na imagem, a etapa `test` roda a suíte em Python 3.12 e exige essa pasta, passada como contexto adicional:

```powershell
wsl sh -c 'docker build --target test --build-context contratos=docs/mercado/contratos -t mercado-service:test services/mercado-service'
```

Resultado esperado: `201 passed, 4 skipped` (os 4 testes de `tests/test_fluxo_real.py` só rodam dentro da demonstração).

## Demonstração local

Subir tudo, conferir a prontidão e rodar o fluxo real numa única sessão do WSL:

```powershell
wsl sh -c 'docker compose -f services/mercado-service/compose.demo.yml up -d --build gateway && docker compose -f services/mercado-service/compose.demo.yml ps -a && sleep 15 && curl -s -i http://localhost:18105/health/ready && curl -s -i http://localhost:18105/api/v1/mercado/produtos/p1/balanco && docker compose -f services/mercado-service/compose.demo.yml run --build --rm --no-deps teste'
```

Resultado esperado:
- `migrar` e `topicos` em `Exited (0)`; `db`, `broker`, `identity`, `app1`, `app2` e `gateway` em `Up`;
- `/health/ready` com `200` e `{"status": "ready"}` (se ainda vier `503`, as réplicas estão terminando de subir: repita o `curl`);
- a rota sem token com `401`, `WWW-Authenticate: Bearer` e corpo `application/problem+json`;
- o serviço `teste` com `4 passed`.

Se algo falhar, os logs da migração e das réplicas:

```powershell
wsl sh -c 'docker compose -f services/mercado-service/compose.demo.yml logs --tail 80 migrar app1 app2'
```

Para parar mantendo os dados, ou para apagar tudo, inclusive os volumes:

```powershell
wsl sh -c 'docker compose -f services/mercado-service/compose.demo.yml stop'
```

```powershell
wsl sh -c 'docker compose -f services/mercado-service/compose.demo.yml down -v'
```

A demonstração sobe PostgreSQL 16, Redpanda com os 12 tópicos do §7.3 e do §7.4, a migração 0001, um emissor de tokens de teste (`tests/demo_identity.py`), duas réplicas e um gateway Nginx em `http://localhost:18105`. O serviço `teste` publica eventos dos parceiros no Kafka, abre uma direta e um leilão pela API, e confere os eventos publicados pela outbox, a deduplicação no banco e a DLT. Credenciais e dados são exclusivos da demonstração. O emissor gera uma chave nova a cada subida: se reiniciar só o `identity`, reinicie também `app1` e `app2`, que guardam o JWKS em cache por 1 h.

## Roteiro da apresentação

Antes de começar, abra o app Ubuntu e deixe-o aberto até o fim: com o WSL desligado, os containers param. O `/docs` fica em `http://localhost:18105/docs` e o Redpanda Console em `http://localhost:18080`. A página do `/docs` carrega o Swagger UI da internet.

Nas réplicas da demonstração, o Uvicorn usa a fábrica `demo/app_demo.py`. Ela chama o mesmo `criar_app_real` da imagem e só declara o esquema Bearer no OpenAPI, para o botão **Authorize** aparecer. A autenticação continua sendo a do §7.1.

**1. Subir a demo** (banco, Redpanda, tópicos, migração, emissor de tokens, duas réplicas, gateway e Console):

```powershell
wsl sh -c 'docker compose -f services/mercado-service/compose.demo.yml up -d --build gateway console && sleep 15 && curl -s http://localhost:18105/health/ready'
```

Esperado: `{"status": "ready"}`. Se vier o erro 503, as réplicas ainda estão subindo: repita o comando.

**2. Popular o cenário:**

```powershell
wsl sh -c 'docker compose -f services/mercado-service/compose.demo.yml run --build --rm --no-deps popular'
```

O script publica no Kafka 1 produto, a oferta X (8.00 × 60.00), a oferta Y (7.50 × 40.00), a demanda A (50.00, máximo 9.00) e a demanda B (80.00). O balanço fica com oferta 100.00 contra demanda 130.00 e a sugestão de leilão abre. Ao final, ele imprime os IDs (todos com o mesmo sufixo, novo a cada execução), os corpos prontos dos passos 7 e 9 e um token de 2 h para Comprador A, Comprador B, Fornecedor X e Fornecedor Y. Deixe essa saída aberta para copiar.

**3. Authorize no `/docs`:** clique em **Authorize**, cole o token do **Comprador A** (só o token, sem a palavra `Bearer`) e confirme. Para trocar de empresa nos passos seguintes, clique em **Authorize** → **Logout** e cole o outro token.

**4. Balanço do produto** (Comprador A): `GET /api/v1/mercado/produtos/{produtoId}/balanco` com o ID do produto. Mostre `ofertaTotal` 100.00, `demandaTotal` 130.00, `situacao` `DEMANDA_EXCEDENTE`, `excessoDemanda` 30.00 e o `sugestaoId`. O balanço mostra só totais, sem identificar demandas nem compradores.

**5. Vitrine** (Comprador A): `GET /api/v1/mercado/produtos/{produtoId}/ofertas` com `demandaId` = demanda A. Mostre a ordem pelo menor preço (oferta Y a 7.50, depois oferta X a 8.00) e `apta` = `true` nas duas, porque as duas estão abaixo do preço máximo de 9.00.

**6. Sugestão de leilão** (troque para o **Fornecedor X**): `GET /api/v1/mercado/sugestoes-leilao`. Mostre a sugestão `ABERTA` com `excessoDemanda` 30.00, `fornecedoresNoLote` 0 e `duracaoLeilaoHoras` 24. Um comprador recebe `403 ACESSO_NEGADO` nessa rota.

**7. Direta com compra imediata** (troque para o **Comprador A**): `POST /api/v1/mercado/negociacoes` com o corpo "Direta" impresso pelo script (demanda A, oferta Y, 10, `compraImediata` `true`). Resposta `201` com `modo` `VENDA_DIRETA`, `compraImediata` `true`, `valorReferencia` 75.00 e o cabeçalho `Location`. A demanda total cai para 120.00 e a sugestão continua aberta.

**8. Evento no Console:** em **Topics** → `modo_negociacao_definido`, abra a mensagem mais recente. A chave é o `negociacaoId`, o `source` é `mercado-service`, e o payload traz `compraImediata` `true` e `demandas[0]` com o destino da demanda A. Mostre também que os tópicos consumidos (`produto_cadastrado`, `fornecimento_criado`, `demanda_criada`) têm as mensagens publicadas pelo script.

**9. Aceites fechando o lote:**
- Com o **Fornecedor X**: `POST /api/v1/mercado/sugestoes-leilao/{sugestaoId}/aceites` com o corpo "Aceite (Fornecedor X)". Resposta `201` com `situacao` `EM_FORMACAO`, `quantidadeNoLote` 60.00 e `janelaEncerraEm` uma hora depois.
- Com o **Fornecedor Y**: a mesma rota com o corpo "Aceite (Fornecedor Y)". Resposta `201` com `situacao` `CONVERTIDA` e o `negociacaoId` do leilão. Os dois fornecedores elegíveis estão no lote, por isso ele fecha na hora (RN-21).
- Opcional: `GET /api/v1/mercado/negociacoes/{negociacaoId}` mostra o leilão com `precoInicial` 780.00, `incrementoMinimo` 7.80 e as participações 0.6154 e 0.3846.

**10. Leilão no Console:** em `modo_negociacao_definido` aparece um segundo evento, com `modo` `LEILAO_DIRETO` e `demandas` vazio. Em `leilao_iniciado`, mostre `precoInicial` 780.00, `incrementoMinimo` 7.80, os dois itens com `participacao`, `iniciaEm` e `encerraEm` 24 h depois. Os dois eventos do leilão usam a mesma chave (`negociacaoId`).

Para repetir a apresentação, basta rodar o passo 2 de novo: cada execução cria um cenário com IDs próprios. Para encerrar, use o `stop` ou o `down -v` da seção anterior.

## Conectar ao kit

O [override](compose.mercado.yml) troca o build de `app1-mercado` e `app2-mercado` por este projeto e acrescenta `mercado-migrate`; as réplicas só sobem depois da migração. Informe `MERCADO_PROJECT_DIR` (caminho absoluto desta pasta), `MERCADO_DATABASE_URL` (role `svc_mercado`) e `MERCADO_MIGRATION_DATABASE_URL` (role `portal_ddl`), com host `pg-primary` e banco `portal_b2b`. Senhas com caracteres especiais vão codificadas na URL (`!` → `%21`, `#` → `%23`). Os tópicos do §7.3 e do §7.4 precisam existir no Redpanda (INT-MRC-03); sem eles, `/health/ready` responde 503.
