# Pedidos do Portal B2B

O primeiro fluxo recebe `negociacao_fechada` pelo Kafka, combina o resultado com as projeções de produto e fornecimento e grava o pedido em `CRIADO`. A mesma transação registra histórico, auditoria e `pedido_criado` na outbox. A API consulta o pedido, e o `POST` de recuperação usa a mesma unicidade por negociação.

A [especificação](../../docs/pedidos/requisitos.md) descreve os marcos seguintes, incluindo confirmação de estoque, frete, conclusão comercial e entrega.

A [validação de 12/11/2026](validacao/relatorio.md) registra os testes executados, as correções e a cobertura dos 23 critérios. O primeiro fluxo está implementado; estoque, frete e conclusão ainda são os próximos marcos.

## Rodar a demonstração

Execute na pasta `services/pedidos-service`, com o Docker Desktop iniciado.

```powershell
docker compose -f compose.demo.yml up -d --build gateway
docker compose -f compose.demo.yml run --build --rm --no-deps teste python -m pytest -q -s
```

O Compose sobe PostgreSQL 16, Redpanda, duas réplicas de Pedidos e o gateway Nginx. O teste publica exemplos dos eventos dos parceiros, valida JWT RS256 emitido por um serviço de teste interno e consulta a API pelo gateway. Os dados e as credenciais são exclusivos da demonstração.

- Prontidão em [localhost:18107/health/ready](http://localhost:18107/health/ready).
- Documentação HTTP em [localhost:18107/docs](http://localhost:18107/docs).
- O teste imprime o UUID do pedido e o ID da mensagem publicada. Ele também verifica a contagem no banco, o histórico, a visibilidade por empresa, a negociação deserta e o envio de mensagem inválida para `pedidos.dlt`.

Para pausar o ambiente preservando os dados, execute `docker compose -f compose.demo.yml stop`.

## Conectar ao kit da turma

O [override do Compose](compose.pedidos.yml) aponta `app1-pedidos` e `app2-pedidos` para este projeto. Use as variáveis de [.env.example](.env.example), o schema `pedidos`, a role de aplicação `svc_pedidos` e a role de migração fornecida pela Infra. Os comandos estão na seção 3 da especificação. O marco demonstrado usa os contratos de trabalho de Pedidos. A homologação com os produtores e consumidores reais depende dos acordos da seção 13.
