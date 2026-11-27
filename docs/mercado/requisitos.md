# ESP-MRC-001 — Especificação de Requisitos: Serviço de Mercado (Matching Engine)

## 0. Metadados

| Campo | Valor |
|---|---|
| Identificador | ESP-MRC-001 |
| Versão | 0.12 |
| Status | Rascunho |
| Serviço / Bounded context | `mercado-service` (microsserviço 5 do Portal B2B Distribuído): Mercado / Matching Engine. Porta `8105`, rotas `/api/v1/mercado`, schema PostgreSQL `mercado`, role `svc_mercado` |
| Autores (PO / Dev / QA) | PO e Dev: Brunno Alexcson (grupo Mercado, Negociação/Leilão e Pedidos). QA: Grupo de Teste e Qualidade (revisão no gate G1) |
| Data | 2026-11-27 |
| Documentos relacionados | Diagrama "Arquitetura do Portal B2B Distribuído" (bloco 5); ESP-PED-001 v2.2 (`docs/pedidos/requisitos.md`); ESP-NEG-001 v1.2 (`docs/negociacao/requisitos.md`); contratos de eventos em `docs/mercado/contratos/`; kit de Infraestrutura (`CONTRACTS.md`, `ORIENTACOES-ARQUITETURA-GRUPOS.md`); contrato de eventos v2.5 do Fornecimento; contratos do grupo de Compra e Demanda (`comum/contratos/`); Identity-Service; modelo ERS-LLM v1.2 do QA |

## 1. Diretivas para a LLM geradora

1. Implemente **exatamente** o que está especificado. Não adicione endpoints, campos, tópicos, perfis ou regras que não estejam neste documento.
2. **Ordem de precedência** quando houver conflito aparente: Catálogo de erros (§10) e Contratos (§7) > Critérios de aceitação (§8) > Regras de negócio (§9) > Requisitos funcionais (§8) > texto descritivo.
3. Nomes de campos, tópicos, códigos de erro, rotas e valores de enumeração são **literais**: use-os byte a byte, com a mesma grafia e caixa.
4. Quando algo não estiver definido, estiver ambíguo ou parecer contraditório, **não suponha nem invente** o comportamento: pare e faça as perguntas no chat antes de prosseguir com a implementação, indicando o trecho deste documento e as opções que você vê, e espere a resposta.
5. Entregue código completo e executável. Proibido: `TODO`, `pass` como implementação, pseudocódigo, trechos omitidos ("...").
6. Grave todos os arquivos completos na estrutura do §5, dentro de `services/mercado-service/`. Na resposta final, liste seus caminhos relativos.
7. A implementação espelha a estrutura de `services/pedidos-service/` do mesmo repositório: domínio (`app/dominio.py`), contratos de entrada e saída (`app/contratos.py`), HTTP (`app/http.py`), ponto de entrada (`app/main.py`), adaptadores `app/adaptadores/postgres.py`, `app/adaptadores/kafka.py` e `app/adaptadores/jwks.py`, e migrações Alembic em `migracoes/`. Espelha as pastas e os módulos; os pontos de entrada (`create_app`, `executar_prazos`, `criar_app_real`) e as portas de `app/portas.py` seguem o §5, porque o código atual do Pedidos é o primeiro fluxo, anterior à ESP-PED-001 v2.1, e ainda cria o `app` no módulo. Os arquivos de `services/pedidos-service/`, `docs/pedidos/` e `docs/negociacao/` são somente leitura.

## 2. Contexto

### 2.1 Objetivo do serviço

Acompanhar, por produto, a oferta disponível e a demanda aberta do portal e decidir como cada compra é negociada: negociação direta entre um comprador e uma oferta escolhida por ele, ou leilão em lote quando a demanda supera a oferta e os fornecedores aceitam a sugestão de leilão.

### 2.2 Posição na arquitetura

O serviço é o bloco 5 do diagrama ("Mercado — Matching Engine": calcula oferta × demanda, define o modo de negociação e inicia leilões quando necessário) e cobre os passos 4 e 5 do fluxo principal do negócio. Ele mantém cópias locais de produtos, ofertas e demandas a partir dos eventos dos serviços de origem e recalcula o balanço do produto a cada evento. Na negociação direta, o comprador escolhe uma oferta na vitrine e pede a negociação a este serviço, que valida e publica `modo_negociacao_definido`. No leilão, os fornecedores aceitam a sugestão de leilão neste serviço; quando o lote fecha (T1), o serviço publica `modo_negociacao_definido` e `leilao_iniciado`. Nos dois casos, o `negociacao-service` abre a negociação com o mesmo `negociacaoId`, pede ao `pedidos-service` o pedido temporário de cada oferta (`pedido_temporario_solicitado`) e conduz propostas e lances; no leilão, os lances só começam com todas as reservas confirmadas (DG-51, DG-52). Ao final, este serviço consome `negociacao_fechada` para atualizar o saldo das demandas.

```text
Produtos       ──[produto_cadastrado, produto_atualizado]─────────────────────────► Mercado
Fornecimento   ──[fornecimento_criado, fornecimento_atualizado, estoque_atualizado]► Mercado
Demanda        ──[demanda_criada, demanda_recorrente_gerada, demanda_cancelada]───► Mercado
Negociação     ──[negociacao_fechada]─────────────────────────────────────────────► Mercado
Portal ─► Gateway Nginx ─REST /api/v1/mercado (JWT do Identity)───────────────────► Mercado
Identity       ──JWKS (HTTP)──────────────────────────────────────────────────────► Mercado

Mercado ──[modo_negociacao_definido]──► Negociação
Mercado ──[leilao_iniciado]───────────► Negociação (pede o pedido temporário de cada item), front-end
Mercado ──[mercado.dlt]───────────────► operação, QA
```

Passos do fluxo principal: (1) o Fornecimento publica as ofertas; (2) o comprador registra a demanda; (3) os eventos chegam ao Kafka; (4) **este serviço calcula oferta × demanda por produto**; (5) **este serviço define o modo**: negociação direta pedida pelo comprador ou leilão formado a partir da sugestão aceita pelos fornecedores; (6) a Negociação abre a negociação, pede o pedido temporário de cada oferta, conduz e define o vencedor; (7) o Pedidos cria, efetiva ou cancela os pedidos; (8–10) Logística e entrega.

## 3. Escopo

### 3.1 Dentro do escopo

- Cópia local de produtos (`produto_cadastrado`, `produto_atualizado`), ofertas (`fornecimento_criado`, `fornecimento_atualizado`, `estoque_atualizado`) e demandas (`demanda_criada`, `demanda_recorrente_gerada`, `demanda_cancelada`).
- Balanço por produto (oferta total × demanda total), recalculado a cada evento que muda ofertas, demandas, aceites ou negociações do produto.
- Vitrine de ofertas do produto ordenada por menor preço (o "leilão reverso" descrito pelo professor), com indicação das ofertas aptas para uma demanda do comprador.
- Abertura de negociação direta pelo comprador, com a oferta e a quantidade escolhidas, validação de aptidão, controle do saldo da demanda e publicação de `modo_negociacao_definido` (`VENDA_DIRETA`), com a opção de compra imediata pelo preço da oferta (RN-35).
- Sugestão de leilão aos fornecedores do produto quando a demanda supera a oferta, acompanhando o balanço em tempo real.
- Formação do lote pela sugestão: aceite, alteração e saída de fornecedores até T1; separação local da quantidade aceita.
- Fechamento do lote em T1 (fim da janela de adesão ou todos os fornecedores elegíveis no lote), cálculo do preço inicial, do incremento mínimo e da participação de cada item, e publicação de `modo_negociacao_definido` (`LEILAO_DIRETO`) e `leilao_iniciado`.
- Consumo de `negociacao_fechada` para encerrar a negociação registrada e abater o saldo das demandas.
- Retirada do balanço das demandas com `prazoDesejado` vencido, sem alterar a situação da demanda (a expiração é regra do Demanda).
- Consultas de balanço, vitrine, demandas, sugestões e negociações, com paginação.
- Autenticação por JWT do Identity-Service, erros RFC 7807, `X-Correlation-Id`, `Idempotency-Key`, `/health`, `/health/ready`, `/metrics`.
- Persistência no schema `mercado`, outbox transacional, consumo idempotente (`processed_events`), DLT `mercado.dlt`, agendador idempotente e execução em duas réplicas.

### 3.2 Fora do escopo (não implementar)

- Conduzir a negociação direta (propostas, contrapropostas, aceite, recusa, expiração) e o leilão (lances, vencedor, encerramento em T2, rateio do valor vencedor): `negociacao-service`.
- Criar, efetivar ou cancelar pedidos e o pedido temporário; destino da entrega no pedido, informado pelo comprador depois da venda: `pedidos-service`.
- Reservar, liberar ou baixar estoque; alterar preço ou saldo de oferta: `fornecimentos-service`.
- Cadastrar produtos, ofertas e demandas; validar se o produto existe no catálogo antes da demanda: serviços de origem.
- Atribuir uma oferta a uma demanda automaticamente: o comprador escolhe a oferta (RN-14). Compra conjunta entre compradores. Lance para parte do lote.
- Preço uniforme sugerido no lote; tamanho mínimo de lote e máximo configurável (há só o limite técnico de 1000 itens e das somas do §4, na RN-21); leilão com mais de um lote do mesmo fornecedor e produto ao mesmo tempo.
- Conversão entre unidades; previsão de demanda futura a partir de recorrências; precificação sugerida.
- Alerta ao fornecedor com preço acima do mercado, comparação com preços externos e avaliação de fornecedores (desejáveis citados pelo professor, fora da v1).
- Notificação ativa (e-mail, SMS, push), SSE ou WebSocket: a sugestão de leilão e o contexto do leilão são só consulta.
- Cancelar negociação ou leilão já definido; papel de administrador da plataforma; consumo de `lance_realizado`, de `pedido_temporario_solicitado`, de `pedido_atualizado`, de `produto_excluido` e de eventos do Identity; trava de auto-negociação por grupo econômico (`grupoId`, DG-54).
- Declarar a demanda expirada, encerrar demandas de produto excluído e converter `prazoDesejado` em data de entrega: regras do Demanda, do Pedidos e do Negociação.
- Pedir o pedido temporário e acompanhar a reserva: a Negociação publica `pedido_temporario_solicitado` por oferta, na direta e no leilão, e retira do lote, antes do primeiro lance, o item com reserva recusada (DG-51, DG-52).

## 4. Glossário

| Termo | Definição precisa |
|---|---|
| Agora | Valor de `clock.now()` (porta `Clock`). O serviço nunca lê o relógio do sistema diretamente. Depois de cada bloqueio (`bloquear_produto`, `bloquear_id_demanda` e a leitura da chave de idempotência, que bloqueia a chave), `agora` é lido de novo, e as verificações de prazo e os instantes gravados a partir daí usam esse valor (DG-69). |
| Instante | Data e hora em UTC, ISO-8601 com milissegundos e sufixo `Z` na saída (ex.: `"2026-11-20T12:00:00.000Z"`). Na entrada, aceita qualquer ISO-8601 **com** fuso e converte para UTC; sem fuso é inválido. |
| Valor monetário | Reais (BRL). Na API e nos eventos: número JSON (nunca string), com no máximo 2 casas decimais e no máximo `99999999999999.99`. Na saída, sempre com exatamente 2 casas. No código, `decimal.Decimal`; `float` é proibido. No banco, `NUMERIC(18,4)`. |
| Quantidade | Número JSON com no máximo 2 casas decimais e no máximo `9999999999.99` (o que cabe em `NUMERIC(12,2)`), na unidade do produto; vale também para a soma das quantidades de um lote (RN-21). Os totais do balanço e o `excessoDemanda` não têm esse limite (DG-64). Saída com exatamente 2 casas. `Decimal` no código, `NUMERIC(12,2)` no banco. |
| Participação | Número JSON entre 0 e 1 com exatamente 4 casas na saída. `Decimal` no código, `NUMERIC(6,4)` no banco. |
| Empresa | Valor do claim `tenant_id` do token. É a identidade usada em `compradorId` e `fornecedorId`. |
| Escopo | Valor do claim `active_scope`: `BUYER` (comprador) ou `SUPPLIER` (fornecedor) nas rotas de negócio. |
| Produto | Registro da cópia local de produtos (`produtoId`, `nome`, `unidade`). A unidade é atributo do produto: ofertas e demandas do mesmo produto usam a mesma unidade. |
| Oferta | Fornecimento de um fornecedor para um produto, com `preco` por unidade e `quantidadeDisponivel`, mantido a partir dos eventos do Fornecimento. |
| Oferta negociável | Oferta com `preco > 0` e `quantidadeDisponivel > 0` (professor: preço ou quantidade zero não aparecem no portal). |
| Quantidade comprometida | Soma das quantidades aceitas por uma oferta em sugestões `EM_FORMACAO` (RN-17). Fica separada localmente até T1. |
| Disponível livre | `max(0, quantidadeDisponivel − quantidade comprometida)` da oferta. As reservas dos pedidos temporários já chegam descontadas em `quantidadeDisponivel` (DG-05). É o que a vitrine mostra e o que pode ser usado em negociação direta ou em novo aceite. |
| Demanda | Necessidade de compra de um comprador para um produto: uma `demanda_criada` com `tipo = "UNICA"` (identificada por `demandaId`) ou uma ocorrência de `demanda_recorrente_gerada` (identificada por `ocorrenciaId`, com `demandaOrigemId` = `demandaId` do evento). O identificador da demanda neste serviço é `demandaId` nos dois casos. |
| Modelo recorrente | `demanda_criada` com `tipo = "RECORRENTE"`. Guardado só para referência; não entra no balanço (RN-08). |
| Saldo | Quantidade da demanda ainda não atendida: `quantidade − Σ quantidades acordadas`. |
| Em negociação | Soma das quantidades da demanda em negociações diretas com `situacao = "ABERTA"`. |
| Saldo negociável | `max(0, saldo − em negociação)`; o estado gravado segue a RN-27 (DG-60). Limite da quantidade de uma nova negociação direta (RN-15). |
| Prazo vencido | `prazoDesejado` não nulo e `prazoDesejado < agora`. Marcado em `prazoVencido` pelo agendador (RN-10). A API-07 e a aptidão comparam com `agora`, mesmo antes da marcação; o balanço usa só a marcação (DG-59). |
| Demanda aberta | Demanda com `situacao = "AGUARDANDO"` e `prazoVencido = false`. |
| Oferta total | Soma do disponível livre das ofertas negociáveis do produto. |
| Demanda total | Soma do saldo negociável das demandas abertas do produto. |
| Balanço | Comparação de oferta total e demanda total do produto, consultável por qualquer comprador ou fornecedor sem identificar demandas nem compradores (DG-50), com uma das situações: `SEM_DEMANDA` (demanda total = 0), `SEM_OFERTA` (demanda total > 0 e oferta total = 0), `OFERTA_SUFICIENTE` (oferta total ≥ demanda total > 0) ou `DEMANDA_EXCEDENTE` (demanda total > oferta total > 0). |
| Excesso de demanda | `demanda total − oferta total`, quando positivo. |
| Gatilho da sugestão | `oferta total > 0` **e** `demanda total > oferta total × (1 + EXCESSO_DEMANDA_MINIMO_PERCENTUAL / 100)`. |
| Oferta apta | Para uma demanda: oferta negociável do mesmo `produtoId`, com disponível livre > 0, `preco ≤ precoMaximo` da demanda (quando não nulo) e `fornecedorId` diferente do `compradorId` da demanda, sendo a demanda aberta (aguardando e sem prazo vencido). |
| Sugestão de leilão | Registro `SugestaoLeilao` (§6.1) de um produto. É também a janela de formação do lote. |
| Aceite | Registro `AceiteLeilao` (§6.1): a decisão de um fornecedor de colocar uma quantidade de uma oferta no lote da sugestão. |
| Fornecedor elegível | Para uma sugestão: empresa dona de oferta negociável do produto com disponível livre > 0, que não tem item em leilão com `situacao = "ABERTA"` do mesmo produto. |
| Janela de adesão | Intervalo entre o primeiro aceite da sugestão e `janelaEncerraEm` = instante do primeiro aceite + `LEILAO_JANELA_ADESAO_MINUTOS`. |
| T1 | Instante em que o lote fecha e o leilão é publicado (RN-21). A partir daí a Negociação pede as reservas e libera os lances quando todas estiverem confirmadas (DG-51). |
| T2 | T1 + duração do leilão: a escolhida pelo fornecedor no primeiro aceite (`duracaoLeilaoHoras`) ou, sem ela, `LEILAO_DURACAO_HORAS` (24 h por padrão) (RN-18, DG-70). Fim dos lances, conduzido pela Negociação. |
| Lote | Conjunto dos itens do leilão (um por aceite em T1). `quantidadeTotal` = soma das quantidades. |
| Preço inicial | `precoInicial` = Σ (`quantidade` × `precoUnitario`) dos itens, com 2 casas. Valor mínimo do primeiro lance. Se um item sair por reserva recusada, a Negociação recalcula pela mesma regra (DG-51). |
| Incremento mínimo | `precoInicial × LEILAO_INCREMENTO_MINIMO_PERCENTUAL / 100`, com 2 casas, nunca menor que 0.01. |
| Negociação | Registro `Negociacao` (§6.1) criado por este serviço: `VENDA_DIRETA` (um comprador, uma oferta) ou `LEILAO_DIRETO` (lote de uma ou mais ofertas). O `negociacaoId` nasce aqui e é usado pela Negociação para abrir a negociação (DG-52). |
| Conflito de interesse | Empresa negociando com oferta própria: abrir negociação direta para demanda da própria empresa com oferta da própria empresa. |
| Compra imediata | Negociação direta aberta com `compraImediata = true` (API-07): o comprador compra pelo preço da oferta, sem troca de propostas. A Negociação encerra a direta sozinha quando a reserva é confirmada (RN-35, DG-58). |

## 5. Restrições técnicas

| Item | Valor obrigatório |
|---|---|
| Linguagem / versão | Python 3.12 |
| Framework | FastAPI com Pydantic 2 e Uvicorn |
| Dependências permitidas | As versões fixadas em `services/pedidos-service/requirements.txt`: `fastapi==0.162.5`, `starlette==1.10.0`, `uvicorn==0.54.0`, `pydantic==2.14.3`, `psycopg[binary]==3.3.9`, `psycopg-pool==3.3.3`, `alembic==1.20.1`, `SQLAlchemy==2.1.8`, `aiokafka==0.15.0`, `PyJWT[crypto]==2.15.1`, `cryptography==50.0.2`, `simplejson==3.20.2`, `prometheus-client==0.26.0`, além da biblioteca padrão. Nenhuma outra. Testes: `httpx==0.28.1` e `pytest==9.2.0` em `requirements-test.txt`, instalados só na etapa `test` da imagem. Os módulos de `app/` fora de `app/adaptadores/` importam somente biblioteca padrão, `fastapi`, `starlette`, `pydantic`, `jwt`, `cryptography`, `simplejson`, `prometheus_client` e o próprio pacote `app`. |
| Persistência | Somente pela porta `MercadoRepositorio` (§5.1). Proibido guardar produtos, ofertas, demandas, sugestões, aceites, negociações, chaves de idempotência ou eventos processados em variáveis de módulo ou da aplicação. |
| Estrutura de arquivos | Ver árvore abaixo. `app/__init__.py`, `app/portas.py` e `app/main.py` são obrigatórios. |
| Ponto de entrada (testes) | `app.main.create_app(event_bus: EventBus, clock: Clock, repositorio: MercadoRepositorio, chaves_publicas: ChavesPublicas, config: ConfigMercado = ConfigMercado(), emissor: str = "identity-service", audiencia: str = "b2b-platform") -> fastapi.FastAPI` e `app.main.executar_prazos(app: fastapi.FastAPI) -> int` (processa janelas de adesão vencidas e demandas cujo prazo venceu em `clock.now()` e devolve quantos registros mudaram). |
| Ponto de entrada (execução) | `uvicorn app.main:criar_app_real --factory --host 0.0.0.0 --port 8105`: monta os adaptadores reais a partir das variáveis de ambiente e inicia, como tarefas de fundo, o consumidor Kafka, o publicador da outbox e o agendador (`executar_prazos` a cada 1 s). |
| Formato de data/hora | Instante (§4). |
| Formato monetário | Valor monetário (§4). Arredondamento sempre explícito, conforme RN-22 e RN-23. |
| Configuração de negócio | `ConfigMercado` (dataclass imutável em `app/portas.py`) com `excesso_demanda_minimo_percentual: Decimal = Decimal("0")`, `leilao_janela_adesao_minutos: int = 60`, `leilao_duracao_horas: int = 24`, `leilao_incremento_minimo_percentual: Decimal = Decimal("1")`. Na execução, lidos de `EXCESSO_DEMANDA_MINIMO_PERCENTUAL`, `LEILAO_JANELA_ADESAO_MINUTOS`, `LEILAO_DURACAO_HORAS` e `LEILAO_INCREMENTO_MINIMO_PERCENTUAL`. Valores inválidos (negativos, janela ou duração < 1) impedem a subida do serviço. |
| Configuração de infraestrutura | `DATABASE_URL` (role `svc_mercado`), `MIGRATION_DATABASE_URL` (role `portal_ddl`), `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_GROUP_ID` (`mercado-service-v1`), `JWKS_URL` (`https://identity.matheus-alves.dev/.well-known/jwks.json` por padrão), `JWT_ISSUER` (`identity-service`), `JWT_AUDIENCE` (`b2b-platform`), `LOG_LEVEL` (`INFO`). A porta é fixa no ponto de entrada (8105, a do kit). Todas documentadas em `.env.example`. **Execução no kit:** O `docker-compose.yml` do kit sobe `app1-mercado` e `app2-mercado` a partir do serviço de exemplo do kit, com `SERVICE_NAME`, `PORT`, `INSTANCE` e `DB_HOST`, que este serviço não usa, e `KAFKA_BOOTSTRAP_SERVERS` com um broker só. O `compose.mercado.yml` é aplicado por cima do Compose do kit (`docker compose -f docker-compose.yml -f <projeto>/services/mercado-service/compose.mercado.yml up -d`), como o `compose.pedidos.yml` do Pedidos: troca o build de `app1-mercado` e `app2-mercado` por este projeto (`MERCADO_PROJECT_DIR`, obrigatório), passa as variáveis acima (`KAFKA_BOOTSTRAP_SERVERS` com padrão `rp-0:9092,rp-1:9092,rp-2:9092`) e acrescenta `mercado-migrate`, que roda `python -m alembic upgrade head` com `MIGRATION_DATABASE_URL`; as duas réplicas só sobem depois que `mercado-migrate` termina com sucesso. Tudo na rede `portal-net` do kit. No kit, `JWKS_URL` é `http://gateway:8080/.well-known/jwks.json` (o gateway repassa ao serviço de usuários), como no Fornecimento e na resposta do Identity (INT-MRC-07). |

```text
services/mercado-service/
├── app/
│   ├── __init__.py
│   ├── portas.py            # Protocols do §5.1, ConfigMercado e RepositorioIndisponivel
│   ├── main.py              # create_app, executar_prazos, criar_app_real
│   ├── dominio.py           # regras puras: aptidão, balanço, sugestão, lote, participação
│   ├── contratos.py         # validação de corpo HTTP e de eventos consumidos; montagem dos eventos publicados
│   ├── http.py              # rotas, autenticação, erros RFC 7807, correlação, idempotência
│   ├── jsonio.py            # JSON com Decimal (simplejson)
│   └── adaptadores/
│       ├── postgres.py      # MercadoRepositorio real (psycopg 3, pool ≤ 5)
│       ├── kafka.py         # EventBus real: outbox, publicador, consumidor, DLT
│       └── jwks.py          # ChavesPublicas real (PyJWKClient, cache de 1 h)
├── migracoes/
│   ├── env.py
│   └── versions/0001_mercado.py, 0002_duracao_leilao.py
├── tests/                   # suíte de aceitação (§8) com dublês das portas, testes do fluxo real no compose.demo.yml e demo_identity.py (emissor de tokens de teste)
├── demo/                    # init.sql (roles svc_mercado e portal_ddl, schema mercado) e nginx.conf (gateway da demonstração)
├── alembic.ini
├── requirements.txt
├── requirements-test.txt    # httpx, pytest e jsonschema, só na etapa test da imagem
├── .env.example
├── .dockerignore            # lista do que entra no build: requirements, app/, tests/, migracoes/, alembic.ini e Dockerfile
├── .gitignore               # .env, .venv/, __pycache__/, .pytest_cache/
├── Dockerfile               # python:3.12-slim em três etapas (deps, test, runtime), usuário 65532, EXPOSE 8105
├── compose.demo.yml         # demonstração local, fora do kit
├── compose.mercado.yml      # override do Compose do kit: app1-mercado, app2-mercado e mercado-migrate
└── README.md
```

### 5.1 Portas (interfaces de infraestrutura)

```python
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Protocol


class RepositorioIndisponivel(Exception):
    """Lançada pelo repositório quando o banco não responde."""


@dataclass(frozen=True)
class ConfigMercado:
    excesso_demanda_minimo_percentual: Decimal = Decimal("0")
    leilao_janela_adesao_minutos: int = 60
    leilao_duracao_horas: int = 24
    leilao_incremento_minimo_percentual: Decimal = Decimal("1")


class EventBus(Protocol):
    def publish(self, topic: str, key: str, message: dict) -> None: ...
    def subscribe(self, topic: str, handler: Callable[[dict], None]) -> None: ...
    def disponivel(self) -> bool: ...


class Clock(Protocol):
    def now(self) -> datetime: ...          # datetime com timezone UTC
    def sleep(self, segundos: float) -> None: ...


class ChavesPublicas(Protocol):
    def obter(self, kid: str) -> object | None: ...   # chave pública RSA do kid, ou None se desconhecido


class MercadoRepositorio(Protocol):
    def transacao(self) -> AbstractContextManager[None]: ...
    def disponivel(self) -> bool: ...
    # consumo idempotente
    def evento_processado(self, source: str, event_id: str) -> bool: ...
    def registrar_evento_processado(self, source: str, event_id: str, topico: str) -> None: ...
    # cópias locais
    def obter_produto(self, produto_id: str) -> dict | None: ...
    def salvar_produto(self, produto: dict) -> None: ...
    def obter_oferta(self, fornecimento_id: str) -> dict | None: ...
    def salvar_oferta(self, oferta: dict) -> None: ...
    def listar_ofertas_do_produto(self, produto_id: str) -> list[dict]: ...
    def obter_demanda(self, demanda_id: str, bloquear: bool = False) -> dict | None: ...
    def salvar_demanda(self, demanda: dict) -> None: ...
    def obter_modelo(self, demanda_id: str) -> dict | None: ...
    def salvar_modelo(self, modelo: dict) -> None: ...
    def obter_cancelamento_antecipado(self, demanda_id: str) -> dict | None: ...
    def salvar_cancelamento_antecipado(self, cancelamento: dict) -> None: ...
    def listar_demandas_abertas(self, produto_id: str) -> list[dict]: ...
    def listar_demandas_por_origem(self, demanda_origem_id: str) -> list[dict]: ...
    def listar_demandas_vencidas(self, agora: datetime, limite: int) -> list[str]: ...
    # produto como unidade de concorrência
    def bloquear_produto(self, produto_id: str) -> None: ...
    def bloquear_id_demanda(self, demanda_id: str) -> None: ...
    def obter_situacao_balanco(self, produto_id: str) -> dict | None: ...
    def salvar_situacao_balanco(self, produto_id: str, situacao: str, situacao_desde: datetime) -> None: ...
    # sugestões e aceites
    def obter_sugestao(self, sugestao_id: str, bloquear: bool = False) -> dict | None: ...
    def obter_sugestao_ativa(self, produto_id: str) -> dict | None: ...
    def salvar_sugestao(self, sugestao: dict) -> None: ...
    def listar_sugestoes_ativas(self) -> list[dict]: ...
    def listar_janelas_vencidas(self, agora: datetime, limite: int) -> list[str]: ...
    def listar_aceites(self, sugestao_id: str) -> list[dict]: ...
    def salvar_aceite(self, aceite: dict) -> None: ...
    def remover_aceite(self, sugestao_id: str, fornecimento_id: str) -> None: ...
    # negociações
    def inserir_negociacao(self, negociacao: dict) -> None: ...
    def obter_negociacao(self, negociacao_id: str, bloquear: bool = False) -> dict | None: ...
    def atualizar_negociacao(self, negociacao: dict) -> None: ...
    def listar_negociacoes(self, empresa: str, escopo: str, modo: str | None, situacao: str | None,
                           offset: int, limite: int) -> tuple[list[dict], int]: ...
    def obter_direta_aberta(self, demanda_id: str, fornecimento_id: str) -> dict | None: ...
    def listar_leiloes_abertos_do_fornecedor(self, produto_id: str, fornecedor_id: str) -> list[dict]: ...
    # idempotência HTTP
    def obter_idempotencia(self, escopo: str) -> dict | None: ...
    def salvar_idempotencia(self, escopo: str, registro: dict) -> None: ...
```

Comportamento das portas:

- `app/portas.py` contém exatamente as definições acima. Quem chama `create_app` fornece as implementações; nos testes, dublês em memória.
- `create_app` chama, durante a criação da aplicação, `event_bus.subscribe` para os tópicos `produto_cadastrado`, `produto_atualizado`, `fornecimento_criado`, `fornecimento_atualizado`, `estoque_atualizado`, `demanda_criada`, `demanda_recorrente_gerada`, `demanda_cancelada` e `negociacao_fechada`, um handler para cada. O handler recebe a mensagem completa (envelope, §7.3) como `dict`. Ele só propaga `RepositorioIndisponivel` (RN-31); qualquer outra exceção é tratada dentro dele.
- `transacao()`: tudo o que é feito no repositório dentro de `with repositorio.transacao():` é gravado de forma atômica; se uma exceção sair do bloco, nada é gravado. Cada requisição HTTP de escrita, cada mensagem consumida e cada registro processado por `executar_prazos` usam uma única transação.
- `bloquear_produto(produto_id)` bloqueia o produto até o fim da transação (no PostgreSQL, `SELECT ... FOR UPDATE` numa linha por produto, criada se não existir). **Toda** escrita que muda ofertas, demandas, aceites, sugestões ou negociações de um produto chama `bloquear_produto` antes de validar regras e recalcular o balanço. É isso que serializa o balanço e a sugestão de um produto entre as duas réplicas. `produto_cadastrado` e `produto_atualizado` também bloqueiam o produto antes de comparar o instante (RN-02).
- `bloquear_id_demanda(demanda_id)` bloqueia um ID de demanda ou de modelo recorrente até o fim da transação (no PostgreSQL, `pg_advisory_xact_lock` sobre o ID). `demanda_criada` bloqueia o `demandaId`; `demanda_recorrente_gerada`, o `demandaId` do modelo e o `ocorrenciaId`; `demanda_cancelada`, o `demandaId`. Sempre antes de `bloquear_produto` e em ordem fixa, e as regras da RN-08 e da RN-09 são conferidas depois do bloqueio (DG-68).
- Os dicionários trocados com o repositório usam as chaves dos atributos do §6.1, com instantes em `datetime` UTC e valores em `Decimal`.
- `listar_ofertas_do_produto` devolve todas as ofertas do produto, negociáveis ou não. `listar_demandas_abertas` devolve as demandas com `situacao = "AGUARDANDO"` do produto, com ou sem prazo vencido (o filtro de "demanda aberta" do §4 é aplicado no domínio), em ordem de `recebidaEm` crescente e, em empate, `demandaId` crescente. `listar_demandas_por_origem` devolve as demandas com aquele `demandaOrigemId`, em qualquer situação.
- `obter_modelo` e `salvar_modelo` leem e gravam o modelo recorrente em `mercado.demanda_modelo` (§6.1), com as chaves `demandaId`, `compradorId`, `produtoId`, `recebidoEm` e `cancelado`. `obter_cancelamento_antecipado` e `salvar_cancelamento_antecipado` leem e gravam em `mercado.cancelamento_antecipado` (`demandaId`, `compradorId`, `canceladoEm`, `recebidoEm`) o `demanda_cancelada` de uma demanda ainda desconhecida (RN-09).
- `obter_situacao_balanco(produto_id)` devolve `{"situacao": ..., "situacaoDesde": ...}` gravados no último recálculo do balanço (linha de `mercado.produto_lock`), ou `None` se o produto nunca teve escrita. `salvar_situacao_balanco` grava esses dois valores (RN-06).
- `listar_demandas_vencidas(agora, limite)` devolve até `limite` `demandaId` com `situacao = "AGUARDANDO"`, `prazoVencido = false`, `prazoDesejado` não nulo e `prazoDesejado < agora`, em ordem de `prazoDesejado`. `listar_janelas_vencidas(agora, limite)` devolve até `limite` `sugestaoId` com `situacao = "EM_FORMACAO"` e `janelaEncerraEm <= agora`, em ordem de `janelaEncerraEm`.
- `obter_sugestao_ativa(produto_id)` devolve a sugestão do produto com `situacao` ∈ {`ABERTA`, `EM_FORMACAO`}, ou `None`. Existe no máximo uma por produto (RN-16). `listar_sugestoes_ativas()` devolve as sugestões `ABERTA` ou `EM_FORMACAO` de todos os produtos, em ordem de `abertaEm` crescente (API-10).
- `listar_negociacoes` devolve `(itens, total)` em ordem de `definidaEm` decrescente e, em empate, `negociacaoId` decrescente. Entram todas as negociações `LEILAO_DIRETO` e as `VENDA_DIRETA` em que (`escopo == "BUYER"` e `compradorId == empresa`) ou (`escopo == "SUPPLIER"` e `fornecedorId` da oferta == empresa). `modo` e `situacao` `None` = sem filtro.
- `obter_direta_aberta` devolve a negociação `VENDA_DIRETA` com `situacao = "ABERTA"` daquela demanda e daquela oferta, ou `None`. `listar_leiloes_abertos_do_fornecedor` devolve as negociações `LEILAO_DIRETO` com `situacao = "ABERTA"` do produto com algum item daquele fornecedor.
- Qualquer método do repositório pode lançar `RepositorioIndisponivel`; o serviço responde `503 DEPENDENCIA_INDISPONIVEL` na API e, no consumo, o handler propaga a exceção (RN-31). Qualquer outra exceção vira `500 ERRO_INTERNO` na API.
- `publish` é chamado **dentro** da transação da mudança, como último passo antes do fim do bloco, e nunca chama handlers de forma síncrona. Ordem de toda escrita: validar → alterar o estado → publicar. Quando um mesmo fato gera dois eventos (`modo_negociacao_definido` e `leilao_iniciado`), `modo_negociacao_definido` é publicado primeiro. As publicações em `mercado.dlt` (RN-31, RN-32) são as únicas chamadas a `publish` fora de uma transação; elas e a propagação de `RepositorioIndisponivel` são as únicas saídas do handler fora de uma transação concluída. Falha lançada por `publish` fica fora da suíte em memória; o que o adaptador real faz nesse caso está no §13.1.
- `clock.now()` sempre devolve `datetime` com fuso; se vier sem fuso, tratar como UTC. `clock.sleep(segundos)` é a única forma de esperar; nos testes, o dublê retorna na hora e registra o valor.
- `chaves_publicas.obter(kid)` devolve a chave pública RSA ou `None` se o `kid` for desconhecido. Se lançar exceção, a resposta é `503 DEPENDENCIA_INDISPONIVEL`.
- `event_bus.disponivel()` e `repositorio.disponivel()` alimentam `/health/ready`.

Comportamento dos adaptadores reais (não usados pela suíte de aceitação em memória):

- **Repositório (`postgres.py`):** psycopg 3 com `psycopg_pool.ConnectionPool(max_size=5)`, role `svc_mercado`, só o schema `mercado`. `transacao()` abre uma conexão do pool numa variável de contexto e faz commit ao sair do bloco sem exceção. São falhas de conexão, convertidas em `RepositorioIndisponivel`: SQLSTATE da classe `08`, `57P01`, `57P02`, `57P03` e `PoolTimeout`; as demais exceções sobem como estão (como no Pedidos).
- **Outbox (`kafka.py`):** dentro da transação, `publish` grava o envelope em `mercado.outbox` pela mesma conexão. Um publicador em cada réplica faz polling a cada 1 s, lê o registro pendente mais antigo que não tenha outro pendente mais antigo com a mesma chave (`NOT EXISTS`), com `FOR UPDATE SKIP LOCKED`, envia com `acks=all`, timeout de 5 s e a chave do registro, marca `confirmado_em` e, em falha, reagenda com espera `min(30, 2^tentativas)` segundos. Todo reenvio usa o mesmo `eventId`. Registros da mesma chave são enviados na ordem de gravação: enquanto o mais antigo estiver pendente, travado ou não, os seguintes da mesma chave esperam. Não há ordem garantida entre tópicos diferentes.
- **Consumidor (`kafka.py`):** `aiokafka` com grupo `mercado-service-v1`, `enable_auto_commit=False`. Bytes que não são JSON ou não são UTF-8 vão direto para `mercado.dlt` (RN-32). O handler roda fora do laço de eventos (`asyncio.to_thread`). Publicações em `mercado.dlt` não passam pela outbox: o adaptador envia direto ao broker, com `acks=all`. O offset é confirmado só depois do retorno do handler e do ACK da DLT, quando houver; se o envio à DLT falhar, o offset não é confirmado e a mensagem é reentregue. Se o handler propagar `RepositorioIndisponivel`, ou se o broker falhar (`KafkaConnectionError`, `KafkaTimeoutError`), o offset não é confirmado e a mensagem é reentregue a cada 5 s, sem limite (RN-31, como no Pedidos).
- **Agendador:** cada réplica chama `executar_prazos` a cada 1 s. A segurança com duas réplicas vem do bloqueio do produto e da nova verificação do estado dentro da transação (RN-33).
- **JWKS (`jwks.py`):** `jwt.PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600, timeout=2)`. Falha de rede lança exceção (→ 503); `kid` ausente no conjunto devolve `None` (→ 401).
- **Logs:** JSON em `stdout` com `timestamp`, `level`, `service` (`mercado-service`), `correlationId`, `message` e, quando conhecidos, `produtoId`, `demandaId`, `negociacaoId` e `eventId` (mesmas chaves da biblioteca comum do kit). Sem `print()`. Nunca registrar o token.
- **Métricas (`/metrics`):** `mercado_http_requisicoes_total` (`rota`, `metodo`, `status`), `mercado_http_latencia_segundos` (histograma, `rota`, `metodo`), `mercado_eventos_consumidos_total` (`topico`, `resultado` ∈ `aplicado`, `duplicado`, `ignorado`, `dlt`), `mercado_outbox_pendentes` e `mercado_sugestoes_ativas`. IDs e tokens nunca são rótulos.
- **Migração (`migracoes/versions/0001_mercado.py`; a `0002_duracao_leilao.py` acrescenta `sugestao_leilao.duracao_leilao_horas`, `integer` nulo e maior que 0, DG-70):** roda com `MIGRATION_DATABASE_URL` (role `portal_ddl`). Começa com `DROP TABLE IF EXISTS` de `mercado.match`, `mercado.modo_negociacao` e `mercado.processed_events`, as tabelas de exemplo que o `database/01-init.sql` do kit cria no schema (DG-56). Depois cria, no schema `mercado`, as tabelas do §6.1 (`produto`, `oferta`, `demanda`, `demanda_modelo`, `sugestao_leilao`, `aceite_leilao`, `negociacao` e `negociacao_item`, com os itens de cada negociação e chave `(negociacao_id, fornecimento_id)`) e as técnicas (`produto_lock`, `processed_events`, `cancelamento_antecipado`, `idempotency_keys` e `outbox`). Colunas em `snake_case`; IDs `VARCHAR(64)` (`negociacao_id` e `sugestao_id` `UUID`), `correlation_id` `VARCHAR(128)`; dinheiro `NUMERIC(18,4)`, quantidade `NUMERIC(12,2)`, participação `NUMERIC(6,4)`, `compra_imediata` `BOOLEAN NOT NULL DEFAULT FALSE`, instantes `TIMESTAMPTZ`, `origem` e `destino` `JSONB`. Exceções: `sugestao_leilao.excesso_demanda` `NUMERIC(18,2)` (DG-64) e `processed_events.source` e `event_id` `TEXT`, porque o `eventId` não tem limite de tamanho. Concede `SELECT, INSERT, UPDATE, DELETE` nessas tabelas e `SELECT` em `alembic_version` a `svc_mercado`, como a migração do Pedidos.

## 6. Modelo de domínio

### 6.1 Entidades

#### Produto (tabela `mercado.produto`, chave `produtoId`)
| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| produtoId | string | Sim | Do evento; 1 a 64 caracteres (DG-55). |
| nome | string | Sim | Do evento; não vazio. |
| unidade | enum | Sim | `KG`, `TON`, `UN`, `CX`, `L`; do evento. |
| atualizadoEm | instante | Sim | `cadastradoEm` ou `atualizadoEm` do evento aplicado (RN-02). |

#### Oferta (tabela `mercado.oferta`, chave `fornecimentoId`)
| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| fornecimentoId | string | Sim | Do evento; 1 a 64 caracteres (DG-55). |
| fornecedorId | string | Sim | Do evento; 1 a 64 caracteres (DG-55). |
| produtoId | string | Sim | Do evento. |
| unidade | enum | Sim | Do evento; igual à do produto (RN-03). |
| preco | valor monetário | Sim | ≥ 0; do evento. |
| quantidadeDisponivel | quantidade | Sim | ≥ 0; do último evento aplicado. |
| origem | objeto `{cidade, uf}` ou `null` | Não | Do evento. |
| criadaEm | instante | Sim | `criadoEm` de `fornecimento_criado`. |
| dadosAtualizadosEm | instante | Sim | Instante do último `fornecimento_*` aplicado aos dados da oferta (RN-01). |
| estoqueAtualizadoEm | instante | Sim | Instante da última `quantidadeDisponivel` aplicada (RN-01). |

#### Demanda (tabela `mercado.demanda`, chave `demandaId`)
| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| demandaId | string | Sim | `demandaId` da demanda única ou `ocorrenciaId` da ocorrência; 1 a 64 caracteres (DG-55). |
| demandaOrigemId | string ou `null` | Sim | `null` na única; `demandaId` do evento na ocorrência. |
| compradorId | string | Sim | Do evento; 1 a 64 caracteres (DG-55). |
| produtoId | string | Sim | Do evento. |
| unidade | enum | Sim | Do evento. |
| quantidade | quantidade | Sim | > 0; do evento. |
| precoMaximo | valor monetário ou `null` | Sim | Do evento; `null` = sem teto. |
| destino | objeto `{cidade, uf}` | Sim | Do evento. |
| prazoDesejado | instante ou `null` | Sim | Do evento. |
| saldo | quantidade | Sim | Inicial = `quantidade`; reduzido por acordos (RN-27). |
| emNegociacao | quantidade | Sim | Inicial 0; soma das diretas `ABERTA` (RN-15, RN-27). |
| situacao | enum | Sim | `AGUARDANDO`, `ATENDIDA`, `CANCELADA` (§6.2). |
| prazoVencido | booleano | Sim | Inicial `false`; `true` quando o agendador encontra o prazo vencido (RN-10). Não muda a `situacao`. |
| recebidaEm | instante | Sim | `clock.now()` no consumo. |
| encerradaEm | instante ou `null` | Sim | Instante da saída de `AGUARDANDO`. |

Modelos recorrentes (`tipo = "RECORRENTE"`) ficam em `mercado.demanda_modelo` (`demandaId`, `compradorId`, `produtoId`, `recebidoEm`, `cancelado`) e não aparecem em nenhuma rota.

#### SugestaoLeilao (tabela `mercado.sugestao_leilao`, chave `sugestaoId`)
| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| sugestaoId | string UUID | Sim | UUID4 gerado pelo serviço. |
| produtoId | string | Sim | Produto do balanço. |
| situacao | enum | Sim | `ABERTA`, `EM_FORMACAO`, `CONVERTIDA`, `ENCERRADA` (§6.2). |
| excessoDemanda | quantidade | Sim | Excesso de demanda no último recálculo. |
| janelaEncerraEm | instante ou `null` | Sim | Primeiro aceite + janela (RN-18); `null` sem aceites. |
| duracaoLeilaoHoras | inteiro ou `null` | Sim | Duração do leilão escolhida no aceite que abriu a janela (RN-18); `null` = `LEILAO_DURACAO_HORAS`, e volta a `null` quando a sugestão volta a `ABERTA`. |
| negociacaoId | string UUID ou `null` | Sim | Leilão criado em T1; `null` fora de `CONVERTIDA`. |
| correlationId | string ou `null` | Sim | Correlação da requisição do aceite que abriu a janela atual (RN-18); `null` sem aceites. Vai nos eventos publicados por `executar_prazos` (§7.4). |
| abertaEm | instante | Sim | `clock.now()` na criação. |
| encerradaEm | instante ou `null` | Sim | Instante da saída para `CONVERTIDA` ou `ENCERRADA`. |

#### AceiteLeilao (tabela `mercado.aceite_leilao`, chave `(sugestaoId, fornecimentoId)`)
| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| sugestaoId | string UUID | Sim | Da rota. |
| fornecimentoId | string | Sim | Do corpo. |
| fornecedorId | string | Sim | Empresa do token, igual ao `fornecedorId` da oferta. |
| quantidade | quantidade | Sim | > 0 e ≤ disponível livre (RN-17). |
| aceitoEm | instante | Sim | `clock.now()` no primeiro aceite; não muda em alterações. |

#### Negociacao (tabela `mercado.negociacao`, chave `negociacaoId`)
| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| negociacaoId | string UUID | Sim | UUID4 gerado pelo serviço. |
| modo | enum | Sim | `VENDA_DIRETA` ou `LEILAO_DIRETO`. |
| produtoId | string | Sim | Da oferta ou da sugestão. |
| unidade | enum | Sim | Da oferta (direta) ou dos itens (leilão); igual à do produto quando ele é conhecido (RN-03); sem produto conhecido, a do primeiro item do lote (DG-61). |
| situacao | enum | Sim | `ABERTA`, `CONCLUIDA`, `SEM_ACORDO` (§6.2). |
| demandaId | string ou `null` | Sim | Demanda da direta; `null` no leilão. |
| compradorId | string ou `null` | Sim | Comprador da direta; `null` no leilão. |
| compraImediata | booleano | Sim | Da API-07 (ausente = `false`); sempre `false` no leilão. Repassado no `modo_negociacao_definido` (RN-35). |
| itens | lista de ItemNegociacao | Sim | 1 na direta; ≥ 1 no leilão. |
| quantidadeTotal | quantidade | Sim | Soma das quantidades dos itens. |
| valorReferencia | valor monetário | Sim | Σ `valorItem`. No leilão, igual a `precoInicial`. |
| precoInicial | valor monetário ou `null` | Sim | Só no leilão (RN-22). |
| incrementoMinimo | valor monetário ou `null` | Sim | Só no leilão (RN-22). |
| sugestaoId | string UUID ou `null` | Sim | Só no leilão. |
| iniciaEm / encerraEm | instante ou `null` | Sim | T1 e T2; só no leilão. |
| definidaEm | instante | Sim | `clock.now()` na criação. |
| encerradaEm | instante ou `null` | Sim | Instante do `negociacao_fechada` aplicado. |
| correlationId | string | Sim | Correlação da requisição ou do evento que criou a negociação. |

ItemNegociacao: `fornecimentoId`, `fornecedorId`, `quantidade`, `precoUnitario` (preço da oferta no momento da definição, congelado), `valorItem` (`quantidade × precoUnitario`, 2 casas, `ROUND_HALF_UP`), `participacao` (RN-23), `origem` (da oferta). Gravados em `mercado.negociacao_item`, na ordem do lote (RN-21).

Tabelas técnicas: `mercado.produto_lock` (uma linha por produto, usada por `bloquear_produto`, com a `situacao` do último balanço calculado e `situacaoDesde`, o instante em que ela mudou, lidas e gravadas por `obter_situacao_balanco` e `salvar_situacao_balanco`; RN-06), `mercado.processed_events` (`source`, `event_id`, `topico`, `processado_em`; único em `source, event_id`), `mercado.cancelamento_antecipado` (RN-09), `mercado.idempotency_keys` e `mercado.outbox`.

### 6.2 Estados e transições

**Demanda**

| Estado atual | Evento / comando | Condição | Próximo estado |
|---|---|---|---|
| — | `demanda_criada` `UNICA` ou `demanda_recorrente_gerada` válido | `demandaId` inexistente, sem cancelamento antecipado (RN-08) | `AGUARDANDO` |
| — | `demanda_criada` `UNICA` ou `demanda_recorrente_gerada` válido | `demandaId` inexistente, com cancelamento antecipado do mesmo comprador (RN-08) | `CANCELADA` |
| `AGUARDANDO` | `negociacao_fechada` `ENCERRADO` que zera o saldo | RN-27 | `ATENDIDA` |
| `AGUARDANDO` | `demanda_cancelada` | RN-09 | `CANCELADA` |
| `ATENDIDA`, `CANCELADA` | qualquer | — | sem mudança (estado final) |

Negociações diretas `ABERTA` de uma demanda que sai de `AGUARDANDO` continuam até o `negociacao_fechada`; o acordo ainda abate o saldo (RN-27), sem reabrir a demanda. O prazo vencido não muda a situação: só marca `prazoVencido = true`, o que tira a demanda do balanço e impede novas diretas (RN-10). A expiração da demanda é regra do Demanda (DG-48).

**SugestaoLeilao**

| Estado atual | Evento / comando | Condição | Próximo estado |
|---|---|---|---|
| — | Recálculo do balanço | Gatilho da sugestão verdadeiro e nenhuma sugestão ativa | `ABERTA` |
| `ABERTA` | Recálculo do balanço | Gatilho falso | `ENCERRADA` |
| `ABERTA` | Aceite (API-11) | — | `EM_FORMACAO` |
| `EM_FORMACAO` | Saída do último aceite (API-13) | Gatilho verdadeiro | `ABERTA` (janela zerada) |
| `EM_FORMACAO` | Saída do último aceite (API-13) | Gatilho falso | `ENCERRADA` |
| `EM_FORMACAO` | Fechamento do lote (RN-21) | Janela vencida, todos os elegíveis no lote ou gatilho falso, com ao menos um item | `CONVERTIDA` |
| `EM_FORMACAO` | Fechamento do lote (RN-21) | Nenhum item restante | `ENCERRADA` |
| `CONVERTIDA`, `ENCERRADA` | qualquer | — | sem mudança (estado final) |

**Negociacao**

| Estado atual | Evento / comando | Condição | Próximo estado |
|---|---|---|---|
| — | API-07 (direta) ou fechamento do lote (leilão, RN-21) | — | `ABERTA` |
| `ABERTA` | `negociacao_fechada` | `status = "ENCERRADO"` | `CONCLUIDA` |
| `ABERTA` | `negociacao_fechada` | `status` ∈ {`DESERTO`, `RECUSADO`, `EXPIRADO`} | `SEM_ACORDO` |
| `CONCLUIDA`, `SEM_ACORDO` | qualquer | — | sem mudança (estado final) |

T2 vencido não muda a negociação neste serviço: o encerramento chega pelo `negociacao_fechada`.

## 7. Contratos de interface

### 7.1 Autenticação e contexto da requisição

Rotas públicas (sem token): `/health`, `/health/ready`, `/metrics`, `/docs`, `/openapi.json`. Todas as rotas `/api/v1/...` exigem `Authorization: Bearer <token>`, um JWT emitido pelo Identity-Service.

Validação do token, nesta ordem (decodificar sempre com `algorithms=["RS256"]` e `options={"verify_exp": False}`, comparando a expiração com `clock.now()`):

1. Cabeçalho ausente, vazio ou sem `Bearer ` seguido de texto; token malformado; cabeçalho do token sem `kid` ou com `alg` diferente de `"RS256"`; `chaves_publicas.obter(kid)` devolvendo `None`; assinatura inválida; `iss` diferente de `emissor`; `aud` que não contém `audiencia` (texto ou lista); `sub` ausente ou vazio; `exp` ausente, não inteiro ou `exp <= int(agora.timestamp())` → `401 TOKEN_INVALIDO`, com o cabeçalho `WWW-Authenticate: Bearer`. Se `chaves_publicas.obter` lançar exceção → `503 DEPENDENCIA_INDISPONIVEL`.
2. `view_mode` diferente de `"SINGLE"`, `tenant_id` ausente, nulo ou vazio, ou `active_scope` ausente ou nulo → `403 CONTEXTO_NAO_SELECIONADO`.
3. Escopo não permitido na rota (tabela do §7.2): `CARRIER` e `TENANT_ADMIN` em qualquer rota de negócio, e o escopo errado para a operação → `403 ACESSO_NEGADO`.

Claims ignorados na v1: `platform_role`, `roles`, `permissions`, `group_id`, `allowed_tenants`, `act`, `fid`, `jti`, `client_id` e `token_use`. Um token com `platform_role = "PLATFORM_ADMIN"` não tem nenhum poder adicional.

`X-Correlation-Id`: valor de 1 a 128 caracteres de `[A-Za-z0-9._-]`; ausente, vazio ou fora desse formato → o serviço gera um UUID4. O valor efetivo volta no cabeçalho `X-Correlation-Id` de toda resposta, no `correlationId` dos erros e no `correlationId` dos eventos publicados pela requisição.

`Idempotency-Key` (opcional em todo `POST` de negócio): 1 a 128 caracteres de `[A-Za-z0-9._-]`; vazio equivale a ausente; fora do formato → `422 VALIDACAO_INVALIDA`. Regras na RN-30.

### 7.2 API REST

Todas as respostas de negócio são JSON com nomes de campo em camelCase. O FastAPI não pode validar o corpo antes dos passos 2 a 4 da ordem de verificação do §10: o corpo é lido com `await request.body()` e validado pelo próprio serviço. `negociacaoId` e `sugestaoId` na URL são UUID; `produtoId`, `demandaId` e `fornecimentoId` na URL ou no corpo são texto de 1 a 64 caracteres de `[A-Za-z0-9._-]`; outro formato → `422 VALIDACAO_INVALIDA`. Corpos com campos fora do contrato, campos ausentes, tipos errados (inclusive número enviado como string ou booleano), quantidade ≤ 0, acima de `9999999999.99` ou com mais de 2 casas, ou JSON malformado → `422 VALIDACAO_INVALIDA`. Listagens: `page` inteiro de 1 a 999999999, com até 9 dígitos (padrão 1) e `size` inteiro de 1 a 100 (padrão 10); fora disso → `422 VALIDACAO_INVALIDA`; resposta `{"items": [...], "page": P, "size": S, "total": T}`, com `items: []` numa página vazia.

| ID | Método | Rota | Escopos | Sucesso | Erros possíveis (na ordem de verificação) |
|---|---|---|---|---|---|
| API-01 | GET | `/health` | público | 200 | — |
| API-02 | GET | `/health/ready` | público | 200 | 503 `DEPENDENCIA_INDISPONIVEL` |
| API-03 | GET | `/metrics` | público | 200 | — |
| API-04 | GET | `/api/v1/mercado/produtos/{produtoId}/balanco` | `BUYER`, `SUPPLIER` | 200 | 401 `TOKEN_INVALIDO` / 503 → 403 `CONTEXTO_NAO_SELECIONADO` → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 `PRODUTO_NAO_ENCONTRADO` |
| API-05 | GET | `/api/v1/mercado/produtos/{produtoId}/ofertas` | `BUYER`, `SUPPLIER` | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 `PRODUTO_NAO_ENCONTRADO` → 404 `DEMANDA_NAO_ENCONTRADA` → 422 `PRODUTO_DIFERENTE` |
| API-06 | GET | `/api/v1/mercado/demandas/{demandaId}` | `BUYER` | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 `DEMANDA_NAO_ENCONTRADA` |
| API-07 | POST | `/api/v1/mercado/negociacoes` | `BUYER` | 201 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → idempotência (replay ou 409 `IDEMPOTENCIA_CONFLITO`) → 404 `DEMANDA_NAO_ENCONTRADA` → 409 `DEMANDA_ENCERRADA` → 422 `OFERTA_INDISPONIVEL` → 422 `PRODUTO_DIFERENTE` → 403 `CONFLITO_DE_INTERESSE` → 422 `PRECO_ACIMA_DO_MAXIMO` → 409 `NEGOCIACAO_JA_ABERTA` → 422 `QUANTIDADE_ACIMA_DO_SALDO` → 422 `QUANTIDADE_INDISPONIVEL` → 422 `VALIDACAO_INVALIDA` (valor do item, RN-14) |
| API-08 | GET | `/api/v1/mercado/negociacoes` | `BUYER`, `SUPPLIER` | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` |
| API-09 | GET | `/api/v1/mercado/negociacoes/{negociacaoId}` | `BUYER`, `SUPPLIER` | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 `NEGOCIACAO_NAO_ENCONTRADA` |
| API-10 | GET | `/api/v1/mercado/sugestoes-leilao` | `SUPPLIER` | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` |
| API-11 | POST | `/api/v1/mercado/sugestoes-leilao/{sugestaoId}/aceites` | `SUPPLIER` | 201 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 `SUGESTAO_NAO_ENCONTRADA` → idempotência → 409 `SUGESTAO_ENCERRADA` → 422 `OFERTA_INDISPONIVEL` → 403 `OFERTA_DE_OUTRA_EMPRESA` → 422 `PRODUTO_DIFERENTE` → 409 `FORNECEDOR_EM_LEILAO` → 409 `ACEITE_JA_REGISTRADO` → 422 `QUANTIDADE_INDISPONIVEL` |
| API-12 | PUT | `/api/v1/mercado/sugestoes-leilao/{sugestaoId}/aceites/{fornecimentoId}` | `SUPPLIER` | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 `SUGESTAO_NAO_ENCONTRADA` → 404 `ACEITE_NAO_ENCONTRADO` → 409 `SUGESTAO_ENCERRADA` → 422 `QUANTIDADE_INDISPONIVEL` |
| API-13 | DELETE | `/api/v1/mercado/sugestoes-leilao/{sugestaoId}/aceites/{fornecimentoId}` | `SUPPLIER` | 204 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 `SUGESTAO_NAO_ENCONTRADA` → 404 `ACEITE_NAO_ENCONTRADO` → 409 `SUGESTAO_ENCERRADA` |

Em qualquer rota, banco indisponível → `503 DEPENDENCIA_INDISPONIVEL`; falha inesperada → `500 ERRO_INTERNO`.

#### API-04 — Balanço do produto

Sem corpo. `produtoId` conhecido = presente na cópia de produtos ou referenciado por alguma oferta ou demanda (RN-06). `ofertaTotal`, `demandaTotal`, `situacao` e `excessoDemanda` são calculados na consulta pela RN-06; `situacaoDesde` vem do último recálculo gravado. Mostra só totais: nenhuma demanda nem comprador é identificado (DG-50). Resposta 200 (cenário E do §8.0):

```json
{
  "produtoId": "35000000-0000-4000-8000-000000000001",
  "unidade": "TON",
  "ofertaTotal": 100.00,
  "demandaTotal": 130.00,
  "situacao": "DEMANDA_EXCEDENTE",
  "situacaoDesde": "2026-11-20T11:57:00.000Z",
  "excessoDemanda": 30.00,
  "sugestaoId": "33000000-0000-4000-8000-000000000001",
  "agora": "2026-11-20T12:00:00.000Z"
}
```

`unidade` é `null` se o produto não estiver na cópia de produtos. `situacaoDesde` é `null` se o balanço do produto nunca foi recalculado (produto só com `produto_cadastrado`). `excessoDemanda` é `0.00` fora de `DEMANDA_EXCEDENTE`. `sugestaoId` é o da sugestão ativa do produto, ou `null`.

#### API-05 — Vitrine de ofertas

Query opcional: `demandaId` (só com escopo `BUYER`; com `SUPPLIER` → `422 VALIDACAO_INVALIDA`), `page`, `size`. Lista as ofertas negociáveis do produto com disponível livre > 0, na ordem da RN-11. Preços e fornecedores são públicos para compradores e fornecedores (DG-49). `quantidadeDisponivel` é o disponível livre. `apta` é `null` sem `demandaId` e, com `demandaId`, indica se a oferta é apta para aquela demanda (RN-07). Resposta 200 (cenário E, COMP-A com `demandaId` = DEM-A):

```json
{
  "items": [
    {
      "fornecimentoId": "34000000-0000-4000-8000-000000000002",
      "fornecedorId": "30000000-0000-4000-8000-000000000003",
      "preco": 7.50,
      "quantidadeDisponivel": 40.00,
      "origem": {"cidade": "Jataí", "uf": "GO"},
      "apta": true
    },
    {
      "fornecimentoId": "34000000-0000-4000-8000-000000000001",
      "fornecedorId": "30000000-0000-4000-8000-000000000002",
      "preco": 8.00,
      "quantidadeDisponivel": 60.00,
      "origem": {"cidade": "Rio Verde", "uf": "GO"},
      "apta": true
    }
  ],
  "page": 1,
  "size": 10,
  "total": 2
}
```

#### API-06 — Demanda

Visível só ao comprador dono (RN-13). Resposta 200 (DEM-A depois da CA-26):

```json
{
  "demandaId": "36000000-0000-4000-8000-000000000001",
  "demandaOrigemId": null,
  "produtoId": "35000000-0000-4000-8000-000000000001",
  "unidade": "TON",
  "quantidade": 50.00,
  "saldo": 50.00,
  "emNegociacao": 30.00,
  "saldoNegociavel": 20.00,
  "precoMaximo": 9.00,
  "destino": {"cidade": "Anápolis", "uf": "GO"},
  "prazoDesejado": "2026-11-30T23:59:59.000Z",
  "situacao": "AGUARDANDO",
  "prazoVencido": false,
  "recebidaEm": "2026-11-20T11:55:00.000Z",
  "encerradaEm": null
}
```

#### API-07 — Abrir negociação direta

Requisição: `demandaId`, `fornecimentoId` e `quantidade` obrigatórios; `compraImediata` opcional (booleano; ausente = `false`); nenhum outro campo aceito. `compraImediata` com valor não booleano, inclusive `null` e `"true"`, → `422 VALIDACAO_INVALIDA`.

```json
{
  "demandaId": "36000000-0000-4000-8000-000000000001",
  "fornecimentoId": "34000000-0000-4000-8000-000000000002",
  "quantidade": 30.00
}
```

Resposta 201 com o cabeçalho `Location: /api/v1/mercado/negociacoes/{negociacaoId}` e o detalhe da negociação (projeção da API-09). Publica `modo_negociacao_definido` com `modo = "VENDA_DIRETA"` (§7.4). Com `"compraImediata": true`, o comprador compra pelo preço da oferta, sem troca de propostas (RN-35); as validações e o controle do saldo são os mesmos.

#### API-08 e API-09 — Negociações

API-08 aceita os filtros opcionais `modo` (`VENDA_DIRETA` ou `LEILAO_DIRETO`) e `situacao` (`ABERTA`, `CONCLUIDA` ou `SEM_ACORDO`); outro valor → `422 VALIDACAO_INVALIDA`. Cada item: `negociacaoId`, `modo`, `produtoId`, `situacao`, `quantidadeTotal`, `valorReferencia`, `definidaEm`. Visibilidade na RN-12.

API-09, detalhe de um leilão (cenário L, visto por COMP-B em `2026-11-20T13:00:00.000Z`, depois de uma oferta nova cobrir a demanda):

```json
{
  "negociacaoId": "31000000-0000-4000-8000-000000000002",
  "modo": "LEILAO_DIRETO",
  "produtoId": "35000000-0000-4000-8000-000000000001",
  "unidade": "TON",
  "situacao": "ABERTA",
  "demandaId": null,
  "compradorId": null,
  "compraImediata": false,
  "quantidadeTotal": 100.00,
  "valorReferencia": 780.00,
  "precoInicial": 780.00,
  "incrementoMinimo": 7.80,
  "sugestaoId": "33000000-0000-4000-8000-000000000001",
  "iniciaEm": "2026-11-20T12:10:00.000Z",
  "encerraEm": "2026-11-21T12:10:00.000Z",
  "itens": [
    {
      "fornecimentoId": "34000000-0000-4000-8000-000000000001",
      "fornecedorId": "30000000-0000-4000-8000-000000000002",
      "quantidade": 60.00,
      "precoUnitario": 8.00,
      "valorItem": 480.00,
      "participacao": 0.6154,
      "origem": {"cidade": "Rio Verde", "uf": "GO"}
    },
    {
      "fornecimentoId": "34000000-0000-4000-8000-000000000002",
      "fornecedorId": "30000000-0000-4000-8000-000000000003",
      "quantidade": 40.00,
      "precoUnitario": 7.50,
      "valorItem": 300.00,
      "participacao": 0.3846,
      "origem": {"cidade": "Jataí", "uf": "GO"}
    }
  ],
  "definidaEm": "2026-11-20T12:10:00.000Z",
  "encerradaEm": null,
  "contextoProduto": {"situacao": "OFERTA_SUFICIENTE", "desde": "2026-11-20T12:30:00.000Z"},
  "agora": "2026-11-20T13:00:00.000Z"
}
```

`contextoProduto` (RN-24) só aparece preenchido em `LEILAO_DIRETO` com `situacao = "ABERTA"`; nos demais casos é `null`. Numa `VENDA_DIRETA`, `demandaId` e `compradorId` são os da demanda e `compraImediata` é o da API-07; `precoInicial`, `incrementoMinimo`, `sugestaoId`, `iniciaEm` e `encerraEm` são `null`; `itens` tem um elemento com `participacao` 1.0000.

#### API-10 — Sugestões de leilão

Lista as sugestões visíveis à empresa do token (RN-16), em ordem de `abertaEm` crescente, com a paginação do §7.2. Cada item (cenário E, visto por FORN-Y em `2026-11-20T12:05:00.000Z`, depois do aceite de FORN-X):

```json
{
  "sugestaoId": "33000000-0000-4000-8000-000000000001",
  "produtoId": "35000000-0000-4000-8000-000000000001",
  "unidade": "TON",
  "situacao": "EM_FORMACAO",
  "excessoDemanda": 90.00,
  "quantidadeNoLote": 60.00,
  "fornecedoresNoLote": 1,
  "janelaEncerraEm": "2026-11-20T13:00:00.000Z",
  "duracaoLeilaoHoras": 24,
  "minhaParticipacao": null,
  "negociacaoId": null,
  "agora": "2026-11-20T12:05:00.000Z"
}
```

`excessoDemanda` é calculado com a quantidade comprometida já fora da oferta total (RN-06). `duracaoLeilaoHoras` é a duração escolhida no primeiro aceite ou, sem ela, `LEILAO_DURACAO_HORAS` (DG-70). `minhaParticipacao` é `{"fornecimentoId": ..., "quantidade": ...}` quando a empresa do token tem aceite na sugestão. Os nomes dos outros fornecedores do lote nunca aparecem (RN-16).

#### API-11, API-12 e API-13 — Aceites

API-11, requisição: `fornecimentoId` e `quantidade` obrigatórios; `duracaoLeilaoHoras` opcional, inteiro de 1 a 999999 (sem máximo de negócio: o limite só evita uma data impossível); `null`, número com casas, texto ou booleano → `422 VALIDACAO_INVALIDA`; nenhum outro campo aceito. Só o aceite que leva a sugestão de `ABERTA` a `EM_FORMACAO` define a duração; nos seguintes, o campo é ignorado e a resposta mostra a duração em vigor (RN-18, DG-70). Exemplo sem o campo opcional:

```json
{
  "fornecimentoId": "34000000-0000-4000-8000-000000000001",
  "quantidade": 60.00
}
```

API-12, requisição: `{"quantidade": 50.00}`. Respostas da API-11 (201) e da API-12 (200): a projeção da sugestão da API-10, vista pela empresa do token, depois da mudança. Se o aceite fechou o lote (RN-21), a resposta traz `situacao = "CONVERTIDA"` e o `negociacaoId` do leilão. API-13 responde 204 sem corpo.

### 7.3 Eventos consumidos

Envelope (kit, `CONTRACTS.md`): `eventId`, `eventType` (igual ao tópico), `eventVersion` (`"1.0"`), `timestamp`, `source`, `correlationId`, `aggregateId` e `payload` (objeto). Campos adicionais no `payload` são aceitos e ignorados. Os schemas estão em `docs/mercado/contratos/<topico>.schema.json` e listam só os campos que este serviço usa.

| Tópico | Produtor | Chave | Ação | Em caso de mensagem inválida |
|---|---|---|---|---|
| `produto_cadastrado` | `produtos-service` | `produtoId` | RF-02 | `mercado.dlt` (RN-32) |
| `produto_atualizado` | `produtos-service` | `produtoId` | RF-02 | `mercado.dlt` (RN-32) |
| `fornecimento_criado` | `fornecimentos-service` | `fornecimentoId` | RF-01 | `mercado.dlt` (RN-32) |
| `fornecimento_atualizado` | `fornecimentos-service` | `fornecimentoId` | RF-01 | `mercado.dlt` (RN-32) |
| `estoque_atualizado` | `fornecimentos-service` | `fornecimentoId` | RF-01 | `mercado.dlt` (RN-32) |
| `demanda_criada` | `demanda-service` | `demandaId` | RF-03 | `mercado.dlt` (RN-32) |
| `demanda_recorrente_gerada` | `demanda-service` | `demandaId` ou `ocorrenciaId` (qualquer uma) | RF-03 | `mercado.dlt` (RN-32) |
| `demanda_cancelada` | `demanda-service` | `demandaId` | RF-04 | `mercado.dlt` (RN-32) |
| `negociacao_fechada` | `negociacao-service` | `negociacaoId` | RF-11 | `mercado.dlt` (RN-32) |

Validação comum (falha → inválida): mensagem não é objeto; falta algum dos oito campos do envelope ou há campo além deles; `eventId` não é string não vazia; `eventType` diferente do tópico; `eventVersion` diferente de `"1.0"`; `source` diferente do produtor da tabela; `correlationId` fora de 1–128 caracteres de `[A-Za-z0-9._-]`; `timestamp` não é instante com fuso; `payload` não é objeto; campo obrigatório do payload ausente, com tipo errado ou fora do domínio do schema; string obrigatória vazia ou só com espaços; quantidade ou valor com mais de 2 casas; quantidade acima de `9999999999.99` ou valor acima de `99999999999999.99`. Validação de unidade na RN-03. `produtoId`, `fornecimentoId`, `fornecedorId`, `compradorId`, `demandaId` e `ocorrenciaId` são texto de 1 a 64 caracteres; maior → inválida (DG-55). Campos adicionais do Fornecimento, como `grupoId` no `fornecimento_criado`, são aceitos e ignorados (DG-54). Três regras desta lista não estão nos schemas e são aplicadas pelo código além deles: string obrigatória só com espaços, mais de 2 casas decimais, os limites do §4 e unidade (RN-03).

`produto_cadastrado`. Obrigatórios: `produtoId`, `nome`, `unidade`, `cadastradoEm`. `produto_atualizado`: os mesmos, com `atualizadoEm` no lugar de `cadastradoEm`.

```json
{
  "eventId": "37000000-0000-4000-8000-000000000001",
  "eventType": "produto_cadastrado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T11:00:00.000Z",
  "source": "produtos-service",
  "correlationId": "teste-mrc-prod",
  "aggregateId": "35000000-0000-4000-8000-000000000001",
  "payload": {
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "nome": "Milho em grão",
    "categoria": "GRAOS",
    "unidade": "TON",
    "tipoTransporte": "GRANEL",
    "pesoUnitarioKg": null,
    "cadastradoPor": {
      "empresaId": "30000000-0000-4000-8000-000000000002",
      "usuarioId": "usr-7"
    },
    "cadastradoEm": "2026-11-20T11:00:00.000Z"
  }
}
```

`fornecimento_criado`. Obrigatórios: `fornecimentoId`, `fornecedorId`, `produtoId`, `unidade`, `preco` (≥ 0), `quantidadeDisponivel` (≥ 0), `criadoEm`. Opcional: `origem` (`{cidade, uf}` ou `null`). `fornecimento_atualizado`: os mesmos, com `atualizadoEm` no lugar de `criadoEm`.

```json
{
  "eventId": "37000000-0000-4000-8000-000000000003",
  "eventType": "fornecimento_criado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T11:40:00.000Z",
  "source": "fornecimentos-service",
  "correlationId": "teste-mrc-forn",
  "aggregateId": "34000000-0000-4000-8000-000000000001",
  "payload": {
    "fornecimentoId": "34000000-0000-4000-8000-000000000001",
    "fornecedorId": "30000000-0000-4000-8000-000000000002",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "unidade": "TON",
    "preco": 8.00,
    "quantidadeDisponivel": 60.00,
    "origem": {
      "cidade": "Rio Verde",
      "uf": "GO"
    },
    "criadoPor": {
      "empresaId": "30000000-0000-4000-8000-000000000002",
      "usuarioId": "usr-7"
    },
    "criadoEm": "2026-11-20T11:40:00.000Z"
  }
}
```

`estoque_atualizado`. Obrigatórios: `fornecimentoId`, `quantidadeDisponivel` (≥ 0), `atualizadoEm`. Os demais campos do contrato do Fornecimento são aceitos e ignorados.

```json
{
  "eventId": "37000000-0000-4000-8000-000000000005",
  "eventType": "estoque_atualizado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:20:00.000Z",
  "source": "fornecimentos-service",
  "correlationId": "teste-mrc-02",
  "aggregateId": "34000000-0000-4000-8000-000000000001",
  "payload": {
    "fornecimentoId": "34000000-0000-4000-8000-000000000001",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "fornecedorId": "30000000-0000-4000-8000-000000000002",
    "operacao": "RESERVA",
    "quantidadeMovimentada": 30.00,
    "quantidadeDisponivel": 30.00,
    "quantidadeReservada": 30.00,
    "pedidoId": "32000000-0000-4000-8000-000000000001",
    "atualizadoEm": "2026-11-20T12:20:00.000Z"
  }
}
```

`demanda_criada`. Obrigatórios: `demandaId`, `compradorId`, `produtoId`, `unidade`, `quantidade` (> 0), `destino` (`{cidade, uf}`, `uf` com 2 letras maiúsculas), `tipo` (`UNICA` ou `RECORRENTE`), `criadoEm`. Opcionais: `precoMaximo` (> 0 ou `null`), `prazoDesejado` (instante ou `null`).

```json
{
  "eventId": "37000000-0000-4000-8000-000000000006",
  "eventType": "demanda_criada",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T11:55:00.000Z",
  "source": "demanda-service",
  "correlationId": "teste-mrc-dem",
  "aggregateId": "36000000-0000-4000-8000-000000000001",
  "payload": {
    "demandaId": "36000000-0000-4000-8000-000000000001",
    "compradorId": "30000000-0000-4000-8000-000000000001",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "unidade": "TON",
    "quantidade": 50.00,
    "precoMaximo": 9.00,
    "destino": {
      "cidade": "Anápolis",
      "uf": "GO"
    },
    "prazoDesejado": "2026-11-30T23:59:59Z",
    "tipo": "UNICA",
    "frequencia": null,
    "criadoPor": {
      "empresaId": "30000000-0000-4000-8000-000000000001",
      "usuarioId": "usr-3"
    },
    "criadoEm": "2026-11-20T11:55:00.000Z"
  }
}
```

`demanda_recorrente_gerada`. Obrigatórios: `demandaId` (origem), `ocorrenciaId`, `compradorId`, `produtoId`, `unidade`, `quantidade`, `destino`, `geradoEm`. Opcionais: `precoMaximo`, `prazoDesejado`.

```json
{
  "eventId": "37000000-0000-4000-8000-000000000007",
  "eventType": "demanda_recorrente_gerada",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T11:56:00.000Z",
  "source": "demanda-service",
  "correlationId": "teste-mrc-dem",
  "aggregateId": "36000000-0000-4000-8000-000000000003",
  "payload": {
    "demandaId": "36000000-0000-4000-8000-000000000003",
    "ocorrenciaId": "36000000-0000-4000-8000-000000000031",
    "compradorId": "30000000-0000-4000-8000-000000000001",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "unidade": "TON",
    "quantidade": 10.00,
    "precoMaximo": null,
    "destino": {
      "cidade": "Anápolis",
      "uf": "GO"
    },
    "prazoDesejado": null,
    "geradoEm": "2026-11-20T11:56:00.000Z",
    "proximaOcorrenciaEm": "2026-12-20T11:56:00.000Z"
  }
}
```

`demanda_cancelada` (tópico proposto pelo grupo de Compra e Demanda e ainda não criado no kit, INT-MRC-03). Obrigatórios: `demandaId`, `compradorId`, `canceladoEm`. Opcional: `motivo`.

```json
{
  "eventId": "37000000-0000-4000-8000-000000000008",
  "eventType": "demanda_cancelada",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T13:00:00.000Z",
  "source": "demanda-service",
  "correlationId": "teste-mrc-dem",
  "aggregateId": "36000000-0000-4000-8000-000000000001",
  "payload": {
    "demandaId": "36000000-0000-4000-8000-000000000001",
    "compradorId": "30000000-0000-4000-8000-000000000001",
    "motivo": "Compra feita por outro canal",
    "canceladoEm": "2026-11-20T13:00:00.000Z",
    "canceladoPor": {
      "empresaId": "30000000-0000-4000-8000-000000000001",
      "usuarioId": "usr-3"
    }
  }
}
```

`negociacao_fechada` (formato da ESP-NEG-001 e da ESP-PED-001, com o campo `demandaId`, aceito pela Negociação na D38 da ESP-NEG-001; INT-MRC-01). Na `VENDA_DIRETA` a Negociação manda `demandaId` nulo: a demanda da direta é a gravada na negociação (RN-27). Obrigatórios: `negociacaoId` (UUID), `tipo` (`LEILAO` ou `VENDA_DIRETA`), `status` (`ENCERRADO`, `DESERTO`, `RECUSADO` ou `EXPIRADO`), `produtoId`, `alocacoes` (lista; cada item com `fornecimentoId` e `quantidade` > 0). Opcionais: `compradorId`, `demandaId`. Os demais campos das alocações (`fornecedorId`, `pedidoId`, `valorUnitario`, `valorTotal`) são aceitos e ignorados (DG-34). Num leilão, `alocacoes` traz só os itens que ficaram no lote depois das reservas (DG-51).

```json
{
  "eventId": "37000000-0000-4000-8000-000000000009",
  "eventType": "negociacao_fechada",
  "eventVersion": "1.0",
  "timestamp": "2026-11-21T12:10:00.000Z",
  "source": "negociacao-service",
  "correlationId": "teste-mrc-07",
  "aggregateId": "31000000-0000-4000-8000-000000000002",
  "payload": {
    "negociacaoId": "31000000-0000-4000-8000-000000000002",
    "tipo": "LEILAO",
    "status": "ENCERRADO",
    "compradorId": "30000000-0000-4000-8000-000000000004",
    "demandaId": "36000000-0000-4000-8000-000000000002",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "alocacoes": [
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000001",
        "fornecedorId": "30000000-0000-4000-8000-000000000002",
        "pedidoId": "32000000-0000-4000-8000-000000000011",
        "quantidade": 60.00,
        "valorUnitario": 9.23,
        "valorTotal": 553.86
      },
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000002",
        "fornecedorId": "30000000-0000-4000-8000-000000000003",
        "pedidoId": "32000000-0000-4000-8000-000000000012",
        "quantidade": 40.00,
        "valorUnitario": 8.65,
        "valorTotal": 346.14
      }
    ]
  }
}
```

### 7.4 Eventos publicados

Envelope comum a todas as mensagens publicadas:

| Campo | Valor |
|---|---|
| eventId | UUID4 novo, único por mensagem |
| eventType | igual ao nome do tópico |
| eventVersion | `"1.0"` |
| timestamp | instante = `clock.now()` |
| source | `"mercado-service"` |
| correlationId | da requisição HTTP; da mensagem consumida que causou o evento; ou `correlationId` da sugestão (do primeiro aceite), nos eventos de `executar_prazos` |
| aggregateId | `negociacaoId` (na DLT, ver abaixo) |
| payload | objeto específico do tópico |

A chave Kafka (`key` de `publish`) é sempre igual ao `aggregateId`. Nos eventos publicados, os IDs de negócio têm de 1 a 64 caracteres (DG-55) e nunca são só espaços (`pattern` `\S` nos schemas), como exige a Negociação ao consumir.

| Tópico | Quando | Chave de partição | Payload | Consumidor |
|---|---|---|---|---|
| `modo_negociacao_definido` | Negociação direta aberta (RF-08) ou lote fechado em T1 (RF-10) | `negociacaoId` | `negociacaoId`, `modo`, `compraImediata` (RN-35; sempre `false` no leilão), `produtoId`, `unidade`, `quantidadeTotal`, `valorReferencia`, `demandas[]` {`demandaId`, `demandaOrigemId`, `compradorId`, `quantidade`, `precoMaximo`, `destino`, `prazoDesejado`} (1 na direta, vazia no leilão), `ofertas[]` {`fornecimentoId`, `fornecedorId`, `quantidade`, `precoUnitario`, `valorItem`, `participacao`, `origem`}, `definidaEm` | Negociação (abre a negociação e pede o pedido temporário de cada oferta; com `compraImediata = true`, encerra a direta pelo preço da oferta quando a reserva é confirmada) |
| `leilao_iniciado` | Lote fechado em T1 (RF-10), logo depois do `modo_negociacao_definido` do mesmo leilão | `negociacaoId` | `negociacaoId`, `sugestaoId`, `produtoId`, `unidade`, `quantidadeTotal`, `precoInicial`, `incrementoMinimo`, `incrementoMinimoPercentual` (o `LEILAO_INCREMENTO_MINIMO_PERCENTUAL` usado, com no mínimo 2 casas e as casas extras da configuração, DG-63, para a Negociação refazer a RN-22 se um item sair), `itens[]` (mesmo formato de `ofertas[]`), `iniciaEm` (T1), `encerraEm` (T2) | Negociação (pede o pedido temporário de cada item e libera os lances com as reservas confirmadas), front-end |
| `mercado.dlt` | Mensagem consumida inválida ou que falhou 3 vezes por erro que não é de banco indisponível (RF-15) | `aggregateId` original se for string de 1 a 128 caracteres; senão `eventId` original convertido com `str` se presente e não vazio; senão `"desconhecido"` | `topicoOrigem`, `originalEvent` (mensagem recebida, intacta), `error` (`"EVENTO_INVALIDO"` ou `"PROCESSAMENTO_FALHOU"`), `detalhe` (1–500 caracteres), `stackTrace` (`""` em `EVENTO_INVALIDO`; pilha de chamadas da última exceção em `PROCESSAMENTO_FALHOU`, como na biblioteca comum do kit), `failedAtAttempt` (1 em `EVENTO_INVALIDO`; 3 em `PROCESSAMENTO_FALHOU`) | operação, QA |

Na negociação direta, `demandas[0].quantidade` e `ofertas[0].quantidade` são a quantidade negociada (não a quantidade total da demanda), `participacao` é 1.0000 e `valorReferencia` = `valorItem`. Na direta, `prazoDesejado` sai normalizado como instante (§4). O destino segue para a Negociação como referência; o destino do pedido é informado pelo comprador no Pedidos (RN-29).

Exemplos (iguais a `docs/mercado/contratos/<topico>.exemplo.json`):

`modo_negociacao_definido` (negociação direta, CA-26):

```json
{
  "eventId": "38000000-0000-4000-8000-000000000001",
  "eventType": "modo_negociacao_definido",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:05:00.000Z",
  "source": "mercado-service",
  "correlationId": "teste-mrc-05",
  "aggregateId": "31000000-0000-4000-8000-000000000001",
  "payload": {
    "negociacaoId": "31000000-0000-4000-8000-000000000001",
    "modo": "VENDA_DIRETA",
    "compraImediata": false,
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "unidade": "TON",
    "quantidadeTotal": 30.00,
    "valorReferencia": 225.00,
    "demandas": [
      {
        "demandaId": "36000000-0000-4000-8000-000000000001",
        "demandaOrigemId": null,
        "compradorId": "30000000-0000-4000-8000-000000000001",
        "quantidade": 30.00,
        "precoMaximo": 9.00,
        "destino": {
          "cidade": "Anápolis",
          "uf": "GO"
        },
        "prazoDesejado": "2026-11-30T23:59:59.000Z"
      }
    ],
    "ofertas": [
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000002",
        "fornecedorId": "30000000-0000-4000-8000-000000000003",
        "quantidade": 30.00,
        "precoUnitario": 7.50,
        "valorItem": 225.00,
        "participacao": 1.0000,
        "origem": {
          "cidade": "Jataí",
          "uf": "GO"
        }
      }
    ],
    "definidaEm": "2026-11-20T12:05:00.000Z"
  }
}
```

`modo_negociacao_definido` (leilão, CA-39):

```json
{
  "eventId": "38000000-0000-4000-8000-000000000002",
  "eventType": "modo_negociacao_definido",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:10:00.000Z",
  "source": "mercado-service",
  "correlationId": "teste-mrc-10",
  "aggregateId": "31000000-0000-4000-8000-000000000002",
  "payload": {
    "negociacaoId": "31000000-0000-4000-8000-000000000002",
    "modo": "LEILAO_DIRETO",
    "compraImediata": false,
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "unidade": "TON",
    "quantidadeTotal": 100.00,
    "valorReferencia": 780.00,
    "demandas": [],
    "ofertas": [
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000001",
        "fornecedorId": "30000000-0000-4000-8000-000000000002",
        "quantidade": 60.00,
        "precoUnitario": 8.00,
        "valorItem": 480.00,
        "participacao": 0.6154,
        "origem": {
          "cidade": "Rio Verde",
          "uf": "GO"
        }
      },
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000002",
        "fornecedorId": "30000000-0000-4000-8000-000000000003",
        "quantidade": 40.00,
        "precoUnitario": 7.50,
        "valorItem": 300.00,
        "participacao": 0.3846,
        "origem": {
          "cidade": "Jataí",
          "uf": "GO"
        }
      }
    ],
    "definidaEm": "2026-11-20T12:10:00.000Z"
  }
}
```

`leilao_iniciado` (CA-39):

```json
{
  "eventId": "38000000-0000-4000-8000-000000000003",
  "eventType": "leilao_iniciado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:10:00.000Z",
  "source": "mercado-service",
  "correlationId": "teste-mrc-10",
  "aggregateId": "31000000-0000-4000-8000-000000000002",
  "payload": {
    "negociacaoId": "31000000-0000-4000-8000-000000000002",
    "sugestaoId": "33000000-0000-4000-8000-000000000001",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "unidade": "TON",
    "quantidadeTotal": 100.00,
    "precoInicial": 780.00,
    "incrementoMinimo": 7.80,
    "incrementoMinimoPercentual": 1.00,
    "itens": [
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000001",
        "fornecedorId": "30000000-0000-4000-8000-000000000002",
        "quantidade": 60.00,
        "precoUnitario": 8.00,
        "valorItem": 480.00,
        "participacao": 0.6154,
        "origem": {
          "cidade": "Rio Verde",
          "uf": "GO"
        }
      },
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000002",
        "fornecedorId": "30000000-0000-4000-8000-000000000003",
        "quantidade": 40.00,
        "precoUnitario": 7.50,
        "valorItem": 300.00,
        "participacao": 0.3846,
        "origem": {
          "cidade": "Jataí",
          "uf": "GO"
        }
      }
    ],
    "iniciaEm": "2026-11-20T12:10:00.000Z",
    "encerraEm": "2026-11-21T12:10:00.000Z"
  }
}
```

`mercado.dlt` (CA-56):

```json
{
  "eventId": "38000000-0000-4000-8000-000000000004",
  "eventType": "mercado.dlt",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:00:00.000Z",
  "source": "mercado-service",
  "correlationId": "teste-mrc-dem",
  "aggregateId": "36000000-0000-4000-8000-000000000001",
  "payload": {
    "topicoOrigem": "demanda_criada",
    "originalEvent": {
      "eventId": "37000000-0000-4000-8000-000000000099",
      "eventType": "demanda_criada",
      "eventVersion": "1.0",
      "timestamp": "2026-11-20T11:59:00.000Z",
      "source": "demanda-service",
      "correlationId": "teste-mrc-dem",
      "aggregateId": "36000000-0000-4000-8000-000000000001"
    },
    "error": "EVENTO_INVALIDO",
    "detalhe": "Campo obrigatório ausente: payload.",
    "stackTrace": "",
    "failedAtAttempt": 1
  }
}
```

## 8. Requisitos funcionais

### 8.0 Massa de teste comum aos critérios

Configuração (`ConfigMercado`): excesso mínimo `0`, janela de adesão `60` min, duração do leilão `24` h, incremento `1` %. Relógio: o instante de cada passo é dado no cenário; sem indicação, `agora = 2026-11-20T12:00:00.000Z`.

Empresas (`tenant_id`): COMP-A `30000000-0000-4000-8000-000000000001`, FORN-X `30000000-0000-4000-8000-000000000002`, FORN-Y `30000000-0000-4000-8000-000000000003`, COMP-B `30000000-0000-4000-8000-000000000004`, FORN-Z `30000000-0000-4000-8000-000000000005`. Tokens válidos com `view_mode = "SINGLE"`: TK-COMP-A e TK-COMP-B (`BUYER`), TK-FORN-X, TK-FORN-Y e TK-FORN-Z (`SUPPLIER`), TK-FORN-X-COMPRA (empresa FORN-X, escopo `BUYER`), TK-CARRIER (`CARRIER`).

Produtos: P `35000000-0000-4000-8000-000000000001` ("Milho em grão", `TON`); Q `35000000-0000-4000-8000-000000000002` ("Soja", `TON`).

Ofertas (por `fornecimento_criado`): OF-X `34000000-0000-4000-8000-000000000001` (FORN-X, P, 8.00, 60.00, Rio Verde/GO, 11:40); OF-Y `34000000-0000-4000-8000-000000000002` (FORN-Y, P, 7.50, 40.00, Jataí/GO, 11:45); OF-Q `34000000-0000-4000-8000-000000000003` (FORN-Z, Q, 10.00, 10.00); OF-W `34000000-0000-4000-8000-000000000004` (FORN-Z, P, 8.20, 200.00, Goiânia/GO), publicada só quando o cenário disser.

Demandas (por `demanda_criada`, `tipo = "UNICA"`): DEM-A `36000000-0000-4000-8000-000000000001` (COMP-A, P, 50.00, máximo 9.00, Anápolis/GO, prazo `2026-11-30T23:59:59.000Z`, 11:55); DEM-B `36000000-0000-4000-8000-000000000002` (COMP-B, P, 80.00, sem máximo, Goiânia/GO, sem prazo, 11:57); DEM-X `36000000-0000-4000-8000-000000000009` (compradora FORN-X, P, 5.00, sem máximo, Rio Verde/GO), só quando o cenário disser.

Cenários base: **S** (oferta suficiente) = P, Q, OF-X, OF-Y, OF-Q e DEM-A (oferta de P 100.00, demanda de P 50.00). **E** (demanda excedente) = S + DEM-B (oferta 100.00, demanda 130.00, sugestão S1 `ABERTA` desde 11:57). **L** (leilão) = E + aceite de FORN-X com OF-X 60.00 às 12:00 + aceite de FORN-Y com OF-Y 40.00 às 12:10, o que fecha o lote L1 em T1 = 12:10 (RN-21).

Eventos consumidos nos cenários usam o envelope do §7.3 com `eventId` distintos; todo `negociacao_fechada` traz o `tipo` da negociação (`VENDA_DIRETA` ou `LEILAO`) e `produtoId` P. Onde o critério diz "publica", o dublê do `EventBus` registra a chamada a `publish`; onde diz "na DLT", registra uma publicação em `mercado.dlt`.

### RF-01 — Manter a cópia local das ofertas
- **Prioridade:** Must
- **Enunciado (EARS):** Quando um `fornecimento_criado`, `fornecimento_atualizado` ou `estoque_atualizado` válido for recebido, o serviço deve atualizar a cópia local da oferta e recalcular o balanço do produto.
- **Regras aplicáveis:** RN-01, RN-03, RN-04, RN-06
- **Interface:** tópicos `fornecimento_criado`, `fornecimento_atualizado`, `estoque_atualizado`; API-05

**Critérios de aceitação**
```gherkin
# CA-01
Cenário: Oferta nova aparece na vitrine
  Dado o produto P sem ofertas
  Quando o handler recebe fornecimento_criado de OF-X com preco 8.00 e quantidadeDisponivel 60.00
  Então GET /api/v1/mercado/produtos/P/ofertas com TK-COMP-A retorna 200 com 1 item: OF-X, preco 8.00, quantidadeDisponivel 60.00, apta null

# CA-02
Cenário: Evento de estoque mais antigo que o gravado é ignorado
  Dado OF-X gravada com dadosAtualizadosEm e estoqueAtualizadoEm 2026-11-20T11:40:00.000Z
  Quando o handler recebe estoque_atualizado de OF-X com quantidadeDisponivel 30.00 e atualizadoEm 2026-11-20T12:20:00.000Z
  E depois estoque_atualizado de OF-X com quantidadeDisponivel 50.00 e atualizadoEm 2026-11-20T12:10:00.000Z
  Então a vitrine mostra OF-X com quantidadeDisponivel 30.00
  E as duas mensagens ficam registradas como processadas, sem publicação na DLT
  Quando depois o handler recebe fornecimento_atualizado de OF-X com preco 8.50, quantidadeDisponivel 50.00 e atualizadoEm 2026-11-20T12:10:00.000Z
  Então a vitrine mostra OF-X com preco 8.50 e quantidadeDisponivel 30.00

# CA-03
Cenário: Alteração de preço e oferta zerada
  Dado o cenário S
  Quando o handler recebe fornecimento_atualizado de OF-X com preco 8.50, quantidadeDisponivel 60.00 e atualizadoEm 2026-11-20T12:01:00.000Z
  Então a vitrine mostra OF-X com preco 8.50
  Quando o handler recebe fornecimento_atualizado de OF-X com preco 0.00 e atualizadoEm 2026-11-20T12:02:00.000Z
  Então a vitrine não lista OF-X
  E GET /api/v1/mercado/produtos/P/balanco retorna ofertaTotal 40.00

# CA-04
Cenário: Oferta com unidade diferente da do produto vai para a DLT
  Dado o produto P com unidade TON na cópia local
  Quando o handler recebe fornecimento_criado de OF-X com unidade "KG"
  Então publica em mercado.dlt com key igual ao aggregateId recebido, error "EVENTO_INVALIDO" e failedAtAttempt 1
  E a vitrine de P não lista OF-X

# CA-05
Cenário: Estoque de oferta desconhecida é ignorado
  Dado nenhuma oferta 34000000-0000-4000-8000-000000000099
  Quando o handler recebe estoque_atualizado dessa oferta com quantidadeDisponivel 10.00
  Então nenhuma oferta é criada, a mensagem fica registrada como processada e nada é publicado
```

### RF-02 — Manter a cópia local dos produtos
- **Prioridade:** Must
- **Enunciado (EARS):** Quando um `produto_cadastrado` ou `produto_atualizado` válido for recebido, o serviço deve gravar ou atualizar o produto na cópia local.
- **Regras aplicáveis:** RN-02, RN-03
- **Interface:** tópicos `produto_cadastrado`, `produto_atualizado`; API-04

**Critérios de aceitação**
```gherkin
# CA-06
Cenário: Produto cadastrado define a unidade do balanço
  Dado nenhum produto na cópia local
  Quando o handler recebe produto_cadastrado de P com nome "Milho em grão" e unidade "TON"
  Então GET /api/v1/mercado/produtos/P/balanco com TK-FORN-X retorna 200 com unidade "TON", ofertaTotal 0.00, demandaTotal 0.00 e situacao "SEM_DEMANDA"

# CA-07
Cenário: Produto sem nome vai para a DLT
  Quando o handler recebe produto_cadastrado de P com nome "   "
  Então publica em mercado.dlt com error "EVENTO_INVALIDO"
  E GET /api/v1/mercado/produtos/P/balanco retorna 404 com codigo "PRODUTO_NAO_ENCONTRADO"
```

### RF-03 — Registrar demandas
- **Prioridade:** Must
- **Enunciado (EARS):** Quando uma `demanda_criada` ou `demanda_recorrente_gerada` válida for recebida, o serviço deve registrar a demanda e recalcular o balanço do produto; um modelo recorrente não entra no balanço.
- **Regras aplicáveis:** RN-03, RN-06, RN-08
- **Interface:** tópicos `demanda_criada`, `demanda_recorrente_gerada`; API-06

**Critérios de aceitação**
```gherkin
# CA-08
Cenário: Demanda única aguardando oferta
  Dado o produto P sem ofertas
  Quando o handler recebe demanda_criada de DEM-A
  Então GET /api/v1/mercado/demandas/DEM-A com TK-COMP-A retorna 200 com situacao "AGUARDANDO", saldo 50.00, emNegociacao 0.00 e saldoNegociavel 50.00
  E o balanço de P retorna demandaTotal 50.00 e situacao "SEM_OFERTA"
  E nenhuma sugestão de leilão é criada

# CA-09
Cenário: Modelo recorrente não entra no balanço
  Dado o cenário S
  Quando o handler recebe demanda_criada 36000000-0000-4000-8000-000000000003 de COMP-A, produto P, quantidade 10.00, tipo "RECORRENTE"
  Então o balanço de P continua com demandaTotal 50.00
  E GET /api/v1/mercado/demandas/36000000-0000-4000-8000-000000000003 com TK-COMP-A retorna 404 com codigo "DEMANDA_NAO_ENCONTRADA"

# CA-10
Cenário: Ocorrência de demanda recorrente entra no balanço
  Dado o cenário S
  Quando o handler recebe demanda_recorrente_gerada com demandaId 36000000-0000-4000-8000-000000000003, ocorrenciaId 36000000-0000-4000-8000-000000000031 e quantidade 10.00
  Então GET /api/v1/mercado/demandas/36000000-0000-4000-8000-000000000031 com TK-COMP-A retorna 200 com demandaOrigemId "36000000-0000-4000-8000-000000000003" e situacao "AGUARDANDO"
  E o balanço de P retorna demandaTotal 60.00

# CA-11
Cenário: Demanda sem destino vai para a DLT
  Quando o handler recebe demanda_criada de DEM-A sem o campo destino
  Então publica em mercado.dlt com key "36000000-0000-4000-8000-000000000001", error "EVENTO_INVALIDO" e failedAtAttempt 1
  E GET /api/v1/mercado/demandas/DEM-A com TK-COMP-A retorna 404 com codigo "DEMANDA_NAO_ENCONTRADA"
```

### RF-04 — Cancelar demandas
- **Prioridade:** Must
- **Enunciado (EARS):** Quando uma `demanda_cancelada` válida for recebida, o serviço deve tirar do balanço a demanda aguardando ou, se for um modelo recorrente, todas as ocorrências aguardando dele.
- **Regras aplicáveis:** RN-06, RN-08, RN-09
- **Interface:** tópico `demanda_cancelada`; API-06

**Critérios de aceitação**
```gherkin
# CA-12
Cenário: Demanda aguardando é cancelada
  Dado o cenário E
  Quando o handler recebe demanda_cancelada de DEM-B com compradorId COMP-B às 12:30
  Então GET /api/v1/mercado/demandas/DEM-B com TK-COMP-B retorna situacao "CANCELADA" e encerradaEm "2026-11-20T12:30:00.000Z"
  E o balanço de P retorna demandaTotal 50.00 e situacao "OFERTA_SUFICIENTE"
  Quando o handler recebe demanda_cancelada da demanda desconhecida 36000000-0000-4000-8000-000000000009 com compradorId COMP-B
  E depois demanda_criada UNICA de 36000000-0000-4000-8000-000000000009 de COMP-B, produto P e quantidade 10.00
  Então GET /api/v1/mercado/demandas/36000000-0000-4000-8000-000000000009 com TK-COMP-B retorna situacao "CANCELADA" e o balanço de P continua com demandaTotal 50.00
  E a sugestão S1 passa a "ENCERRADA"

# CA-13
Cenário: Cancelar o modelo recorrente cancela as ocorrências abertas
  Dado o modelo recorrente 36000000-0000-4000-8000-000000000003 e as ocorrências 36000000-0000-4000-8000-000000000031 e 36000000-0000-4000-8000-000000000032 aguardando
  Quando o handler recebe demanda_cancelada com demandaId 36000000-0000-4000-8000-000000000003
  Então as demandas ...031 e ...032 ficam com situacao "CANCELADA" e o balanço de P deixa de contá-las
  Quando depois o handler recebe demanda_recorrente_gerada com demandaId 36000000-0000-4000-8000-000000000003 e ocorrenciaId 36000000-0000-4000-8000-000000000033
  Então a demanda ...033 não é criada, a mensagem fica registrada como processada e nada é publicado

# CA-14
Cenário: Cancelamento repetido, de demanda desconhecida e de modelo recorrente ainda desconhecido
  Dado DEM-A com situacao "CANCELADA"
  Quando o handler recebe demanda_cancelada de DEM-A com outro eventId
  E o handler recebe demanda_cancelada de 36000000-0000-4000-8000-000000000098
  Então nenhuma demanda muda, as duas mensagens ficam registradas como processadas e nada é publicado
  Quando o handler recebe demanda_recorrente_gerada de COMP-B com demandaId 36000000-0000-4000-8000-000000000004 (modelo ainda não recebido) e ocorrenciaId 36000000-0000-4000-8000-000000000041
  E depois demanda_cancelada com demandaId 36000000-0000-4000-8000-000000000004 e compradorId COMP-B
  Então a demanda ...041 fica com situacao "CANCELADA"
  Quando depois o handler recebe demanda_recorrente_gerada com demandaId 36000000-0000-4000-8000-000000000004 e ocorrenciaId 36000000-0000-4000-8000-000000000042
  Então a demanda ...042 não é criada e nada é publicado
```

### RF-05 — Calcular o balanço do produto
- **Prioridade:** Must
- **Enunciado (EARS):** O serviço deve manter, para cada produto, a oferta total, a demanda total e a situação do balanço, recalculadas na mesma transação de toda mudança de ofertas, demandas, aceites ou negociações do produto.
- **Regras aplicáveis:** RN-04, RN-06
- **Interface:** API-04

**Critérios de aceitação**
```gherkin
# CA-15
Cenário: Demanda maior que a oferta
  Dado o cenário E
  Quando GET /api/v1/mercado/produtos/P/balanco com TK-COMP-A
  Então retorna 200 com ofertaTotal 100.00, demandaTotal 130.00, situacao "DEMANDA_EXCEDENTE", situacaoDesde "2026-11-20T11:57:00.000Z", excessoDemanda 30.00 e sugestaoId de S1

# CA-16
Cenário: Oferta cobre a demanda
  Dado o cenário S
  Quando GET /api/v1/mercado/produtos/P/balanco com TK-COMP-A
  Então retorna 200 com ofertaTotal 100.00, demandaTotal 50.00, situacao "OFERTA_SUFICIENTE", excessoDemanda 0.00 e sugestaoId null

# CA-17
Cenário: Produto desconhecido e escopo sem acesso
  Quando GET /api/v1/mercado/produtos/35000000-0000-4000-8000-000000000077/balanco com TK-COMP-A
  Então retorna 404 com codigo "PRODUTO_NAO_ENCONTRADO"
  Quando GET /api/v1/mercado/produtos/P/balanco com TK-CARRIER
  Então retorna 403 com codigo "ACESSO_NEGADO"

# CA-18
Cenário: Excesso abaixo do limiar configurado não abre sugestão
  Dado ConfigMercado com excesso mínimo 50 e o cenário E
  Quando GET /api/v1/mercado/produtos/P/balanco com TK-COMP-A
  Então retorna situacao "DEMANDA_EXCEDENTE", excessoDemanda 30.00 e sugestaoId null
  Quando o handler recebe demanda_criada 36000000-0000-4000-8000-000000000005 de COMP-B para P com quantidade 21.00
  Então o balanço retorna demandaTotal 151.00 e sugestaoId não nulo
```

### RF-06 — Manter a sugestão de leilão
- **Prioridade:** Must
- **Enunciado (EARS):** Enquanto o gatilho da sugestão for verdadeiro para um produto, o serviço deve manter uma sugestão de leilão ativa visível aos fornecedores elegíveis; quando o gatilho deixar de valer e não houver aceites, deve encerrá-la.
- **Regras aplicáveis:** RN-06, RN-16
- **Interface:** API-10

**Critérios de aceitação**
```gherkin
# CA-19
Cenário: Sugestão aparece só para fornecedores elegíveis
  Dado o cenário E
  Quando GET /api/v1/mercado/sugestoes-leilao com TK-FORN-X
  Então retorna 200 com 1 item: situacao "ABERTA", excessoDemanda 30.00, quantidadeNoLote 0.00, fornecedoresNoLote 0, janelaEncerraEm null, duracaoLeilaoHoras 24 e minhaParticipacao null
  Quando GET /api/v1/mercado/sugestoes-leilao com TK-FORN-Z
  Então retorna 200 com items []

# CA-20
Cenário: Sugestão some assim que a oferta passa a cobrir a demanda
  Dado o cenário E, sem aceites
  Quando às 12:30 o handler recebe fornecimento_criado de OF-W com quantidadeDisponivel 200.00
  Então S1 fica com situacao "ENCERRADA" e encerradaEm "2026-11-20T12:30:00.000Z"
  E GET /api/v1/mercado/sugestoes-leilao com TK-FORN-X retorna items []
  E o balanço de P retorna situacao "OFERTA_SUFICIENTE"

# CA-21
Cenário: Comprador não consulta sugestões
  Quando GET /api/v1/mercado/sugestoes-leilao com TK-COMP-A
  Então retorna 403 com codigo "ACESSO_NEGADO"
```

### RF-07 — Consultar a vitrine e a demanda
- **Prioridade:** Must
- **Enunciado (EARS):** Quando um comprador ou fornecedor consultar a vitrine de um produto, o serviço deve listar as ofertas negociáveis pelo menor preço e, se informada uma demanda do comprador, indicar as ofertas aptas para ela.
- **Regras aplicáveis:** RN-07, RN-11, RN-13
- **Interface:** API-05, API-06

**Critérios de aceitação**
```gherkin
# CA-22
Cenário: Vitrine ordenada por menor preço com aptidão
  Dado o cenário E
  Quando GET /api/v1/mercado/produtos/P/ofertas?demandaId=DEM-A com TK-COMP-A
  Então retorna 200 com items na ordem OF-Y (7.50, apta true) e OF-X (8.00, apta true) e total 2

# CA-23
Cenário: Oferta acima do preço máximo não é apta
  Dado o cenário E e fornecimento_atualizado de OF-X com preco 9.50
  Quando GET /api/v1/mercado/produtos/P/ofertas?demandaId=DEM-A com TK-COMP-A
  Então retorna OF-Y com apta true e OF-X (9.50) com apta false

# CA-24
Cenário: Demanda de outra empresa, de outro produto ou consulta por fornecedor
  Dado o cenário E
  Quando GET /api/v1/mercado/produtos/P/ofertas?demandaId=DEM-B com TK-COMP-A
  Então retorna 404 com codigo "DEMANDA_NAO_ENCONTRADA"
  Quando GET /api/v1/mercado/produtos/Q/ofertas?demandaId=DEM-A com TK-COMP-A
  Então retorna 422 com codigo "PRODUTO_DIFERENTE"
  Quando GET /api/v1/mercado/produtos/P/ofertas?demandaId=DEM-A com TK-FORN-X
  Então retorna 422 com codigo "VALIDACAO_INVALIDA"

# CA-25
Cenário: Demanda visível só ao dono
  Dado o cenário E
  Quando GET /api/v1/mercado/demandas/DEM-B com TK-COMP-A
  Então retorna 404 com codigo "DEMANDA_NAO_ENCONTRADA"
  Quando GET /api/v1/mercado/demandas/DEM-B com TK-FORN-X
  Então retorna 403 com codigo "ACESSO_NEGADO"
```

### RF-08 — Abrir negociação direta
- **Prioridade:** Must
- **Enunciado (EARS):** Quando um comprador pedir uma negociação direta para uma demanda própria, uma oferta e uma quantidade válidas, o serviço deve criar a negociação `VENDA_DIRETA`, somar a quantidade ao que está em negociação na demanda e publicar `modo_negociacao_definido`.
- **Regras aplicáveis:** RN-05, RN-07, RN-14, RN-15, RN-29, RN-30, RN-34, RN-35
- **Interface:** API-07; tópico `modo_negociacao_definido`

**Critérios de aceitação**
```gherkin
# CA-26
Cenário: Comprador abre negociação direta com a oferta escolhida
  Dado o cenário S e agora 2026-11-20T12:05:00.000Z
  Quando TK-COMP-A envia POST /api/v1/mercado/negociacoes com {"demandaId": DEM-A, "fornecimentoId": OF-Y, "quantidade": 30.00} e X-Correlation-Id "teste-mrc-05"
  Então retorna 201 com modo "VENDA_DIRETA", situacao "ABERTA", compraImediata false, demandaId DEM-A, compradorId COMP-A, quantidadeTotal 30.00, valorReferencia 225.00 e itens [OF-Y, 30.00, precoUnitario 7.50, valorItem 225.00, participacao 1.0000]
  E publica 1 modo_negociacao_definido com key = negociacaoId, correlationId "teste-mrc-05", compraImediata false, demandas[0] com quantidade 30.00 e destino Anápolis/GO, válido contra modo_negociacao_definido.schema.json
  E GET /api/v1/mercado/demandas/DEM-A retorna saldo 50.00, emNegociacao 30.00 e saldoNegociavel 20.00
  E o balanço de P retorna demandaTotal 20.00

# CA-27
Cenário: Negociações simultâneas limitadas ao saldo
  Dado o estado final da CA-26
  Quando TK-COMP-A envia POST com {DEM-A, OF-X, 20.01}
  Então retorna 422 com codigo "QUANTIDADE_ACIMA_DO_SALDO"
  Quando TK-COMP-A envia POST com {DEM-A, OF-X, 20.00}
  Então retorna 201 e a demanda fica com emNegociacao 50.00 e saldoNegociavel 0.00
  Quando TK-COMP-A envia POST com {DEM-A, OF-Y, 1.00}
  Então retorna 409 com codigo "NEGOCIACAO_JA_ABERTA"

# CA-28
Cenário: Preço acima do máximo e quantidade acima do disponível livre
  Dado o cenário E e fornecimento_atualizado de OF-X com preco 9.50
  Quando TK-COMP-A envia POST com {DEM-A, OF-X, 10.00}
  Então retorna 422 com codigo "PRECO_ACIMA_DO_MAXIMO"
  Quando TK-COMP-B envia POST com {DEM-B, OF-Y, 40.01}
  Então retorna 422 com codigo "QUANTIDADE_INDISPONIVEL"

# CA-29
Cenário: Empresa não negocia com oferta própria
  Dado o cenário S e a demanda DEM-X
  Quando TK-FORN-X-COMPRA envia POST com {DEM-X, OF-X, 5.00}
  Então retorna 403 com codigo "CONFLITO_DE_INTERESSE"
  E nada é publicado

# CA-30
Cenário: Oferta inexistente, zerada ou de outro produto
  Dado o cenário S
  Quando TK-COMP-A envia POST com {DEM-A, "34000000-0000-4000-8000-000000000099", 1.00}
  Então retorna 422 com codigo "OFERTA_INDISPONIVEL"
  Quando TK-COMP-A envia POST com {DEM-A, OF-Q, 1.00}
  Então retorna 422 com codigo "PRODUTO_DIFERENTE"

# CA-31
Cenário: Demanda de outra empresa ou encerrada
  Dado o cenário E e DEM-A cancelada
  Quando TK-COMP-A envia POST com {DEM-B, OF-Y, 1.00}
  Então retorna 404 com codigo "DEMANDA_NAO_ENCONTRADA"
  Quando TK-COMP-A envia POST com {DEM-A, OF-Y, 1.00}
  Então retorna 409 com codigo "DEMANDA_ENCERRADA"

# CA-32
Cenário: Escopo e corpo inválidos
  Dado o cenário S
  Quando TK-FORN-X envia POST /api/v1/mercado/negociacoes com corpo válido
  Então retorna 403 com codigo "ACESSO_NEGADO"
  Quando TK-COMP-A envia POST com {"demandaId": DEM-A, "fornecimentoId": OF-Y, "quantidade": "30.00"}
  Então retorna 422 com codigo "VALIDACAO_INVALIDA"
  Quando TK-COMP-A envia POST com quantidade 0.001
  Então retorna 422 com codigo "VALIDACAO_INVALIDA"
  Quando TK-COMP-A envia POST com {"demandaId": DEM-A, "fornecimentoId": OF-Y, "quantidade": 30.00, "compraImediata": "true"}
  Então retorna 422 com codigo "VALIDACAO_INVALIDA"
  Quando o handler recebe fornecimento_atualizado de OF-Y com preco 0.01 e TK-COMP-A envia POST com {DEM-A, OF-Y, 0.01}
  Então retorna 422 com codigo "VALIDACAO_INVALIDA" (valorItem 0.00) e nada é publicado

# CA-33
Cenário: Idempotency-Key repetida e compra imediata
  Dado o cenário S
  Quando TK-COMP-A envia duas vezes POST com {DEM-A, OF-Y, 30.00} e Idempotency-Key "k-1"
  Então as duas respostas são 201 com o mesmo corpo e 1 único modo_negociacao_definido é publicado
  Quando TK-COMP-A envia POST com {DEM-A, OF-Y, 10.00} e Idempotency-Key "k-1"
  Então retorna 409 com codigo "IDEMPOTENCIA_CONFLITO"
  Quando TK-COMP-A envia POST com {"demandaId": DEM-A, "fornecimentoId": OF-X, "quantidade": 10.00, "compraImediata": true}
  Então retorna 201 com compraImediata true e itens [OF-X, 10.00, precoUnitario 8.00, valorItem 80.00, participacao 1.0000]
  E publica 1 modo_negociacao_definido com compraImediata true, válido contra modo_negociacao_definido.schema.json
  E GET /api/v1/mercado/demandas/DEM-A retorna emNegociacao 40.00 e saldoNegociavel 10.00
```

### RF-09 — Formar o lote pela sugestão
- **Prioridade:** Must
- **Enunciado (EARS):** Enquanto a sugestão estiver `ABERTA` ou `EM_FORMACAO` e a janela de adesão não tiver vencido, o serviço deve permitir que fornecedores aceitem, alterem a quantidade e saiam do lote, separando localmente a quantidade aceita.
- **Regras aplicáveis:** RN-04, RN-16, RN-17, RN-18, RN-19, RN-20, RN-34
- **Interface:** API-11, API-12, API-13

**Critérios de aceitação**
```gherkin
# CA-34
Cenário: Primeiro aceite abre a janela de adesão
  Dado o cenário E
  Quando TK-FORN-X envia às 12:00 POST /api/v1/mercado/sugestoes-leilao/S1/aceites com {"fornecimentoId": OF-X, "quantidade": 60.00}
  Então retorna 201 com situacao "EM_FORMACAO", quantidadeNoLote 60.00, fornecedoresNoLote 1, janelaEncerraEm "2026-11-20T13:00:00.000Z", duracaoLeilaoHoras 24 e minhaParticipacao {OF-X, 60.00}
  E o balanço de P retorna ofertaTotal 40.00 e a vitrine não lista OF-X
  E nada é publicado

# CA-35
Cenário: Alterar a quantidade, sair do lote antes de T1 e escolher a duração do leilão
  Dado o estado final da CA-34
  Quando TK-FORN-X envia PUT /api/v1/mercado/sugestoes-leilao/S1/aceites/OF-X com {"quantidade": 50.00}
  Então retorna 200 com quantidadeNoLote 50.00 e a vitrine lista OF-X com quantidadeDisponivel 10.00
  Quando TK-FORN-X envia DELETE /api/v1/mercado/sugestoes-leilao/S1/aceites/OF-X
  Então retorna 204, S1 volta a "ABERTA" com janelaEncerraEm null e a vitrine lista OF-X com 60.00
  Quando TK-FORN-X envia às 12:00 POST com {"fornecimentoId": OF-X, "quantidade": 60.00, "duracaoLeilaoHoras": 48}
  Então retorna 201 com situacao "EM_FORMACAO" e duracaoLeilaoHoras 48
  Quando TK-FORN-Y envia às 12:05 POST com {"fornecimentoId": OF-Y, "quantidade": 40.00, "duracaoLeilaoHoras": 12}
  Então retorna 201 com situacao "CONVERTIDA" e duracaoLeilaoHoras 48
  E o leilao_iniciado publicado tem iniciaEm "2026-11-20T12:05:00.000Z" e encerraEm "2026-11-22T12:05:00.000Z"

# CA-36
Cenário: Oferta de outra empresa, de outro produto, repetida ou acima do disponível
  Dado o estado final da CA-34
  Quando TK-FORN-Y envia POST com {OF-X, 10.00}
  Então retorna 403 com codigo "OFERTA_DE_OUTRA_EMPRESA"
  Quando TK-FORN-Z envia POST com {OF-Q, 1.00}
  Então retorna 422 com codigo "PRODUTO_DIFERENTE"
  Quando TK-FORN-X envia POST com {OF-X, 1.00}
  Então retorna 409 com codigo "ACEITE_JA_REGISTRADO"
  Quando TK-FORN-Y envia POST com {OF-Y, 40.01}
  Então retorna 422 com codigo "QUANTIDADE_INDISPONIVEL"
  Quando TK-FORN-Y envia POST com {"fornecimentoId": OF-Y, "quantidade": 1.00, "duracaoLeilaoHoras": 0}, e de novo com 1.5, "48" e null
  Então cada resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando TK-FORN-Y envia PUT /api/v1/mercado/sugestoes-leilao/S1/aceites/OF-Y com {"quantidade": 1.00}
  Então retorna 404 com codigo "ACEITE_NAO_ENCONTRADO"

# CA-37
Cenário: Sugestão inexistente ou encerrada
  Quando TK-FORN-X envia POST /api/v1/mercado/sugestoes-leilao/33000000-0000-4000-8000-000000000077/aceites com {OF-X, 1.00}
  Então retorna 404 com codigo "SUGESTAO_NAO_ENCONTRADA"
  Dado o estado final da CA-20
  Quando TK-FORN-X envia POST /api/v1/mercado/sugestoes-leilao/S1/aceites com {OF-X, 1.00}
  Então retorna 409 com codigo "SUGESTAO_ENCERRADA"

# CA-38
Cenário: Oferta zerada não entra no lote
  Dado o cenário E e fornecimento_atualizado de OF-Y com quantidadeDisponivel 0.00
  Quando TK-FORN-Y envia POST /api/v1/mercado/sugestoes-leilao/S1/aceites com {OF-Y, 1.00}
  Então retorna 422 com codigo "OFERTA_INDISPONIVEL"
```

### RF-10 — Fechar o lote e iniciar o leilão em T1
- **Prioridade:** Must
- **Enunciado (EARS):** Quando a janela de adesão vencer, todos os fornecedores elegíveis estiverem no lote ou o gatilho da sugestão deixar de valer com aceites registrados, o serviço deve criar o leilão `LEILAO_DIRETO` com os itens aceitos, calcular preço inicial, incremento e participações e publicar `modo_negociacao_definido` e `leilao_iniciado`.
- **Regras aplicáveis:** RN-21, RN-22, RN-23, RN-28, RN-29, RN-33
- **Interface:** `executar_prazos`; API-11; tópicos `modo_negociacao_definido`, `leilao_iniciado`

**Critérios de aceitação**
```gherkin
# CA-39
Cenário: Lote fecha quando todos os fornecedores elegíveis aceitam
  Dado o estado final da CA-34 e agora 2026-11-20T12:10:00.000Z
  Quando TK-FORN-Y envia POST /api/v1/mercado/sugestoes-leilao/S1/aceites com {OF-Y, 40.00} e X-Correlation-Id "teste-mrc-10"
  Então retorna 201 com situacao "CONVERTIDA" e negociacaoId não nulo
  E publica, nesta ordem e com key = negociacaoId, modo_negociacao_definido com modo "LEILAO_DIRETO", demandas [] e valorReferencia 780.00, e leilao_iniciado com quantidadeTotal 100.00, precoInicial 780.00, incrementoMinimo 7.80, iniciaEm "2026-11-20T12:10:00.000Z", encerraEm "2026-11-21T12:10:00.000Z" e itens [OF-X 60.00 × 8.00 = 480.00, participacao 0.6154; OF-Y 40.00 × 7.50 = 300.00, participacao 0.3846]
  E os dois eventos são válidos contra os schemas de docs/mercado/contratos/

# CA-40
Cenário: Lote fecha no fim da janela com um único fornecedor
  Dado o estado final da CA-34
  Quando executar_prazos roda com agora 2026-11-20T12:59:59.999Z
  Então devolve 0 e nada é publicado
  Quando executar_prazos roda com agora 2026-11-20T13:00:00.000Z
  Então devolve 1 e publica leilao_iniciado com 1 item (OF-X 60.00, participacao 1.0000), precoInicial 480.00, incrementoMinimo 4.80 e encerraEm "2026-11-21T13:00:00.000Z"
  Quando executar_prazos roda de novo com o mesmo agora
  Então devolve 0 e nada é publicado

# CA-41
Cenário: Oferta nova durante a formação fecha o lote com quem aceitou
  Dado o estado final da CA-34
  Quando às 12:20 o handler recebe fornecimento_criado de OF-W com quantidadeDisponivel 200.00
  Então S1 passa a "CONVERTIDA" e é publicado leilao_iniciado com 1 item (OF-X 60.00) e iniciaEm "2026-11-20T12:20:00.000Z"

# CA-42
Cenário: Item cuja oferta encerrou durante a formação sai do lote
  Dado o estado final da CA-34 e estoque_atualizado de OF-X com operacao "ENCERRAMENTO" e quantidadeDisponivel 0.00
  Quando executar_prazos roda com agora 2026-11-20T13:00:00.000Z
  Então S1 passa a "ENCERRADA", nenhuma negociação é criada e nada é publicado

# CA-43
Cenário: Fornecedor com leilão aberto do produto não entra em outra sugestão
  Dado o cenário E, aceite de FORN-X com OF-X 60.00 e de FORN-Y com OF-Y 20.00 às 12:00, lote L1 fechado às 12:00
  E estoque_atualizado de OF-X com quantidadeDisponivel 0.00 e de OF-Y com quantidadeDisponivel 20.00 (reserva do pedido temporário)
  Quando GET /api/v1/mercado/sugestoes-leilao com TK-FORN-Y
  Então retorna items []
  Quando TK-FORN-Y envia POST /api/v1/mercado/sugestoes-leilao/S2/aceites com {OF-Y, 10.00}, sendo S2 a sugestão ativa de P aberta depois de L1
  Então retorna 409 com codigo "FORNECEDOR_EM_LEILAO"
```

### RF-11 — Registrar o resultado da negociação
- **Prioridade:** Must
- **Enunciado (EARS):** Quando um `negociacao_fechada` válido de uma negociação aberta deste serviço for recebido, o serviço deve encerrar a negociação e atualizar o saldo e o que está em negociação nas demandas envolvidas.
- **Regras aplicáveis:** RN-25, RN-26, RN-27
- **Interface:** tópico `negociacao_fechada`; API-06, API-09

**Critérios de aceitação**
```gherkin
# CA-44
Cenário: Acordo na direta abate o saldo
  Dado o estado final da CA-26 (negociação D1)
  Quando o handler recebe negociacao_fechada de D1 com status "ENCERRADO" e alocacoes [{OF-Y, quantidade 30.00}]
  Então GET /api/v1/mercado/negociacoes/D1 retorna situacao "CONCLUIDA"
  E DEM-A fica com saldo 20.00, emNegociacao 0.00 e situacao "AGUARDANDO"
  Quando TK-COMP-A abre a direta D2 com {DEM-A, OF-X, 20.00} e o handler recebe negociacao_fechada de D2 com status "ENCERRADO" e alocacoes [{OF-X, 20.00}]
  Então DEM-A fica com saldo 0.00 e situacao "ATENDIDA"

# CA-45
Cenário: Direta sem acordo devolve a quantidade ao saldo negociável
  Dado o estado final da CA-26
  Quando o handler recebe negociacao_fechada de D1 com status "RECUSADO" e alocacoes []
  Então D1 fica "SEM_ACORDO" e DEM-A fica com saldo 50.00, emNegociacao 0.00 e saldoNegociavel 50.00

# CA-46
Cenário: Leilão vencido com demanda indicada pelo comprador
  Dado o cenário L
  Quando o handler recebe negociacao_fechada de L1 com status "ENCERRADO", compradorId COMP-B, demandaId DEM-B e alocacoes [{OF-X, 60.00}, {OF-Y, 40.00}]
  Então L1 fica "CONCLUIDA" e DEM-B fica com saldo 0.00 e situacao "ATENDIDA"

# CA-47
Cenário: Leilão deserto não muda demandas
  Dado o cenário L
  Quando o handler recebe negociacao_fechada de L1 com status "DESERTO" e alocacoes []
  Então L1 fica "SEM_ACORDO" e DEM-A e DEM-B continuam "AGUARDANDO" com saldo 50.00 e 80.00

# CA-48
Cenário: Fechamento desconhecido, repetido ou com demanda de outro comprador
  Dado o cenário L
  Quando o handler recebe negociacao_fechada de 31000000-0000-4000-8000-000000000099 com status "ENCERRADO"
  Então nada muda, a mensagem fica registrada como processada e nada é publicado
  Quando o handler recebe negociacao_fechada de L1 com status "ENCERRADO", compradorId COMP-B e demandaId DEM-A
  Então L1 fica "CONCLUIDA" e DEM-A continua "AGUARDANDO" com saldo 50.00
  Quando o handler recebe outro negociacao_fechada de L1 com status "DESERTO"
  Então L1 continua "CONCLUIDA"
```

### RF-12 — Retirar do balanço demandas com prazo vencido
- **Prioridade:** Should
- **Enunciado (EARS):** Quando `executar_prazos` rodar com `agora` maior que o `prazoDesejado` de uma demanda aguardando, o serviço deve marcar `prazoVencido`, retirá-la do balanço e recusar novas negociações diretas para ela, sem mudar a situação da demanda.
- **Regras aplicáveis:** RN-10, RN-14, RN-33
- **Interface:** `executar_prazos`; API-06, API-07

**Critérios de aceitação**
```gherkin
# CA-49
Cenário: Demanda com prazo vencido sai do balanço
  Dado o cenário S
  Quando executar_prazos roda com agora 2026-11-30T23:59:59.000Z
  Então devolve 0 e GET /api/v1/mercado/demandas/DEM-A retorna prazoVencido false
  Quando executar_prazos roda com agora 2026-11-30T23:59:59.001Z
  Então devolve 1, DEM-A continua "AGUARDANDO" com prazoVencido true e encerradaEm null, e o balanço de P retorna demandaTotal 0.00
  Quando TK-COMP-A envia POST /api/v1/mercado/negociacoes com {DEM-A, OF-Y, 10.00}
  Então retorna 409 com codigo "DEMANDA_ENCERRADA"
```

### RF-13 — Consultar negociações
- **Prioridade:** Must
- **Enunciado (EARS):** Quando um comprador ou fornecedor consultar negociações, o serviço deve devolver os leilões e as negociações diretas de que a empresa participa, com o contexto atual do produto nos leilões abertos.
- **Regras aplicáveis:** RN-12, RN-24
- **Interface:** API-08, API-09

**Critérios de aceitação**
```gherkin
# CA-50
Cenário: Cada empresa vê leilões e as próprias diretas
  Dado o cenário L e a direta D1 de COMP-A com {DEM-A, OF-Y, 10.00}, aberta às 12:05
  Quando GET /api/v1/mercado/negociacoes com TK-COMP-A
  Então retorna total 2 (D1 e L1)
  Quando GET /api/v1/mercado/negociacoes?modo=VENDA_DIRETA com TK-COMP-B
  Então retorna total 0
  Quando GET /api/v1/mercado/negociacoes com TK-FORN-Y
  Então retorna total 2
  Quando GET /api/v1/mercado/negociacoes?size=101 com TK-COMP-A
  Então retorna 422 com codigo "VALIDACAO_INVALIDA"

# CA-51
Cenário: Leilão aberto mostra que a oferta passou a cobrir a demanda
  Dado o cenário L, estoque_atualizado de OF-X e OF-Y com quantidadeDisponivel 0.00 às 12:15 e fornecimento_criado de OF-W às 12:30
  Quando GET /api/v1/mercado/negociacoes/L1 com TK-COMP-B às 13:00
  Então retorna 200 com contextoProduto {"situacao": "OFERTA_SUFICIENTE", "desde": "2026-11-20T12:30:00.000Z"} e situacao "ABERTA"

# CA-52
Cenário: Direta de outra empresa
  Dado a direta D1 de COMP-A
  Quando GET /api/v1/mercado/negociacoes/D1 com TK-COMP-B
  Então retorna 404 com codigo "NEGOCIACAO_NAO_ENCONTRADA"
```

### RF-14 — Autenticar e autorizar
- **Prioridade:** Must
- **Enunciado (EARS):** Se o token for ausente, inválido, sem contexto de empresa ou de escopo não permitido, então o serviço deve recusar a requisição antes de qualquer efeito.
- **Regras aplicáveis:** RN-12, RN-34
- **Interface:** §7.1; todas as rotas `/api/v1/mercado`

**Critérios de aceitação**
```gherkin
# CA-53
Cenário: Token ausente, sem contexto e escopo errado
  Quando GET /api/v1/mercado/produtos/P/balanco sem Authorization
  Então retorna 401 com codigo "TOKEN_INVALIDO" e o cabeçalho WWW-Authenticate "Bearer"
  Quando a mesma chamada usa um token válido com view_mode "NONE"
  Então retorna 403 com codigo "CONTEXTO_NAO_SELECIONADO"
  Quando a mesma chamada usa TK-CARRIER
  Então retorna 403 com codigo "ACESSO_NEGADO"

# CA-54
Cenário: Chaves públicas indisponíveis
  Dado chaves_publicas.obter lançando exceção
  Quando GET /api/v1/mercado/produtos/P/balanco com TK-COMP-A
  Então retorna 503 com codigo "DEPENDENCIA_INDISPONIVEL"
```

### RF-15 — Consumir mensagens com idempotência e DLT
- **Prioridade:** Must
- **Enunciado (EARS):** Quando uma mensagem for recebida, o serviço deve validá-la, ignorar duplicatas válidas, repetir o processamento em falha até 3 execuções e publicar na DLT a mensagem inválida ou que continuar falhando; com o banco indisponível, a mensagem é reentregue, sem ir para a DLT.
- **Regras aplicáveis:** RN-31, RN-32
- **Interface:** todos os tópicos do §7.3; `mercado.dlt`

**Critérios de aceitação**
```gherkin
# CA-55
Cenário: Mesma demanda entregue cinco vezes
  Dado o cenário S sem DEM-A
  Quando o handler recebe 5 vezes o mesmo envelope de demanda_criada de DEM-A
  Então existe 1 demanda DEM-A, o balanço retorna demandaTotal 50.00 e (demanda-service, eventId) aparece 1 vez em processed_events

# CA-56
Cenário: Envelope sem payload, duas vezes
  Quando o handler recebe 2 vezes o envelope de demanda_criada com aggregateId DEM-A e sem o campo payload
  Então publica 2 vezes em mercado.dlt com key "36000000-0000-4000-8000-000000000001", topicoOrigem "demanda_criada", error "EVENTO_INVALIDO", stackTrace "" e failedAtAttempt 1
  E o eventId não é registrado como processado

# CA-57
Cenário: Banco indisponível e falha persistente
  Dado repositorio.transacao lançando RepositorioIndisponivel
  Quando o handler recebe demanda_criada de DEM-A
  Então o handler propaga RepositorioIndisponivel, nada é publicado e o eventId não é registrado como processado
  Dado o repositório lançando ValueError em toda transação
  Quando o handler recebe demanda_criada de DEM-A
  Então clock.sleep é chamado com 1 e 2, nesta ordem
  E publica em mercado.dlt com error "PROCESSAMENTO_FALHOU", failedAtAttempt 3 e stackTrace não vazio
```

### RF-16 — Saúde, prontidão e métricas
- **Prioridade:** Must
- **Enunciado (EARS):** O serviço deve expor `/health`, `/health/ready` e `/metrics` sem autenticação, com a prontidão dependente do banco e do broker.
- **Regras aplicáveis:** —
- **Interface:** API-01, API-02, API-03

**Critérios de aceitação**
```gherkin
# CA-58
Cenário: Prontidão acompanha as dependências
  Quando GET /health
  Então retorna 200 com {"status": "up"}
  Dado repositorio.disponivel() devolvendo false
  Quando GET /health/ready
  Então retorna 503 com codigo "DEPENDENCIA_INDISPONIVEL"
  Dado repositorio.disponivel() e event_bus.disponivel() devolvendo true
  Quando GET /health/ready
  Então retorna 200 com {"status": "ready"}
  Quando GET /metrics
  Então retorna 200 em formato texto do Prometheus
```

### RF-17 — Tratar falhas inesperadas
- **Prioridade:** Must
- **Enunciado (EARS):** Se uma exceção não prevista ocorrer numa rota de negócio, então o serviço deve responder `500 ERRO_INTERNO` no formato do §10, sem alterar estado.
- **Regras aplicáveis:** RN-34
- **Interface:** todas as rotas `/api/v1/mercado`

**Critérios de aceitação**
```gherkin
# CA-59
Cenário: Exceção inesperada do repositório
  Dado o cenário S e repositorio.obter_demanda lançando ValueError
  Quando TK-COMP-A envia POST /api/v1/mercado/negociacoes com {DEM-A, OF-Y, 10.00}
  Então retorna 500 com codigo "ERRO_INTERNO" e Content-Type application/problem+json
  E nada é publicado
```

## 9. Regras de negócio

| ID | Regra | Exemplo concreto |
|---|---|---|
| RN-01 | Cópia da oferta, com dois instantes porque preço e estoque chegam por tópicos diferentes e o Kafka não garante ordem entre tópicos. Instante do evento = `criadoEm` ou `atualizadoEm` do payload. `fornecimento_criado` e `fornecimento_atualizado`: gravam `fornecedorId`, `produtoId`, `unidade`, `preco` e `origem` se o instante for **maior** que `dadosAtualizadosEm`, e `quantidadeDisponivel` se for **maior** que `estoqueAtualizadoEm`; cada parte atualiza o seu instante. `estoque_atualizado`: grava `quantidadeDisponivel` se o instante for **maior** que `estoqueAtualizadoEm`. Igual ou menor → aquela parte é ignorada, sem DLT. `estoque_atualizado` ou `fornecimento_atualizado` de oferta desconhecida → ignorado, sem DLT. `fornecimento_criado` de oferta já gravada segue a mesma comparação de instante. (DG-05, PROF-10) | Estoque 12:20 com 30.00 e depois estoque 12:10 com 50.00 → fica 30.00. Estoque 12:20 com 30.00 e depois `fornecimento_atualizado` 12:10 com preço 8.50 e 50.00 → preço 8.50, estoque 30.00. |
| RN-02 | Cópia do produto: `produto_cadastrado` e `produto_atualizado` gravam `nome`, `unidade` e `atualizadoEm`, com a mesma comparação de instante da RN-01, feita depois de `bloquear_produto` (DG-68). | `produto_atualizado` com instante igual ao gravado → ignorado. |
| RN-03 | Unidade: se o produto estiver na cópia local, oferta (`fornecimento_*`) e demanda (`demanda_*`) com `unidade` diferente da do produto são mensagens inválidas (DLT `EVENTO_INVALIDO`, detalhe `"Unidade diferente da do produto."`). A comparação é feita depois de bloquear o produto (e, nos eventos de demanda, os IDs antes dele, DG-68), para valer a unidade gravada naquele momento; o `demanda_criada` `RECORRENTE` não bloqueia o produto (§13.1) (DG-69). Produto ainda desconhecido: a unidade do evento é aceita sem comparação. Não há conversão entre unidades. (DG-06) | P em `TON`, oferta em `KG` → DLT. |
| RN-04 | Oferta negociável: `preco > 0` e `quantidadeDisponivel > 0`. Quantidade comprometida = soma das quantidades dos aceites da oferta em sugestões `EM_FORMACAO`. Disponível livre = `max(0, quantidadeDisponivel − quantidade comprometida)`. A reserva de estoque dos pedidos temporários (pedidos pela Negociação, na direta e no leilão) já chega descontada em `quantidadeDisponivel`; o serviço não faz outro desconto. (DG-05, DG-10, DG-51, PROF-09) | Disponível 60.00 com aceite de 60.00 → livre 0.00. |
| RN-05 | Escala: valores e quantidades com mais de 2 casas (participação: 4 casas) são rejeitados na entrada; toda conta usa `Decimal` e arredonda só onde a regra manda. | `quantidade` 0.001 → `422 VALIDACAO_INVALIDA`. |
| RN-06 | Balanço por `produtoId`: oferta total = soma do disponível livre das ofertas negociáveis; demanda total = soma do saldo negociável das demandas abertas; situação conforme o glossário. É recalculado, com `bloquear_produto`, na mesma transação de toda mudança de ofertas, demandas, aceites ou negociações do produto. Quando a situação muda, inclusive no primeiro cálculo do produto, grava `situacaoDesde = agora` (`salvar_situacao_balanco`). Depois de cada recálculo, aplica a RN-16 e, se houver sugestão `EM_FORMACAO` com o gatilho falso, a RN-21. Produto conhecido = na cópia de produtos ou referenciado por oferta ou demanda (`obter_produto` ou `obter_situacao_balanco` não nulo); senão a API-04 e a API-05 respondem `404 PRODUTO_NAO_ENCONTRADO`. (DG-01, DG-02, DG-05, PROF-01) | Cenário E: 100.00 × 130.00 → `DEMANDA_EXCEDENTE`, excesso 30.00. |
| RN-07 | Aptidão (oferta para demanda): mesmo `produtoId`; oferta negociável com disponível livre > 0; `preco ≤ precoMaximo` quando `precoMaximo` não é nulo; `fornecedorId ≠ compradorId`; demanda aberta (aguardando e sem prazo vencido; senão `apta = false` para todas as ofertas). A quantidade não precisa cobrir a demanda inteira (atendimento parcial, RN-15). (DG-32) | DEM-A (máx. 9.00) × OF-X a 9.50 → não apta. |
| RN-08 | Demandas: `demanda_criada` `UNICA` cria a demanda `AGUARDANDO` com `saldo = quantidade`; `RECORRENTE` só grava o modelo (fora do balanço e das rotas); `demanda_recorrente_gerada` cria a demanda com `demandaId = ocorrenciaId` e `demandaOrigemId = demandaId` do evento; se o modelo de origem estiver gravado com `cancelado = true`, ou houver cancelamento antecipado (RN-09) do `demandaId` de origem com o mesmo `compradorId`, a ocorrência é ignorada, sem DLT (DG-04). Se houver cancelamento antecipado (RN-09) do mesmo `demandaId` (ou `ocorrenciaId`) e do mesmo `compradorId`, a demanda nasce `CANCELADA` com `encerradaEm = agora` e o modelo nasce com `cancelado = true`; nenhum dos dois entra no balanço. `demandaId` já existente (com outro `eventId`) → ignorado, sem DLT. `destino` é obrigatório. (DG-02, DG-03) | Ocorrência ...031 de origem ...003 → demanda ...031. |
| RN-09 | Cancelamento: se `demandaId` for uma demanda `AGUARDANDO` com o mesmo `compradorId` → `CANCELADA`, `encerradaEm = agora`. Se for um modelo recorrente com o mesmo `compradorId` → modelo cancelado e todas as demandas `AGUARDANDO` com aquele `demandaOrigemId` → `CANCELADA`. `demandaId` desconhecido (nem demanda nem modelo) → grava o cancelamento antecipado (`salvar_cancelamento_antecipado`), porque `demanda_cancelada` pode chegar antes da criação (tópicos diferentes), e cancela as demandas `AGUARDANDO` com `demandaOrigemId` igual a esse `demandaId` e o mesmo `compradorId` (ocorrências que chegaram antes do modelo). Já encerrado ou com outro `compradorId` → ignorado, sem DLT. Negociações diretas abertas da demanda continuam. (DG-04) | Cancelar ...003 → ...031 e ...032 `CANCELADA`. |
| RN-10 | Prazo vencido: em `executar_prazos`, demanda `AGUARDANDO` com `prazoVencido = false` e `prazoDesejado < agora` passa a `prazoVencido = true`, sem mudar `situacao` nem `encerradaEm`, e o balanço do produto é recalculado. Demanda com prazo vencido não conta no balanço e não abre negociação direta (`409 DEMANDA_ENCERRADA`); diretas já abertas continuam. `prazoDesejado` nulo nunca vence. O Mercado não declara a demanda expirada: isso é regra do Demanda (DG-48). | Prazo 23:59:59.000; agora 23:59:59.001 → `prazoVencido` true, demanda total 0.00. |
| RN-11 | Vitrine pública para compradores e fornecedores, com preço, quantidade, origem e fornecedor de cada oferta (DG-49): ofertas negociáveis do produto com disponível livre > 0, em ordem de `preco` crescente; empate por disponível livre decrescente; depois `criadaEm` crescente; depois `fornecimentoId` crescente. A vitrine é o "leilão reverso" do professor: só ordenação, sem evento nem prazo. (PROF-06) | OF-Y 7.50 antes de OF-X 8.00. |
| RN-12 | Escopos e visibilidade: rotas de negócio aceitam só `BUYER` e `SUPPLIER`, conforme a tabela do §7.2; escopo errado → `403 ACESSO_NEGADO`. O balanço (API-04) é consultável por qualquer `BUYER` ou `SUPPLIER`, mesmo sem oferta ou demanda do produto, e mostra só totais (DG-50). Leilões são visíveis a todo token `BUYER` ou `SUPPLIER`. Negociação direta é visível ao comprador (`BUYER` e `compradorId` = empresa) e ao fornecedor da oferta (`SUPPLIER` e `fornecedorId` = empresa); invisível → `404 NEGOCIACAO_NAO_ENCONTRADA`. | COMP-B em D1 de COMP-A → 404. |
| RN-13 | Demanda visível só ao comprador dono (`BUYER` e `compradorId` = empresa); outra empresa ou demanda inexistente → `404 DEMANDA_NAO_ENCONTRADA`. Vale para a API-06, o `demandaId` da API-05 e da API-07. | COMP-A em DEM-B → 404. |
| RN-14 | Negociação direta: o comprador escolhe a oferta e a quantidade (o serviço nunca forma pares sozinho). Na ordem do §7.2: demanda visível (RN-13); demanda `AGUARDANDO` e sem prazo vencido (senão `409 DEMANDA_ENCERRADA`); oferta existente e negociável (senão `422 OFERTA_INDISPONIVEL`); mesmo produto (senão `422 PRODUTO_DIFERENTE`); `fornecedorId ≠ compradorId` (senão `403 CONFLITO_DE_INTERESSE`); preço dentro do máximo (senão `422 PRECO_ACIMA_DO_MAXIMO`); nenhuma direta `ABERTA` para o mesmo par demanda × oferta (senão `409 NEGOCIACAO_JA_ABERTA`); quantidade ≤ saldo negociável (senão `422 QUANTIDADE_ACIMA_DO_SALDO`); quantidade ≤ disponível livre (senão `422 QUANTIDADE_INDISPONIVEL`); `valorItem` (`quantidade × precoUnitario`, 2 casas, `ROUND_HALF_UP`) maior que `0.00` e no máximo `99999999999999.99` (senão `422 VALIDACAO_INVALIDA`). O preço da oferta é copiado para o item e não muda depois. A Negociação abre a direta com o mesmo `negociacaoId` e pede o pedido temporário, que nasce na abertura (PROF-03). (DG-30, DG-31, DG-34, DG-52) | DEM-A + OF-Y 30.00 → 201, `valorItem` 225.00. |
| RN-15 | Saldo: abrir uma direta soma a quantidade em `emNegociacao`. Uma demanda pode ter várias diretas abertas com ofertas diferentes, desde que a soma não passe do saldo. (DG-32, DG-33) | DEM-A 50.00: 30.00 + 20.00 → ok; mais 0.01 → 422. |
| RN-16 | Sugestão: no máximo uma ativa (`ABERTA` ou `EM_FORMACAO`) por produto. Recalculado o balanço: sem sugestão ativa e gatilho verdadeiro → cria uma `ABERTA`; sugestão `ABERTA` e gatilho falso → `ENCERRADA`. `excessoDemanda` é atualizado a cada recálculo. A API-10 mostra a sugestão à empresa com aceite nela ou que é fornecedor elegível; nunca mostra nomes de outros fornecedores. Sem expiração por tempo. (DG-01, DG-07, DG-09, DG-12) | Oferta nova cobre a demanda → `ENCERRADA` na hora. |
| RN-17 | Aceite (API-11), na ordem do §7.2: sugestão ativa e, se `EM_FORMACAO`, `agora < janelaEncerraEm` (senão `409 SUGESTAO_ENCERRADA`); oferta existente e negociável (senão `422 OFERTA_INDISPONIVEL`); oferta da empresa do token (senão `403 OFERTA_DE_OUTRA_EMPRESA`); mesmo produto (senão `422 PRODUTO_DIFERENTE`); empresa sem item em leilão `ABERTA` do produto (senão `409 FORNECEDOR_EM_LEILAO`); oferta sem aceite na sugestão (senão `409 ACEITE_JA_REGISTRADO`); quantidade ≤ disponível livre (senão `422 QUANTIDADE_INDISPONIVEL`). Uma empresa pode ter parte do estoque no lote e o resto na direta. (DG-14, DG-15, DG-17) | FORN-Y com OF-Y 40.01 (livre 40.00) → 422. |
| RN-18 | Janela de adesão: o primeiro aceite de uma sugestão `ABERTA` a leva a `EM_FORMACAO` com `janelaEncerraEm = agora + LEILAO_JANELA_ADESAO_MINUTOS` e grava na sugestão o `correlationId` da requisição e a duração do leilão (`duracaoLeilaoHoras` do aceite, ou `null` para usar `LEILAO_DURACAO_HORAS`). Aceites seguintes não mudam a janela, a duração nem o `correlationId`; o `duracaoLeilaoHoras` deles é ignorado. Quando a sugestão volta a `ABERTA` (último aceite removido), janela, duração e `correlationId` voltam a `null`. (DG-10, DG-13, DG-70) | Aceite às 12:00 com 48 → janela até 13:00 e leilão de 48 h. |
| RN-19 | Alterar e sair (API-12, API-13): permitidos enquanto a sugestão está `EM_FORMACAO` e `agora < janelaEncerraEm`; senão `409 SUGESTAO_ENCERRADA`. Na alteração, a nova quantidade deve ser ≤ disponível livre + quantidade atual do aceite (senão `422 QUANTIDADE_INDISPONIVEL`). Aceite inexistente ou de oferta de outra empresa → `404 ACEITE_NAO_ENCONTRADO`. Ao sair o último aceite: gatilho verdadeiro → `ABERTA` com `janelaEncerraEm = null` e `correlationId = null`; falso → `ENCERRADA`. (DG-18) | Sair de S1 com gatilho verdadeiro → `ABERTA`. |
| RN-20 | Depois de T1 não há entrada, alteração nem saída: o lote fica fixo. (DG-14) | API-11 em S1 `CONVERTIDA` → 409. |
| RN-21 | Fechamento do lote (T1 = `agora`), numa sugestão `EM_FORMACAO`, no primeiro destes: (a) `executar_prazos` com `janelaEncerraEm <= agora`; (b) depois de um aceite (API-11), todas as empresas fornecedoras elegíveis têm aceite; (c) recálculo do balanço com o gatilho falso. Itens na ordem de `aceitoEm` e depois `fornecimentoId`, com `quantidade = min(quantidade aceita, quantidadeDisponivel atual)`; itens com quantidade 0, com `valorItem` 0.00 (RN-22) ou de oferta não negociável saem; depois, na ordem acima, sai cada item que faria o lote passar de 1000 itens, de `9999999999.99` na soma das quantidades ou de `99999999999999.99` na soma dos `valorItem`. Sem itens → sugestão `ENCERRADA`, sem negociação e sem evento; o balanço é recalculado na mesma transação e, com o gatilho verdadeiro, uma nova sugestão abre ali (DG-65). Com itens → cria a negociação `LEILAO_DIRETO` `ABERTA` com `iniciaEm = T1` e `encerraEm = T1 + duração do leilão` (RN-18, DG-70), a sugestão vai a `CONVERTIDA` com o `negociacaoId`, e são publicados `modo_negociacao_definido` e `leilao_iniciado`, nessa ordem. A Negociação então pede o pedido temporário de cada item e só libera os lances com todas as reservas confirmadas; item com reserva recusada sai do lote antes do primeiro lance (DG-51). Depois, o balanço é recalculado (uma nova sugestão pode abrir para os demais fornecedores). (DG-10, DG-13, DG-16, PROF-02, PROF-03) | Cenário L: lote fechado às 12:10 com OF-X e OF-Y. |
| RN-22 | Preço inicial e incremento: `valorItem = quantidade × precoUnitario` (`ROUND_HALF_UP`, 2 casas); `precoInicial = Σ valorItem`; `incrementoMinimo = max(0.01, precoInicial × LEILAO_INCREMENTO_MINIMO_PERCENTUAL / 100)` (`ROUND_HALF_UP`, 2 casas). O preço inicial é o valor mínimo do primeiro lance; o lance é o valor total do lote inteiro. (DG-11, DG-19, DG-20, PROF-04, PROF-05) | 480.00 + 300.00 = 780.00; incremento 7.80. 0.01 × 0.01 → `valorItem` 0.00 → o item sai do lote (RN-21). |
| RN-23 | Participação, pelo maior resto: (1) cada item recebe `valorItem / precoInicial` truncado em 4 casas; o que daria `0.0000` fica com `0.0001` (os schemas exigem participação > 0); (2) seja `d = 1.0000 − Σ participações`; se `d > 0`, soma-se `0.0001` a `d / 0.0001` itens, um por item, na ordem do maior resto (`valorItem / precoInicial` menos o valor truncado; empate: o primeiro na ordem); se `d < 0` (só por causa do piso), subtrai-se `|d|` começando pelo item de maior participação (empate: o primeiro na ordem), sem deixar nenhum abaixo de `0.0001`, e o que faltar sai do seguinte. O limite de 1000 itens (RN-21) garante que a soma é sempre `1.0000` e nenhuma participação fica negativa ou zero. Na direta, `participacao = 1.0000`. A participação de 4 casas é informativa (telas e API-09); o rateio do valor vencedor é feito pela Negociação na proporção exata de `valorItem`: cada item recebe `lance × valorItem / precoInicial`, arredondado para baixo em centavos, e o que sobra vai para o item de maior `valorItem` (DG-22). Com o lance igual ou acima do preço inicial, nenhum item recebe menos que o seu `valorItem`; se um item sair por reserva recusada, ela recalcula preço inicial, incremento e participações pela RN-22 e por esta regra, sobre os itens restantes, com o `incrementoMinimoPercentual` do `leilao_iniciado` (não na proporção do incremento anterior). (DG-22, DG-23, DG-51) | 480/780 → 0.6154; 300/780 → 0.3846; soma 1.0000. Item de 0.01 num lote de 1000.01 → 0.0001; o maior fica com 0.9999. 155 itens de valor igual → 0.0065 nos 80 primeiros e 0.0064 nos 75 seguintes. |
| RN-24 | Contexto do produto: no detalhe (API-09) de um `LEILAO_DIRETO` `ABERTA`, `contextoProduto = {situacao, desde}` com a situação atual do balanço do produto e `situacaoDesde`. Só consulta; o leilão nunca é interrompido por mudança de balanço. (DG-08) | Oferta nova às 12:30 → `OFERTA_SUFICIENTE` desde 12:30. |
| RN-25 | `negociacao_fechada` de `negociacaoId` desconhecido ou de negociação já em estado final → ignorado, registrado como processado, sem DLT. Vale também para um fechamento republicado com outro `eventId` (por exemplo, corrigido pela Negociação depois de ir para a DLT do Pedidos): só o primeiro é aplicado. (DG-53) | Segundo fechamento de L1 → sem efeito. |
| RN-26 | Negociação `ABERTA`: `status = "ENCERRADO"` → `CONCLUIDA`; `DESERTO`, `RECUSADO` ou `EXPIRADO` → `SEM_ACORDO`; `encerradaEm = agora`. (DG-29) | Leilão deserto → `SEM_ACORDO`. |
| RN-27 | Efeito nas demandas. **Direta:** a quantidade do item sai de `emNegociacao` da demanda; com `ENCERRADO`, `acordado = Σ alocacoes[].quantidade` sai do `saldo` (sem ficar negativo). **Leilão:** com `ENCERRADO` e `demandaId` não nulo que seja demanda `AGUARDANDO` do mesmo `compradorId` e produto, `min(saldo negociável, Σ alocacoes[].quantidade)` sai do `saldo`; outro caso não muda demandas. Em qualquer caso, demanda `AGUARDANDO` com `saldo = 0` → `ATENDIDA`, `encerradaEm = agora`. O valor acordado não é usado. (DG-27, DG-32, DG-34) | DEM-B 80.00 com lote de 100.00 → `ATENDIDA`. |
| RN-28 | Fornecedor elegível e conflito no leilão: quem tem oferta no lote não dá lance nele, e qualquer comprador pode dar lance, com ou sem demanda; essas regras são aplicadas pela Negociação. Este serviço envia em `leilao_iniciado` os `fornecedorId` dos itens. (DG-24, DG-25, DG-26) | — |
| RN-29 | Destino: na direta, o destino da demanda segue em `modo_negociacao_definido` como referência para a Negociação. O destino do pedido, na direta e no leilão, é informado pelo comprador no Pedidos depois da venda (ESP-PED-001 D-02). (DG-28) | — |
| RN-30 | Idempotência HTTP: escopo = (empresa, `sub`, método, caminho canônico, `Idempotency-Key`). Só respostas 2xx são memorizadas, com status, corpo, `Location` e o hash do corpo da requisição, por 24 h, na mesma transação da mudança. Mesmo escopo e mesmo corpo → mesma resposta, sem efeito e sem evento. Mesmo escopo e outro corpo → `409 IDEMPOTENCIA_CONFLITO`. | Repetir CA-33 com o mesmo corpo → 1 evento. |
| RN-31 | Consumo: a validação (RN-32) vem antes da deduplicação. Mensagem válida com (`source`, `eventId`) já processado → sem efeito, sem publicação. Efeito e `registrar_evento_processado` na mesma transação. `RepositorioIndisponivel` → o handler propaga a exceção, sem publicar nada (o adaptador não confirma o offset e reentrega a cada 5 s, sem limite; DG-57). Outra exceção → o handler repete a transação até 3 execuções no total, chamando `clock.sleep(1)` e `clock.sleep(2)` entre elas, como a biblioteca comum do kit (`services/common/consumer.py`); se a terceira falhar, publica em `mercado.dlt` com `error` `"PROCESSAMENTO_FALHOU"`, `failedAtAttempt` 3 e `stackTrace` com a pilha de chamadas da última exceção, fora da transação que falhou. | Banco fora do ar → reentrega, sem DLT; `ValueError` 3 vezes → DLT com tentativa 3. |
| RN-32 | Mensagem consumida inválida (§7.3, RN-03) não altera estado e é publicada em `mercado.dlt` na hora, com `error` `"EVENTO_INVALIDO"`, `failedAtAttempt` 1 e chave conforme §7.4. Não é registrada como processada. Aqui o handler não propaga exceção. | Envelope sem `payload` → DLT com chave DEM-A. |
| RN-33 | `executar_prazos` processa, cada um numa transação própria e com `bloquear_produto`, os itens de `listar_janelas_vencidas(agora, 100)` (RN-21 a) e de `listar_demandas_vencidas(agora, 100)` (RN-10), verificando de novo a condição antes de agir. Chamar duas vezes no mesmo instante não repete efeitos. Devolve quantos registros mudaram (sugestões fechadas e demandas marcadas com prazo vencido). | Segunda chamada às 13:00 → 0. |
| RN-34 | Requisição rejeitada (qualquer 4xx ou 5xx) não altera estado, não memoriza idempotência e não publica evento. | Direta com 422 → `emNegociacao` igual. |
| RN-35 | Compra imediata: `compraImediata` (API-07, opcional, ausente = `false`) é gravado na negociação e repassado no `modo_negociacao_definido`; no leilão é sempre `false`. Não muda as validações da RN-14, o saldo (RN-15) nem a aplicação do `negociacao_fechada` (RN-25 a RN-27). Quem encerra é a Negociação: com a reserva confirmada, fecha a direta como `ENCERRADO` pelo preço da oferta e publica o `negociacao_fechada` de sempre; com a reserva recusada, `RECUSADO`. Este serviço não acompanha a reserva. (DG-58) | DEM-A + OF-X 10.00 com `compraImediata` true → 201 e evento com `compraImediata` true. |

## 10. Catálogo de erros

Formato único de erro (`Content-Type: application/problem+json`), igual ao da ESP-PED-001 e da ESP-NEG-001:

```json
{
  "type": "about:blank",
  "title": "Conflito",
  "status": 409,
  "codigo": "SUGESTAO_ENCERRADA",
  "detail": "A sugestão de leilão não aceita mais mudanças.",
  "instance": "/api/v1/mercado/sugestoes-leilao/33000000-0000-4000-8000-000000000001/aceites",
  "correlationId": "teste-mrc-09",
  "timestamp": "2026-11-20T13:00:00.000Z"
}
```

`type` é sempre `"about:blank"`; `instance` é o caminho sem query string; `correlationId` é o valor efetivo do §7.1; `timestamp` é `clock.now()`. `title`, `status`, `codigo` e `detail` são os da tabela, byte a byte.

| Código | HTTP | `title` | `detail` | Quando ocorre |
|---|---|---|---|---|
| TOKEN_INVALIDO | 401 | Não autenticado | Token de acesso ausente ou inválido. | §7.1, passo 1. |
| CONTEXTO_NAO_SELECIONADO | 403 | Acesso negado | Selecione um contexto de empresa válido. | §7.1, passo 2. |
| ACESSO_NEGADO | 403 | Acesso negado | Você não tem permissão para esta operação. | RN-12 (escopo). |
| CONFLITO_DE_INTERESSE | 403 | Acesso negado | A empresa não pode negociar com uma oferta própria. | RN-14. |
| OFERTA_DE_OUTRA_EMPRESA | 403 | Acesso negado | A oferta pertence a outra empresa. | RN-17. |
| PRODUTO_NAO_ENCONTRADO | 404 | Não encontrado | Produto não encontrado. | RN-06. |
| DEMANDA_NAO_ENCONTRADA | 404 | Não encontrado | Demanda não encontrada. | RN-13. |
| NEGOCIACAO_NAO_ENCONTRADA | 404 | Não encontrado | Negociação não encontrada. | RN-12. |
| SUGESTAO_NAO_ENCONTRADA | 404 | Não encontrado | Sugestão de leilão não encontrada. | `sugestaoId` inexistente. |
| ACEITE_NAO_ENCONTRADO | 404 | Não encontrado | Aceite não encontrado. | RN-19. |
| DEMANDA_ENCERRADA | 409 | Conflito | A demanda não está aguardando oferta ou está com o prazo vencido. | RN-10, RN-14. |
| NEGOCIACAO_JA_ABERTA | 409 | Conflito | Já existe uma negociação aberta para esta demanda e esta oferta. | RN-14. |
| SUGESTAO_ENCERRADA | 409 | Conflito | A sugestão de leilão não aceita mais mudanças. | RN-17, RN-19, RN-20. |
| ACEITE_JA_REGISTRADO | 409 | Conflito | Esta oferta já está no lote. | RN-17. |
| FORNECEDOR_EM_LEILAO | 409 | Conflito | A empresa já tem um leilão aberto deste produto. | RN-17. |
| IDEMPOTENCIA_CONFLITO | 409 | Conflito | A chave de idempotência já foi usada com outro corpo. | RN-30. |
| VALIDACAO_INVALIDA | 422 | Entrada inválida | Os dados da requisição são inválidos. | Corpo, ID na URL, query, `Idempotency-Key` ou paginação fora do contrato (§7.1, §7.2); `valorItem` da direta fora do intervalo (RN-14). |
| OFERTA_INDISPONIVEL | 422 | Entrada inválida | A oferta não existe ou está com preço ou quantidade zerados. | RN-14, RN-17. |
| PRODUTO_DIFERENTE | 422 | Entrada inválida | A oferta ou a demanda é de outro produto. | RN-14, RN-17, API-05. |
| PRECO_ACIMA_DO_MAXIMO | 422 | Entrada inválida | O preço da oferta é maior que o preço máximo da demanda. | RN-14. |
| QUANTIDADE_ACIMA_DO_SALDO | 422 | Entrada inválida | A quantidade é maior que o saldo negociável da demanda. | RN-14, RN-15. |
| QUANTIDADE_INDISPONIVEL | 422 | Entrada inválida | A quantidade é maior que o disponível livre da oferta. | RN-14, RN-17, RN-19. |
| ERRO_INTERNO | 500 | Erro interno | Não foi possível processar a requisição. | Exceção não prevista. |
| DEPENDENCIA_INDISPONIVEL | 503 | Serviço indisponível | Uma dependência necessária está indisponível. | JWKS indisponível, `RepositorioIndisponivel` ou prontidão falhando (API-02). |

Respostas fora do catálogo: rota inexistente (404) e método não permitido (405) mantêm a resposta padrão do framework; `/docs` e `/openapi.json` ficam habilitados. Nenhuma delas faz parte da avaliação de conformidade.

**Ordem de verificação** (quando mais de um erro se aplica, retorna-se o primeiro):

1. Correlação (§7.1; nunca é erro).
2. `TOKEN_INVALIDO`, ou `DEPENDENCIA_INDISPONIVEL` se as chaves públicas falharem.
3. `CONTEXTO_NAO_SELECIONADO`.
4. `ACESSO_NEGADO` pelo escopo da rota.
5. `VALIDACAO_INVALIDA` (ID na URL, query, `Idempotency-Key`, corpo).
6. Recurso da URL: `PRODUTO_NAO_ENCONTRADO`, `DEMANDA_NAO_ENCONTRADA` (API-06), `NEGOCIACAO_NAO_ENCONTRADA` ou `SUGESTAO_NAO_ENCONTRADA`.
7. Idempotência (só em `POST`): replay da resposta memorizada ou `IDEMPOTENCIA_CONFLITO`.
8. Regras de domínio, na ordem da coluna "Erros possíveis" do §7.2 para cada rota.

`DEPENDENCIA_INDISPONIVEL` (banco) e `ERRO_INTERNO` podem ocorrer em qualquer passo que use o repositório. A validação automática de corpo do FastAPI não pode ser usada nas rotas com corpo, porque responderia antes dos passos 2 a 4.

## 11. Requisitos não funcionais

| ID | Característica (ISO/IEC 25010) | Requisito | Métrica e limiar | Verificação |
|---|---|---|---|---|
| RNF-01 | Adequação funcional — exatidão | Aritmética com `Decimal` e saída com as casas do §4. | Itens 1.00 × 0.10 e 1.00 × 0.20 → `precoInicial` 0.30 e participações 0.3333 e 0.6667; três itens de valor igual → 0.3334, 0.3333 e 0.3333 (maior resto; empate: o primeiro na ordem); 155 itens de valor igual → nenhuma participação negativa; soma das participações = 1.0000 em 100% dos leilões. | Teste automatizado de domínio (RN-22, RN-23) e CA-39 |
| RNF-02 | Interoperabilidade | Formato único de erro. | 100% das respostas 4xx e 5xx de negócio em `application/problem+json` com os 8 campos do §10. | CAs de erro do §8 |
| RNF-03 | Confiabilidade — ordenação e contrato | Eventos da mesma negociação com a mesma chave (ordem garantida dentro de cada tópico, não entre tópicos) e envelope do kit. | 100% das mensagens publicadas (exceto DLT) com `key == aggregateId == negociacaoId`, `eventType` = tópico, `eventVersion` `"1.0"`, `source` `"mercado-service"`, `timestamp` = relógio e `eventId` UUID distinto; 100% válidas contra `docs/mercado/contratos/<topico>.schema.json`. | CA-26, CA-39 |
| RNF-04 | Manutenibilidade — modularidade | Dependências permitidas e estado só nas portas. | Nenhum import fora do §5 nos módulos de domínio; duas aplicações de `create_app` com repositórios diferentes não compartilham demandas nem sugestões. | Inspeção e teste automatizado |
| RNF-05 | Confiabilidade — idempotência do consumidor | Reentrega não repete efeito, com o serviço real. | 5 entregas do mesmo `demanda_criada` no Redpanda → 1 demanda e 1 linha em `processed_events`. | CA-55 repetido no kit |
| RNF-06 | Confiabilidade — concorrência | Balanço e sugestão serializados por produto com duas réplicas. | 20 requisições concorrentes de aceite e de abertura de direta para P, nas duas réplicas pelo gateway → no máximo uma sugestão ativa de P, soma dos aceites de cada oferta nunca acima do disponível, soma das diretas abertas de cada demanda nunca acima do saldo, 0 respostas 5xx. | Teste de integração no kit (evidência no G2) |
| RNF-07 | Confiabilidade — prazos | Fechamento do lote único e pontual com duas réplicas. | Com as duas réplicas e o agendador ativo, 1 sugestão com `janelaEncerraEm` em T → exatamente 1 `leilao_iniciado`, publicado em até 2 s após T. | Teste de integração no kit (evidência no G2) |
| RNF-08 | Eficiência de desempenho | Latência no kit, via gateway, duas réplicas. | Leitura (API-04, API-05, API-08) p95 ≤ 500 ms com 200 usuários simultâneos por 2 min após 30 s de aquecimento; escrita (API-07, API-11) p95 ≤ 1000 ms com 20 usuários; do `demanda_criada` publicado à sugestão visível na API-10, p95 ≤ 1000 ms com 1.000 ofertas no produto; respostas 5xx < 1%. | k6 no kit (evidência no G2) |
| RNF-09 | Segurança — controle de acesso | RBAC por escopo e sigilo entre empresas. | 100% das chamadas com token `CARRIER` ou `TENANT_ADMIN` → 403 `ACESSO_NEGADO`; 0 respostas com demanda ou negociação direta de outra empresa; 0 nomes de outros fornecedores na API-10. | CA-53, CA-25, CA-52 |
| RNF-10 | Confiabilidade — publicação durável | Outbox com mesmo `eventId`. | Publicador interrompido antes da confirmação → após a retomada por outra réplica, toda cópia publicada tem o `eventId` gravado na outbox; 0 eventos perdidos. | Teste de integração no kit (evidência no G2) |

## 12. Matriz de rastreabilidade

| Requisito | Regras | Interfaces | Critérios de aceitação |
|---|---|---|---|
| RF-01 | RN-01, RN-03, RN-04, RN-06 | `fornecimento_criado`, `fornecimento_atualizado`, `estoque_atualizado`, API-05 | CA-01, CA-02, CA-03, CA-04, CA-05 |
| RF-02 | RN-02, RN-03 | `produto_cadastrado`, `produto_atualizado`, API-04 | CA-06, CA-07 |
| RF-03 | RN-03, RN-06, RN-08 | `demanda_criada`, `demanda_recorrente_gerada`, API-06 | CA-08, CA-09, CA-10, CA-11 |
| RF-04 | RN-06, RN-08, RN-09 | `demanda_cancelada`, API-06 | CA-12, CA-13, CA-14 |
| RF-05 | RN-04, RN-06 | API-04 | CA-15, CA-16, CA-17, CA-18 |
| RF-06 | RN-06, RN-16 | API-10 | CA-19, CA-20, CA-21 |
| RF-07 | RN-07, RN-11, RN-13 | API-05, API-06 | CA-22, CA-23, CA-24, CA-25 |
| RF-08 | RN-05, RN-07, RN-14, RN-15, RN-29, RN-30, RN-34, RN-35 | API-07, `modo_negociacao_definido` | CA-26, CA-27, CA-28, CA-29, CA-30, CA-31, CA-32, CA-33 |
| RF-09 | RN-04, RN-16 a RN-20, RN-34 | API-11, API-12, API-13 | CA-34, CA-35, CA-36, CA-37, CA-38 |
| RF-10 | RN-21, RN-22, RN-23, RN-28, RN-29, RN-33 | `executar_prazos`, API-11, `modo_negociacao_definido`, `leilao_iniciado` | CA-39, CA-40, CA-41, CA-42, CA-43 |
| RF-11 | RN-25, RN-26, RN-27 | `negociacao_fechada`, API-06, API-09 | CA-44, CA-45, CA-46, CA-47, CA-48 |
| RF-12 | RN-10, RN-14, RN-33 | `executar_prazos`, API-06, API-07 | CA-49. O mesmo CA cobre o erro (`409 DEMANDA_ENCERRADA` ao abrir direta com prazo vencido) e o caso-limite (instante igual ao prazo). |
| RF-13 | RN-12, RN-24 | API-08, API-09 | CA-50, CA-51, CA-52 |
| RF-14 | RN-12, RN-34 | §7.1 | CA-53, CA-54 |
| RF-15 | RN-31, RN-32 | §7.3, `mercado.dlt` | CA-55, CA-56, CA-57 |
| RF-16 | — | API-01, API-02, API-03 | CA-58 |
| RF-17 | RN-34 | rotas `/api/v1/mercado` | CA-59 |
| RNF-01 a RNF-10 | §11 | §11 | §11, coluna "Verificação" |

Campos da entidade × projeções: `saldoNegociavel` (API-06), `excessoDemanda` do balanço (API-04; o da sugestão é gravado, §6.1), `quantidadeNoLote`, `fornecedoresNoLote`, `minhaParticipacao` (API-10), `contextoProduto` e `agora` (API-04, API-09, API-10) são derivados na resposta e não são gravados. `eventId`, `eventType`, `timestamp`, `source` e `aggregateId` pertencem só ao envelope; `correlationId` também é gravado na negociação e na sugestão (§6.1), para os eventos que elas geram depois.

## 13. Premissas, decisões e questões em aberto

Decisões do autor (Brunno Alexcson), tomadas a partir das respostas do professor, dos repositórios dos outros grupos e das discussões do grupo. Cada decisão traz a alternativa registrada (↻) e se vale validá-la no Status Report. Fontes: transcrição da sessão de dúvidas com o professor (Granola) e resumo da aula sobre oferta, demanda e leilão; respostas do professor pelo Teams em 12/11/2026, registradas pelo Fornecimento (F14) e transcrição da aula 2 (F7); diagrama da arquitetura; kit de Infra; ESP-PED-001 v2.1 e respostas do Pedidos de 19/11; ESP-NEG-001; contrato de eventos v2.3 do Fornecimento (v2.5 na INT-MRC-05); contratos do grupo de Compra e Demanda; Identity-Service.

**Regras do professor citadas**

| ID | O que o professor disse | Onde entra |
|---|---|---|
| PROF-01 | O Mercado monitora oferta × demanda por produto. Oferta ≥ demanda: negociação direta disponível para todos. Demanda > oferta: o sistema sugere ao fornecedor criar um leilão; cada fornecedor escolhe entre leilão e negociação direta. | RN-06, RN-16 |
| PROF-02 | Ofertas de vários fornecedores do mesmo produto podem ser reunidas num único leilão. | RN-21 |
| PROF-03 | Ao criar o leilão, o estoque é bloqueado por um pedido temporário sem comprador; sem lance, o pedido é cancelado e o estoque volta; com vencedor, é efetivado. O pedido também nasce na abertura da negociação direta (F7, F14). | RN-14, RN-21 |
| PROF-04 | O valor é dividido proporcionalmente entre os fornecedores ("dentro do valor total", regra de três); o lance inicial é a soma ponderada; o sistema pode sugerir preço igual para simplificar. | RN-22, RN-23 |
| PROF-05 | O comprador dá lance no lote inteiro; quem precisa de parte usa a demanda (direta) ou combina com outro comprador. | RN-22, RN-28 |
| PROF-06 | "Leilão reverso" é só mostrar o menor preço quando a oferta supera a demanda; não é evento programado. | RN-11 |
| PROF-07 | Alerta de preço acima do mercado e comparação externa são desejáveis, não obrigatórios. | §3.2 |
| PROF-08 | O mesmo fornecedor não tem duas ofertas do mesmo produto; preço inicial único; desconto por volume é negociação direta. | RN-14 |
| PROF-09 | Oferta com preço ou quantidade zero não é exibida. | RN-04 |
| PROF-10 | O fornecedor pode reajustar o preço de todo o estoque da oferta. | RN-01 |
| PROF-11 | Demanda sem fornecedor no portal é válida e serve para atrair fornecedores; recorrência avisa o mercado. | RN-06, RN-08 |
| PROF-12 | O estoque é por fornecimento (produto × fornecedor). | §6.1 |
| PROF-13 | (27/11) A duração do leilão pode ser definida pelo sistema, como 24 h, mas o fornecedor também pode escolher a janela de tempo; sem a escolha, vale o padrão. | RN-18, RN-21, DG-70 |

**Decisões do grupo**

| ID | Tipo | Descrição | Decisão |
|---|---|---|---|
| DG-01 | Decisão | Gatilho da sugestão de leilão | Qualquer excesso de demanda sobre a oferta (com oferta > 0), configurável por `EXCESSO_DEMANDA_MINIMO_PERCENTUAL` (padrão 0). ↻ Margem mínima definida pelo professor (só configuração). Status Report: sim. |
| DG-02 | Decisão | O que conta como demanda no balanço | Demandas únicas e ocorrências já geradas, enquanto aguardam; o modelo recorrente não conta; sem janela de tempo. ↻ Somar as ocorrências futuras numa janela (ex.: 30 dias) usando `frequencia` e `proximaOcorrenciaEm`. Status Report: opcional. |
| DG-03 | Decisão | Contratos do Demanda | Os campos de `demanda_criada` e `demanda_recorrente_gerada` atendem o matching. Pedidos ao grupo (17/11, refeitos em 25/11 na issue #1 de `LuccaStuart/portal-b2b-compra-demanda`): `precoMaximo` com `exclusiveMinimum: 0`; `uf` como uma das 27 siglas, em maiúsculas; `cidade` de 1 a 255 caracteres, aparada; IDs de 1 a 64 caracteres (DG-55); registrar o Mercado como consumidor de `demanda_recorrente_gerada` e `demanda_cancelada` e enviar os exemplos desses dois eventos. Pergunta: a demanda pode ser editada na v1? Se puder, o Mercado precisa de um evento de alteração. Saíram dos pedidos: `frequencia` como lista fechada (o Mercado não usa o campo, DG-45) e `ocorrenciaId` em `demanda_cancelada` (para cancelar uma ocorrência, basta `demandaId` igual ao `ocorrenciaId`, RN-08 e RN-09). Pedidos complementares na issue #2 (25/11): `quantidade` e `precoMaximo` com até 2 casas; chave `demandaId` nas mensagens (no `demanda_recorrente_gerada`, a da origem); a primeira ocorrência da recorrente também como `demanda_recorrente_gerada`; `ocorrenciaId` único e nenhuma ocorrência depois do cancelamento do modelo. Respondido em 26/11: todos aceitos; a demanda não é editável na v1 (o comprador cancela e cria outra), então não há evento de alteração (INT-MRC-04). |
| DG-04 | Decisão | Cancelamento de recorrente | Cancelar o modelo cancela as ocorrências aguardando (RN-09), e ocorrência que chega depois do cancelamento do modelo é ignorada (RN-08). ↻ Manter as ocorrências já geradas. |
| DG-05 | Decisão | O que conta como oferta | `quantidadeDisponivel` mais recente; a reserva dos pedidos já vem descontada pelo Fornecimento. ↻ Desconto local na abertura da negociação. O intervalo entre T1 e a reserva é tratado na PR-01. |
| DG-06 | Decisão | Unidades | Balanço por `produtoId`; a unidade é do produto; sem conversão; unidade divergente vai para a DLT. ↻ Conversão KG↔TON. |
| DG-07 | Decisão | Estabilidade da sugestão | A sugestão acompanha o balanço em tempo real, sem expiração por tempo. ↻ Tempo mínimo aberta. Status Report: opcional. |
| DG-08 | Decisão | Balanço muda durante o leilão | O leilão segue até T2; o detalhe mostra `contextoProduto` para fornecedor e comprador. ↻ Aviso só ao fornecedor; fornecedor poder desistir sem lances (exige evento novo). |
| DG-09 | Decisão | Quem vê a sugestão | Fornecedores com oferta do produto e disponível livre > 0; só consulta, sem notificação. ↻ Evento para um serviço de notificação. |
| DG-10 | Decisão | Leilão compartilhado | A sugestão é a janela de formação do lote; adesão opcional; lote de um só fornecedor é válido; separação local até T1. ↻ Leilão individual; janela como entidade separada. Status Report: sim. |
| DG-11 | Decisão | Lance no lote inteiro | Sem lance parcial (PROF-05). ↻ Leilão de várias unidades (custo alto, na Negociação). |
| DG-12 | Decisão | Sugestão sem aceite | Continua enquanto houver excesso; a escassez é resolvida na negociação direta, por quem abre primeiro; demandas sem oferta aguardam até serem atendidas, canceladas ou terem o prazo vencido (DG-48). ↻ Prioridade por maior `precoMaximo`. |
| DG-13 | Decisão | T1 e T2 | T1 pelo sistema (RN-21); T2 = T1 + duração do leilão, de 24 h por padrão (`LEILAO_DURACAO_HORAS`) ou escolhida pelo fornecedor que abre a janela (DG-70, PROF-13). ↻ T2 limitado pelo menor `prazoDesejado`. |
| DG-14 | Decisão | Entrada no lote | Por conta própria, até T1. ↻ Inclusão automática por preferência na oferta. |
| DG-15 | Decisão | Quantidade no lote | O fornecedor escolhe, até o disponível livre. ↻ Sempre todo o disponível. |
| DG-16 | Decisão | Tamanho do lote | Sem mínimo; o máximo é só o limite técnico da RN-21 (1000 itens e as somas de quantidade e valor dentro dos limites do §4), que mantém a participação sempre positiva (RN-23). ↻ `LEILAO_LOTE_MINIMO`; reduzir o lote em T1 à demanda total. |
| DG-17 | Decisão | Estoque dividido e dois leilões | Parte no lote e o resto na direta: sim; dois leilões abertos do mesmo produto pela mesma empresa: não. ↻ Permitir mais de um leilão. |
| DG-18 | Decisão | Desistência antes de T1 | Pode alterar ou sair até T1. ↻ Aceite irrevogável. |
| DG-19 | Decisão | Lance inicial | Σ quantidade × preço de cada item (PROF-04). ↻ Preço uniforme aceito ao entrar. |
| DG-20 | Decisão | Preço mínimo e incremento | O preço inicial é o mínimo do primeiro lance; incremento = `LEILAO_INCREMENTO_MINIMO_PERCENTUAL` (padrão 1%) do preço inicial, enviado no `leilao_iniciado`. ↻ Incremento fixo em reais ou definido pela Negociação. Status Report: sim. |
| DG-21 | Decisão | Preço uniforme | Fora da v1. Se adotado: média ponderada pela quantidade, com aceite do fornecedor. |
| DG-22 | Decisão | Base do rateio | Rateio na proporção exata do **valor** de cada item (`valorItem / precoInicial`, sem passar pela participação arredondada), arredondando para baixo em centavos e dando o resto ao item de maior valor: com o lance igual ou acima do preço inicial, nenhum fornecedor recebe menos que o próprio preço. A participação de 4 casas só é exibida (com ela arredondada, 0.3846 × 780.00 = 299.99 < 300.00). Pedido à Negociação. ↻ Participação na quantidade (exemplo do professor com preços iguais). Status Report: sim. |
| DG-23 | Decisão | Quem calcula o rateio | O Mercado calcula a participação em T1, só para exibição; a Negociação rateia o valor vencedor pela proporção exata de `valorItem` (DG-22) e, se um item sair, recalcula preço inicial, incremento e participações pelas RN-22 e RN-23; o Pedidos só registra. ↻ A Negociação calcula também a participação. |
| DG-24 | Decisão | Quem dá lance | Qualquer comprador, com ou sem demanda; quem tem oferta no lote não dá lance. ↻ Só compradores com demanda aberta. Status Report: sim. |
| DG-25 | Decisão | Compra conjunta | Fora do portal. ↻ Lance em grupo (custo alto). |
| DG-26 | Decisão | Regras de lance | Aplicadas pela Negociação: incremento (DG-20), sem empate, sem preço de reserva oculto. ↻ Preço de reserva. |
| DG-27 | Decisão | Demanda atendida pelo leilão | O comprador indica, opcionalmente, a demanda no lance; a Negociação repassa `demandaId` no `negociacao_fechada`; o Mercado abate o saldo. ↻ O Mercado deduzir a demanda pelo comprador e produto. Status Report: sim. |
| DG-28 | Decisão | Destino da entrega | Informado pelo comprador no Pedidos depois da venda, na direta e no leilão (ESP-PED-001 D-02). O Mercado só repassa o destino da demanda na direta, como referência (RN-29). Substitui a proposta da v0.4 (destino da demanda indicada no lance), não adotada pelo Pedidos. |
| DG-29 | Decisão | Leilão deserto | O estoque volta pela cadeia Negociação → Pedidos → Fornecimento; o Mercado só reage ao `estoque_atualizado`. |
| DG-30 | Decisão | Par na negociação direta | O comprador escolhe a oferta na vitrine e pede a negociação ao Mercado, que valida e publica. ↻ O Mercado forma o par com a oferta mais barata. Status Report: sim. |
| DG-31 | Decisão | Abertura da direta | O pedido temporário nasce na abertura da direta (F7, F14): a Negociação o pede ao consumir `modo_negociacao_definido` (DG-52), sem comprador e sem destino, que chegam depois (DG-28, DG-39). ↻ Pedido só no acordo. Status Report: sim. |
| DG-32 | Decisão | Atendimento parcial | A demanda tem saldo; uma oferta pode atender parte, e várias ofertas somam. ↻ Só atendimento integral, como o boilerplate do kit (deixa demandas paradas com oferta suficiente). Status Report: sim. |
| DG-33 | Decisão | Diretas simultâneas | Várias ao mesmo tempo, limitadas ao saldo da demanda. ↻ Uma por vez; ou sem limite. Status Report: sim. |
| DG-34 | Decisão | Preço negociado | Vale só para o pedido; o Mercado não usa o valor acordado. |
| DG-35 | Decisão | Nomes dos modos | `VENDA_DIRETA` e `LEILAO_DIRETO`, como no kit, no diagrama e na ESP-NEG-001; sem `LEILAO_REVERSO`. Substitui `NEGOCIACAO_DIRETA` e `LEILAO` da v0.2. |
| DG-36 | Decisão | Formato do documento | Modelo ERS-LLM v1.2 do QA, em `docs/mercado/requisitos.md`, com contratos em `docs/mercado/contratos/`, seguindo a convenção `docs/<serviço>/` do README do repositório (como `docs/pedidos/`). Substitui a v0.2 do mesmo arquivo. |
| DG-37 | Decisão | Gatilho do pedido temporário | A Negociação publica `pedido_temporario_solicitado` por oferta ao consumir `leilao_iniciado` (leilão) ou `modo_negociacao_definido` (direta); o Pedidos não consome eventos do Mercado (DG-51, DG-52). Substitui a proposta da v0.4 (Pedidos consumindo `leilao_iniciado`). |
| DG-38 | Decisão | Pedido por item do lote | Um pedido temporário por oferta, com unicidade `(negociacaoId, fornecimentoId)`: confirmado pelo Pedidos em 19/11. |
| DG-39 | Decisão | Comprador e solicitante | O pedido temporário nasce sem comprador, na direta e no leilão, e recebe o comprador no `negociacao_fechada`; o solicitante é a Negociação. Confirmado pelo Pedidos em 19/11. |
| DG-40 | Decisão | Consumo de `negociacao_fechada` | O Mercado consome para encerrar a negociação e abater o saldo (RN-25 a RN-27). A Negociação aceitou o `demandaId` em 19/11 (D38 da ESP-NEG-001). Pendência: Infra registrar o consumidor (INT-MRC-03). |
| DG-41 | Decisão | `lance_realizado` | Não consumido; o resultado chega pelo `negociacao_fechada`. Pedir à Infra que retire o Mercado da lista do `CONTRACTS.md`. ↻ Consumir para estatísticas de preço. |
| DG-42 | Decisão | Evento de sugestão | Sem tópico próprio; a sugestão é só consulta (DG-09). ↻ Evento para serviço de notificação. |
| DG-43 | Decisão | Alteração de preço da oferta | Resolvida pelo `fornecimento_atualizado` (contrato v2.0 do Fornecimento), consumido pelo Mercado (RN-01). Pendência: Infra criar o tópico. |
| DG-44 | Decisão | `demanda_cancelada` | Consumido; demanda aguardando vira `CANCELADA`; diretas abertas continuam; leilões não são afetados (RN-09, DG-08). ↻ Encerrar as diretas abertas (exige evento novo para o Negociação). |
| DG-45 | Decisão | Demanda recorrente | Qualquer chave aceita na `demanda_recorrente_gerada` (o Demanda usará o `demandaId` da origem, confirmado em 26/11); ocorrência identificada por `ocorrenciaId`; modelo fora do balanço; `frequencia` não usada no cálculo (DG-02, DG-03). |
| DG-46 | Decisão | Referência dos contratos | `CONTRACTS.md` do kit (`eventVersion`, chaves em camelCase, `produto_cadastrado`); as divergências do `ORIENTACOES` não são seguidas. Pendência: confirmação da Infra. |
| DG-47 | Decisão | Exclusão de produto | `produto_excluido` não é consumido: só se exclui produto sem fornecimento e sem pedido (RN-12 e RN-13 do Fornecimento), então não há oferta, negociação nem sugestão a ajustar. Demandas de produto excluído são canceladas pelo Demanda: aceito na issue #2 de `LuccaStuart/portal-b2b-compra-demanda` (26/11), ele consome o evento de exclusão do Fornecimento e publica `demanda_cancelada` para cada demanda única aberta e cada modelo recorrente do produto (o Mercado cancela as ocorrências, RN-09); depende do nome do tópico confirmado pelo Fornecimento. ↻ Consumir e responder 404 no balanço e na vitrine. |
| DG-48 | Decisão | Prazo da demanda | Expirar a demanda é regra do Demanda. O Mercado só deixa de contar no balanço a demanda com prazo vencido e recusa novas diretas para ela (RN-10); a situação não muda. Confirmado pelo Demanda na issue #2 (26/11): não haverá evento de expiração na v1. ↻ Marcar `EXPIRADA` localmente (como na v0.3). |
| DG-49 | Decisão | Preços na vitrine | Públicos para compradores e fornecedores, como num marketplace: o fornecedor vê os preços dos concorrentes (professor: "mostrar o menor preço", "princípio da publicidade"). ↻ Vitrine só para compradores. Status Report: opcional. |
| DG-50 | Decisão | O que o fornecedor vê da demanda | Só os totais do produto no balanço, sem demandas nem compradores; qualquer fornecedor pode consultar, para a procura atrair fornecedores (professor). ↻ Mostrar também quantidade de demandas e UFs de destino agregadas. |
| DG-51 | Decisão | Reserva do lote antes dos lances | O Mercado forma o lote e publica `leilao_iniciado` em T1, como no diagrama ("inicia leilões quando necessário"). A Negociação consome, publica `pedido_temporario_solicitado` por item (como na ESP-NEG-001) e só libera os lances com todas as reservas confirmadas; item com reserva recusada sai do lote antes do primeiro lance, e ela recalcula preço inicial, incremento e participações sobre os itens restantes (RN-22, RN-23). Nenhum lance acontece sobre estoque sem reserva, que era a exigência do Pedidos (ESP-PED-001 D-01). ↻ A Negociação forma o lote, com o fornecedor criando o leilão por HTTP (ESP-NEG-001); ou o Mercado pede a reserva a cada aceite (mais dois tópicos no Mercado). Status Report: sim. |
| DG-52 | Decisão | Abertura da direta | O Mercado valida aptidão e saldo da demanda e publica `modo_negociacao_definido`; a Negociação abre a direta com o mesmo `negociacaoId` e publica `pedido_temporario_solicitado`, como no leilão. O pedido nasce sem comprador e o destino vem depois (DG-28). ↻ O comprador abre a direta na Negociação por HTTP (D16 da ESP-NEG-001). Status Report: sim. |
| DG-53 | Decisão | Fechamento republicado | Só o primeiro `negociacao_fechada` de cada `negociacaoId` é aplicado. Se o Pedidos mandar o fechamento para a DLT e a Negociação republicar corrigido com outro `eventId`, o Mercado ignora a correção (RN-25); é um caso de erro raro, aceito na v1. ↻ Reprocessar o fechamento quando as quantidades mudarem, desfazendo o abatimento anterior (exige guardar o abatido por demanda). Status Report: não. |
| DG-54 | Decisão | Auto-negociação por grupo econômico | Na v1 o conflito de interesse compara só a empresa (`tenant_id`, RN-14, RN-28); o `grupoId` que o Fornecimento passou a enviar no `fornecimento_criado` (issue #35) é aceito e ignorado. ↻ Comparar o `group_id` do token com o `grupoId` da oferta (exige guardar o `grupoId` na cópia da oferta). Status Report: opcional. |
| DG-55 | Decisão | Tamanho dos IDs de negócio | `produtoId`, `fornecimentoId`, `fornecedorId`, `compradorId`, `demandaId` e `ocorrenciaId` têm de 1 a 64 caracteres, na API, nos eventos consumidos e nos publicados, como na Negociação, no Pedidos, no Fornecimento e no kit da Infra (`database/01-init.sql`, `VARCHAR(64)`). Evento consumido com ID maior vai para `mercado.dlt`. O envelope (`correlationId`, `aggregateId`) continua com até 128. Fecha a INT-NEG-12 da ESP-NEG-001. ↻ Aceitar até 128 exigiria alterar as colunas de ID da Negociação e do Pedidos. |
| DG-56 | Decisão | Tabelas de exemplo do kit | O `database/01-init.sql` do kit cria `mercado.match`, `mercado.modo_negociacao` e `mercado.processed_events`, sem dados e num formato diferente do §6.1. A migração 0001 do Mercado começa com `DROP TABLE IF EXISTS` dessas três tabelas e cria as suas; a Infra é avisada para retirá-las do `01-init.sql` (INT-MRC-03). O dono do schema (`portal_ddl`) pode apagá-las. ↻ Usar outro nome para `processed_events` e conviver com as tabelas do kit. |
| DG-57 | Decisão | Banco indisponível no consumo | Como no Pedidos (ESP-PED-001, RN-26): o handler propaga `RepositorioIndisponivel` e o adaptador reentrega a mensagem a cada 5 s, sem limite e sem DLT; só falhas de outro tipo vão para a `mercado.dlt` depois de 3 execuções (RN-31). ↻ Tratar como as demais falhas (3 execuções e DLT), o que perde a mensagem se o banco ficar fora do ar por mais de 3 s. |
| DG-58 | Decisão | Compra imediata | Para comprar pelo preço do anúncio sem negociar, a API-07 aceita `compraImediata` e o repassa no `modo_negociacao_definido`; a Negociação encerra a direta sozinha quando a reserva é confirmada (RN-35). É o mesmo tópico e o mesmo evento, com um campo opcional a mais: o schema da Negociação aceita campos extras no payload e o `CONTRACTS.md` do kit só define o envelope, então nada muda para Infra, Pedidos e Fornecimento. Descartadas: o front chamar a abertura e o aceite em sequência (a direta abre de forma assíncrona e o aceite exige a reserva confirmada, o que obriga o front a repetir consultas e deixa estoque e saldo presos por até 48 h se a segunda chamada não acontecer); um fluxo de compra separado da negociação (tópico novo, consumidor novo no Pedidos e a mesma reserva feita de novo). ↻ Fluxo de compra separado. |
| DG-59 | Decisão | Prazo vencido na direta e na aptidão | A API-07 (RN-14) e a indicação de oferta apta (RN-07) tratam como vencida a demanda com `prazoDesejado < agora`, mesmo antes de o agendador marcar `prazoVencido`. O balanço (RN-06) usa só a marcação, para mudar apenas num recálculo gravado, coerente com `situacaoDesde`. ↻ Usar a marcação em tudo (diferença de até 1 s). |
| DG-60 | Decisão | Saldo negociável nunca negativo | O estado é gravado como a RN-27 manda; a API-06, a demanda total (RN-06) e a RN-15 usam `max(0, saldo − emNegociacao)`. Evita valor negativo se uma alocação passar da quantidade do item. ↻ Limitar o abatimento a `saldo − emNegociacao`. |
| DG-61 | Decisão | Unidade sem produto conhecido | A unidade da negociação é a do produto, se conhecido; senão, a do primeiro item do lote. `produto_atualizado` não revalida ofertas, demandas e negociações já gravadas. ↻ A primeira oferta ou demanda fixa a unidade e as divergentes vão para a DLT. |
| DG-62 | Decisão | Casos da `mercado.dlt` fora do §7.4 | `correlationId` original ausente ou inválido → UUID4 novo; `str(eventId)` com mais de 128 caracteres → chave `"desconhecido"`; bytes que não são JSON nem UTF-8 → `originalEvent` em texto UTF-8 com substituição, sem truncar e sem redigir. O `detalhe` usa os dois textos literais desta ESP; os demais são escritos pelo serviço (ex.: `"Campo inválido: payload.quantidade."`). ↻ Truncar a chave em 128 caracteres. |
| DG-63 | Decisão | Casas do `incrementoMinimoPercentual` | Publicado com no mínimo 2 casas, mantendo as casas extras da configuração (`1.00`, `0.125`), para a Negociação recalcular o mesmo incremento (RN-22, DG-51). ↻ Sempre 2 casas, recusando na subida configuração com mais casas. |
| DG-64 | Decisão | Totais acima do limite de quantidade | `ofertaTotal`, `demandaTotal` e `excessoDemanda` não têm o limite do §4; a coluna `excesso_demanda` é `NUMERIC(18,2)`. ↻ Manter `NUMERIC(12,2)` e saturar o valor gravado. |
| DG-65 | Decisão | Lote fechado sem itens | O balanço é recalculado na mesma transação (RN-06: toda mudança de sugestão recalcula); com o gatilho verdadeiro, a nova sugestão abre ali (CA-42). ↻ A nova sugestão abre no próximo evento do produto. |
| DG-66 | Decisão | Projeção da sugestão | `unidade` é `null` sem produto conhecido, como na API-04. Na resposta da API-11 que converte o lote, `quantidadeNoLote`, `fornecedoresNoLote` e `minhaParticipacao` vêm dos aceites (os itens ficam na negociação). Empresa com dois aceites: `minhaParticipacao` do primeiro na ordem do lote; `fornecedoresNoLote` conta empresas distintas. |
| DG-67 | Decisão | Oferta que muda de produto durante a formação do lote | O aceite da oferta na sugestão `EM_FORMACAO` do produto anterior é removido, como na API-13 (sem aceites, a sugestão volta a `ABERTA` sem janela), e a quantidade deixa de estar comprometida. Em T1, item de oferta de outro produto sai do lote (RN-21). Evita que a mesma quantidade fique livre no produto novo e entre no leilão do produto anterior. ↻ Manter o aceite e contar o compromisso em todas as sugestões. |
| DG-68 | Decisão | Concorrência entre réplicas na cópia de produtos e nas demandas | `produto_cadastrado` e `produto_atualizado` comparam o instante depois de `bloquear_produto`, para um evento antigo não sobrescrever um mais novo gravado pela outra réplica (RN-02). Criação e cancelamento de demandas e modelos recorrentes bloqueiam o ID com `bloquear_id_demanda` antes dos produtos e conferem modelo cancelado e cancelamento antecipado depois do bloqueio, para uma ocorrência ou demanda não ficar aberta quando o cancelamento chega ao mesmo tempo (RN-08, RN-09). |
| DG-69 | Decisão | Instante e unidade depois do bloqueio | Com duas réplicas, uma operação pode esperar o bloqueio enquanto a outra muda o produto ou o relógio passa do prazo. Por isso `agora` é lido de novo depois de cada bloqueio, inclusive o da chave de idempotência (os prazos da janela de adesão, da demanda e das 24 h da RN-30 usam esse valor) e a unidade da RN-03 é comparada com o produto já bloqueado. Ex.: aceite que começa às 12:59:59.999 e obtém o bloqueio às 13:00:01, numa janela que encerra às 13:00 → `409 SUGESTAO_ENCERRADA`. ↻ Usar o instante de chegada da requisição ou do evento. |
| DG-70 | Decisão | Duração escolhida pelo fornecedor | Pedido do professor em 27/11 (PROF-13). O aceite que abre a janela de adesão pode trazer `duracaoLeilaoHoras` (inteiro em horas, a partir de 1, sem máximo de negócio); sem ele, vale `LEILAO_DURACAO_HORAS` (24 h). Os aceites seguintes não mudam a duração: os demais fornecedores veem a duração na sugestão antes de aderir. T2 = T1 + duração. Nenhum contrato muda: a Negociação já usa o `encerraEm` do `leilao_iniciado`. ↻ Duração por data e hora de término; menor ou maior duração entre os aceites. |
| PR-01 | Premissa | Intervalo entre T1 e a reserva | Em T1 a separação local é liberada; até chegar o `estoque_atualizado` da reserva do pedido temporário, a quantidade do lote volta a contar como disponível livre. Uma direta aberta nesse intervalo pode tomar o estoque; nesse caso a reserva do item é recusada e a Negociação o retira do lote antes do primeiro lance (DG-51). Nenhuma venda acontece sem reserva; o efeito possível é um lote menor que o aceito. |
| PR-02 | Premissa | Fim do leilão | Este serviço não acompanha T2; a negociação só muda com o `negociacao_fechada`. |
| PR-03 | Premissa | Ordem entre tópicos | Demanda ou oferta podem chegar antes do produto; o balanço usa o `produtoId` e a unidade do evento até o produto chegar (RN-03). |

**Questões de integração (abertas)**

| ID | Tipo | Descrição | Decisão |
|---|---|---|---|
| INT-MRC-03 | Questão de integração → **Infra** (Pedro) | Tópicos e catálogo | Pedido na [issue #6 do kit](https://github.com/Printf-PedroCardoso/portal-b2b-kit-infra/issues/6), aberta em 26/11: (1) criar `produto_atualizado`, `fornecimento_atualizado` e `demanda_cancelada` no `scripts/init_topics.sh` (sem eles, o `/health/ready` responde 503); (2) no `CONTRACTS.md`, retirar o Mercado dos consumidores de `lance_realizado`, incluí-lo em `negociacao_fechada` e nos 3 tópicos novos; (3) retirar `mercado.match`, `mercado.modo_negociacao` e `mercado.processed_events` do `database/01-init.sql` e do `services/common/db_roles.sql` (DG-56; só limpeza, a migração já as apaga). Já atendido no kit (`90085b3`): rota `/api/v1/mercado` e `/.well-known/jwks.json` no gateway; `modo_negociacao_definido`, `leilao_iniciado` e `mercado.dlt` criados e com o Mercado como produtor. Situação: aguardando a Infra. |
| INT-MRC-06 | Questão de integração → **Banco de Dados** | DDL do schema `mercado` | O grupo de Banco entregou o DR-BD-002 (pacote `Portal_B2B_PostgreSQL_17_6`), um modelo em schema único, sem tabelas do Mercado e com quantidade inteira. O Mercado segue o kit (schema `mercado`, roles `svc_mercado` e `portal_ddl`) com migração própria (§5.1), como o Pedidos e o Fornecimento. Pedido ao grupo: validar as tabelas do §6.1, `NUMERIC(18,4)` para dinheiro e `NUMERIC(12,2)` para quantidade. Situação: a responder pelo Teams (o grupo não tem repositório). |
| INT-MRC-08 | Questão de integração → **UX/UI** | Telas | Vitrine pelo menor preço com ofertas aptas; abertura de direta com quantidade; tela de sugestão com os estados `ABERTA`, `EM_FORMACAO` e "você está no lote"; `contextoProduto` no leilão; saldo da demanda. Situação: a comunicar. |
| INT-MRC-09 | Questão → **QA** | Gate G1 | Revisar esta ESP. O identificador `ESP-NEG-001` é usado pelo exemplo do QA e pela especificação da Negociação. Situação: a submeter. |

**Questões de integração (fechadas)**

| ID | Tipo | Descrição | Decisão |
|---|---|---|---|
| INT-MRC-01 | Questão de integração → **Negociação** (Caio) | Divergências com a ESP-NEG-001 | Esta ESP define o papel do Mercado no fluxo, apoiada no diagrama: o Mercado "inicia leilões quando necessário" e publica `modo_negociacao_definido` e `leilao_iniciado`; a Negociação fica com "lances e propostas, regras do leilão e definição do vencedor". Ajustes pedidos à Negociação: (a) abrir a direta e o leilão consumindo `modo_negociacao_definido` e `leilao_iniciado`, com o mesmo `negociacaoId`, em vez de criá-los por HTTP (D16, INT-NEG-03); (b) publicar `pedido_temporario_solicitado` por item ao abrir, como já especificado, e só liberar os lances com todas as reservas confirmadas, retirando antes do primeiro lance o item com reserva recusada e recalculando preço inicial, incremento e participações (DG-51); (c) datas pelo sistema (DG-13) em vez de `dataInicio`/`dataFim` do fornecedor (D24); (d) rateio pela participação no valor enviada pelo Mercado (DG-22) em vez do volume (D19, RN-15); (e) incremento mínimo enviado pelo Mercado (DG-20) em vez de "sem incremento" (D22); (f) `demandaId` opcional no lance e no `negociacao_fechada` (DG-27), ausente na D11; (g) saída do lote antes de T1 é do Mercado (DG-18); (h) manter `tipo` e `produtoId` no `negociacao_fechada`. Pontos já alinhados: leilão conjunto, lance no lote inteiro, preço inicial pela soma ponderada, qualquer comprador dá lance, conflito de interesse, sem leilão reverso, deserto, formato `alocacoes`, pedido temporário pedido pela Negociação. Situação: fechada em 19/11: a ESP-NEG-001 v1.2 aceitou (a) a (g) e manteve (h) (INT-NEG-03, D33 a D42; commit `5851451`). |
| INT-MRC-02 | Questão de integração → **Pedidos** (Daniel) | Pedido na abertura | Respondida em 19/11: um pedido temporário por oferta, sem comprador, com unicidade `(negociacaoId, fornecimentoId)`; efetivar ou cancelar pelo `negociacao_fechada`, com o `demandaId` aceito e ignorado; o próprio pedido temporário é a reserva; gatilho `pedido_temporario_solicitado` da Negociação (DG-37, DG-51); destino informado pelo comprador depois da venda (DG-28). A ESP-PED-001 não muda; a referência à ESP-MRC-001 v0.5 foi atualizada no mesmo dia (commit `8091833`). Situação: fechada. |
| INT-MRC-04 | Questão de integração → **Compra e Demanda** (Lucca) | Ajustes nos contratos | Pedidos da DG-03 (17/11, refeitos em 25/11 nas issues [#1](https://github.com/LuccaStuart/portal-b2b-compra-demanda/issues/1) e [#2](https://github.com/LuccaStuart/portal-b2b-compra-demanda/issues/2) de `LuccaStuart/portal-b2b-compra-demanda`). Respondida em 26/11: todos os pedidos aceitos (`precoMaximo` > 0 ou nulo; `uf` com as 27 siglas em maiúsculas; `cidade` de 1 a 255 caracteres, aparada; IDs de 1 a 64; até 2 casas em `quantidade` e `precoMaximo`; chave `demandaId`, da origem no `demanda_recorrente_gerada`; primeira ocorrência também como `demanda_recorrente_gerada`; `ocorrenciaId` UUID único e nenhuma ocorrência depois do cancelamento do modelo; Mercado registrado como consumidor e exemplos de `demanda_recorrente_gerada` e `demanda_cancelada`). A demanda não é editável na v1 (cancelar e criar outra), sem evento de alteração; não haverá evento de expiração (DG-48); tipos só `UNICA` e `RECORRENTE`; o destino continua obrigatório nos eventos de demanda (DG-28). Cancelamento das demandas de produto excluído aceito (DG-47), dependendo do nome do tópico confirmado pelo Fornecimento. Situação: fechada em 26/11; a implementação é do Compra e Demanda. |
| INT-MRC-05 | Questão de integração → **Fornecimento** (Davi) | Consumo das ofertas e produtos | Registrar o `mercado-service` como consumidor de `produto_cadastrado`, `produto_atualizado`, `fornecimento_criado`, `fornecimento_atualizado` e `estoque_atualizado`. Respondida em 23/11: o contrato de eventos v2.5 do Fornecimento registra o `mercado-service` como consumidor dos 5 eventos, e os exemplos do contrato validam contra os schemas do §7.3. O alinhamento do documento de requisitos e da ESP-PRD-001 deles foi pedido na issue #44 de `davicalvarenga/Sistema-MicServ-SD` e concluído no PR #45 deles (documento de requisitos v0.13, ESP-PRD-001 v2.3); a issue foi fechada em 25/11. Sobre o `grupoId` (issue #35 deles, fechada em 23/11): o Mercado não aplica a trava por grupo na v1 (DG-54). Situação: fechada. |
| INT-MRC-07 | Questão de integração → **Autenticação** | Erro e escopos | Respondida em 23/11 pelo Identity, na issue #4 deste repositório (aberta pelo Pedidos e movida para a issue #27 do `Matheus-Allvz/Identity-Service`): `iss` `identity-service`, `aud` `b2b-platform`, RS256 com JWKS, claims `sub`, `tenant_id` e `active_scope`, `view_mode` `SINGLE` nas operações, `CARRIER` sem acesso e `exp` sem tolerância; tudo já está no §7.1. Formato de erro: o Identity usa RFC 9457 (`code`, `correlation_id`) e recomenda que os clientes leiam os dois formatos; o Mercado mantém o do §10 (`codigo`, `correlationId`), como o Pedidos e a Negociação, e não consome erros do Identity. Situação: fechada em 25/11. |
| INT-MRC-10 | Questão de integração → **Negociação** (Caio) | Compra imediata | Ler `compraImediata` no `modo_negociacao_definido` (`VENDA_DIRETA`) e, com `true`, encerrar a direta como `ENCERRADO` pelo preço da oferta ao receber a reserva `CONFIRMADA`, com a mesma alocação do aceite (RN-13 da ESP-NEG-001); reserva `RECUSADA` continua levando a `RECUSADO`. Sem `compraImediata` ou com `false`, nada muda (DG-58, RN-35). Respondida em 25/11 pela ESP-NEG-001 v1.3 (D46, RN-24, CA-62 e CA-63; PR #12). Situação: fechada. |
| INT-MRC-11 | Questão de integração → **Negociação** (Caio) | Rateio, incremento e participação | [Issue #11](https://github.com/brunnoalexcson/portal-b2b-mercado-negociacao-pedidos/issues/11), aberta em 25/11: rateio pela proporção exata de `valorItem` (DG-22, RN-23), incremento recalculado pelo `incrementoMinimoPercentual` (RN-22), motivo da D40 e, no recálculo depois da saída de um item, participação pelo maior resto com piso `0.0001` (RN-23). Respondida em 25/11: rateio e participação na ESP-NEG-001 v1.4 (RN-11 e D36; RN-05 e D48), incremento já na v1.3 (D47) e motivo da D40 corrigido; a issue #11 foi fechada. Situação: fechada. |

### 13.1 Casos de borda obrigatórios

| Caso de borda | Decisão |
|---|---|
| String vazia ou só com espaços em campos obrigatórios | Inválida: `422 VALIDACAO_INVALIDA` na API; mensagem inválida (DLT) no consumo. |
| Cabeçalho opcional presente porém vazio | `Idempotency-Key` e `X-Correlation-Id` vazios equivalem a ausentes. |
| Mensagem duplicada **e** inválida | A validação vem antes da deduplicação: vai para a DLT de novo (RN-31, RN-32). |
| Campo de chave/ID com tipo inesperado | Na API: `422 VALIDACAO_INVALIDA`. No consumo: mensagem inválida. Na chave da DLT, `eventId` não string é convertido com `str`; ausente, nulo ou vazio → `"desconhecido"` (§7.4). |
| Data/hora sem fuso horário | Na API e no consumo: inválida. `clock.now()` sem fuso: tratar como UTC. |
| Falha da porta de publicação / banco | Banco: `RepositorioIndisponivel` → 503 na API; no consumo, o handler propaga e a mensagem é reentregue a cada 5 s, sem DLT (RN-31). Outra exceção → 500 na API; no consumo, 3 execuções e DLT. `publish` dentro da transação: no adaptador real é a gravação na outbox, que falha junto com a transação; falhas do envio ao broker ficam com o publicador da outbox (§5.1). Falha ao publicar na DLT no adaptador real: o offset não é confirmado e a mensagem é reprocessada. |
| Rotas e métodos fora do contrato | Resposta padrão do framework (§10). |
| Número com mais de 2 casas ou como string | `422 VALIDACAO_INVALIDA` na API; mensagem inválida no consumo. |
| Evento com instante igual ao gravado | Ignorado (RN-01, RN-02). |
| Disponível menor que a quantidade comprometida (oferta encerrada durante a formação) | Disponível livre fica em 0.00 (RN-04); em T1 o item usa `min(aceite, disponível)` e sai se der 0 (RN-21). |
| `duracaoLeilaoHoras` num aceite que não abre a janela | Ignorado: vale a duração do aceite que abriu a janela; a resposta mostra a duração em vigor (RN-18, DG-70). |
| Aceite e mudança de balanço ao mesmo tempo | Serializados por `bloquear_produto`; vale a ordem de entrada na transação (RN-06). |
| `negociacao_fechada` de negociação desconhecida ou encerrada | Ignorado, sem DLT (RN-25). |
| `negociacao_fechada` republicado (mesmo `negociacaoId`, outro `eventId`) | Ignorado: só o primeiro é aplicado (RN-25, DG-53). |
| Lote com mais de 1000 itens ou somas acima dos limites do §4 | Os itens que estourariam saem em T1, na ordem do lote (RN-21). |
| Direta com `valorItem` 0.00 ou acima do limite | `422 VALIDACAO_INVALIDA`, sem efeito (RN-14). |
| Ocorrência recorrente antes do modelo e do cancelamento | O cancelamento do modelo desconhecido cancela as ocorrências `AGUARDANDO` e as seguintes são ignoradas (RN-08, RN-09). |
| Segundo `demanda_cancelada` para o mesmo ID ainda desconhecido | Vale o primeiro cancelamento antecipado; o segundo é ignorado, sem DLT (RN-09). |
| Token com `tenant_id` que não é texto, só com espaços ou com mais de 64 caracteres; `active_scope` desconhecido; `sub` só com espaços | `403 CONTEXTO_NAO_SELECIONADO`; `403 ACESSO_NEGADO`; `401 TOKEN_INVALIDO` (§7.1). |
| Idempotência: números iguais com outra grafia, UUID em maiúsculas, chave fora de `POST` | O hash é do JSON normalizado (`30`, `30.0` e `30.00` são o mesmo corpo); o caminho canônico usa o UUID em minúsculas; `Idempotency-Key` é ignorado em `PUT` e `DELETE` (RN-30). |
| Parâmetro de query desconhecido | Ignorado (§7.2). |
| Número JSON | Lido como `Decimal` (`parse_float=Decimal`), nunca como `float`. Zeros à direita não contam como casas: `30.000` vale como 2 casas; `0.001` e número em string são inválidos. |
| Campo opcional do schema presente na mensagem consumida | É validado: fora do domínio (ex.: `operacao: "VENDA"` no `estoque_atualizado`; `motivo` inválido no `demanda_cancelada`) → mensagem inválida (RN-32). |
| ID de demanda igual ao de um modelo recorrente (ou o contrário) | Demandas e modelos usam o mesmo espaço de IDs; ID já usado por um ou outro → ignorado, sem DLT (RN-08, RN-09). |
| Modelo recorrente de produto ainda desconhecido | O modelo não bloqueia nem recalcula o produto e não o torna conhecido para a API-04 (RN-06). |
| Oferta que troca de `produtoId` | Os dois produtos são bloqueados em ordem fixa e recalculados; `criadaEm` continua o do primeiro `fornecimento_criado` (RN-01). O aceite da oferta na sugestão `EM_FORMACAO` do produto anterior é removido, e o lote nunca inclui oferta de outro produto (DG-67). |
| `tipo` e `produtoId` do `negociacao_fechada` diferentes dos gravados | Só passam pela validação do schema; vale o que está gravado na negociação (RN-25). |
| Escrita que gera vários fatos | Os eventos saem na ordem dos fatos. Ex.: API-07 que fecha um lote pela RN-21 (c): primeiro o `modo_negociacao_definido` da direta, depois o `modo_negociacao_definido` e o `leilao_iniciado` do leilão. |
| `excessoDemanda` de sugestão ativa | Atualizado em todo recálculo como `max(0, demanda total − oferta total)` (RN-16). |
| API-12 ou API-13 em sugestão `CONVERTIDA` ou `ENCERRADA` | Os aceites continuam gravados; a resposta é `409 SUGESTAO_ENCERRADA`, não 404. |
| `executar_prazos` | Processa as janelas de adesão antes das demandas vencidas; exceção sobe para o agendador, que tenta de novo no segundo seguinte; lote fechado sem itens conta como registro mudado (RN-33). |
| Item do lote com reserva recusada depois de T1 | Regra da Negociação: sai do lote antes do primeiro lance; o `negociacao_fechada` traz só os itens restantes (DG-51, PR-01). |
| Janela vencida antes de `executar_prazos` rodar | Aceite, alteração e saída já são recusados comparando com `agora` (RN-17, RN-19); o lote fecha na próxima execução. |

### 13.2 Fora desta versão

Dúvidas levantadas que não foram decididas nesta versão. Ficam para a próxima iteração ou dependem de outros grupos; nenhuma bloqueia os RFs do §8.

| Tema | Pontos | Situação |
|---|---|---|
| Contratos de eventos | Decididos do lado do Mercado (DG-40 a DG-47, DG-53); payloads de `modo_negociacao_definido` e `leilao_iniciado` aceitos pela Negociação em 19/11 (ESP-NEG-001 v1.2); falta a criação dos tópicos pela Infra; o Pedidos não consome eventos do Mercado (INT-MRC-02 fechada) | INT-MRC-03 |
| Regras do leilão na Negociação | Espera pelas reservas e recálculo do lote aceitos pela Negociação (DG-51; D35 da ESP-NEG-001); fica para depois a trava por grupo econômico (DG-54) | INT-MRC-01, fechada em 19/11 |
| Regras de dados de outros serviços | Expiração da demanda (sem evento na v1, DG-48); fuso para converter `prazoDesejado` em `dataEntregaDesejada` (Pedidos e Negociação); oferta pausada e segunda oferta do mesmo fornecedor e produto (Fornecimento; professor: "não usual") | Outros grupos |
| Visibilidade e interface | Decididas as regras do Mercado (DG-49, DG-50); detalhes de tela, avaliação de fornecedores (0 a 5 estrelas), alerta de preço acima do mercado e comparação com preço externo | INT-MRC-08; desejáveis fora da v1 |
| Plataforma e entrega | Administrador da plataforma; formato `entrega_grupo/`; padrão de erro da turma; DDL oficial; origem das regras citadas no boilerplate do kit | INT-MRC-06, INT-MRC-09 (INT-MRC-07 fechada em 25/11) |

## 14. Checklist de prontidão (Definition of Ready) — preenchido pelo QA

Pré-conferência do autor antes do gate G1; o QA confirma ou desmarca cada item.

- [x] Nenhum `{{ }}` restante; comentários de orientação removidos.
- [x] Todo RF tem enunciado EARS, prioridade e CAs de sucesso e erro quando aplicáveis; sem exceções (o RF-12 cobre o erro no próprio CA).
- [x] Todo CA usa valores concretos e resultado observável.
- [x] Todo código de erro citado em CA existe no §10 e vice-versa.
- [x] Ordem de verificação de erros definida para cada endpoint.
- [x] Campos de entidade, projeções de resposta e campos derivados estão mapeados; exemplos JSON respeitam os contratos.
- [x] Todo tópico citado tem produtor/consumidor, chave e exemplo.
- [x] Toda dependência externa está modelada como porta (§5.1).
- [x] Todo RNF tem métrica, limiar e forma de verificação.
- [x] Matriz de rastreabilidade completa (nenhum RF sem CA, nenhum CA órfão).
- [x] Não há palavras vagas sem quantificação.
- [ ] Seção 13 sem questões abertas e tabela 13.1 (casos de borda) toda decidida. A tabela 13.1 está decidida; continuam abertas as questões de integração INT-MRC-03, INT-MRC-06, INT-MRC-08 e INT-MRC-09 (a INT-MRC-01 e a INT-MRC-02 foram fechadas em 19/11; a INT-MRC-05, a INT-MRC-07, a INT-MRC-10 e a INT-MRC-11, em 25/11; a INT-MRC-04, em 26/11). Neste serviço vale o contrato desta ESP; os CAs usam dublês dos serviços parceiros.
- [x] Perguntas da última geração (diretiva 4 do §1), se houver, respondidas e incorporadas como regra (etapas 1 e 2 da geração respondidas nas DG-59 a DG-66 e no §13.1).

## 15. Histórico de versões

| Versão | Data | Autor | Mudança |
|---|---|---|---|
| 0.1 | 2026-11-12 | Brunno Alexcson | Criação em `docs/mercado/requisitos.md`, no formato da ESP-PED-001, com os contratos publicados pelos grupos parceiros e a regra de modos D-10 do kit. |
| 0.2 | 2026-11-16 | Brunno Alexcson | Respostas do professor (PROF-01 a PROF-12): pedido temporário na abertura do leilão, leilão em lote com rateio, "leilão reverso" como vitrine, balanço por produto e sugestão ao fornecedor. |
| 0.3 | 2026-11-18 | Brunno Alexcson | Reescrita no modelo ERS-LLM v1.2 do QA (DG-36); decisões DG-01 a DG-35; negociação direta pedida pelo comprador com saldo e atendimento parcial; leilão compartilhado formado pela sugestão; contratos e schemas em `docs/mercado/contratos/`; divergências com a ESP-NEG-001 na INT-MRC-01; dúvidas não decididas no §13.2. |
| 0.4 | 2026-11-18 | Brunno Alexcson | Decisões DG-37 a DG-50: propostas ao Pedidos para o pedido temporário; contratos de eventos fechados do lado do Mercado; prazo vencido tira a demanda do balanço sem expirá-la (RN-10, RF-12; sai a situação `EXPIRADA`); vitrine e balanço públicos com totais; INT-MRC-02 e INT-MRC-04 atualizadas; §13.2 revisada. |
| 0.5 | 2026-11-19 | Brunno Alexcson | Decisões DG-51 a DG-54, alinhadas com Pedidos e Negociação e com o diagrama: o Mercado continua formando o lote e publicando `leilao_iniciado`; a Negociação pede o pedido temporário de cada oferta com `pedido_temporario_solicitado`, na direta e no leilão, e só libera os lances com as reservas confirmadas (DG-51, DG-52); fechamento republicado é ignorado (DG-53); `grupoId` ignorado na v1 (DG-54); `tipo` e `produtoId` obrigatórios no `negociacao_fechada`; PR-01 reescrita; DG-28 e DG-37 a DG-39 revistas; INT-MRC-01 a INT-MRC-03 e INT-MRC-05 atualizadas (INT-MRC-02 fechada); RNF-06 corrigido; histórico em ordem. |
| 0.6 | 2026-11-22 | Brunno Alexcson | DG-55: IDs de negócio (`produtoId`, `fornecimentoId`, `fornecedorId`, `compradorId`, `demandaId`, `ocorrenciaId`) limitados a 64 caracteres na API, no consumo e nos schemas de `docs/mercado/contratos/`, alinhado com a Negociação e o Pedidos (INT-NEG-12). |
| 0.7 | 2026-11-25 | Brunno Alexcson | Alinhamento do dia 25/11: INT-MRC-01, INT-MRC-02 e INT-MRC-05 fechadas e separadas das abertas no §13; pedidos ao Compra e Demanda nas issues #1 e #2 (DG-03, DG-47, DG-48, INT-MRC-04); `mercado.dlt` alinhada à biblioteca do kit (3 execuções, `stackTrace` em `PROCESSAMENTO_FALHOU`); execução no kit pelo `compose.mercado.yml`; diretivas 4 (perguntar em vez de supor) e 7; árvore do §5 com `tests/`, `demo/`, `.dockerignore` e `.gitignore`. Revisão para a geração: métodos `obter_modelo`, `salvar_modelo`, `obter_situacao_balanco` e `salvar_situacao_balanco` nas portas; `correlationId` na sugestão; ocorrência de modelo cancelado ignorada (RN-08, CA-13, DG-04); lote sem itens no §6.2; adaptadores (falhas de conexão, polling da outbox, DLT fora da outbox), métricas e migração; envelope sem campos extras no §7.3; DG-31 e DG-40 atualizadas; §13.2 depois do §13.1. Decisões: banco indisponível no consumo como no Pedidos (DG-57, RN-31, CA-57); tabelas de exemplo do kit apagadas pela migração (DG-56); INT-MRC-06 atualizada e INT-MRC-07 fechada; IDs dos eventos publicados com `pattern` `\S`; `JWKS_URL` do kit pelo serviço `gateway`. Revisão externa: item com `valorItem` 0.00 sai do lote e limites numéricos do §4; rateio pela proporção exata de `valorItem` (DG-22); `incrementoMinimoPercentual` no `leilao_iniciado`; preço e estoque com instantes separados (RN-01, CA-02); cancelamento antecipado (RN-08, RN-09, CA-12); ordem da outbox por chave; §12, §5.1, §5 e RNF-03 corrigidos. |
| 0.8 | 2026-11-25 | Brunno Alexcson | Compra imediata pelo preço da oferta: campo opcional `compraImediata` na API-07, na negociação (§6.1 e migração), na API-09 e no `modo_negociacao_definido` (§7.4, schema e exemplos), sem tópico novo (RN-35, DG-58, CA-26, CA-32, CA-33); pedido à Negociação na INT-MRC-10. Revisão externa: limite de 1000 itens e da soma das quantidades no lote (RN-21); `valorItem` da direta entre 0.00 (exclusive) e o limite (RN-14, CA-32); cancelamento de modelo recorrente desconhecido cancela as ocorrências já recebidas e ignora as seguintes (RN-08, RN-09, CA-14, §6.2); participação pelo maior resto, sem valor negativo (RN-23, RNF-01); DG-23 alinhada ao rateio pelo `valorItem`; issue #11 registrada na INT-MRC-11; casos de borda no §13.1; §3.2, DG-16, CA-32 e §14 ajustados. |
| 0.9 | 2026-11-25 | Brunno Alexcson | Decisões da implementação (etapas 1 e 2): DG-59 a DG-66 (prazo vencido na direta e na aptidão, saldo negociável sem valor negativo, unidade sem produto, casos da `mercado.dlt`, casas do `incrementoMinimoPercentual`, totais sem limite, lote fechado sem itens, projeção da sugestão); glossário, §5 (`jsonschema` só nos testes), migração (`excesso_demanda` `NUMERIC(18,2)` e `processed_events` com `TEXT`), §6.1, §7.4 e RN-21 ajustados; premissas da implementação no §13.1. |
| 0.10 | 2026-11-26 | Brunno Alexcson | Revisão independente do código: oferta que muda de produto sai da sugestão do produto anterior e nunca entra em lote de outro produto (DG-67); bloqueio do produto na RN-02 e porta `bloquear_id_demanda` nas RN-08 e RN-09 (DG-68, §5.1); `30` e `30.00` com o mesmo hash de idempotência (§13.1). |
| 0.11 | 2026-11-26 | Brunno Alexcson | Segunda revisão independente do código: `agora` relido depois dos bloqueios e unidade da RN-03 comparada com o produto bloqueado (DG-69, glossário); `page` limitado a 9 dígitos (§7.2), para número muito longo responder `422` e não `500`; validade da chave de idempotência conferida depois do bloqueio da chave; §14 atualizado; INT-MRC-03 registrada na issue #6 do kit; INT-MRC-04 fechada com as respostas das issues #1 e #2 do Compra e Demanda (DG-03, DG-45, DG-47, DG-48). |
| 0.12 | 2026-11-27 | Brunno Alexcson | Duração do leilão escolhida pelo fornecedor no primeiro aceite, com 24 h por padrão (PROF-13, DG-70): `duracaoLeilaoHoras` na API-11 e na sugestão (§6.1, migração `0002`), RN-18, RN-21, DG-13, glossário (T2), CA-34 a CA-36 e §13.1. |
