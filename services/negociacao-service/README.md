# Negociação e Leilão do Portal B2B

Implementação da [ESP-NEG-001](../../docs/negociacao/requisitos.md). Esta versão abre a negociação direta pelo `modo_negociacao_definido` do Mercado, pede a reserva ao Pedidos (`pedido_temporario_solicitado`) e registra a resposta (`pedido_atualizado`). A compra imediata fecha sozinha quando a reserva confirma, e a reserva recusada fecha a direta como `RECUSADO`. Pela API, com o token do Identity, as partes consultam as negociações e as propostas (API-04, API-05 e API-08), fazem contraproposta, aceitam e recusam (API-09 a API-11); sem resposta em 48 h, a direta vai a `EXPIRADO`. As escritas aceitam `Idempotency-Key`. O leilão é aberto pelo `leilao_iniciado`, pede uma reserva por oferta e libera os lances quando todas respondem, recalculando o lote se alguma oferta foi recusada, ou fecha `DESERTO` se nenhuma confirmou. Com o leilão aberto, os compradores dão lances pela API (API-07), cada um acima do melhor lance mais o incremento, e consultam os lances com o sigilo do comprador (API-06); cada lance aceito publica `lance_realizado`. Em `dataFim`, o agendador encerra o leilão: com lance, `ENCERRADO` para o autor do melhor lance, com o valor rateado entre as ofertas na proporção do `valorItem`; sem lance, ou com reserva ainda pendente, `DESERTO`. Mensagens repetidas não repetem efeito, e as inválidas vão para a `negociacao.dlt`.

## Estrutura

- `app/dominio.py`: regras da negociação direta (vez, prazo, teto da proposta, ações permitidas, visibilidade, alocação) e do leilão (recálculo do lote, participações, lance mínimo, conflito de interesse, sigilo, rateio do lance vencedor), idempotência das escritas e o serviço `Negociacoes`, que as aplica sobre as portas de `app/portas.py`.
- `app/contratos.py`: validação dos corpos HTTP e dos eventos consumidos e montagem dos publicados. `app/http.py`: rotas, JWT, RFC 7807, correlação e métricas.
- `app/main.py`: `create_app`, `executar_prazos` e `criar_app_real`.
- `app/adaptadores/`: PostgreSQL (psycopg 3, pool ≤ 5), Kafka (outbox, publicador, consumidor, DLT) e JWKS.
- `migracoes/versions/0001_negociacao.py`: schema `negociacao`, executado com a role `portal_ddl`.

## Testes

Execute na pasta `services/negociacao-service`. A etapa `test` da imagem roda a suíte em Python 3.12 e o build falha se algum teste falhar. Os contratos de `docs/negociacao/contratos` entram como contexto adicional:

```bash
docker build --target test --build-context contratos=../../docs/negociacao/contratos -t negociacao-teste .
```

O `tests/test_fluxo_real.py` só roda contra a demonstração (seção seguinte), com ela no ar:

```bash
docker compose -f compose.demo.yml run --build --rm --no-deps teste
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

**6. Contraproposta do comprador** (Comprador A): `POST /api/v1/negociacoes/{negociacaoId}/propostas` com `{"precoUnitario": 125.00}` e `Idempotency-Key` `demo-1` responde 201 com a proposta 2. O detalhe passa a mostrar `precoAtual` 125.00, `vez` `SUPPLIER` e `expiraEm` renovado para 48 h depois. Para mostrar as regras:

- antes dela, `{"precoUnitario": 130.01}` responde `422 PROPOSTA_ACIMA_DA_OFERTA`: nenhuma parte propõe acima do preço da oferta;
- reenviar o mesmo corpo com `demo-1` devolve a mesma proposta, sem criar outra; com `{"precoUnitario": 124.00}` e `demo-1`, a resposta é `409 IDEMPOTENCIA_CONFLITO`;
- sem chave, uma nova proposta do Comprador A responde `409 FORA_DA_VEZ`.

**7. Aceite antes da reserva:** com o token do Fornecedor X (Authorize de novo), `POST /api/v1/negociacoes/{negociacaoId}/aceitar` responde `409 RESERVA_PENDENTE`.

**8. Confirmar a reserva da direta comum.** O script faz o papel do Pedidos: lê o `pedido_temporario_solicitado` da negociação e responde com `pedido_atualizado` `CONFIRMADA`.

```bash
docker compose -f compose.demo.yml run --rm --no-deps popular python demo/popular.py confirmar <negociacaoId>
```

Consultando de novo, `reservaSituacao` fica `CONFIRMADA` e as `acoesPermitidas` do fornecedor ficam `["PROPOR", "ACEITAR", "RECUSAR"]`.

**9. Aceite do fornecedor** (Fornecedor X): `POST /api/v1/negociacoes/{negociacaoId}/aceitar`, sem corpo, responde 200 com `status` `ENCERRADO`, `encerradoEm` preenchido e `acoesPermitidas` vazio. Um segundo aceite responde `409 NEGOCIACAO_ENCERRADA`.

**10. Evento no Console:** em **Topics** → `negociacao_fechada`, a mensagem com a chave da negociação traz `status` `ENCERRADO`, o `compradorId` e uma alocação com o `pedidoId` da reserva, 10.00 × 125.00 = 1250.00.

**11. Compra imediata:** qualquer contraproposta, aceite ou recusa na outra negociação responde `409 COMPRA_IMEDIATA`. Confirme a reserva dela com o comando do passo 8 e o outro `negociacaoId`: ela fecha sozinha como `ENCERRADO` e o Console mostra o `negociacao_fechada` com 10.00 × 130.00 = 1300.00.

**12. Recusa e prazo:** abra outro cenário (passo 2). `POST .../recusar` de qualquer participante, mesmo fora da vez, fecha a direta como `RECUSADO`, e o `negociacao_fechada` sai sem alocações; `recusar` no lugar de `confirmar` no passo 8 tem o mesmo efeito pela reserva. A direta sem resposta por 48 h vai a `EXPIRADO` pelo agendador, com o `correlationId` da abertura.

**13. DLT:** mensagem que não segue o contrato vai para a `negociacao.dlt`, com o motivo em `detalhe`:

```bash
echo '{"eventId": "E-62", "payload": {}}' | docker compose -f compose.demo.yml exec -T broker rpk topic produce modo_negociacao_definido -k demo
```

**14. Abrir o leilão.** O script faz o papel do Mercado e publica um `leilao_iniciado` com 2 ofertas: A (5.00 × 120.00 = 600.00, participação 0.2353) e B (15.00 × 130.00 = 1950.00, participação 0.7647), preço inicial 2550.00 e incremento 25.50.

```bash
docker compose -f compose.demo.yml run --rm --no-deps popular python demo/popular.py leilao
```

Ele imprime o `negociacaoId`, os dois `fornecimentoId` e tokens do Comprador A, do Comprador B, do Fornecedor A (dono da oferta A, com escopo `SUPPLIER` e também `BUYER`) e do Fornecedor X (fora do leilão). `GET /api/v1/negociacoes/{negociacaoId}` mostra `status` `AGUARDANDO_RESERVAS`, as duas ofertas `PENDENTE` e `acoesPermitidas` vazio. No Console, `pedido_temporario_solicitado` tem uma mensagem por oferta, com `tipo` `LEILAO`, na ordem das ofertas.

**15. Confirmar uma oferta e recusar a outra**, como o Pedidos:

```bash
docker compose -f compose.demo.yml run --rm --no-deps popular python demo/popular.py confirmar <negociacaoId> <fornecimentoId da oferta A>
docker compose -f compose.demo.yml run --rm --no-deps popular python demo/popular.py recusar <negociacaoId> <fornecimentoId da oferta B>
```

Depois da primeira, o leilão continua `AGUARDANDO_RESERVAS`. Depois da segunda, fica `ABERTO` com o lote recalculado sem a oferta B: `quantidadeTotal` 5.00, `precoInicial` 600.00, `incrementoMinimo` 6.00 (1% de 600.00) e a oferta A com participação 1.0000; a oferta B continua na lista como `RECUSADA`. O Comprador A vê `acoesPermitidas` `["DAR_LANCE"]`; o Fornecedor A e o Fornecedor X, `[]`.

**16. Lances** em `POST /api/v1/negociacoes/{negociacaoId}/lances`, com o lote do passo 15 (preço inicial 600.00, incremento 6.00). O lance é o valor total pelo lote:

- Comprador A, `{"valor": 599.99}` → `422 LANCE_INSUFICIENTE`: o primeiro lance precisa chegar ao preço inicial;
- Comprador A, `{"valor": 600.00, "demandaId": "demanda-a"}` com `Idempotency-Key` `lance-1` → 201 com `sequencia` 1 e o `compradorId` do Comprador A, sem o `demandaId`. Reenviar o mesmo corpo com `lance-1` devolve o mesmo lance, sem criar outro; com `{"valor": 700.00}` e `lance-1`, `409 IDEMPOTENCIA_CONFLITO`;
- Comprador B, `{"valor": 605.99}` → `422 LANCE_INSUFICIENTE`: com lances, o mínimo é o melhor lance mais o incremento (606.00); `{"valor": 606.00}` → 201 com `sequencia` 2;
- Fornecedor A com o token `BUYER`, `{"valor": 700.00}` → `403 CONFLITO_DE_INTERESSE`: quem fornece no leilão não dá lance nele, com a oferta em qualquer situação; com o token `SUPPLIER` de qualquer fornecedor, `403 ACESSO_NEGADO`.

**17. Evento e sigilo:** no Console, **Topics** → `lance_realizado` tem uma mensagem por lance aceito, com a chave igual ao `negociacaoId` e o `compradorId` sem máscara. Em `GET /api/v1/negociacoes/{negociacaoId}/lances`, o Comprador A vê o próprio `compradorId` no lance 1 e `OCULTO` no lance 2; o Comprador B, o contrário; o Fornecedor A vê os dois. No detalhe (`GET /api/v1/negociacoes/{negociacaoId}`), `melhorLance` é o lance 2, com o mesmo sigilo, e `totalLances` é 2. Os valores nunca são ocultados.

**18. Leilão deserto:** abra outro leilão (passo 14) e recuse as duas ofertas. O leilão fica `DESERTO`, com `encerradoEm`, e o Console mostra um `negociacao_fechada` com `status` `DESERTO` e `alocacoes` vazio.

**19. Leilão curto até o encerramento.** Com um número de minutos, o script abre o mesmo leilão com `dataFim` nesse prazo (aqui, 3 minutos) e imprime o `dataFim`:

```bash
docker compose -f compose.demo.yml run --rm --no-deps popular python demo/popular.py leilao 3
```

Confirme as duas ofertas (comandos do passo 15, com `confirmar` nas duas). O leilão fica `ABERTO` com o lote inteiro: preço inicial 2550.00 e incremento 25.50. Dê dois lances antes do `dataFim`: Comprador A com `{"valor": 2550.00}` e Comprador B com `{"valor": 2600.00, "demandaId": "demanda-b"}`.

**20. Encerramento em `dataFim`.** Espere o `dataFim` impresso: as duas réplicas rodam o agendador a cada segundo, mas só uma encerra o leilão. `GET /api/v1/negociacoes/{negociacaoId}` (Comprador B) mostra `status` `ENCERRADO`, `compradorId` do Comprador B, `encerradoEm` e `acoesPermitidas` vazio; um novo lance responde `409 LEILAO_NAO_ABERTO`. No Console, **Topics** → `negociacao_fechada` tem uma única mensagem com a chave do leilão, `status` `ENCERRADO`, o `compradorId`, `demandaId` `demanda-b` e as alocações do rateio, na proporção do `valorItem` (600.00 e 1950.00 de 2550.00), somando o lance vencedor:

- oferta A: 5.00, `valorUnitario` 122.35, `valorTotal` 611.76;
- oferta B: 15.00, `valorUnitario` 132.55, `valorTotal` 1988.24.

O mesmo leilão curto sem nenhum lance, ou com uma oferta sem resposta da reserva, fica `DESERTO` em `dataFim`, e o `negociacao_fechada` sai sem alocações. A reserva que chega depois disso é ignorada.

## Conectar ao kit da turma

O [override do Compose](compose.negociacao.yml) troca o build de `app1-negociacao` e `app2-negociacao` por este projeto e acrescenta `negociacao-migrate`; as réplicas só sobem depois da migração. Informe `NEGOCIACAO_PROJECT_DIR` (caminho absoluto desta pasta), `NEGOCIACAO_DATABASE_URL` (role `svc_negociacao`) e `NEGOCIACAO_MIGRATION_DATABASE_URL` (role `portal_ddl`), como em [.env.example](.env.example). O tópico `pedido_temporario_solicitado` ainda precisa ser criado no kit (issue #3 do kit, aberta pelo Pedidos).
