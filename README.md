# Portal B2B Distribuído

Projeto da disciplina de Sistemas Distribuídos. Este repositório reúne os serviços de Mercado, Negociação/Leilão e Pedidos do grupo.

## Situação atual

| Serviço | Responsabilidade | Situação |
| --- | --- | --- |
| [Mercado](docs/mercado/) | Calcular oferta × demanda por produto, mostrar a vitrine pelo menor preço, abrir a negociação direta e iniciar leilões em lote quando a demanda supera a oferta. | Especificação ESP-MRC-001 v0.11 no modelo ERS-LLM do QA, com contratos de eventos. Código em `services/mercado-service/`, com 201 testes e demonstração local. |
| [Negociação/Leilão](docs/negociacao/) | Conduzir os leilões que o Mercado inicia e as negociações diretas e publicar o resultado para Pedidos e Mercado. | Especificação ESP-NEG-001 v1.5 no modelo ERS-LLM do QA, com contratos de eventos. Código em `services/negociacao-service/`, com 186 testes e demonstração local. |
| [Pedidos](docs/pedidos/) | Criar o pedido temporário que reserva o estoque na abertura da negociação, efetivar ou cancelar no fechamento e acompanhar destino, frete e entrega. | Especificação ESP-PED-001 v2.2 no modelo ERS-LLM do QA, com contratos de eventos. Código do primeiro fluxo (v1.1) em `services/pedidos-service/`, a adaptar à v2.2. |

## Fluxo de Pedidos

Na abertura de um leilão ou de uma negociação direta, Pedidos cria um pedido temporário, sem comprador, para cada oferta. O `pedido_criado` desse pedido faz o Fornecimento reservar o estoque. No fechamento (`negociacao_fechada`), os pedidos vendidos vão a `VENDA_CONCLUIDA` (baixa do estoque) e os demais a `CANCELADO` (liberação). Depois, o comprador informa o destino, a Logística cota e seleciona o frete, e a entrega encerra o pedido. Detalhes na [visão geral de Pedidos](docs/pedidos/README.md).

A demonstração do primeiro fluxo (v1.1) roda com FastAPI, PostgreSQL 16, Redpanda, Nginx e duas réplicas de Pedidos. Os eventos de outros serviços e a autenticação vêm de componentes de teste.

## Estrutura

```text
.
├── docs/
│   ├── mercado/
│   │   ├── README.md
│   │   ├── requisitos.md
│   │   └── contratos/
│   ├── negociacao/
│   │   ├── README.md
│   │   ├── requisitos.md
│   │   ├── contratos/
│   │   └── adr/
│   └── pedidos/
│       ├── README.md
│       ├── requisitos.md
│       └── contratos/
└── services/
    └── pedidos-service/
        ├── app/
        ├── demo/
        ├── migracoes/
        ├── tests/
        ├── compose.demo.yml
        └── compose.pedidos.yml
```

Cada serviço segue a mesma convenção: código em `services/<serviço>-service/` e documentação em `docs/<serviço>/`, com `README.md`, `requisitos.md` (especificação) e `contratos/` (schema e exemplo de cada tópico).

## Executar a demonstração

Com Docker e Compose disponíveis, execute na raiz do repositório:

```powershell
cd services/pedidos-service
docker compose -f compose.demo.yml up -d --build gateway
docker compose -f compose.demo.yml run --build --rm --no-deps teste python -m pytest -q -s
```

O teste publica uma negociação fechada, verifica a reentrega sem duplicação e consulta o pedido na API. Consulte a [documentação HTTP local](http://localhost:18107/docs) e o [guia de execução e integração](services/pedidos-service/README.md).

## Documentação

- [Mercado: visão geral, fronteiras e pendências](docs/mercado/README.md)
- [Especificação de Mercado (ESP-MRC-001 v0.11, ERS-LLM)](docs/mercado/requisitos.md) e [contratos de eventos](docs/mercado/contratos/)
- [Negociação e Leilão: visão geral, fronteiras e pendências](docs/negociacao/README.md)
- [Especificação de Negociação e Leilão (ESP-NEG-001 v1.5, ERS-LLM)](docs/negociacao/requisitos.md) e [contratos de eventos](docs/negociacao/contratos/)
- [Pedidos: visão geral, fronteiras e pendências](docs/pedidos/README.md)
- [Especificação de Pedidos (ESP-PED-001 v2.2, ERS-LLM)](docs/pedidos/requisitos.md) e [contratos de eventos](docs/pedidos/contratos/)
