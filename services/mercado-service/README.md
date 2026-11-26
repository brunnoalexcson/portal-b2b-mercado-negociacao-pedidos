# Mercado (Matching Engine) do Portal B2B

Implementação da [ESP-MRC-001 v0.8](../../docs/mercado/requisitos.md). O serviço mantém cópias locais de produtos, ofertas e demandas, calcula o balanço por produto e define o modo de negociação. Quando o comprador pede, abre uma negociação direta; quando a demanda supera a oferta, forma um leilão em lote a partir da sugestão aceita pelos fornecedores. Publica `modo_negociacao_definido`, `leilao_iniciado` e `mercado.dlt`.

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

Resultado esperado: `186 passed, 4 skipped` (os 4 testes de `tests/test_fluxo_real.py` só rodam dentro da demonstração).

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

## Conectar ao kit

O [override](compose.mercado.yml) troca o build de `app1-mercado` e `app2-mercado` por este projeto e acrescenta `mercado-migrate`; as réplicas só sobem depois da migração. Informe `MERCADO_PROJECT_DIR` (caminho absoluto desta pasta), `MERCADO_DATABASE_URL` (role `svc_mercado`) e `MERCADO_MIGRATION_DATABASE_URL` (role `portal_ddl`), com host `pg-primary` e banco `portal_b2b`. Senhas com caracteres especiais vão codificadas na URL (`!` → `%21`, `#` → `%23`). Os tópicos do §7.3 e do §7.4 precisam existir no Redpanda (INT-MRC-03); sem eles, `/health/ready` responde 503.
