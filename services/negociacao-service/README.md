# Negociação e Leilão do Portal B2B

Implementação da [ESP-NEG-001](../../docs/negociacao/requisitos.md). Esta versão é o esqueleto: a estrutura e as portas do §5, os adaptadores reais (PostgreSQL, outbox e consumidor do Kafka, JWKS), a migração 0001 com as tabelas do §6.1 e os endpoints técnicos `/health`, `/health/ready` e `/metrics`. As regras de negócio entram nas próximas partes.

## Rodar a demonstração

Execute na pasta `services/negociacao-service`, com o Docker iniciado.

```bash
docker compose -f compose.demo.yml up -d --build --wait gateway
```

O Compose sobe PostgreSQL 16, Redpanda com os 7 tópicos do serviço, a migração, duas réplicas e o gateway Nginx. O `--wait` só termina quando as duas réplicas respondem `/health/ready`. O `demo/init.sql` cria as tabelas de exemplo do kit no schema `negociacao` para a migração provar que as apaga (D45). Os dados e as credenciais valem só para a demonstração.

- Prontidão em [localhost:18106/health/ready](http://localhost:18106/health/ready).
- Métricas em [localhost:18106/metrics](http://localhost:18106/metrics).

Para parar preservando os dados, execute `docker compose -f compose.demo.yml stop`.

## Conectar ao kit da turma

O [override do Compose](compose.negociacao.yml) aponta `app1-negociacao` e `app2-negociacao` para este projeto. Use as variáveis de [.env.example](.env.example), o schema `negociacao`, a role de aplicação `svc_negociacao` e a role de migração `portal_ddl`. O tópico `pedido_temporario_solicitado` ainda precisa ser criado no kit (INT-NEG-05).
