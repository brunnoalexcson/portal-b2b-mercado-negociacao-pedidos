# Negociação e Leilão do Portal B2B

Implementação da [ESP-NEG-001](../../docs/negociacao/requisitos.md). Esta versão abre a negociação direta pelo `modo_negociacao_definido` do Mercado, pede a reserva ao Pedidos (`pedido_temporario_solicitado`) e registra a resposta (`pedido_atualizado`). A compra imediata fecha sozinha quando a reserva confirma, e a reserva recusada fecha a direta como `RECUSADO`. A API lista e detalha as negociações e lista as propostas (API-04, API-05 e API-08), com o token do Identity. Mensagens repetidas não repetem efeito, e as inválidas vão para a `negociacao.dlt`. Contraproposta, aceite, recusa, prazo de 48 h e o leilão entram nas próximas partes.

## Estrutura

- `app/dominio.py`: regras da negociação direta (ações permitidas, visibilidade, alocação) e o serviço `Negociacoes`, que as aplica sobre as portas de `app/portas.py`.
- `app/contratos.py`: validação dos eventos consumidos e montagem dos publicados. `app/http.py`: rotas, JWT, RFC 7807, correlação e métricas.
- `app/main.py`: `create_app`, `executar_prazos` e `criar_app_real`.
- `app/adaptadores/`: PostgreSQL (psycopg 3, pool ≤ 5), Kafka (outbox, publicador, consumidor, DLT) e JWKS.
- `migracoes/versions/0001_negociacao.py`: schema `negociacao`, executado com a role `portal_ddl`.

## Testes

Execute na pasta `services/negociacao-service`. A etapa `test` da imagem roda a suíte em Python 3.12 e o build falha se algum teste falhar. Os contratos de `docs/negociacao/contratos` entram como contexto adicional:

```bash
docker build --target test --build-context contratos=../../docs/negociacao/contratos -t negociacao-teste .
```

## Demonstração local

Subir banco, Redpanda com os 7 tópicos, migração, emissor de tokens de teste, duas réplicas, gateway e Redpanda Console:

```bash
docker compose -f compose.demo.yml up -d --build --wait gateway console
```

O `--wait` só termina quando as duas réplicas respondem `/health/ready`. O `demo/init.sql` cria as tabelas de exemplo do kit no schema `negociacao` para a migração provar que as apaga (D45). Os dados e as credenciais valem só para a demonstração. O emissor gera uma chave nova a cada subida: se reiniciar só o `identity`, reinicie também `app1` e `app2`, que guardam o JWKS em cache por 1 h.

- API e `/docs` em [localhost:18106/docs](http://localhost:18106/docs).
- Redpanda Console em [localhost:18086](http://localhost:18086).
- Prontidão em [localhost:18106/health/ready](http://localhost:18106/health/ready) e métricas em [localhost:18106/metrics](http://localhost:18106/metrics).

Para parar mantendo os dados, ou para apagar tudo, inclusive os volumes:

```bash
docker compose -f compose.demo.yml stop
```

```bash
docker compose -f compose.demo.yml down -v
```

## Roteiro da apresentação

**1. Subir a demo** com o comando da seção anterior.

**2. Abrir as negociações diretas.** O script `popular` faz o papel do Mercado: publica dois `modo_negociacao_definido`, uma direta comum e uma compra imediata, e espera as duas aparecerem na API.

```bash
docker compose -f compose.demo.yml run --build --rm --no-deps popular
```

Ele imprime os dois `negociacaoId` e um token de 2 h para o Comprador A, o Fornecedor X e o Comprador B, que não participa. Deixe essa saída aberta para copiar.

**3. Evento no Console:** em **Topics** → `pedido_temporario_solicitado`, há uma mensagem por negociação, com a chave igual ao `negociacaoId`, `tipo` `VENDA_DIRETA` e o preço da oferta.

**4. Authorize no `/docs`:** clique em **Authorize**, cole o token do Comprador A (sem a palavra `Bearer`) e confirme.

**5. Consultar a direta comum** (Comprador A): `GET /api/v1/negociacoes/{negociacaoId}` mostra `status` `ABERTO`, `vez` `BUYER`, `reservaSituacao` `PENDENTE` e `acoesPermitidas` `["PROPOR", "RECUSAR"]`. `GET /api/v1/negociacoes/{negociacaoId}/propostas` mostra a proposta 1 do fornecedor, pelo preço da oferta. Com o token do Comprador B, a mesma rota responde `404 NEGOCIACAO_NAO_ENCONTRADA`.

**6. Confirmar a reserva da direta comum.** O script faz o papel do Pedidos: lê o `pedido_temporario_solicitado` da negociação e responde com `pedido_atualizado` `CONFIRMADA`.

```bash
docker compose -f compose.demo.yml run --rm --no-deps popular python demo/popular.py confirmar <negociacaoId>
```

Consultando de novo, `reservaSituacao` fica `CONFIRMADA` e `acoesPermitidas` ganha `ACEITAR`.

**7. Confirmar a reserva da compra imediata** com o mesmo comando e o outro `negociacaoId`. A negociação fecha sozinha como `ENCERRADO`, com `acoesPermitidas` vazio, e o Console mostra o `negociacao_fechada` com a alocação de 10.00 × 130.00 = 1300.00. Para ver a recusa, abra outro cenário (passo 2) e use `recusar` no lugar de `confirmar`: a direta vai a `RECUSADO`.

**8. DLT:** mensagem que não segue o contrato vai para a `negociacao.dlt`, com o motivo em `detalhe`:

```bash
echo '{"eventId": "E-62", "payload": {}}' | docker compose -f compose.demo.yml exec -T broker rpk topic produce modo_negociacao_definido -k demo
```

## Conectar ao kit da turma

O [override do Compose](compose.negociacao.yml) troca o build de `app1-negociacao` e `app2-negociacao` por este projeto e acrescenta `negociacao-migrate`; as réplicas só sobem depois da migração. Informe `NEGOCIACAO_PROJECT_DIR` (caminho absoluto desta pasta), `NEGOCIACAO_DATABASE_URL` (role `svc_negociacao`) e `NEGOCIACAO_MIGRATION_DATABASE_URL` (role `portal_ddl`), como em [.env.example](.env.example). O tópico `pedido_temporario_solicitado` ainda precisa ser criado no kit (issue #3 do kit, aberta pelo Pedidos).
