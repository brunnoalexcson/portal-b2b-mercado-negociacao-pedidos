# Portal B2B Distribuído

Projeto da disciplina de Sistemas Distribuídos. Este repositório reúne os serviços de Mercado, Negociação/Leilão e Pedidos do grupo.

## Situação atual

| Serviço | Responsabilidade | Situação |
| --- | --- | --- |
| Mercado | Cruzar ofertas e demandas e encaminhar o resultado para Negociação. | Contribuição em preparação. Caminho previsto: `services/mercado-service/`. |
| Negociação/Leilão | Conduzir a negociação e publicar o resultado com o vencedor. | Contribuição em preparação. Caminho previsto: `services/negociacao-service/`. |
| [Pedidos](docs/pedidos/) | Criar o pedido temporário que reserva o estoque na abertura da negociação, efetivar ou cancelar no fechamento e acompanhar destino, frete e entrega. | Especificação ESP-PED-001 v2.1 no modelo ERS-LLM do QA, com contratos de eventos. Código do primeiro fluxo (v1.1) em `services/pedidos-service/`, a adaptar à v2.1. |

## Fluxo de Pedidos

Na abertura de um leilão ou de uma negociação direta, Pedidos cria um pedido temporário, sem comprador, para cada oferta. O `pedido_criado` desse pedido faz o Fornecimento reservar o estoque. No fechamento (`negociacao_fechada`), os pedidos vendidos vão a `VENDA_CONCLUIDA` (baixa do estoque) e os demais a `CANCELADO` (liberação). Depois, o comprador informa o destino, a Logística cota e seleciona o frete, e a entrega encerra o pedido. Detalhes na [visão geral de Pedidos](docs/pedidos/README.md).

A demonstração do primeiro fluxo (v1.1) roda com FastAPI, PostgreSQL 16, Redpanda, Nginx e duas réplicas de Pedidos. Os eventos de outros serviços e a autenticação vêm de componentes de teste.

## Estrutura

```text
.
├── docs/
│   └── pedidos/
│       ├── README.md
│       ├── requisitos.md
│       ├── contratos/
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

- [Pedidos: visão geral, fronteiras e pendências](docs/pedidos/README.md)
- [Especificação de Pedidos (ESP-PED-001 v2.1, ERS-LLM)](docs/pedidos/requisitos.md) e [contratos de eventos](docs/pedidos/contratos/)
- [Especificação de Pedidos v2.0 no formato AED](docs/pedidos/historico/ESP-PED-001_v2.0_AED.md)
- [Plano de implementação e integração inicial](docs/pedidos/historico/plano-de-implementacao.md)
- [Rascunho VIES original](docs/pedidos/historico/requisitos-vies.md) e [requisitos iniciais em PDF](docs/pedidos/historico/requisitos-pedidos.pdf)
