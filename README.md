# Portal B2B Distribuído

Projeto da disciplina de Sistemas Distribuídos. Este repositório reúne os serviços de Mercado, Negociação/Leilão e Pedidos do grupo.

## Situação atual

| Serviço | Responsabilidade | Situação |
| --- | --- | --- |
| Mercado | Cruzar ofertas e demandas e encaminhar o resultado para Negociação. | Contribuição em preparação. Caminho previsto: `services/mercado-service/`. |
| Negociação/Leilão | Conduzir a negociação e publicar o resultado com o vencedor. | Contribuição em preparação. Caminho previsto: `services/negociacao-service/`. |
| [Pedidos](services/pedidos-service/) | Criar um pedido a partir do resultado da negociação e manter sua consulta e seu histórico. | Primeiro fluxo implementado e demonstrado localmente. |

## Fluxo de Pedidos

Pedidos consome `negociacao_fechada`, combina o resultado com os dados de produto e fornecimento e grava o pedido em `CRIADO`. A mesma transação registra o histórico e o evento `pedido_criado` na outbox. O publicador envia o evento ao Redpanda, e a API permite consultar o pedido pelo gateway.

A demonstração roda com FastAPI, PostgreSQL 16, Redpanda, Nginx e duas réplicas de Pedidos. Os eventos de outros serviços e a autenticação vêm de componentes de teste. A homologação com os serviços da turma está descrita na especificação.

## Estrutura

```text
.
├── docs/
│   └── pedidos/
│       ├── requisitos.md
│       └── historico/
└── services/
    └── pedidos-service/
        ├── app/
        ├── demo/
        ├── migracoes/
        ├── tests/
        ├── compose.demo.yml
        └── compose.pedidos.yml
```

As próximas contribuições podem seguir a convenção `services/<serviço>-service/` para código e `docs/<serviço>/` para documentação.

## Executar a demonstração

Com Docker e Compose disponíveis, execute na raiz do repositório:

```powershell
cd services/pedidos-service
docker compose -f compose.demo.yml up -d --build gateway
docker compose -f compose.demo.yml run --build --rm --no-deps teste python -m pytest -q -s
```

O teste publica uma negociação fechada, verifica a reentrega sem duplicação e consulta o pedido na API. Consulte a [documentação HTTP local](http://localhost:18107/docs) e o [guia de execução e integração](services/pedidos-service/README.md).

## Documentação

- [Especificação de Pedidos (ESP-PED-001)](docs/pedidos/requisitos.md)
- [Plano de implementação e integração inicial](docs/pedidos/historico/plano-de-implementacao.md)
- [Rascunho VIES original](docs/pedidos/historico/requisitos-vies.md) e [requisitos iniciais em PDF](docs/pedidos/historico/requisitos-pedidos.pdf)
