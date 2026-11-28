# Pedidos do Portal B2B

Implementação da [ESP-PED-001 v2.2](../../docs/pedidos/requisitos.md). Para cada oferta de uma negociação aberta, o serviço cria um pedido temporário, sem comprador, e acompanha a reserva no Fornecimento. No fechamento, conclui a venda dos pedidos alocados e cancela os demais. Depois, o comprador informa o destino, e os eventos da Logística levam o pedido até `ENTREGUE`.

| Item | Onde |
|---|---|
| Portas (`EventBus`, `Clock`, `PedidosRepositorio`, `ChavesPublicas`) | `app/portas.py` |
| `create_app`, `reconciliar` e `criar_app_real` | `app/main.py` |
| Regras de cada tópico e da API-07 | `app/servico.py` e `app/dominio.py` |
| Validação de eventos e montagem das saídas | `app/contratos.py` |
| Rotas, JWT e erros RFC 7807 | `app/http.py` |
| Postgres, Kafka (outbox, consumidor e DLT) e JWKS | `app/adaptadores/` |
| Migração do schema `pedidos` | `migracoes/versions/0001_pedidos.py` |

## Testes de aceitação

A suíte roda em memória, com dublês das portas, e cobre os 39 critérios de aceitação da ESP.

```powershell
python -m pip install -r requirements.txt -r requirements-test.txt
python -m pytest -q
```

## Apresentação em um comando

Com o Docker Desktop aberto, no `cmd` ou no PowerShell, rode na pasta do serviço:

```powershell
.\apresentar
```

O roteiro sobe a stack e percorre o ciclo do pedido em 9 etapas, com uma pausa em cada uma:

1. ambiente;
2. projeções;
3. pedido temporário;
4. reserva;
5. fechamento;
6. destino;
7. frete;
8. entrega;
9. segurança e DLT.

Ele publica no Kafka os eventos de teste dos parceiros (PROF-05) e mostra pela API o efeito de cada um. Opções:

- `-SemPausa`: roda direto;
- `-Reiniciar`: apaga os dados antes de começar.

Se o Docker Desktop fechar ao abrir com "An unexpected error occurred", rode `.\consertar-docker`. Esse erro costuma aparecer depois de reiniciar o Windows e vem de um socket antigo que o Docker não consegue apagar sozinho. O script move as pastas desse socket e abre o Docker de novo.

## Simulador com menu

Para escolher caso a caso o que acontece, rode na pasta do serviço:

```powershell
.\simular
```

Cada opção do menu publica o evento que o parceiro publicaria e mostra na hora os pedidos e a auditoria. As opções são:

- abrir leilão ou venda direta;
- confirmar ou recusar a reserva;
- encerrar com vencedor;
- leilão deserto;
- fechamento incompatível;
- destino;
- frete, troca de transportadora e frete indisponível;
- entrega;
- histórico;
- quem vê o quê;
- evento inválido e DLT;
- derrubar e religar a réplica `app1`, para mostrar que a `app2` segue sozinha.

Para ensaiar sem digitar, passe a sequência de respostas com `-Passos`, por exemplo `.\simular -Passos "1,3,T,5"`. Use `-` para aceitar o valor padrão.

## Consultar e simular erros pelo Swagger

1. Pegue um token pronto em [localhost:18100/tokens](http://localhost:18100/tokens). Há tokens para:
   - comprador;
   - fornecedor F1;
   - outro comprador;
   - transportadora;
   - login sem contexto;
   - suporte;
   - token expirado.
2. Abra [localhost:18107/docs](http://localhost:18107/docs), clique em **Authorize**, cole o token e confirme.
3. Em cada rota, use **Try it out** e depois **Execute**. Para trocar de perfil, volte em **Authorize**, clique em **Logout** e cole outro token.

## Rodar no Docker

Execute na pasta do serviço, com o Docker Desktop iniciado.

```powershell
docker compose -f compose.demo.yml up -d --build gateway console
```

O Compose sobe:

- PostgreSQL 16;
- Redpanda, com os 14 tópicos;
- a migração;
- um emissor de JWT de teste;
- duas réplicas de Pedidos;
- o gateway Nginx em [localhost:18107](http://localhost:18107/docs);
- o Redpanda Console em [localhost:18091](http://localhost:18091), para ver as mensagens de cada tópico;
- o PostgreSQL em `localhost:15433` (banco `portal_b2b`, usuário `demo_admin`, senha `pedidos_demo_admin`), para abrir no DBeaver ou no pgAdmin.

Os dados e as credenciais valem só para a demonstração.

Para semear uma negociação com os eventos de teste dos parceiros (PROF-05), rode o comando abaixo. Ele leva P1 e P2 a `VENDA_CONCLUIDA` e P3 a `CANCELADO`, e imprime os IDs e um token de comprador.

```powershell
docker compose -f compose.demo.yml run --build --rm semear
```

Para testar o fluxo inteiro (temporário, reserva, fechamento, destino, frete, entrega e DLT) pelo gateway:

```powershell
docker compose -f compose.demo.yml run --build --rm teste
```

Rotas da API:

- `GET /api/v1/pedidos`;
- `GET /api/v1/pedidos/{id}`;
- `GET /api/v1/pedidos/{id}/historico`;
- `PUT /api/v1/pedidos/{id}/destino`;
- `/health`, `/health/ready` e `/metrics`.

Para parar o ambiente, use `docker compose -f compose.demo.yml stop`. Para apagar os dados, use `down -v`.

## Conectar ao kit da turma

O [override do Compose](compose.pedidos.yml) aponta `app1-pedidos` e `app2-pedidos` para este projeto. Use as variáveis de [.env.example](.env.example), o schema `pedidos`, a role de aplicação `svc_pedidos` e a role de migração fornecida pela Infra.

A migração começa com `DROP TABLE IF EXISTS` das tabelas de exemplo que o `database/01-init.sql` do kit cria no schema `pedidos`.

## Deploy na AWS (ambiente de integração do kit)

O workflow `.github/workflows/deploy-pedidos.yml`, na raiz do repositório do grupo, segue o modelo do kit. Ele constrói a imagem a partir de `services/pedidos-service` e publica em `app1` e `app2` a cada push na `main` que mexa nessa pasta. Também dá para rodar manualmente, em **Actions → Run workflow**.

O container lê as variáveis que o `docker-compose.aws.yml` do kit injeta: `DATABASE_URL` e `KAFKA_BOOTSTRAP_SERVERS`.

| Variável | Sem ela |
|---|---|
| `JWKS_URL` | Usa `http://usuarios-service:8101/.well-known/jwks.json` (rede interna da AWS) |
| `MIGRATION_DATABASE_URL` (role `portal_ddl`) | O container não migra; a Infra precisa aplicar a migração `0001` |
| `KAFKA_GROUP_ID` | `pedidos-service-v1` |

Quando `MIGRATION_DATABASE_URL` existe, o container roda `alembic upgrade head` antes de subir. Uma trava no banco serializa as duas réplicas.

O `/health/ready` só responde 200 com os 14 tópicos do Pedidos criados no broker.
