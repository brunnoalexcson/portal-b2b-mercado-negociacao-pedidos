# ESP-NEG-001 — Especificação de Requisitos: Serviço de Negociação e Leilão

## 0. Metadados

| Campo | Valor |
|---|---|
| Identificador | ESP-NEG-001 |
| Versão | 1.2 |
| Status | Rascunho |
| Serviço / Bounded context | `negociacao-service` (microsserviço 6 do Portal B2B Distribuído): Negociação e Leilão. Porta `8106`, rotas `/api/v1/negociacoes`, schema PostgreSQL `negociacao`, role `svc_negociacao` |
| Autores (PO / Dev / QA) | PO e Dev: Caio Almeida Oliveira (grupo Mercado, Negociação/Leilão e Pedidos). QA: Grupo de Teste e Qualidade (revisão no gate G1) |
| Data | 2026-11-19 |
| Documentos relacionados | ESP-MRC-001 v0.5 (`docs/mercado/requisitos.md`, revisão `5c8e8a2`); ESP-PED-001 v2.1 (`docs/pedidos/requisitos.md`, revisão `8091833`); contratos de eventos em `docs/negociacao/contratos/`; kit de Infraestrutura (`CONTRACTS.md` e `ORIENTACOES-ARQUITETURA-GRUPOS.md`, revisão `630515c`); Identity-Service (revisão `800f2d8`); DR-BD-002 v1.0; modelo ERS-LLM v1.2 do QA (revisão `efc05b6`) |

## 1. Diretivas para a LLM geradora

1. Implemente **exatamente** o que está especificado. Não adicione endpoints, campos, tópicos, perfis ou regras que não estejam neste documento.
2. **Ordem de precedência** quando houver conflito aparente: Catálogo de erros (§10) e Contratos (§7) > Critérios de aceitação (§8) > Regras de negócio (§9) > Requisitos funcionais (§8) > texto descritivo.
3. Nomes de campos, tópicos, códigos de erro, rotas e valores de enumeração são **literais**: use-os byte a byte, com a mesma grafia e caixa.
4. Quando algo não estiver definido, escolha o comportamento mais simples que **não viole** nenhum item deste documento e registre a decisão em um comentário `# DECISÃO:` no código.
5. Entregue código completo e executável. Proibido: `TODO`, `pass` como implementação, pseudocódigo, trechos omitidos ("...").
6. Grave todos os arquivos completos na estrutura do §5, dentro de `services/negociacao-service/`. Na resposta final, liste seus caminhos relativos.
7. A implementação espelha a estrutura de `services/pedidos-service/` do mesmo repositório: domínio (`app/dominio.py`), contratos de entrada e saída (`app/contratos.py`), HTTP (`app/http.py`), ponto de entrada (`app/main.py`), adaptadores `app/adaptadores/postgres.py`, `app/adaptadores/kafka.py` e `app/adaptadores/jwks.py`, e migrações Alembic em `migracoes/`. Os arquivos de `services/pedidos-service/` e `docs/pedidos/` são somente leitura.

## 2. Contexto

### 2.1 Objetivo do serviço

Conduzir as negociações que o Mercado define: leilões em que compradores disputam o lote de uma ou mais ofertas do mesmo produto, e negociações diretas de preço entre um comprador e um fornecedor. O serviço pede ao Pedidos um pedido temporário por oferta, espera as reservas, conduz lances e propostas e, ao fim, informa o resultado ao Pedidos, que efetiva ou cancela os pedidos temporários, e ao Mercado, que atualiza o saldo das demandas.

### 2.2 Posição na arquitetura

O Mercado forma o lote do leilão e publica `leilao_iniciado`, ou valida a negociação direta pedida pelo comprador e publica `modo_negociacao_definido`. Este serviço abre a negociação com o mesmo `negociacaoId` e, para cada oferta, pede ao Pedidos um **pedido temporário** sem comprador (`pedido_temporario_solicitado`), que reserva o estoque no Fornecimento. O Pedidos informa o resultado da reserva por `pedido_atualizado`. No leilão, os lances só começam quando nenhuma reserva está pendente; a oferta recusada sai do lote antes do primeiro lance. Até `dataFim`, compradores dão lances no lote inteiro; em `dataFim`, o serviço encerra o leilão sozinho, rateia o valor entre as ofertas e publica `negociacao_fechada`. Na negociação direta, comprador e fornecedor trocam propostas de preço até o aceite, a recusa ou a expiração. O serviço não consome eventos do Fornecimento, da Demanda nem do Identity.

```text
Mercado      ──[leilao_iniciado, modo_negociacao_definido]─────────────────► Negociação
Pedidos      ──[pedido_atualizado]─────────────────────────────────────────► Negociação
Portal ─► Gateway Nginx ─REST /api/v1/negociacoes (JWT do Identity)────────► Negociação
Identity     ──JWKS (HTTP)─────────────────────────────────────────────────► Negociação

Negociação ──[pedido_temporario_solicitado]──► Pedidos (cria o pedido sem comprador; reserva via pedido_criado → Fornecimento)
Negociação ──[lance_realizado]───────────────► front-end, auditoria
Negociação ──[negociacao_fechada]────────────► Pedidos (efetiva os pedidos de alocacoes e cancela os demais), Mercado (saldo das demandas)
Negociação ──[negociacao.dlt]────────────────► operação, QA
```

Passos do fluxo principal: (1) o Fornecimento publica as ofertas e o Demanda, as demandas; (2) o Mercado calcula oferta × demanda; (3) o Mercado define a negociação: direta pedida pelo comprador ou leilão formado pela sugestão aceita pelos fornecedores; (4) **este serviço abre a negociação** e pede os pedidos temporários; (5) reservas; (6) lances ou propostas; (7) fechamento e `negociacao_fechada`; (8) o Pedidos segue com destino, frete e entrega.

## 3. Escopo

### 3.1 Dentro do escopo

- Abertura do leilão (`LEILAO_DIRETO`) a partir de `leilao_iniciado`, com as ofertas, o preço inicial, o incremento mínimo, as participações, `dataInicio` e `dataFim` enviados pelo Mercado.
- Abertura da negociação direta (`VENDA_DIRETA`) a partir de `modo_negociacao_definido`, com a primeira proposta pelo preço da oferta.
- Um pedido temporário por oferta da negociação (`pedido_temporario_solicitado`).
- Registro da confirmação ou da recusa da reserva a partir de `pedido_atualizado`.
- Liberação dos lances quando nenhuma reserva do leilão está pendente, com a retirada das ofertas recusadas e o recálculo do preço inicial, do incremento mínimo e das participações.
- Lances de compradores no lote inteiro, com incremento mínimo, `demandaId` opcional e sigilo do nome dos concorrentes.
- Encerramento automático do leilão em `dataFim`, com vencedor (`ENCERRADO`) ou sem lance (`DESERTO`), e rateio do valor pela participação de cada oferta.
- Negociação direta com contrapropostas de preço, aceite, recusa e expiração em 48 h.
- Consultas de negociações, lances e propostas, com paginação.
- Autenticação por JWT do Identity-Service, erros RFC 7807, `X-Correlation-Id`, `Idempotency-Key`, `/health`, `/health/ready`, `/metrics`.
- Persistência no schema `negociacao`, outbox transacional, consumo idempotente (`processed_events`), DLT `negociacao.dlt` e execução em duas réplicas.

### 3.2 Fora do escopo (não implementar)

- Comparar oferta × demanda, sugerir o leilão, formar o lote (aceites, alteração e saída antes de `dataInicio`), definir `dataInicio`, `dataFim`, preço inicial, incremento e participações, e validar demanda, oferta, saldo e conflito de interesse da negociação direta (Mercado). Este serviço usa esses valores como chegam.
- "Leilão reverso" (`LEILAO_REVERSO`): não é leilão; mostrar o menor preço é a vitrine do Mercado.
- Criar leilão ou negociação direta por HTTP; entrar com oferta num leilão; retirar oferta de um leilão.
- Consumir eventos do Fornecimento (`fornecimento_criado`, `fornecimento_atualizado`, `estoque_atualizado`), da Demanda ou do Identity (`tenant_bloqueado`, `usuario_bloqueado`, `sessao_comprometida`); manter cópia local das ofertas.
- Validar ou consultar o `demandaId` informado no lance (o Mercado confere ao consumir `negociacao_fechada`).
- Criar, efetivar ou cancelar pedidos; estados do pedido; destino da entrega; nota fiscal (Pedidos).
- Reservar, baixar ou liberar estoque; alterar preço ou saldo de oferta (Fornecimento).
- Demandas, frete, cotação, emissão de token, cadastro de empresas e usuários, telas.
- DDL oficial do banco (a proposta do schema `negociacao` é deste serviço; a aprovação é do grupo de Banco).
- Cancelamento de leilão; encerramento manual ou antecipado; papel de administrador da plataforma; forma de pagamento; lance para parte do lote; edição ou exclusão de lances e propostas; republicação automática de `negociacao_fechada` recusado pelo Pedidos; SSE ou WebSocket.
- Bloqueio imediato de empresa ou usuário revogado no Identity: o token é aceito até expirar.

## 4. Glossário

| Termo | Definição precisa |
|---|---|
| Agora | Valor de `clock.now()` (porta `Clock`). O serviço nunca lê o relógio do sistema diretamente. |
| Instante | Data e hora em UTC, ISO-8601 com milissegundos e sufixo `Z` na saída (ex.: `"2026-11-20T13:00:00.000Z"`). Na entrada, aceita qualquer ISO-8601 **com** fuso (`Z` ou `±hh:mm`) e converte para UTC; sem fuso é inválido. |
| Valor monetário | Reais (BRL). Na API e nos eventos: número JSON (nunca string), maior que zero, com no máximo 2 casas decimais e no máximo 12 dígitos na parte inteira. Na saída, sempre com exatamente 2 casas (`2550` recebido → `2550.00` devolvido). No código, `decimal.Decimal`; `float` é proibido. No banco, `NUMERIC(18,4)`. |
| Quantidade | Número JSON maior que zero com no máximo 2 casas decimais e no máximo 10 dígitos na parte inteira, na unidade do produto. Saída com exatamente 2 casas. `Decimal` no código, `NUMERIC(12,2)` no banco. |
| Participação | Fração do valor do lote que cabe a uma oferta: número JSON maior que 0 e no máximo 1, com no máximo 4 casas. Saída com exatamente 4 casas. `Decimal` no código, `NUMERIC(6,4)` no banco. Vem do Mercado e só muda pela RN-05. |
| Empresa | Valor do claim `tenant_id` do token. É a identidade comparada com `compradorId` e `fornecedorId`. |
| Escopo | Valor do claim `active_scope`: `BUYER` (comprador) ou `SUPPLIER` (fornecedor) nas rotas de negócio. |
| Oferta | Um fornecimento do Fornecimento, identificado por `fornecimentoId`, com dono (`fornecedorId`). Este serviço só conhece as ofertas que chegam nos eventos do Mercado. |
| Leilão | Negociação com `modo = "LEILAO_DIRETO"`. Compradores disputam um lote; vence o maior lance. |
| Oferta do leilão | Registro `OfertaLeilao` (§6.1): um item do lote enviado pelo Mercado, com quantidade, preço unitário, valor, participação e situação da reserva. |
| Lote | Ofertas do leilão com `situacao = "CONFIRMADA"`. `quantidadeTotal` = soma das quantidades do lote. Fica fixo quando os lances são liberados (RN-05). |
| Preço inicial | `precoInicial`: valor mínimo do primeiro lance. Vem do `leilao_iniciado` e é recalculado pela RN-05 se alguma oferta sair do lote. |
| Incremento mínimo | `incrementoMinimo`: quanto cada lance seguinte precisa superar o melhor lance. Vem do `leilao_iniciado` e é recalculado pela RN-05. |
| Lance | Valor monetário **total** oferecido por um comprador pelo lote inteiro. |
| Melhor lance | Lance de maior valor do leilão. Pela RN-07, é sempre o último lance aceito. |
| Aguardando reservas | `status == "AGUARDANDO_RESERVAS"`: leilão aberto com alguma oferta de reserva `PENDENTE`. Não aceita lances. |
| Leilão aceitando lances | `status == "ABERTO"` **e** `agora < dataFim`. |
| Fornecedor do leilão | Empresa dona de alguma oferta do leilão, em qualquer situação. |
| Pedido temporário | Pedido criado pelo Pedidos sem comprador, a partir de `pedido_temporario_solicitado`, que reserva o estoque de uma oferta. Este serviço só conhece o seu `pedidoId`, recebido em `pedido_atualizado`. |
| Reserva | Situação do pedido temporário: `PENDENTE` (pedido enviado), `CONFIRMADA` ou `RECUSADA` (informadas por `pedido_atualizado`). |
| Rateio | Divisão do valor do lance vencedor entre as ofertas do lote, pela participação de cada uma (RN-11). |
| Negociação direta | Negociação com `modo = "VENDA_DIRETA"`: um comprador e uma oferta, com troca de propostas de preço unitário. |
| Proposta | Preço unitário proposto numa negociação direta por uma das partes. A primeira é o preço da oferta, registrada como do fornecedor, na abertura. |
| Vez | Parte (`BUYER` ou `SUPPLIER`) que deve responder à última proposta. Só a parte da vez propõe ou aceita. |
| Prazo de resposta | `expiraEm` = instante da última proposta + 48 h. Negociação direta aceitando ações: `status == "ABERTO"` **e** `agora < expiraEm`. |
| Participante | Na negociação direta: o comprador (escopo `BUYER` e empresa = `compradorId`) ou o fornecedor (escopo `SUPPLIER` e empresa = `fornecedorId`). |
| Conflito de interesse | Empresa que dá lance num leilão em que é fornecedor do leilão. Na negociação direta, o Mercado impede a oferta própria antes da abertura. |
| `"OCULTO"` | String literal que substitui o `compradorId` nas respostas, conforme o sigilo (RN-09). |

## 5. Restrições técnicas

| Item | Valor obrigatório |
|---|---|
| Linguagem / versão | Python 3.12 |
| Framework | FastAPI com Pydantic 2 e Uvicorn |
| Dependências permitidas | As versões fixadas em `services/pedidos-service/requirements.txt`: `fastapi==0.162.5`, `starlette==1.10.0`, `uvicorn==0.54.0`, `pydantic==2.14.3`, `psycopg[binary]==3.3.9`, `psycopg-pool==3.3.3`, `alembic==1.20.1`, `SQLAlchemy==2.1.8`, `aiokafka==0.15.0`, `PyJWT[crypto]==2.15.1`, `cryptography==50.0.2`, `simplejson==3.20.2`, `prometheus-client==0.26.0`, além da biblioteca padrão. Nenhuma outra. Os módulos fora de `app/adaptadores/` e `migracoes/` importam somente biblioteca padrão, `fastapi`, `starlette`, `pydantic`, `jwt`, `cryptography`, `simplejson`, `prometheus_client` e o próprio pacote `app`. |
| Persistência | Somente pela porta `NegociacaoRepositorio` (§5.1). Proibido guardar negociações, ofertas, lances, propostas, chaves de idempotência ou eventos processados em variáveis de módulo ou da aplicação. |
| Estrutura de arquivos | Ver árvore abaixo. `app/__init__.py`, `app/portas.py` e `app/main.py` são obrigatórios. |
| Ponto de entrada (testes) | `app.main.create_app(event_bus: EventBus, clock: Clock, repositorio: NegociacaoRepositorio, chaves_publicas: ChavesPublicas, emissor: str = "identity-service", audiencia: str = "b2b-platform") -> fastapi.FastAPI` e `app.main.executar_prazos(app: fastapi.FastAPI) -> int` (processa os prazos vencidos em `clock.now()` e devolve quantas negociações mudaram de estado). |
| Ponto de entrada (execução) | `uvicorn app.main:criar_app_real --factory --host 0.0.0.0 --port 8106`: monta os adaptadores reais a partir das variáveis de ambiente e inicia, como tarefas de fundo, o consumidor Kafka, o publicador da outbox e o agendador (`executar_prazos` a cada 1 s). |
| Formato de data/hora | Instante (§4). |
| Formato monetário | Valor monetário (§4). Arredondamento sempre explícito, conforme RN-05 e RN-11. |
| Configuração | `DATABASE_URL` (role `svc_negociacao`), `MIGRATION_DATABASE_URL` (role `portal_ddl`), `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_GROUP_ID` (`negociacao-service-v1`), `JWKS_URL` (`https://identity.matheus-alves.dev/.well-known/jwks.json` por padrão), `JWT_ISSUER` (`identity-service`), `JWT_AUDIENCE` (`b2b-platform`), `APP_PORT` (`8106`), `LOG_LEVEL` (`INFO`). Documentadas em `.env.example`. |

```text
services/negociacao-service/
├── app/
│   ├── __init__.py
│   ├── portas.py            # Protocols do §5.1 e a exceção RepositorioIndisponivel
│   ├── main.py              # create_app, executar_prazos, criar_app_real
│   ├── dominio.py           # regras puras: estados, lance, rateio, vez, prazos
│   ├── contratos.py         # validação de corpo HTTP e de eventos consumidos; montagem dos eventos publicados
│   ├── http.py              # rotas, autenticação, erros RFC 7807, correlação, idempotência
│   ├── jsonio.py            # JSON com Decimal (simplejson)
│   └── adaptadores/
│       ├── postgres.py      # NegociacaoRepositorio real (psycopg 3, pool ≤ 5)
│       ├── kafka.py         # EventBus real: outbox, publicador, consumidor, DLT
│       └── jwks.py          # ChavesPublicas real (PyJWKClient, cache de 1 h)
├── migracoes/
│   ├── env.py
│   └── versions/0001_negociacao.py
├── alembic.ini
├── requirements.txt
├── .env.example
├── Dockerfile
├── compose.demo.yml
├── compose.negociacao.yml
└── README.md
```

### 5.1 Portas (interfaces de infraestrutura)

```python
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Callable, Protocol


class RepositorioIndisponivel(Exception):
    """Lançada pelo repositório quando o banco não responde."""


class EventBus(Protocol):
    def publish(self, topic: str, key: str, message: dict) -> None: ...
    def subscribe(self, topic: str, handler: Callable[[dict], None]) -> None: ...
    def disponivel(self) -> bool: ...


class Clock(Protocol):
    def now(self) -> datetime: ...          # datetime com timezone UTC
    def sleep(self, segundos: float) -> None: ...


class ChavesPublicas(Protocol):
    def obter(self, kid: str) -> object | None: ...   # chave pública RSA do kid, ou None se desconhecido


class NegociacaoRepositorio(Protocol):
    def transacao(self) -> AbstractContextManager[None]: ...
    def disponivel(self) -> bool: ...
    # consumo idempotente
    def evento_processado(self, source: str, event_id: str) -> bool: ...
    def registrar_evento_processado(self, source: str, event_id: str, topico: str) -> None: ...
    # negociações
    def inserir_negociacao(self, negociacao: dict) -> None: ...
    def obter_negociacao(self, negociacao_id: str, bloquear: bool = False) -> dict | None: ...
    def atualizar_negociacao(self, negociacao: dict) -> None: ...
    def listar_negociacoes(self, empresa: str, escopo: str, modo: str | None, status: str | None,
                           offset: int, limite: int) -> tuple[list[dict], int]: ...
    def listar_vencidas(self, agora: datetime, limite: int) -> list[str]: ...
    # ofertas do leilão
    def inserir_oferta_leilao(self, oferta: dict) -> None: ...
    def listar_ofertas_leilao(self, negociacao_id: str) -> list[dict]: ...
    def atualizar_oferta_leilao(self, oferta: dict) -> None: ...
    # lances e propostas
    def inserir_lance(self, lance: dict) -> None: ...
    def listar_lances(self, negociacao_id: str, offset: int, limite: int) -> tuple[list[dict], int]: ...
    def inserir_proposta(self, proposta: dict) -> None: ...
    def listar_propostas(self, negociacao_id: str, offset: int, limite: int) -> tuple[list[dict], int]: ...
    # idempotência HTTP
    def obter_idempotencia(self, escopo: str) -> dict | None: ...
    def salvar_idempotencia(self, escopo: str, registro: dict) -> None: ...
```

Comportamento das portas:

- `app/portas.py` contém exatamente as definições acima. Quem chama `create_app` fornece as implementações; nos testes, dublês em memória.
- `create_app` chama, durante a criação da aplicação, `event_bus.subscribe` para os tópicos `leilao_iniciado`, `modo_negociacao_definido` e `pedido_atualizado`, um handler para cada. O handler recebe a mensagem completa (envelope, §7.3) como `dict` e **nunca** propaga exceção.
- `transacao()`: tudo o que é feito no repositório dentro de `with repositorio.transacao():` é gravado de forma atômica; se uma exceção sair do bloco, nada é gravado. Cada requisição HTTP de escrita, cada mensagem consumida e cada negociação processada por `executar_prazos` usam uma única transação.
- Os dicionários trocados com o repositório usam as chaves dos atributos do §6.1, com instantes em `datetime` UTC e valores monetários, quantidades e participações em `Decimal`.
- `obter_negociacao(id, bloquear=True)` bloqueia a negociação até o fim da transação (no PostgreSQL, `SELECT ... FOR UPDATE`). Toda escrita sobre uma negociação existente (reserva, liberação dos lances, lance, proposta, aceite, recusa, prazo) obtém a negociação com `bloquear=True` antes de validar as regras de domínio. É isso que serializa os lances do mesmo leilão entre as duas réplicas.
- `listar_negociacoes` devolve `(itens, total)`: no máximo `limite` itens a partir de `offset`, em ordem de `criadoEm` decrescente e, em empate, `negociacaoId` decrescente; `total` conta todos os itens do filtro. Entram todas as negociações com `modo = "LEILAO_DIRETO"` e as com `modo = "VENDA_DIRETA"` em que (`escopo == "BUYER"` e `compradorId == empresa`) ou (`escopo == "SUPPLIER"` e `fornecedorId == empresa`). `modo` e `status` `None` = sem filtro.
- `listar_vencidas(agora, limite)` devolve até `limite` identificadores de negociações em que: (`modo = "LEILAO_DIRETO"`, `status` ∈ {`"AGUARDANDO_RESERVAS"`, `"ABERTO"`} e `dataFim <= agora`) ou (`modo = "VENDA_DIRETA"`, `status = "ABERTO"` e `expiraEm <= agora`), em ordem do prazo mais antigo.
- `listar_ofertas_leilao` devolve as ofertas do leilão em ordem de `ordem` crescente. `listar_lances` e `listar_propostas` devolvem em ordem de `sequencia` crescente, com o total.
- `salvar_idempotencia` insere ou substitui pelo escopo.
- Qualquer método do repositório pode lançar `RepositorioIndisponivel`; o serviço responde `503 DEPENDENCIA_INDISPONIVEL` na API e, no consumo, aplica as novas tentativas da RN-19. Qualquer outra exceção vira `500 ERRO_INTERNO` na API.
- `publish` é chamado **dentro** da transação da mudança, como último passo antes do fim do bloco, e nunca chama handlers de forma síncrona. Ordem de toda escrita: validar → alterar o estado → publicar. Falha lançada por `publish` está fora do escopo das portas (§13.1).
- `clock.now()` sempre devolve `datetime` com fuso; se vier sem fuso, tratar como UTC. `clock.sleep(segundos)` é a única forma de esperar; nos testes, o dublê retorna na hora e registra o valor.
- `chaves_publicas.obter(kid)` devolve a chave pública RSA (objeto aceito por `jwt.decode`) ou `None` se o `kid` for desconhecido. Se lançar exceção, a resposta é `503 DEPENDENCIA_INDISPONIVEL`. O serviço nunca faz requisição HTTP para obter chaves fora desse adaptador.
- `event_bus.disponivel()` e `repositorio.disponivel()` alimentam `/health/ready`.

Comportamento dos adaptadores reais (não usados pela suíte de aceitação em memória):

- **Repositório (`postgres.py`):** psycopg 3 com `psycopg_pool.ConnectionPool(max_size=5)`, role `svc_negociacao`, só o schema `negociacao`. `transacao()` abre uma conexão do pool numa variável de contexto e faz commit ao sair do bloco sem exceção. Erros de conexão viram `RepositorioIndisponivel`.
- **Outbox (`kafka.py`):** dentro da transação, `publish` grava o envelope em `negociacao.outbox` pela mesma conexão. Um publicador em cada réplica lê o registro pendente mais antigo com `FOR UPDATE SKIP LOCKED`, envia com `acks=all` e a chave do registro, marca `confirmado_em` e, em falha, reagenda com espera `min(30, 2^tentativas)` segundos. Todo reenvio usa o mesmo `eventId`.
- **Consumidor (`kafka.py`):** `aiokafka` com grupo `negociacao-service-v1`, `enable_auto_commit=False`. Cada mensagem é decodificada como JSON; bytes que não são JSON vão direto para `negociacao.dlt` (RN-20). O handler roda fora do laço de eventos (`asyncio.to_thread`). O offset é confirmado só depois do retorno do handler. `processed_events` (`source`, `event_id`) é gravado na mesma transação do efeito.
- **Agendador:** cada réplica chama `executar_prazos` a cada 1 s. A segurança com duas réplicas vem do `bloquear=True` e da nova verificação do estado dentro da transação (RN-21).
- **JWKS (`jwks.py`):** `jwt.PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600, timeout=2)`. Falha de rede lança exceção (→ 503); `kid` ausente no conjunto devolve `None` (→ 401).
- **Logs:** JSON em `stdout` com `timestamp`, `level`, `service` (`negociacao-service`), `correlationId`, `message` e, quando conhecidos, `negociacaoId` e `eventId`. Sem `print()`. Nunca registrar o token.

## 6. Modelo de domínio

### 6.1 Entidades

#### Negociacao (tabela `negociacao.processo_negociacao`)

| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| negociacaoId | UUID (string) | Sim | `negociacaoId` do evento do Mercado que abriu a negociação. Coluna `id`. |
| modo | enum | Sim | `"LEILAO_DIRETO"` (aberta por `leilao_iniciado`) ou `"VENDA_DIRETA"` (aberta por `modo_negociacao_definido`). Nos eventos publicados, `tipo` = `"LEILAO"` ou `"VENDA_DIRETA"`, respectivamente. |
| status | enum | Sim | Leilão: `"AGUARDANDO_RESERVAS"`, `"ABERTO"`, `"ENCERRADO"`, `"DESERTO"`. Direta: `"ABERTO"`, `"ENCERRADO"`, `"RECUSADO"`, `"EXPIRADO"`. |
| produtoId | string 1–64 | Sim | Do evento de abertura. |
| dataInicio | instante | Sim | Leilão: `iniciaEm` do evento. Direta: igual a `criadoEm` no banco (coluna `NOT NULL` do DR-BD-002), exposta como `null` no resumo da API-04. |
| dataFim | instante | Leilão | Leilão: `encerraEm` do evento. Direta: `null`. |
| quantidadeTotal | quantidade | Leilão | Leilão: do evento; recalculada pela RN-05. Direta: `null`. |
| precoInicial | valor monetário | Leilão | Leilão: do evento; recalculado pela RN-05. Coluna `valor_reserva` (DR-BD-002). Direta: `null`. |
| incrementoMinimo | valor monetário | Leilão | Leilão: do evento; recalculado pela RN-05. Direta: `null`. |
| melhorLanceId | UUID | Não | Último lance aceito; `null` sem lances. |
| totalLances | inteiro ≥ 0 | Sim | Inicial 0. |
| compradorId | string 1–64 | Não | Leilão: empresa do lance vencedor, preenchida no encerramento `ENCERRADO`. Direta: `demandas[0].compradorId` do evento. |
| fornecimentoId | string 1–64 | Direta | Direta: `ofertas[0].fornecimentoId`. Leilão: `null`. |
| fornecedorId | string 1–64 | Direta | Direta: `ofertas[0].fornecedorId`. Leilão: `null`. |
| quantidade | quantidade | Direta | Direta: `ofertas[0].quantidade`. Leilão: `null`. |
| precoOferta | valor monetário | Direta | Direta: `ofertas[0].precoUnitario`. Leilão: `null`. |
| precoAtual | valor monetário | Direta | Direta: preço da última proposta (na abertura, `precoOferta`). Leilão: `null`. |
| vez | enum | Direta | `"BUYER"` ou `"SUPPLIER"`. Direta: `"BUYER"` na abertura. Leilão: `null`. |
| expiraEm | instante | Direta | Direta: instante da última proposta + 48 h. Leilão: `null`. |
| reservaSituacao | enum | Direta | `"PENDENTE"`, `"CONFIRMADA"` ou `"RECUSADA"`. Direta: `"PENDENTE"` na abertura. Leilão: `null` (a reserva fica em cada `OfertaLeilao`). |
| pedidoId | string 1–64 | Não | Direta: pedido temporário, recebido em `pedido_atualizado` `CONFIRMADA`. Leilão: `null`. |
| totalPropostas | inteiro ≥ 0 | Sim | Direta: 1 na abertura. Leilão: 0. |
| correlationId | string 1–128 | Sim | `correlationId` do evento que abriu a negociação; reutilizado nos eventos publicados por `executar_prazos`. |
| criadoEm | instante | Sim | `clock.now()` no consumo do evento de abertura. |
| encerradoEm | instante | Não | `clock.now()` quando a negociação chega a um estado final. |

#### OfertaLeilao (tabela `negociacao.oferta_leilao`, chave `(negociacaoId, fornecimentoId)`)

| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| negociacaoId | UUID | Sim | Leilão ao qual pertence. |
| fornecimentoId | string 1–64 | Sim | `itens[].fornecimentoId` do `leilao_iniciado`. Único por leilão. |
| fornecedorId | string 1–64 | Sim | Do item. |
| quantidade | quantidade | Sim | Do item. |
| precoUnitario | valor monetário | Sim | Do item; não muda. |
| valorItem | valor monetário | Sim | Do item; não muda. |
| participacao | participação | Sim | Do item; recalculada pela RN-05. |
| ordem | inteiro ≥ 1 | Sim | Posição do item na lista `itens` do evento (1 para o primeiro). Ordena as ofertas, os pedidos temporários e o rateio. Não aparece na API. |
| situacao | enum | Sim | `"PENDENTE"` na abertura; `"CONFIRMADA"` ou `"RECUSADA"` por `pedido_atualizado` (RN-04). |
| pedidoId | string 1–64 | Não | Recebido em `pedido_atualizado` `CONFIRMADA`. Não aparece na API. |

#### Lance (tabela `negociacao.lance`, `UNIQUE(negociacaoId, sequencia)`)

| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| lanceId | UUID | Sim | UUID4 gerado pelo serviço. |
| negociacaoId | UUID | Sim | Leilão. |
| compradorId | string 1–64 | Sim | Empresa do token. |
| usuarioId | string | Sim | `sub` do token. Não aparece na API nem nos eventos. |
| valor | valor monetário | Sim | Do corpo: valor total pelo lote. |
| demandaId | string 1–64 | Não | Do corpo (opcional), sem validação. Não aparece na API nem no `lance_realizado`; vai no `negociacao_fechada` quando o lance vence (RN-10). |
| sequencia | inteiro ≥ 1 | Sim | 1 no primeiro lance do leilão; +1 a cada lance aceito, sem buracos. |
| criadoEm | instante | Sim | `clock.now()` na aceitação. |

#### Proposta (tabela `negociacao.proposta`, `UNIQUE(negociacaoId, sequencia)`)

| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| propostaId | UUID | Sim | UUID4 gerado pelo serviço. |
| negociacaoId | UUID | Sim | Negociação direta. |
| autor | enum | Sim | `"BUYER"` ou `"SUPPLIER"`: parte que propôs. Na abertura, `"SUPPLIER"`. |
| empresaId | string 1–64 | Sim | Empresa do token; na abertura, `fornecedorId` da negociação. Não aparece na API. |
| precoUnitario | valor monetário | Sim | Do corpo; na abertura, `precoOferta`. |
| sequencia | inteiro ≥ 1 | Sim | 1 na abertura; +1 a cada contraproposta. |
| criadoEm | instante | Sim | `clock.now()`. |

Tabelas técnicas do schema `negociacao`: `idempotency_keys` (escopo, hash do corpo, status, corpo e `Location` da resposta, `criado_em`; validade de 24 h), `processed_events` (`source`, `event_id` únicos, `topico`, `processado_em`) e `outbox` (`event_id` UUID único, `topico`, `chave`, `envelope` JSONB, `tentativas`, `proximo_envio`, `confirmado_em`, `criado_em`). Nenhuma FK para outro schema. Dinheiro `NUMERIC(18,4)`; quantidade `NUMERIC(12,2)`; participação `NUMERIC(6,4)`; instantes `TIMESTAMPTZ`.

### 6.2 Estados e transições

Leilão (`LEILAO_DIRETO`):

| Estado atual | Evento / comando | Condição | Próximo estado |
|---|---|---|---|
| (inexistente) | `leilao_iniciado` válido | `negociacaoId` desconhecido (RN-01) | `AGUARDANDO_RESERVAS` (ofertas `PENDENTE`; um `pedido_temporario_solicitado` por oferta) |
| `AGUARDANDO_RESERVAS` | `pedido_atualizado` de oferta `PENDENTE` | ainda resta oferta `PENDENTE` (RN-04) | `AGUARDANDO_RESERVAS` (oferta `CONFIRMADA` ou `RECUSADA`) |
| `AGUARDANDO_RESERVAS` | `pedido_atualizado` da última oferta `PENDENTE` | ≥ 1 oferta `CONFIRMADA` (RN-05) | `ABERTO` (lote fixado; recálculo se houve recusa) |
| `AGUARDANDO_RESERVAS` | `pedido_atualizado` da última oferta `PENDENTE` | 0 oferta `CONFIRMADA` (RN-05) | `DESERTO` (publica `negociacao_fechada`) |
| `AGUARDANDO_RESERVAS` | `executar_prazos` | `agora >= dataFim` (RN-10) | `DESERTO` (publica `negociacao_fechada`) |
| `ABERTO` | API-07 lance | RN-06 a RN-08 | `ABERTO` (novo melhor lance) |
| `ABERTO` | `executar_prazos` | `agora >= dataFim` e `totalLances >= 1` | `ENCERRADO` |
| `ABERTO` | `executar_prazos` | `agora >= dataFim` e `totalLances = 0` | `DESERTO` |
| `ENCERRADO` / `DESERTO` | qualquer | — | (final, sem transições) |

Negociação direta (`VENDA_DIRETA`):

| Estado atual | Evento / comando | Condição | Próximo estado |
|---|---|---|---|
| (inexistente) | `modo_negociacao_definido` válido com `modo = "VENDA_DIRETA"` | `negociacaoId` desconhecido (RN-02) | `ABERTO` (`vez = "BUYER"`, reserva `PENDENTE`, proposta 1 do fornecedor) |
| `ABERTO` | API-09 contraproposta | RN-12 | `ABERTO` (`vez` invertida, `expiraEm` renovado) |
| `ABERTO` | API-10 aceitar | RN-12, RN-13 | `ENCERRADO` |
| `ABERTO` | API-11 recusar | RN-12 (prazo) | `RECUSADO` |
| `ABERTO` | `pedido_atualizado` `RECUSADA` | reserva `PENDENTE` | `RECUSADO` |
| `ABERTO` | `pedido_atualizado` `CONFIRMADA` | reserva `PENDENTE` | `ABERTO` (reserva `CONFIRMADA`) |
| `ABERTO` | `executar_prazos` | `agora >= expiraEm` | `EXPIRADO` |
| `ENCERRADO` / `RECUSADO` / `EXPIRADO` | qualquer | — | (final, sem transições) |

O prazo vencido não altera o `status` sozinho: até `executar_prazos` rodar, as ações já são recusadas pela RN-06 e pela RN-12, que comparam com `agora`.

## 7. Contratos de interface

### 7.1 Autenticação e contexto da requisição

Rotas públicas (sem token): `/health`, `/health/ready`, `/metrics`, `/docs`, `/openapi.json`. Todas as rotas `/api/v1/...` exigem `Authorization: Bearer <token>`, um JWT emitido pelo Identity-Service.

Validação do token, nesta ordem (decodificar sempre com `algorithms=["RS256"]` e `options={"verify_exp": False}`, comparando a expiração com `clock.now()`):

1. Cabeçalho ausente, vazio ou sem `Bearer ` seguido de texto; token malformado; cabeçalho do token sem `kid` ou com `alg` diferente de `"RS256"`; `chaves_publicas.obter(kid)` devolvendo `None`; assinatura inválida; `iss` diferente de `emissor`; `aud` que não contém `audiencia` (texto ou lista); `sub` ausente ou vazio; `exp` ausente, não inteiro ou `exp <= int(agora.timestamp())` → `401 TOKEN_INVALIDO`, com o cabeçalho `WWW-Authenticate: Bearer`. Se `chaves_publicas.obter` lançar exceção → `503 DEPENDENCIA_INDISPONIVEL`.
2. `view_mode` diferente de `"SINGLE"`, `tenant_id` ausente, nulo ou vazio, ou `active_scope` ausente ou nulo → `403 CONTEXTO_NAO_SELECIONADO`. É o token do login antes do `switch-context` (`view_mode = "NONE"`) ou o da visão de grupo econômico (`"MACRO_GROUP"`).
3. Escopo não permitido na rota (tabela do §7.2): `CARRIER` e `TENANT_ADMIN` em qualquer rota de negócio, e o escopo errado para a operação → `403 ACESSO_NEGADO`.

Claims ignorados na v1: `platform_role`, `roles`, `permissions`, `group_id`, `allowed_tenants`, `act`, `fid`, `jti`, `client_id` e `token_use`. Um token com `platform_role = "PLATFORM_ADMIN"` não tem nenhum poder adicional. Um token revogado no Identity é aceito até `exp` (no máximo 15 min).

`X-Correlation-Id`: valor de 1 a 128 caracteres de `[A-Za-z0-9._-]`; ausente, vazio ou fora desse formato → o serviço gera um UUID4. O valor efetivo volta no cabeçalho `X-Correlation-Id` de toda resposta, no `correlationId` dos erros e no `correlationId` dos eventos publicados pela requisição.

`Idempotency-Key` (opcional em todo `POST` de negócio): 1 a 128 caracteres de `[A-Za-z0-9._-]`; vazio equivale a ausente; fora do formato → `422 VALIDACAO_INVALIDA`. Regras na RN-18.

### 7.2 API REST

Todas as respostas de negócio são JSON com nomes de campo em camelCase. O FastAPI não pode validar o corpo antes das verificações 1 a 6 do §10: o corpo é lido com `await request.body()` e validado pelo próprio serviço. IDs na URL são UUID; outro formato → `422 VALIDACAO_INVALIDA`. Corpos com campos fora do contrato, campos ausentes, tipos errados (inclusive número enviado como string ou booleano), número com mais de 2 casas ou JSON malformado → `422 VALIDACAO_INVALIDA`. Listagens: `page` inteiro ≥ 1 (padrão 1) e `size` inteiro de 1 a 100 (padrão 10); fora disso → `422 VALIDACAO_INVALIDA`; resposta `{"items": [...], "page": P, "size": S, "total": T}`, com `items: []` numa página vazia. Não há rota de criação: leilões e negociações diretas são abertos pelos eventos do Mercado (§7.3).

| ID | Método | Rota | Escopos | Sucesso | Erros possíveis (na ordem de verificação) |
|---|---|---|---|---|---|
| API-01 | GET | `/health` | público | 200 | — |
| API-02 | GET | `/health/ready` | público | 200 | 503 `DEPENDENCIA_INDISPONIVEL` |
| API-03 | GET | `/metrics` | público | 200 | — |
| API-04 | GET | `/api/v1/negociacoes` | `BUYER`, `SUPPLIER` | 200 | 401 `TOKEN_INVALIDO` / 503 `DEPENDENCIA_INDISPONIVEL` → 403 `CONTEXTO_NAO_SELECIONADO` → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` |
| API-05 | GET | `/api/v1/negociacoes/{negociacaoId}` | `BUYER`, `SUPPLIER` | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 `NEGOCIACAO_NAO_ENCONTRADA` |
| API-06 | GET | `/api/v1/negociacoes/{negociacaoId}/lances` | `BUYER`, `SUPPLIER` | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` |
| API-07 | POST | `/api/v1/negociacoes/{negociacaoId}/lances` | `BUYER` | 201 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` → idempotência (replay ou 409 `IDEMPOTENCIA_CONFLITO`) → 403 `CONFLITO_DE_INTERESSE` → 409 `LEILAO_NAO_ABERTO` → 422 `LANCE_INSUFICIENTE` |
| API-08 | GET | `/api/v1/negociacoes/{negociacaoId}/propostas` | `BUYER`, `SUPPLIER` (participantes) | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` |
| API-09 | POST | `/api/v1/negociacoes/{negociacaoId}/propostas` | `BUYER`, `SUPPLIER` (participantes) | 201 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` → idempotência → 409 `NEGOCIACAO_ENCERRADA` → 409 `FORA_DA_VEZ` |
| API-10 | POST | `/api/v1/negociacoes/{negociacaoId}/aceitar` | `BUYER`, `SUPPLIER` (participantes) | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` → idempotência → 409 `NEGOCIACAO_ENCERRADA` → 409 `FORA_DA_VEZ` → 409 `RESERVA_PENDENTE` |
| API-11 | POST | `/api/v1/negociacoes/{negociacaoId}/recusar` | `BUYER`, `SUPPLIER` (participantes) | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` → idempotência → 409 `NEGOCIACAO_ENCERRADA` |

Visibilidade (RN-16): um leilão é visível a todo token `BUYER` ou `SUPPLIER`; uma negociação direta só aos participantes. Negociação inexistente ou invisível → `404 NEGOCIACAO_NAO_ENCONTRADA`. Em qualquer rota, banco indisponível → `503 DEPENDENCIA_INDISPONIVEL`; falha inesperada → `500 ERRO_INTERNO`.

#### Projeções de resposta

**Detalhe de leilão** (API-05). Campos derivados: `melhorLance` (lance de `melhorLanceId`, ou `null`), `agora` (`clock.now()`), `acoesPermitidas` (RN-17). `compradorId` é `null` até o encerramento `ENCERRADO`. `melhorLance.compradorId` e `compradorId` seguem o sigilo (RN-09). `ofertas` lista todas as ofertas do leilão na ordem de `ordem`, inclusive as recusadas. Exemplo visto por COMP-X em `2026-11-20T13:20:00.000Z`:

```json
{
  "negociacaoId": "31000000-0000-4000-8000-000000000001",
  "modo": "LEILAO_DIRETO",
  "status": "ABERTO",
  "produtoId": "35000000-0000-4000-8000-000000000001",
  "dataInicio": "2026-11-20T12:00:00.000Z",
  "dataFim": "2026-11-20T15:00:00.000Z",
  "quantidadeTotal": 20.00,
  "precoInicial": 2550.00,
  "incrementoMinimo": 25.50,
  "melhorLance": {
    "lanceId": "33000000-0000-4000-8000-000000000002",
    "negociacaoId": "31000000-0000-4000-8000-000000000001",
    "compradorId": "OCULTO",
    "valor": 2600.00,
    "sequencia": 2,
    "criadoEm": "2026-11-20T13:10:00.000Z"
  },
  "totalLances": 2,
  "compradorId": null,
  "ofertas": [
    {
      "negociacaoId": "31000000-0000-4000-8000-000000000001",
      "fornecimentoId": "34000000-0000-4000-8000-000000000001",
      "fornecedorId": "a1000000-0000-4000-8000-000000000001",
      "quantidade": 5.00,
      "precoUnitario": 120.00,
      "valorItem": 600.00,
      "participacao": 0.2353,
      "situacao": "CONFIRMADA"
    },
    {
      "negociacaoId": "31000000-0000-4000-8000-000000000001",
      "fornecimentoId": "34000000-0000-4000-8000-000000000002",
      "fornecedorId": "a1000000-0000-4000-8000-000000000002",
      "quantidade": 15.00,
      "precoUnitario": 130.00,
      "valorItem": 1950.00,
      "participacao": 0.7647,
      "situacao": "CONFIRMADA"
    }
  ],
  "criadoEm": "2026-11-20T12:00:00.000Z",
  "encerradoEm": null,
  "agora": "2026-11-20T13:20:00.000Z",
  "acoesPermitidas": ["DAR_LANCE"]
}
```

**Detalhe de negociação direta** (API-05, API-10, API-11). Exemplo visto por COMP-X logo após a abertura:

```json
{
  "negociacaoId": "31000000-0000-4000-8000-000000000002",
  "modo": "VENDA_DIRETA",
  "status": "ABERTO",
  "produtoId": "35000000-0000-4000-8000-000000000001",
  "fornecimentoId": "34000000-0000-4000-8000-000000000002",
  "fornecedorId": "a1000000-0000-4000-8000-000000000002",
  "compradorId": "b2000000-0000-4000-8000-000000000001",
  "quantidade": 10.00,
  "precoOferta": 130.00,
  "precoAtual": 130.00,
  "vez": "BUYER",
  "expiraEm": "2026-11-22T12:00:00.000Z",
  "reservaSituacao": "PENDENTE",
  "totalPropostas": 1,
  "criadoEm": "2026-11-20T12:00:00.000Z",
  "encerradoEm": null,
  "agora": "2026-11-20T12:00:00.000Z",
  "acoesPermitidas": ["PROPOR", "RECUSAR"]
}
```

**Resumo** (itens da API-04): `negociacaoId`, `modo`, `status`, `produtoId`, `dataInicio` (`null` na direta), `dataFim` (`null` na direta), `expiraEm` (`null` no leilão), `criadoEm`.

**Oferta do leilão** (itens de `ofertas`): `negociacaoId`, `fornecimentoId`, `fornecedorId`, `quantidade`, `precoUnitario`, `valorItem`, `participacao`, `situacao`.

**Lance** (API-06 e API-07): `lanceId`, `negociacaoId`, `compradorId`, `valor`, `sequencia`, `criadoEm`. Na API-06 o `compradorId` segue o sigilo; na API-07 nunca é mascarado.

**Proposta** (API-08 e API-09): `propostaId`, `negociacaoId`, `autor`, `precoUnitario`, `sequencia`, `criadoEm`.

#### API-01 — Saúde

Resposta 200: `{"status": "UP", "service": "negociacao-service", "timestamp": "2026-11-20T12:00:00.000Z"}` (`timestamp` = `clock.now()`).

#### API-02 — Prontidão

Resposta 200 quando `repositorio.disponivel()` e `event_bus.disponivel()` são verdadeiros: `{"status": "READY", "service": "negociacao-service"}`. Caso contrário, `503 DEPENDENCIA_INDISPONIVEL` no formato do §10.

#### API-03 — Métricas

Resposta 200 em `text/plain; version=0.0.4` (formato Prometheus), com ao menos `http_requests_total{metodo,rota,status}` e `http_request_duration_seconds{metodo,rota}`; `rota` é o modelo da rota (ex.: `/api/v1/negociacoes/{negociacaoId}/lances`), nunca o caminho com IDs.

#### API-04 — Listar negociações

Query opcional: `page`, `size`, `modo` (`LEILAO_DIRETO` ou `VENDA_DIRETA`), `status` (um dos sete valores do §6.1). Resposta 200:

```json
{
  "items": [
    {
      "negociacaoId": "31000000-0000-4000-8000-000000000002",
      "modo": "VENDA_DIRETA",
      "status": "ABERTO",
      "produtoId": "35000000-0000-4000-8000-000000000001",
      "dataInicio": null,
      "dataFim": null,
      "expiraEm": "2026-11-22T12:00:00.000Z",
      "criadoEm": "2026-11-20T12:00:00.000Z"
    },
    {
      "negociacaoId": "31000000-0000-4000-8000-000000000001",
      "modo": "LEILAO_DIRETO",
      "status": "ABERTO",
      "produtoId": "35000000-0000-4000-8000-000000000001",
      "dataInicio": "2026-11-20T12:00:00.000Z",
      "dataFim": "2026-11-20T15:00:00.000Z",
      "expiraEm": null,
      "criadoEm": "2026-11-20T12:00:00.000Z"
    }
  ],
  "page": 1,
  "size": 10,
  "total": 2
}
```

#### API-05 — Detalhar negociação

Resposta 200 com o detalhe de leilão ou de negociação direta, conforme o `modo`.

#### API-06 — Listar lances

Resposta 200 (ordem de `sequencia` crescente), vista por COMP-X:

```json
{
  "items": [
    {
      "lanceId": "33000000-0000-4000-8000-000000000001",
      "negociacaoId": "31000000-0000-4000-8000-000000000001",
      "compradorId": "b2000000-0000-4000-8000-000000000001",
      "valor": 2550.00,
      "sequencia": 1,
      "criadoEm": "2026-11-20T13:05:00.000Z"
    },
    {
      "lanceId": "33000000-0000-4000-8000-000000000002",
      "negociacaoId": "31000000-0000-4000-8000-000000000001",
      "compradorId": "OCULTO",
      "valor": 2600.00,
      "sequencia": 2,
      "criadoEm": "2026-11-20T13:10:00.000Z"
    }
  ],
  "page": 1,
  "size": 10,
  "total": 2
}
```

#### API-07 — Dar lance

`valor` é obrigatório; `demandaId` é opcional: texto de 1 a 64 caracteres de `[A-Za-z0-9._-]`, ou `null` (equivale a ausente). O `demandaId` não é conferido contra nenhuma demanda (RN-07).

```json
{ "valor": 2600.00, "demandaId": "36000000-0000-4000-8000-000000000002" }
```

Resposta 201 (o `demandaId` não aparece):

```json
{
  "lanceId": "33000000-0000-4000-8000-000000000002",
  "negociacaoId": "31000000-0000-4000-8000-000000000001",
  "compradorId": "b2000000-0000-4000-8000-000000000002",
  "valor": 2600.00,
  "sequencia": 2,
  "criadoEm": "2026-11-20T13:10:00.000Z"
}
```

Publica `lance_realizado`.

#### API-08 — Listar propostas

Resposta 200 (ordem de `sequencia` crescente):

```json
{
  "items": [
    {
      "propostaId": "36000000-0000-4000-8000-000000000101",
      "negociacaoId": "31000000-0000-4000-8000-000000000002",
      "autor": "SUPPLIER",
      "precoUnitario": 130.00,
      "sequencia": 1,
      "criadoEm": "2026-11-20T12:00:00.000Z"
    },
    {
      "propostaId": "36000000-0000-4000-8000-000000000102",
      "negociacaoId": "31000000-0000-4000-8000-000000000002",
      "autor": "BUYER",
      "precoUnitario": 125.00,
      "sequencia": 2,
      "criadoEm": "2026-11-21T12:00:00.000Z"
    }
  ],
  "page": 1,
  "size": 10,
  "total": 2
}
```

#### API-09 — Contraproposta

```json
{ "precoUnitario": 125.00 }
```

Resposta 201: a proposta criada (segundo item do exemplo da API-08). Não publica evento.

#### API-10 — Aceitar e API-11 — Recusar

Sem corpo, ou corpo `{}`; qualquer outro corpo → `422 VALIDACAO_INVALIDA`. Resposta 200 com o detalhe de negociação direta já em `ENCERRADO` (API-10) ou `RECUSADO` (API-11), `encerradoEm` preenchido e `acoesPermitidas` vazio. Publicam `negociacao_fechada`.

### 7.3 Eventos consumidos

Envelope (kit, `CONTRACTS.md`): `eventId`, `eventType` (igual ao tópico), `eventVersion` (`"1.0"`), `timestamp`, `source`, `correlationId`, `aggregateId` (igual à chave Kafka) e `payload` (objeto). Campos adicionais no `payload` são aceitos e ignorados. Os schemas estão em `docs/negociacao/contratos/<topico>.schema.json` e listam só os campos que este serviço usa.

| Tópico | Produtor | Chave | Ação | Em caso de mensagem inválida |
|---|---|---|---|---|
| `leilao_iniciado` | `mercado-service` | `negociacaoId` | RF-01 | `negociacao.dlt` (RN-20) |
| `modo_negociacao_definido` | `mercado-service` | `negociacaoId` | RF-02 | `negociacao.dlt` (RN-20) |
| `pedido_atualizado` | `pedidos-service` | `pedidoId` | RF-03, RF-04 | `negociacao.dlt` (RN-20) |

Validação comum (falha → inválida): mensagem não é objeto; falta algum dos oito campos do envelope; `eventId` não é string não vazia; `eventType` diferente do tópico; `eventVersion` diferente de `"1.0"`; `source` diferente do produtor da tabela; `correlationId` fora de 1–128 caracteres de `[A-Za-z0-9._-]`; `timestamp` não é instante com fuso; `payload` não é objeto; campo obrigatório do payload ausente, com tipo errado ou fora do domínio do schema; string obrigatória vazia ou só com espaços; valor monetário ou quantidade com mais de 2 casas; participação com mais de 4 casas. `produtoId`, `fornecimentoId`, `fornecedorId` e `compradorId` são texto de 1 a 64 caracteres (o Mercado aceita até 128; INT-NEG-12).

`leilao_iniciado`. Obrigatórios: `negociacaoId` (UUID), `produtoId`, `quantidadeTotal` (> 0), `precoInicial` (> 0), `incrementoMinimo` (> 0), `itens` (lista com ao menos 1 item; cada item com `fornecimentoId`, `fornecedorId`, `quantidade` (> 0), `precoUnitario` (> 0), `valorItem` (> 0) e `participacao` (> 0 e ≤ 1)), `iniciaEm` e `encerraEm` (instantes, com `encerraEm > iniciaEm`). Também é inválido um `fornecimentoId` repetido em `itens`. `sugestaoId`, `unidade` e `origem` são aceitos e ignorados.

```json
{
  "eventId": "38000000-0000-4000-8000-000000000001",
  "eventType": "leilao_iniciado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:00:00.000Z",
  "source": "mercado-service",
  "correlationId": "teste-neg-01",
  "aggregateId": "31000000-0000-4000-8000-000000000001",
  "payload": {
    "negociacaoId": "31000000-0000-4000-8000-000000000001",
    "sugestaoId": "39000000-0000-4000-8000-000000000001",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "unidade": "TON",
    "quantidadeTotal": 20.00,
    "precoInicial": 2550.00,
    "incrementoMinimo": 25.50,
    "itens": [
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000001",
        "fornecedorId": "a1000000-0000-4000-8000-000000000001",
        "quantidade": 5.00,
        "precoUnitario": 120.00,
        "valorItem": 600.00,
        "participacao": 0.2353,
        "origem": {"cidade": "Rio Verde", "uf": "GO"}
      },
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000002",
        "fornecedorId": "a1000000-0000-4000-8000-000000000002",
        "quantidade": 15.00,
        "precoUnitario": 130.00,
        "valorItem": 1950.00,
        "participacao": 0.7647,
        "origem": {"cidade": "Jataí", "uf": "GO"}
      }
    ],
    "iniciaEm": "2026-11-20T12:00:00.000Z",
    "encerraEm": "2026-11-20T15:00:00.000Z"
  }
}
```

`modo_negociacao_definido`. Obrigatórios: `negociacaoId` (UUID) e `modo` (`VENDA_DIRETA` ou `LEILAO_DIRETO`). Com `modo = "VENDA_DIRETA"`, também: `produtoId`, `demandas` (exatamente 1 item, com `compradorId`) e `ofertas` (exatamente 1 item, com `fornecimentoId`, `fornecedorId`, `quantidade` (> 0) e `precoUnitario` (> 0)). Com `modo = "LEILAO_DIRETO"`, nenhum outro campo é validado e a mensagem é ignorada (RN-02). Os demais campos (`unidade`, `quantidadeTotal`, `valorReferencia`, `demandaId`, `destino`, `valorItem`, `participacao`, `definidaEm` e outros) são aceitos e ignorados.

```json
{
  "eventId": "38000000-0000-4000-8000-000000000002",
  "eventType": "modo_negociacao_definido",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:00:00.000Z",
  "source": "mercado-service",
  "correlationId": "teste-neg-02",
  "aggregateId": "31000000-0000-4000-8000-000000000002",
  "payload": {
    "negociacaoId": "31000000-0000-4000-8000-000000000002",
    "modo": "VENDA_DIRETA",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "unidade": "TON",
    "quantidadeTotal": 10.00,
    "valorReferencia": 1300.00,
    "demandas": [
      {
        "demandaId": "36000000-0000-4000-8000-000000000001",
        "demandaOrigemId": null,
        "compradorId": "b2000000-0000-4000-8000-000000000001",
        "quantidade": 10.00,
        "precoMaximo": 135.00,
        "destino": {"cidade": "Anápolis", "uf": "GO"},
        "prazoDesejado": null
      }
    ],
    "ofertas": [
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000002",
        "fornecedorId": "a1000000-0000-4000-8000-000000000002",
        "quantidade": 10.00,
        "precoUnitario": 130.00,
        "valorItem": 1300.00,
        "participacao": 1.0000,
        "origem": {"cidade": "Jataí", "uf": "GO"}
      }
    ],
    "definidaEm": "2026-11-20T12:00:00.000Z"
  }
}
```

`pedido_atualizado` (ESP-PED-001 §7.4). Obrigatórios: `pedidoId`, `negociacaoId` (texto de 1 a 64 caracteres), `fornecimentoId`, `status` e `reservaSituacao` (`PENDENTE`, `CONFIRMADA`, `RECUSADA` ou `ENCERRADA`). Os demais campos são aceitos e ignorados.

```json
{
  "eventId": "37000000-0000-4000-8000-000000000006",
  "eventType": "pedido_atualizado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:01:00.000Z",
  "source": "pedidos-service",
  "correlationId": "teste-neg-01",
  "aggregateId": "32000000-0000-4000-8000-000000000001",
  "payload": {
    "pedidoId": "32000000-0000-4000-8000-000000000001",
    "negociacaoId": "31000000-0000-4000-8000-000000000001",
    "fornecimentoId": "34000000-0000-4000-8000-000000000001",
    "status": "CRIADO",
    "statusAnterior": "CRIADO",
    "reservaSituacao": "CONFIRMADA",
    "versao": 2
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
| source | `"negociacao-service"` |
| correlationId | da requisição HTTP; da mensagem consumida que causou o evento; ou `correlationId` da negociação, nos eventos de `executar_prazos` |
| aggregateId | `negociacaoId` (na DLT, ver abaixo) |
| payload | objeto específico do tópico |

A chave Kafka (`key` de `publish`) é sempre igual ao `aggregateId`.

| Tópico | Quando | Chave de partição | Payload | Consumidor |
|---|---|---|---|---|
| `pedido_temporario_solicitado` | Negociação aberta: um por oferta do leilão, na ordem de `ordem` (RF-01), e um na negociação direta (RF-02) | `negociacaoId` | `negociacaoId`, `tipo` (`"LEILAO"` ou `"VENDA_DIRETA"`), `fornecimentoId`, `fornecedorId`, `produtoId`, `quantidade`, `precoUnitario` (preço da oferta no evento do Mercado) | Pedidos |
| `lance_realizado` | Lance aceito (RF-05) | `negociacaoId` | `negociacaoId`, `lanceId`, `compradorId` (sem máscara), `valor`, `sequencia`, `criadoEm` | front-end, auditoria |
| `negociacao_fechada` | Negociação chega a estado final (RF-03, RF-04, RF-06, RF-08, RF-09, RF-10) | `negociacaoId` | `negociacaoId`, `tipo`, `status`, `compradorId` (texto só em `ENCERRADO`; `null` nos demais), `demandaId` (leilão `ENCERRADO`: o do lance vencedor, ou `null`; `null` nos demais), `produtoId`, `alocacoes[]` {`fornecimentoId`, `fornecedorId`, `pedidoId`, `quantidade`, `valorUnitario`, `valorTotal`} (vazia fora de `ENCERRADO`) | Pedidos, Mercado |
| `negociacao.dlt` | Mensagem consumida inválida ou com falha após 4 execuções (RF-15) | `aggregateId` original se for string de 1 a 128 caracteres; senão `eventId` original convertido com `str` se presente e não vazio; senão `"desconhecido"` | `topicoOrigem`, `originalEvent` (mensagem recebida, intacta), `error` (`"EVENTO_INVALIDO"` ou `"PROCESSAMENTO_FALHOU"`), `detalhe` (motivo, 1–500 caracteres), `stackTrace` (`""`), `failedAtAttempt` (1 a 4) | operação, QA |

Exemplos (iguais a `docs/negociacao/contratos/<topico>.exemplo.json`):

`pedido_temporario_solicitado`:

```json
{
  "eventId": "37000000-0000-4000-8000-000000000003",
  "eventType": "pedido_temporario_solicitado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:00:00.000Z",
  "source": "negociacao-service",
  "correlationId": "teste-neg-01",
  "aggregateId": "31000000-0000-4000-8000-000000000001",
  "payload": {
    "negociacaoId": "31000000-0000-4000-8000-000000000001",
    "tipo": "LEILAO",
    "fornecimentoId": "34000000-0000-4000-8000-000000000001",
    "fornecedorId": "a1000000-0000-4000-8000-000000000001",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "quantidade": 5.00,
    "precoUnitario": 120.00
  }
}
```

`lance_realizado`:

```json
{
  "eventId": "37000000-0000-4000-8000-000000000002",
  "eventType": "lance_realizado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T13:10:00.000Z",
  "source": "negociacao-service",
  "correlationId": "teste-neg-05",
  "aggregateId": "31000000-0000-4000-8000-000000000001",
  "payload": {
    "negociacaoId": "31000000-0000-4000-8000-000000000001",
    "lanceId": "33000000-0000-4000-8000-000000000002",
    "compradorId": "b2000000-0000-4000-8000-000000000002",
    "valor": 2600.00,
    "sequencia": 2,
    "criadoEm": "2026-11-20T13:10:00.000Z"
  }
}
```

`negociacao_fechada` (leilão conjunto encerrado; rateio do lance de 2600.00 pelas participações 0.2353 e 0.7647):

```json
{
  "eventId": "37000000-0000-4000-8000-000000000004",
  "eventType": "negociacao_fechada",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T15:00:00.000Z",
  "source": "negociacao-service",
  "correlationId": "teste-neg-01",
  "aggregateId": "31000000-0000-4000-8000-000000000001",
  "payload": {
    "negociacaoId": "31000000-0000-4000-8000-000000000001",
    "tipo": "LEILAO",
    "status": "ENCERRADO",
    "compradorId": "b2000000-0000-4000-8000-000000000002",
    "demandaId": "36000000-0000-4000-8000-000000000002",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "alocacoes": [
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000001",
        "fornecedorId": "a1000000-0000-4000-8000-000000000001",
        "pedidoId": "32000000-0000-4000-8000-000000000001",
        "quantidade": 5.00,
        "valorUnitario": 122.36,
        "valorTotal": 611.78
      },
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000002",
        "fornecedorId": "a1000000-0000-4000-8000-000000000002",
        "pedidoId": "32000000-0000-4000-8000-000000000002",
        "quantidade": 15.00,
        "valorUnitario": 132.55,
        "valorTotal": 1988.22
      }
    ]
  }
}
```

`negociacao_fechada` deserto (payload): `{"negociacaoId": "31000000-0000-4000-8000-000000000001", "tipo": "LEILAO", "status": "DESERTO", "compradorId": null, "demandaId": null, "produtoId": "35000000-0000-4000-8000-000000000001", "alocacoes": []}`.

`negociacao.dlt`:

```json
{
  "eventId": "37000000-0000-4000-8000-000000000005",
  "eventType": "negociacao.dlt",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:05:00.000Z",
  "source": "negociacao-service",
  "correlationId": "teste-neg-dlt",
  "aggregateId": "32000000-0000-4000-8000-000000000001",
  "payload": {
    "topicoOrigem": "pedido_atualizado",
    "originalEvent": {
      "eventId": "38000000-0000-4000-8000-000000000009",
      "eventType": "pedido_atualizado",
      "eventVersion": "1.0",
      "timestamp": "2026-11-20T12:04:59.000Z",
      "source": "pedidos-service",
      "correlationId": "teste-neg-dlt",
      "aggregateId": "32000000-0000-4000-8000-000000000001"
    },
    "error": "EVENTO_INVALIDO",
    "detalhe": "payload ausente",
    "stackTrace": "",
    "failedAtAttempt": 1
  }
}
```

## 8. Requisitos funcionais

### 8.0 Massa de teste comum aos critérios

Os critérios usam esta massa. `token(ESCOPO, EMPRESA)` é um JWT RS256 assinado com a chave de teste registrada em `chaves_publicas` (`kid` `"teste-1"`), com `iss` `"identity-service"`, `aud` `"b2b-platform"`, `view_mode` `"SINGLE"`, `tenant_id` = EMPRESA, `active_scope` = ESCOPO, `sub` `"c3000000-0000-4000-8000-000000000001"` e `exp` = agora + 900 s. "Relógio em H" significa `clock.now()` = `2026-11-20T` H `.000Z`. Toda resposta de erro tem o corpo do §10 com os valores da linha do código citado.

| Nome | Valor |
|---|---|
| FORN-A, FORN-B, FORN-C (empresas fornecedoras) | `a1000000-0000-4000-8000-000000000001`, `…000000000002`, `…000000000003` |
| COMP-X, COMP-Y (empresas compradoras) | `b2000000-0000-4000-8000-000000000001`, `…000000000002` |
| Produto P | `35000000-0000-4000-8000-000000000001` |
| OF-A, OF-B (ofertas) | `34000000-0000-4000-8000-000000000001` (de FORN-A) e `34000000-0000-4000-8000-000000000002` (de FORN-B) |
| OF-D, OF-E (ofertas) | `34000000-0000-4000-8000-000000000006` e `34000000-0000-4000-8000-000000000005`, ambas de FORN-C |
| PT-A, PT-B, PT-D | pedidos temporários `32000000-0000-4000-8000-000000000001`, `…000000000002`, `…000000000003` |
| DEM-X, DEM-Y (demandas no Mercado) | `36000000-0000-4000-8000-000000000001` (de COMP-X) e `36000000-0000-4000-8000-000000000002` (de COMP-Y) |
| Leilão L1 | `31000000-0000-4000-8000-000000000001`, aberto às `12:00:00` pelo `leilao_iniciado` do §7.3 (igual a `docs/negociacao/contratos/leilao_iniciado.exemplo.json`): OF-A (5.00 × 120.00 = 600.00, participação 0.2353) e OF-B (15.00 × 130.00 = 1950.00, participação 0.7647), `precoInicial` 2550.00, `incrementoMinimo` 25.50, `dataInicio` 12:00, `dataFim` 15:00, `correlationId` `"teste-neg-01"` |
| "L1 ABERTO" | L1 com as reservas de OF-A (PT-A) e de OF-B (PT-B) confirmadas às `12:01:00`, sem lances |
| Direta D1 | `31000000-0000-4000-8000-000000000002`, aberta às `12:00:00` pelo `modo_negociacao_definido` do §7.3 (igual a `docs/negociacao/contratos/modo_negociacao_definido.exemplo.json`): COMP-X, OF-B de FORN-B, `quantidade` 10.00, preço da oferta 130.00, `correlationId` `"teste-neg-02"` |

Quando um CA compara uma resposta com um exemplo do §7, os identificadores gerados pelo serviço (`lanceId`, `propostaId`, `eventId`) são comparados com os valores realmente devolvidos, e não com os UUIDs ilustrativos do exemplo; `negociacaoId` vem do evento do Mercado. "Confirmar a reserva de X com PT" significa entregar ao handler um `pedido_atualizado` com `negociacaoId` da negociação, `fornecimentoId` X, `pedidoId` PT, `status` `"CRIADO"` e `reservaSituacao` `"CONFIRMADA"` (eventId novo). "Recusar a reserva" é igual, com `status` `"CANCELADO"` e `reservaSituacao` `"RECUSADA"`. "Abrir o leilão Ln com itens …" significa entregar ao handler um `leilao_iniciado` igual ao de L1, com eventId novo, `negociacaoId` `31000000-0000-4000-8000-00000000000n` e os itens, `precoInicial` e `incrementoMinimo` indicados.

### RF-01 — Abrir o leilão

- **Prioridade:** Must
- **Enunciado (EARS):** Quando um `leilao_iniciado` válido chegar com um `negociacaoId` desconhecido, o serviço deve abrir o leilão em `AGUARDANDO_RESERVAS` com as ofertas do evento e publicar um `pedido_temporario_solicitado` por oferta.
- **Regras aplicáveis:** RN-01, RN-03, RN-19, RN-20
- **Interface:** tópicos `leilao_iniciado`, `pedido_temporario_solicitado`; API-05

```gherkin
# CA-01
Cenário: Leilão aberto pelo Mercado
  Dado o relógio em 12:00:00
  Quando o handler recebe o leilao_iniciado de docs/negociacao/contratos/leilao_iniciado.exemplo.json
  Então GET /api/v1/negociacoes/L1 por token(SUPPLIER, FORN-C) responde 200 com status "AGUARDANDO_RESERVAS", produtoId P, dataInicio "2026-11-20T12:00:00.000Z", dataFim "2026-11-20T15:00:00.000Z", quantidadeTotal 20.00, precoInicial 2550.00, incrementoMinimo 25.50, melhorLance null, totalLances 0, compradorId null, criadoEm "2026-11-20T12:00:00.000Z" e acoesPermitidas []
  E ofertas tem, nesta ordem, OF-A (fornecedorId FORN-A, quantidade 5.00, precoUnitario 120.00, valorItem 600.00, participacao 0.2353, situacao "PENDENTE") e OF-B (FORN-B, 15.00, 130.00, 1950.00, 0.7647, "PENDENTE")
  E foram publicadas exatamente 2 mensagens em "pedido_temporario_solicitado", nesta ordem, ambas com chave L1 e correlationId "teste-neg-01":
    | payload                                                                                                                       |
    | negociacaoId L1, tipo "LEILAO", fornecimentoId OF-A, fornecedorId FORN-A, produtoId P, quantidade 5.00, precoUnitario 120.00  |
    | negociacaoId L1, tipo "LEILAO", fornecimentoId OF-B, fornecedorId FORN-B, produtoId P, quantidade 15.00, precoUnitario 130.00 |

# CA-02
Cenário: Evento repetido, negociação já conhecida e modo de leilão
  Dado L1 aberto como no CA-01
  Quando o handler recebe o mesmo leilao_iniciado com o mesmo eventId, e depois com outro eventId e quantidadeTotal 30.00
  Então L1 continua com quantidadeTotal 20.00, nenhuma mensagem nova é publicada e nada vai para "negociacao.dlt"
  Quando o handler recebe um modo_negociacao_definido válido com modo "LEILAO_DIRETO" e negociacaoId "31000000-0000-4000-8000-000000000003"
  Então GET /api/v1/negociacoes/31000000-0000-4000-8000-000000000003 responde 404, nenhuma mensagem é publicada e nada vai para "negociacao.dlt"

# CA-03
Cenário: leilao_iniciado inválido
  Quando o handler recebe o leilao_iniciado do CA-01 com negociacaoId "31000000-0000-4000-8000-000000000004", eventId novo e qualquer uma destas alterações: source "negociacao-service"; itens []; encerraEm igual a iniciaEm; precoInicial 2550.001; participacao 1.5 no primeiro item; fornecimentoId de OF-A nos dois itens; sem incrementoMinimo
  Então cada mensagem gera 1 publicação em "negociacao.dlt" com error "EVENTO_INVALIDO" e failedAtAttempt 1
  E GET /api/v1/negociacoes/31000000-0000-4000-8000-000000000004 responde 404 e nada é publicado em "pedido_temporario_solicitado"
```

### RF-02 — Abrir a negociação direta

- **Prioridade:** Must
- **Enunciado (EARS):** Quando um `modo_negociacao_definido` válido com `modo` `"VENDA_DIRETA"` chegar com um `negociacaoId` desconhecido, o serviço deve abrir a negociação direta com a proposta do fornecedor pelo preço da oferta, `vez` `"BUYER"` e prazo de 48 h, e publicar `pedido_temporario_solicitado`.
- **Regras aplicáveis:** RN-02, RN-03, RN-19, RN-20
- **Interface:** tópicos `modo_negociacao_definido`, `pedido_temporario_solicitado`; API-05, API-08

```gherkin
# CA-04
Cenário: Negociação direta aberta pelo Mercado
  Dado o relógio em 12:00:00
  Quando o handler recebe o modo_negociacao_definido de docs/negociacao/contratos/modo_negociacao_definido.exemplo.json
  Então GET /api/v1/negociacoes/D1 por token(BUYER, COMP-X) responde 200 igual ao exemplo "Detalhe de negociação direta" do §7.2
  E foi publicada exatamente 1 mensagem, em "pedido_temporario_solicitado", com chave D1, correlationId "teste-neg-02" e payload {negociacaoId D1, tipo "VENDA_DIRETA", fornecimentoId OF-B, fornecedorId FORN-B, produtoId P, quantidade 10.00, precoUnitario 130.00}
  E GET /api/v1/negociacoes/D1/propostas tem 1 item com autor "SUPPLIER", precoUnitario 130.00, sequencia 1 e criadoEm "2026-11-20T12:00:00.000Z"
  E GET /api/v1/negociacoes/D1 por token(SUPPLIER, FORN-B) mostra acoesPermitidas ["RECUSAR"]

# CA-05
Cenário: Negociação direta inválida ou repetida
  Quando o handler recebe o modo_negociacao_definido do CA-04 com negociacaoId "31000000-0000-4000-8000-000000000005", eventId novo e qualquer uma destas alterações: demandas []; duas ofertas; quantidade da oferta "10.00" (string); sem produtoId
  Então cada mensagem gera 1 publicação em "negociacao.dlt" com error "EVENTO_INVALIDO" e nenhuma negociação é criada
  Dado D1 aberta como no CA-04
  Quando o handler recebe o mesmo modo_negociacao_definido com outro eventId
  Então D1 continua com totalPropostas 1 e nenhuma mensagem nova é publicada
```

### RF-03 — Registrar o resultado da reserva

- **Prioridade:** Must
- **Enunciado (EARS):** Quando um `pedido_atualizado` válido com `reservaSituacao` `"CONFIRMADA"` ou `"RECUSADA"` chegar para uma oferta com reserva `PENDENTE`, o serviço deve registrar a nova situação e o `pedidoId`; numa negociação direta, a recusa encerra a negociação como `RECUSADO` e publica `negociacao_fechada`.
- **Regras aplicáveis:** RN-04, RN-14, RN-19
- **Interface:** tópicos `pedido_atualizado`, `negociacao_fechada`

```gherkin
# CA-06
Cenário: Reserva confirmada no leilão com outra ainda pendente
  Dado L1 com OF-A e OF-B PENDENTE
  Quando o handler recebe a confirmação da reserva de OF-A em L1 com PT-A
  Então GET /api/v1/negociacoes/L1 mostra status "AGUARDANDO_RESERVAS", OF-A com situacao "CONFIRMADA" e OF-B com situacao "PENDENTE"
  E nenhuma mensagem é publicada

# CA-07
Cenário: pedido_atualizado que não muda nada
  Dado L1 com OF-A CONFIRMADA e OF-B PENDENTE
  Quando o handler recebe, cada um com eventId novo: reservaSituacao "PENDENTE" para OF-B; reservaSituacao "ENCERRADA" para OF-B; negociacaoId "31000000-0000-4000-8000-000000000099"; fornecimentoId OF-D em L1; e a recusa de OF-A em L1
  Então OF-A continua "CONFIRMADA", OF-B continua "PENDENTE", L1 continua "AGUARDANDO_RESERVAS", nenhuma mensagem é publicada e nada vai para "negociacao.dlt"

# CA-08
Cenário: Reserva recusada na negociação direta
  Dado D1 com reserva PENDENTE e o relógio em 12:05:00
  Quando o handler recebe a recusa da reserva de OF-B em D1, com correlationId "teste-neg-08"
  Então GET /api/v1/negociacoes/D1 por token(BUYER, COMP-X) mostra status "RECUSADO", reservaSituacao "RECUSADA" e encerradoEm "2026-11-20T12:05:00.000Z"
  E foi publicada exatamente 1 mensagem em "negociacao_fechada" com chave D1, correlationId "teste-neg-08" e payload {negociacaoId D1, tipo "VENDA_DIRETA", status "RECUSADO", compradorId null, demandaId null, produtoId P, alocacoes []}
```

### RF-04 — Liberar os lances

- **Prioridade:** Must
- **Enunciado (EARS):** Quando a última reserva `PENDENTE` de um leilão em `AGUARDANDO_RESERVAS` for resolvida, o serviço deve passar o leilão a `ABERTO` com as ofertas confirmadas, recalculando preço inicial, incremento mínimo e participações se alguma oferta foi recusada; sem oferta confirmada, ou com reserva ainda pendente em `dataFim`, o serviço deve encerrá-lo como `DESERTO` e publicar `negociacao_fechada`.
- **Regras aplicáveis:** RN-04, RN-05, RN-10, RN-21
- **Interface:** tópico `pedido_atualizado`; `app.main.executar_prazos`; tópico `negociacao_fechada`

```gherkin
# CA-09
Cenário: Todas as reservas confirmadas
  Dado L1 com OF-A CONFIRMADA e OF-B PENDENTE, e o relógio em 12:01:00
  Quando o handler recebe a confirmação da reserva de OF-B em L1 com PT-B
  Então GET /api/v1/negociacoes/L1 por token(BUYER, COMP-X) mostra status "ABERTO", quantidadeTotal 20.00, precoInicial 2550.00, incrementoMinimo 25.50, participacao 0.2353 em OF-A e 0.7647 em OF-B, e acoesPermitidas ["DAR_LANCE"]
  E nenhuma mensagem é publicada

# CA-10
Cenário: Oferta recusada sai do lote e o lote é recalculado
  Dado L1 com OF-A CONFIRMADA e OF-B PENDENTE
  Quando o handler recebe a recusa da reserva de OF-B em L1
  Então L1 fica "ABERTO" com quantidadeTotal 5.00, precoInicial 600.00 e incrementoMinimo 6.00
  E OF-A tem participacao 1.0000 e OF-B continua em ofertas com situacao "RECUSADA"
  E nenhuma mensagem é publicada
  Dado o leilão L2 aberto com itens OF-A (2.00 × 100.00 = 200.00, 0.4000), OF-B (1.00 × 100.00 = 100.00, 0.2000), OF-D (1.00 × 100.00 = 100.00, 0.2000) e OF-E (1.00 × 100.00 = 100.00, 0.2000), precoInicial 500.00 e incrementoMinimo 5.00
  Quando as reservas de OF-B, OF-D e OF-E são confirmadas e a de OF-A é recusada
  Então L2 fica "ABERTO" com quantidadeTotal 3.00, precoInicial 300.00, incrementoMinimo 3.00 e participacao 0.3334 em OF-B, 0.3333 em OF-D e 0.3333 em OF-E

# CA-11
Cenário: Nenhuma reserva confirmada
  Dado L1 com OF-A RECUSADA e OF-B PENDENTE, e o relógio em 12:02:00
  Quando o handler recebe a recusa da reserva de OF-B em L1
  Então L1 fica "DESERTO" com compradorId null e encerradoEm "2026-11-20T12:02:00.000Z"
  E foi publicada exatamente 1 mensagem em "negociacao_fechada" com chave L1 e payload {negociacaoId L1, tipo "LEILAO", status "DESERTO", compradorId null, demandaId null, produtoId P, alocacoes []}
  Quando, às 12:30:00, token(BUYER, COMP-X) envia POST /api/v1/negociacoes/L1/lances com {"valor": 3000.00}
  Então a resposta é 409 com codigo "LEILAO_NAO_ABERTO"
  Quando, às 15:00:00, executar_prazos é chamado
  Então devolve 0 e nenhuma nova mensagem é publicada

# CA-12
Cenário: Reserva ainda pendente em dataFim
  Dado L1 com OF-A CONFIRMADA e OF-B PENDENTE, e o relógio em 14:59:59.999
  Quando executar_prazos é chamado
  Então devolve 0 e L1 continua "AGUARDANDO_RESERVAS"
  Dado o relógio em 15:00:00
  Quando executar_prazos é chamado
  Então devolve 1, L1 fica "DESERTO" com encerradoEm "2026-11-20T15:00:00.000Z"
  E foi publicada exatamente 1 mensagem em "negociacao_fechada" com chave L1, correlationId "teste-neg-01" e status "DESERTO"
  Quando o handler recebe depois a confirmação da reserva de OF-B em L1 com PT-B
  Então L1 continua "DESERTO", OF-B continua "PENDENTE" e nenhuma mensagem é publicada
```

### RF-05 — Dar lance

- **Prioridade:** Must
- **Enunciado (EARS):** Enquanto o leilão estiver aceitando lances, quando um token `BUYER` de empresa que não é fornecedora do leilão enviar `POST /api/v1/negociacoes/{id}/lances` com valor válido, o serviço deve registrar o lance, com o `demandaId` quando informado, responder 201 e publicar `lance_realizado`.
- **Regras aplicáveis:** RN-06, RN-07, RN-08, RN-15, RN-16, RN-22
- **Interface:** API-07; tópico `lance_realizado`

```gherkin
# CA-13
Cenário: Primeiro lance igual ao preço inicial, com demanda
  Dado L1 ABERTO e o relógio em 13:05:00
  Quando token(BUYER, COMP-X) envia {"valor": 2550.00, "demandaId": DEM-X} com X-Correlation-Id "teste-neg-13"
  Então a resposta é 201 com negociacaoId L1, compradorId COMP-X, valor 2550.00, sequencia 1, criadoEm "2026-11-20T13:05:00.000Z" e sem o campo demandaId
  E foi publicada exatamente 1 mensagem em "lance_realizado" com chave L1, correlationId "teste-neg-13" e payload {negociacaoId L1, lanceId igual ao da resposta, compradorId COMP-X, valor 2550.00, sequencia 1, criadoEm "2026-11-20T13:05:00.000Z"}

# CA-14
Cenário: Valor mínimo e incremento
  Dado L1 ABERTO, com precoInicial 2550.00, incrementoMinimo 25.50 e sem lances
  Quando token(BUYER, COMP-X) envia {"valor": 2549.99}
  Então a resposta é 422 com codigo "LANCE_INSUFICIENTE"
  Quando token(BUYER, COMP-X) envia {"valor": 2550.00}
  Então a resposta é 201
  Quando token(BUYER, COMP-Y) envia {"valor": 2575.49}
  Então a resposta é 422 com codigo "LANCE_INSUFICIENTE"
  Quando token(BUYER, COMP-Y) envia {"valor": 2575.50}
  Então a resposta é 201 com sequencia 2

# CA-15
Cenário: Lance fora da janela
  Dado L1 AGUARDANDO_RESERVAS e o relógio em 12:00:30
  Quando token(BUYER, COMP-X) envia {"valor": 3000.00}
  Então a resposta é 409 com codigo "LEILAO_NAO_ABERTO"
  Dado L1 ABERTO e o relógio em 15:00:00, sem executar_prazos
  Quando token(BUYER, COMP-X) envia {"valor": 3000.00}
  Então a resposta é 409 com codigo "LEILAO_NAO_ABERTO"
  Dado L1 ENCERRADO
  Quando token(BUYER, COMP-X) envia {"valor": 3000.00}
  Então a resposta é 409 com codigo "LEILAO_NAO_ABERTO"

# CA-16
Cenário: Conflito de interesse no leilão
  Dado L1 ABERTO com OF-A (FORN-A) CONFIRMADA e OF-B (FORN-B) RECUSADA
  Quando token(BUYER, FORN-A) envia {"valor": 3000.00}
  Então a resposta é 403 com codigo "CONFLITO_DE_INTERESSE"
  Quando token(BUYER, FORN-B) envia {"valor": 3000.00}
  Então a resposta é 403 com codigo "CONFLITO_DE_INTERESSE"

# CA-17
Cenário: Escopo, corpo e modo no lance
  Dado L1 ABERTO e D1 ABERTO
  Quando token(SUPPLIER, FORN-C) envia {"valor": 3000.00} para L1
  Então a resposta é 403 com codigo "ACESSO_NEGADO"
  Quando token(BUYER, COMP-X) envia para L1 qualquer um destes corpos: {"valor": "3000.00"}; {"valor": 3000.001}; {"valor": 0}; {"valor": -1.00}; {"valor": true}; {}; {"valor": 3000.00, "comprador": "x"}; {"valor": 3000.00, "demandaId": ""}; {"valor": 3000.00, "demandaId": "a b"}; {"valor": 3000.00, "demandaId": 1}
  Então cada resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando token(BUYER, COMP-X) envia {"valor": 3000.00} para D1
  Então a resposta é 409 com codigo "MODO_INCOMPATIVEL"

# CA-18
Cenário: Precedência de erros no lance
  Dado L1 ENCERRADO, com OF-A de FORN-A
  Quando token(BUYER, FORN-A) envia {"valor": "abc"}
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando token(BUYER, FORN-A) envia {"valor": 1.00}
  Então a resposta é 403 com codigo "CONFLITO_DE_INTERESSE"
  Dado L1 ABERTO e o relógio em 15:00:00
  Quando token(BUYER, COMP-X) envia {"valor": 1.00}
  Então a resposta é 409 com codigo "LEILAO_NAO_ABERTO"

# CA-19
Cenário: O autor do melhor lance pode cobrir o próprio lance
  Dado L1 ABERTO cujo melhor lance é 2550.00 de COMP-X
  Quando token(BUYER, COMP-X) envia {"valor": 2575.50}
  Então a resposta é 201 com sequencia 2
```

### RF-06 — Encerrar o leilão em dataFim e ratear

- **Prioridade:** Must
- **Enunciado (EARS):** Quando `executar_prazos` rodar com `agora >= dataFim` num leilão `ABERTO`, o serviço deve encerrá-lo como `ENCERRADO` (com lance) ou `DESERTO` (sem lance) e publicar uma única `negociacao_fechada`, com as alocações do rateio e o `demandaId` do lance vencedor no caso `ENCERRADO`.
- **Regras aplicáveis:** RN-10, RN-11, RN-21
- **Interface:** `app.main.executar_prazos`; tópico `negociacao_fechada`

```gherkin
# CA-20
Cenário: Leilão conjunto encerrado com vencedor
  Dado L1 ABERTO com o lote OF-A (PT-A) e OF-B (PT-B)
  E os lances 2550.00 de COMP-X (sequencia 1) e 2600.00 de COMP-Y com demandaId DEM-Y (sequencia 2)
  E o relógio em 15:00:00
  Quando executar_prazos é chamado
  Então GET /api/v1/negociacoes/L1 por token(BUYER, COMP-Y) mostra status "ENCERRADO", compradorId COMP-Y e encerradoEm "2026-11-20T15:00:00.000Z"
  E foi publicada exatamente 1 mensagem em "negociacao_fechada" com chave L1 e payload igual ao de docs/negociacao/contratos/negociacao_fechada.exemplo.json: compradorId COMP-Y, demandaId DEM-Y e alocacoes [{OF-A, FORN-A, PT-A, 5.00, valorUnitario 122.36, valorTotal 611.78}, {OF-B, FORN-B, PT-B, 15.00, valorUnitario 132.55, valorTotal 1988.22}]

# CA-21
Cenário: Leilão aberto sem lances
  Dado L1 ABERTO, sem lances, e o relógio em 15:00:00
  Quando executar_prazos é chamado
  Então L1 fica "DESERTO" com compradorId null
  E foi publicada 1 mensagem em "negociacao_fechada" com payload {negociacaoId L1, tipo "LEILAO", status "DESERTO", compradorId null, demandaId null, produtoId P, alocacoes []}

# CA-22
Cenário: Encerramento uma única vez e no instante certo
  Dado L1 ABERTO com 2 lances e o relógio em 14:59:59.999
  Quando executar_prazos é chamado
  Então devolve 0 e L1 continua "ABERTO"
  Dado o relógio em 15:00:00
  Quando executar_prazos é chamado duas vezes
  Então a primeira chamada devolve 1, a segunda devolve 0 e há exatamente 1 mensagem em "negociacao_fechada" para L1

# CA-23
Cenário: Rateio com centavo que sobra e lote recalculado
  Dado o leilão L3 aberto com itens OF-A (1.00 × 120.00 = 120.00, 0.3333), OF-B (1.00 × 130.00 = 130.00, 0.3611) e OF-D (1.00 × 110.00 = 110.00, 0.3056), precoInicial 360.00 e incrementoMinimo 3.60, as três reservas confirmadas e o lance vencedor 1000.01 de COMP-X, sem demandaId
  Quando L3 é encerrado
  Então alocacoes tem valorTotal 333.30 (OF-A), 361.11 (OF-B) e 305.60 (OF-D), somando 1000.01, valorUnitario igual ao valorTotal e demandaId null
  Dado L1 ABERTO com OF-B recusada (lote só com OF-A, participacao 1.0000) e o lance vencedor 700.00 de COMP-X
  Quando L1 é encerrado
  Então alocacoes tem 1 item {OF-A, FORN-A, PT-A, quantidade 5.00, valorUnitario 140.00, valorTotal 700.00}
```

### RF-07 — Enviar contraproposta

- **Prioridade:** Must
- **Enunciado (EARS):** Enquanto a negociação direta estiver aceitando ações, quando o participante da vez enviar `POST /api/v1/negociacoes/{id}/propostas` com `precoUnitario` válido, o serviço deve registrar a proposta, passar a vez à outra parte, renovar `expiraEm` para agora + 48 h e responder 201.
- **Regras aplicáveis:** RN-12, RN-15, RN-16, RN-22
- **Interface:** API-09

```gherkin
# CA-24
Cenário: Comprador responde com contraproposta
  Dado a direta D1 recém-aberta (vez "BUYER") e o relógio em "2026-11-21T12:00:00.000Z"
  Quando token(BUYER, COMP-X) envia POST /api/v1/negociacoes/D1/propostas com {"precoUnitario": 125.00}
  Então a resposta é 201 com autor "BUYER", precoUnitario 125.00, sequencia 2 e criadoEm "2026-11-21T12:00:00.000Z"
  E GET /api/v1/negociacoes/D1 mostra precoAtual 125.00, vez "SUPPLIER", expiraEm "2026-11-23T12:00:00.000Z" e totalPropostas 2
  E nenhuma mensagem é publicada

# CA-25
Cenário: Proposta fora da vez
  Dado a direta D1 recém-aberta (vez "BUYER")
  Quando token(SUPPLIER, FORN-B) envia {"precoUnitario": 128.00}
  Então a resposta é 409 com codigo "FORA_DA_VEZ"

# CA-26
Cenário: Proposta em negociação encerrada ou vencida
  Dado a direta D1 com expiraEm "2026-11-22T12:00:00.000Z" e o relógio em "2026-11-22T12:00:00.000Z", sem executar_prazos
  Quando token(BUYER, COMP-X) envia {"precoUnitario": 125.00}
  Então a resposta é 409 com codigo "NEGOCIACAO_ENCERRADA"
  Dado D1 em RECUSADO
  Quando token(BUYER, COMP-X) envia {"precoUnitario": 125.00}
  Então a resposta é 409 com codigo "NEGOCIACAO_ENCERRADA"

# CA-27
Cenário: Quem não participa não vê a negociação direta
  Dado a direta D1 e o leilão L1
  Quando token(BUYER, COMP-Y) ou token(SUPPLIER, FORN-A) envia {"precoUnitario": 128.00} para D1
  Então cada resposta é 404 com codigo "NEGOCIACAO_NAO_ENCONTRADA"
  Quando token(SUPPLIER, FORN-A) envia {"precoUnitario": 128.00} para L1
  Então a resposta é 409 com codigo "MODO_INCOMPATIVEL"
```

### RF-08 — Aceitar a proposta

- **Prioridade:** Must
- **Enunciado (EARS):** Enquanto a negociação direta estiver aceitando ações e com reserva `CONFIRMADA`, quando o participante da vez enviar `POST /api/v1/negociacoes/{id}/aceitar`, o serviço deve encerrar a negociação como `ENCERRADO` pelo `precoAtual`, responder 200 e publicar `negociacao_fechada` com uma alocação.
- **Regras aplicáveis:** RN-12, RN-13, RN-15, RN-16, RN-22
- **Interface:** API-10; tópico `negociacao_fechada`

```gherkin
# CA-28
Cenário: Fornecedor aceita a proposta do comprador
  Dado a direta D1 depois da contraproposta 125.00 de COMP-X (vez "SUPPLIER", precoAtual 125.00), com reserva CONFIRMADA com PT-D e o relógio em 13:00:00
  Quando token(SUPPLIER, FORN-B) envia POST /api/v1/negociacoes/D1/aceitar sem corpo
  Então a resposta é 200 com status "ENCERRADO", encerradoEm "2026-11-20T13:00:00.000Z" e acoesPermitidas []
  E foi publicada exatamente 1 mensagem em "negociacao_fechada" com chave D1 e payload {negociacaoId D1, tipo "VENDA_DIRETA", status "ENCERRADO", compradorId COMP-X, demandaId null, produtoId P, alocacoes [{fornecimentoId OF-B, fornecedorId FORN-B, pedidoId PT-D, quantidade 10.00, valorUnitario 125.00, valorTotal 1250.00}]}

# CA-29
Cenário: Aceite fora da vez ou com reserva pendente
  Dado a direta D1 com vez "SUPPLIER" e reserva PENDENTE
  Quando token(BUYER, COMP-X) envia aceitar
  Então a resposta é 409 com codigo "FORA_DA_VEZ"
  Quando token(SUPPLIER, FORN-B) envia aceitar
  Então a resposta é 409 com codigo "RESERVA_PENDENTE"

# CA-30
Cenário: Corpo inválido e aceite repetido
  Dado a direta D1 com vez "SUPPLIER" e reserva CONFIRMADA
  Quando token(SUPPLIER, FORN-B) envia aceitar com corpo {"precoUnitario": 1.00}
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando token(SUPPLIER, FORN-B) envia aceitar com corpo {}
  Então a resposta é 200
  Quando token(SUPPLIER, FORN-B) envia aceitar de novo, sem Idempotency-Key
  Então a resposta é 409 com codigo "NEGOCIACAO_ENCERRADA" e nenhuma nova mensagem é publicada
```

### RF-09 — Recusar a negociação direta

- **Prioridade:** Must
- **Enunciado (EARS):** Enquanto a negociação direta estiver aceitando ações, quando qualquer participante enviar `POST /api/v1/negociacoes/{id}/recusar`, o serviço deve encerrá-la como `RECUSADO`, responder 200 e publicar `negociacao_fechada` sem alocações.
- **Regras aplicáveis:** RN-12, RN-14, RN-15, RN-16, RN-22
- **Interface:** API-11; tópico `negociacao_fechada`

```gherkin
# CA-31
Cenário: Comprador recusa fora da sua vez
  Dado a direta D1 com vez "SUPPLIER"
  Quando token(BUYER, COMP-X) envia POST /api/v1/negociacoes/D1/recusar
  Então a resposta é 200 com status "RECUSADO"
  E foi publicada exatamente 1 mensagem em "negociacao_fechada" com chave D1 e payload {negociacaoId D1, tipo "VENDA_DIRETA", status "RECUSADO", compradorId null, demandaId null, produtoId P, alocacoes []}

# CA-32
Cenário: Erros na recusa
  Dado a direta D1 em EXPIRADO e o leilão L1
  Quando token(SUPPLIER, FORN-B) envia recusar para D1
  Então a resposta é 409 com codigo "NEGOCIACAO_ENCERRADA"
  Quando token(BUYER, COMP-X) envia recusar para L1
  Então a resposta é 409 com codigo "MODO_INCOMPATIVEL"
  Quando token(CARRIER, COMP-X) envia recusar para D1
  Então a resposta é 403 com codigo "ACESSO_NEGADO"
```

### RF-10 — Expirar a negociação direta

- **Prioridade:** Must
- **Enunciado (EARS):** Quando `executar_prazos` rodar com `agora >= expiraEm` numa negociação direta `ABERTO`, o serviço deve encerrá-la como `EXPIRADO` e publicar `negociacao_fechada` sem alocações.
- **Regras aplicáveis:** RN-14, RN-21
- **Interface:** `app.main.executar_prazos`; tópico `negociacao_fechada`

```gherkin
# CA-33
Cenário: Negociação direta sem resposta por 48 h
  Dado a direta D1 com expiraEm "2026-11-22T12:00:00.000Z" e correlationId de abertura "teste-neg-02"
  E o relógio em "2026-11-22T11:59:59.999Z"
  Quando executar_prazos é chamado
  Então D1 continua "ABERTO"
  Dado o relógio em "2026-11-22T12:00:00.000Z"
  Quando executar_prazos é chamado
  Então D1 fica "EXPIRADO" com encerradoEm "2026-11-22T12:00:00.000Z"
  E foi publicada 1 mensagem em "negociacao_fechada" com chave D1, correlationId "teste-neg-02" e payload {negociacaoId D1, tipo "VENDA_DIRETA", status "EXPIRADO", compradorId null, demandaId null, produtoId P, alocacoes []}
```

### RF-11 — Consultar negociações

- **Prioridade:** Must
- **Enunciado (EARS):** Quando um token `BUYER` ou `SUPPLIER` solicitar `GET /api/v1/negociacoes` ou `GET /api/v1/negociacoes/{id}`, o serviço deve devolver as negociações visíveis, com paginação, sigilo, `agora` e `acoesPermitidas`.
- **Regras aplicáveis:** RN-09, RN-16, RN-17
- **Interface:** API-04, API-05

```gherkin
# CA-34
Cenário: Detalhe do leilão com sigilo e ações
  Dado o leilão L1 ABERTO com lances 2550.00 de COMP-X e 2600.00 de COMP-Y, e o relógio em 13:20:00
  Quando token(BUYER, COMP-X) consulta GET /api/v1/negociacoes/L1
  Então a resposta é 200 igual ao exemplo "Detalhe de leilão" do §7.2, com melhorLance.compradorId "OCULTO", agora "2026-11-20T13:20:00.000Z" e acoesPermitidas ["DAR_LANCE"]
  Quando token(BUYER, COMP-Y) consulta
  Então melhorLance.compradorId é COMP-Y
  Quando token(SUPPLIER, FORN-A) consulta
  Então melhorLance.compradorId é COMP-Y e acoesPermitidas é []
  Dado o leilão L1 AGUARDANDO_RESERVAS e o relógio em 12:00:30
  Quando token(BUYER, COMP-X) consulta
  Então acoesPermitidas é []

# CA-35
Cenário: Listagem com visibilidade e paginação
  Dado o leilão L1 e a direta D1 de COMP-X, ambos abertos às 12:00:00, e nenhuma outra negociação
  Quando token(BUYER, COMP-X) consulta GET /api/v1/negociacoes
  Então a resposta é 200 com page 1, size 10, total 2 e um item para L1 (status "AGUARDANDO_RESERVAS", dataInicio e dataFim do leilão, expiraEm null) e outro para D1 (dataInicio null, dataFim null, expiraEm "2026-11-22T12:00:00.000Z"), com os campos do exemplo da API-04
  Quando token(BUYER, COMP-Y) consulta GET /api/v1/negociacoes
  Então a resposta é 200 com total 1 e só o item de L1
  Quando token(BUYER, COMP-Y) consulta GET /api/v1/negociacoes?modo=VENDA_DIRETA
  Então a resposta é 200 com {"items": [], "page": 1, "size": 10, "total": 0}
  Quando token(BUYER, COMP-X) consulta com ?size=101, ?size=0, ?page=0, ?page=a ou ?status=CANCELADO
  Então cada resposta é 422 com codigo "VALIDACAO_INVALIDA"

# CA-36
Cenário: Detalhe invisível ou inexistente
  Dado a direta D1 de COMP-X
  Quando token(BUYER, COMP-Y) consulta GET /api/v1/negociacoes/D1
  Então a resposta é 404 com codigo "NEGOCIACAO_NAO_ENCONTRADA"
  Quando token(SUPPLIER, FORN-B) consulta GET /api/v1/negociacoes/D1 com vez "SUPPLIER" e reserva CONFIRMADA
  Então a resposta é 200 com acoesPermitidas ["PROPOR", "ACEITAR", "RECUSAR"]
  Quando token(BUYER, COMP-X) consulta GET /api/v1/negociacoes/123
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
```

### RF-12 — Listar lances e propostas

- **Prioridade:** Must
- **Enunciado (EARS):** Quando um token visível à negociação solicitar `GET /api/v1/negociacoes/{id}/lances` (leilão) ou `GET /api/v1/negociacoes/{id}/propostas` (direta), o serviço deve devolver os itens em ordem de `sequencia`, com paginação, aplicando o sigilo nos lances.
- **Regras aplicáveis:** RN-09, RN-15, RN-16
- **Interface:** API-06, API-08

```gherkin
# CA-37
Cenário: Lances com sigilo e paginação
  Dado o leilão L1 com lances 2550.00 de COMP-X e 2600.00 de COMP-Y
  Quando token(BUYER, COMP-X) consulta GET /api/v1/negociacoes/L1/lances
  Então a resposta é 200 igual ao exemplo da API-06
  Quando token(SUPPLIER, FORN-B) consulta a mesma rota
  Então os itens têm compradorId COMP-X e COMP-Y
  Quando token(BUYER, COMP-X) consulta ?page=2&size=1
  Então a resposta tem 1 item com sequencia 2, page 2, size 1 e total 2

# CA-38
Cenário: Propostas e rotas de outro modo
  Dado a direta D1 com as propostas 130.00 (SUPPLIER, na abertura) e 125.00 (BUYER), e o leilão L1
  Quando token(SUPPLIER, FORN-B) consulta GET /api/v1/negociacoes/D1/propostas
  Então a resposta é 200 igual ao exemplo da API-08
  Quando token(BUYER, COMP-Y) consulta GET /api/v1/negociacoes/D1/propostas
  Então a resposta é 404 com codigo "NEGOCIACAO_NAO_ENCONTRADA"
  Quando token(BUYER, COMP-X) consulta GET /api/v1/negociacoes/L1/propostas ou GET /api/v1/negociacoes/D1/lances
  Então cada resposta é 409 com codigo "MODO_INCOMPATIVEL"
```

### RF-13 — Autenticar e autorizar

- **Prioridade:** Must
- **Enunciado (EARS):** Se uma requisição a `/api/v1/...` chegar sem token válido, sem contexto de empresa ou com escopo não permitido, então o serviço deve responder 401, 403 ou 503 conforme o §7.1, sem alterar estado.
- **Regras aplicáveis:** RN-16, RN-22
- **Interface:** API-04 a API-11

```gherkin
# CA-39
Cenário: Token inválido
  Quando GET /api/v1/negociacoes é chamado: sem Authorization; com "Bearer " vazio; com token assinado por outra chave; com alg "HS256"; com kid "desconhecido"; com iss "outro"; com aud "outra"; sem sub; com exp igual a agora
  Então cada resposta é 401 com codigo "TOKEN_INVALIDO" e cabeçalho WWW-Authenticate "Bearer"

# CA-40
Cenário: Token sem contexto de empresa
  Quando GET /api/v1/negociacoes é chamado com token de view_mode "NONE" e tenant_id null; com view_mode "MACRO_GROUP" e tenant_id null; ou com view_mode "SINGLE" e active_scope null
  Então cada resposta é 403 com codigo "CONTEXTO_NAO_SELECIONADO"

# CA-41
Cenário: Escopos sem acesso
  Quando token(CARRIER, COMP-X) ou token(TENANT_ADMIN, COMP-X) chama GET /api/v1/negociacoes
  Então cada resposta é 403 com codigo "ACESSO_NEGADO"
  Quando token(SUPPLIER, FORN-C) com platform_role "PLATFORM_ADMIN", roles ["BUYER_PURCHASER"] e client_id "svc-teste" envia {"valor": 3000.00} para L1 ABERTO
  Então a resposta é 403 com codigo "ACESSO_NEGADO"

# CA-42
Cenário: Dependência indisponível e falha inesperada
  Quando chaves_publicas.obter lança exceção numa chamada a GET /api/v1/negociacoes
  Então a resposta é 503 com codigo "DEPENDENCIA_INDISPONIVEL"
  Quando repositorio.obter_negociacao lança RepositorioIndisponivel em GET /api/v1/negociacoes/L1
  Então a resposta é 503 com codigo "DEPENDENCIA_INDISPONIVEL"
  Quando repositorio.obter_negociacao lança RuntimeError em GET /api/v1/negociacoes/L1
  Então a resposta é 500 com codigo "ERRO_INTERNO"

# CA-43
Cenário: Correlação
  Quando GET /api/v1/negociacoes/L1 é chamado com X-Correlation-Id "teste-neg-57"
  Então a resposta tem X-Correlation-Id "teste-neg-57"
  Quando é chamado sem X-Correlation-Id, ou com "a b", ou com 129 caracteres "a"
  Então a resposta tem X-Correlation-Id com um UUID novo
  Quando GET /api/v1/negociacoes/123 é chamado com X-Correlation-Id "teste-neg-57"
  Então o corpo do erro tem correlationId "teste-neg-57" e instance "/api/v1/negociacoes/123"
```

### RF-14 — Idempotência das escritas HTTP

- **Prioridade:** Must
- **Enunciado (EARS):** Onde um `POST` de negócio trouxer `Idempotency-Key`, o serviço deve devolver a resposta original de uma chamada bem-sucedida com o mesmo escopo, a mesma chave e o mesmo corpo, sem repetir o efeito; com corpo diferente, deve responder 409.
- **Regras aplicáveis:** RN-18
- **Interface:** API-07, API-09, API-10, API-11

```gherkin
# CA-44
Cenário: Reenvio do mesmo lance
  Dado o leilão L1 ABERTO com precoInicial 2550.00
  E token(BUYER, COMP-X) enviou {"valor": 2550.00} com Idempotency-Key "K1" e recebeu 201
  Quando token(BUYER, COMP-X) reenvia {"valor": 2550.00} com Idempotency-Key "K1"
  Então a resposta é 201 com corpo idêntico ao da primeira
  E totalLances é 1 e há só 1 mensagem em "lance_realizado"
  Dado L1 ENCERRADO
  Quando token(BUYER, COMP-X) reenvia a mesma requisição com "K1"
  Então a resposta é 201 com o mesmo corpo

# CA-45
Cenário: Chave reutilizada com outro corpo, de outro usuário ou inválida
  Dado que token(BUYER, COMP-X) enviou {"valor": 2550.00} com Idempotency-Key "K1"
  Quando token(BUYER, COMP-X) envia {"valor": 2600.00} com "K1"
  Então a resposta é 409 com codigo "IDEMPOTENCIA_CONFLITO"
  Quando token(BUYER, COMP-Y) envia {"valor": 2600.00} com "K1"
  Então a resposta é 201 com sequencia 2
  Quando token(BUYER, COMP-Y) envia {"valor": 2700.00} com Idempotency-Key "a b"
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando token(BUYER, COMP-Y) envia {"valor": 2700.00} com Idempotency-Key ""
  Então a resposta é 201
```

### RF-15 — Consumir mensagens com idempotência e DLT

- **Prioridade:** Must
- **Enunciado (EARS):** Quando uma mensagem consumida já processada chegar de novo, o serviço deve ignorá-la; se a mensagem for inválida ou continuar falhando após 4 execuções, então o serviço deve publicá-la em `negociacao.dlt` sem propagar exceção.
- **Regras aplicáveis:** RN-19, RN-20
- **Interface:** tópicos consumidos; tópico `negociacao.dlt`

```gherkin
# CA-46
Cenário: Cinco entregas do mesmo evento
  Dado a direta D1 com reserva PENDENTE
  Quando o handler recebe 5 vezes a mesma recusa da reserva de OF-B em D1, com o mesmo eventId "38000000-0000-4000-8000-000000000060"
  Então D1 fica "RECUSADO", há exatamente 1 mensagem em "negociacao_fechada" para D1 e evento_processado("pedidos-service", "38000000-0000-4000-8000-000000000060") é verdadeiro

# CA-47
Cenário: Mensagem inválida vai para a DLT
  Quando o handler de pedido_atualizado recebe o originalEvent de docs/negociacao/contratos/negociacao.dlt.exemplo.json (sem payload)
  Então o handler não lança exceção
  E é publicada 1 mensagem em "negociacao.dlt" com chave PT-A e payload {topicoOrigem "pedido_atualizado", originalEvent igual à mensagem recebida, error "EVENTO_INVALIDO", detalhe não vazio, stackTrace "", failedAtAttempt 1}
  Quando o handler recebe a mesma mensagem de novo
  Então é publicada outra mensagem em "negociacao.dlt"
  Quando o handler de pedido_atualizado recebe mensagens com eventType "pedido_criado", com eventVersion "2.0", com source "outro-service", com payload sem status, ou sem reservaSituacao
  Então cada uma gera 1 mensagem em "negociacao.dlt" e nenhum estado muda

# CA-48
Cenário: Chave da DLT sem aggregateId
  Quando o handler de modo_negociacao_definido recebe {"eventId": "E-62", "payload": {}}
  Então a mensagem em "negociacao.dlt" tem chave "E-62"
  Quando recebe {"payload": {}}
  Então a mensagem em "negociacao.dlt" tem chave "desconhecido"

# CA-49
Cenário: Falha transitória e falha persistente
  Dado o repositório lançando RepositorioIndisponivel nas 2 primeiras transações e funcionando depois
  Quando o handler recebe a confirmação da reserva de OF-A em L1
  Então OF-A fica "CONFIRMADA", clock.sleep foi chamado com 1 e 2, e nada vai para "negociacao.dlt"
  Dado o repositório lançando RepositorioIndisponivel em toda transação de processamento
  Quando o handler recebe a confirmação da reserva de OF-A em L1
  Então o handler executa o processamento 4 vezes, clock.sleep é chamado com 1, 2 e 4, e é publicada 1 mensagem em "negociacao.dlt" com error "PROCESSAMENTO_FALHOU" e failedAtAttempt 4
```

### RF-16 — Saúde, prontidão e métricas

- **Prioridade:** Must
- **Enunciado (EARS):** Quando `/health`, `/health/ready` ou `/metrics` forem chamados, o serviço deve responder sem exigir token, com prontidão 503 se o banco ou o barramento estiverem indisponíveis.
- **Regras aplicáveis:** —
- **Interface:** API-01, API-02, API-03

```gherkin
# CA-50
Cenário: Endpoints técnicos
  Dado o relógio em 12:00:00
  Quando GET /health é chamado sem cabeçalhos
  Então a resposta é 200 com {"status": "UP", "service": "negociacao-service", "timestamp": "2026-11-20T12:00:00.000Z"}
  Quando GET /health/ready é chamado com repositório e barramento disponíveis
  Então a resposta é 200 com {"status": "READY", "service": "negociacao-service"}
  Quando repositorio.disponivel() devolve false, ou event_bus.disponivel() devolve false
  Então GET /health/ready responde 503 com codigo "DEPENDENCIA_INDISPONIVEL"
  Quando GET /metrics é chamado depois de um GET /api/v1/negociacoes com resposta 200
  Então a resposta é 200 em text/plain e contém uma linha http_requests_total com metodo="GET", rota="/api/v1/negociacoes" e status="200"
```

## 9. Regras de negócio

| ID | Regra | Exemplo concreto |
|---|---|---|
| RN-01 | Abertura do leilão: um `leilao_iniciado` válido (§7.3) com `negociacaoId` desconhecido cria a negociação `LEILAO_DIRETO` em `AGUARDANDO_RESERVAS`, com `dataInicio = iniciaEm`, `dataFim = encerraEm`, `quantidadeTotal`, `precoInicial` e `incrementoMinimo` do evento e `correlationId` do envelope, e uma `OfertaLeilao` `PENDENTE` por item, com `ordem` = posição do item na lista. Os valores do Mercado não são conferidos entre si (por exemplo, `precoInicial` contra Σ `valorItem`). `negociacaoId` já existente, em qualquer modo → ignorado, sem DLT. (D33, D40) | CA-01: L1 com 2 ofertas `PENDENTE`. |
| RN-02 | Abertura da direta: um `modo_negociacao_definido` válido com `modo = "VENDA_DIRETA"` e `negociacaoId` desconhecido cria a negociação `VENDA_DIRETA` `ABERTO` com `compradorId = demandas[0].compradorId`; `fornecimentoId`, `fornecedorId`, `quantidade` e `precoOferta = precoUnitario` de `ofertas[0]`; `precoAtual = precoOferta`; a proposta 1 (`autor = "SUPPLIER"`, `empresaId = fornecedorId`, `precoUnitario = precoOferta`); `vez = "BUYER"`; `expiraEm = agora + 48 h`; `reservaSituacao = "PENDENTE"`; `totalPropostas = 1`. Com `modo = "LEILAO_DIRETO"`, a mensagem é registrada como processada e ignorada: o leilão é aberto pelo `leilao_iniciado` (RN-01). `negociacaoId` já existente → ignorado, sem DLT. Demanda, saldo, preço máximo e conflito de interesse já foram conferidos pelo Mercado. (D34, D40) | D1 aberta com 130.00 do fornecedor e a vez do comprador. |
| RN-03 | Pedido temporário: a abertura publica exatamente um `pedido_temporario_solicitado` por oferta (no leilão, em ordem de `ordem`), com chave `negociacaoId`, `tipo` (`"LEILAO"` ou `"VENDA_DIRETA"`) e `precoUnitario` = preço da oferta no evento do Mercado. (D7, D42) | L1 → 2 mensagens; D1 → 1. |
| RN-04 | Reserva: um `pedido_atualizado` válido com `reservaSituacao` ∈ {`CONFIRMADA`, `RECUSADA`} muda a reserva da oferta correspondente (oferta do leilão, ou negociação direta, com aquele `negociacaoId` e `fornecimentoId`) **somente se ela estiver `PENDENTE`**. `CONFIRMADA` grava também o `pedidoId`. Numa direta, `RECUSADA` leva a negociação a `RECUSADO` (RN-14). Num leilão, depois da mudança aplica-se a RN-05. É ignorado, sem DLT: evento com outra `reservaSituacao`, de negociação ou oferta desconhecida, de negociação em estado final, ou de oferta que já não está `PENDENTE`. (D7, D30, D35) | Recusa de OF-A já confirmada → ignorada. |
| RN-05 | Liberação dos lances: num leilão `AGUARDANDO_RESERVAS`, quando nenhuma oferta fica `PENDENTE`: (a) com ≥ 1 oferta `CONFIRMADA`, `status = "ABERTO"`. Se alguma oferta foi `RECUSADA`, recalcula-se com as `CONFIRMADA`: `quantidadeTotal` = Σ `quantidade`; novo preço inicial `P'` = Σ `valorItem`; `incrementoMinimo` = `max(0.01, incrementoMinimo × P' / precoInicial anterior)`, com 2 casas `ROUND_HALF_UP`; `participacao` de cada oferta = `valorItem / P'`, com 4 casas `ROUND_HALF_UP`, e a diferença para `1.0000` somada à oferta de maior `valorItem` (empate: menor `ordem`). As ofertas recusadas continuam na lista com `situacao = "RECUSADA"`, fora do lote. Sem recusa, ficam os valores do evento. Não publica evento. (b) Sem oferta `CONFIRMADA`, o leilão vai a `DESERTO`: `compradorId = null`, `encerradoEm = agora` e publica uma única `negociacao_fechada` com `alocacoes []`. (D35, D37) | OF-B recusada em L1: preço inicial 2550.00 → 600.00, incremento 25.50 → 6.00, OF-A com 1.0000. |
| RN-06 | Lance só é aceito com o leilão aceitando lances (`status = "ABERTO"` e `agora < dataFim`). Caso contrário → `409 LEILAO_NAO_ABERTO`. (P3, D35) | `AGUARDANDO_RESERVAS`, ou agora = `dataFim` → 409. |
| RN-07 | O lance é o valor total pelo lote inteiro. Sem lances, `valor >= precoInicial`; com lances, `valor >= melhor lance + incrementoMinimo`. Caso contrário → `422 LANCE_INSUFICIENTE`. A empresa do melhor lance pode dar outro lance. `demandaId` opcional (texto de 1 a 64 caracteres de `[A-Za-z0-9._-]`, ou `null`) é gravado no lance sem nenhuma conferência. (P10, D28, D32, D37, D38) | Preço inicial 2550.00 e incremento 25.50: 2549.99 → 422; 2550.00 → 201; depois 2575.49 → 422 e 2575.50 → 201. |
| RN-08 | Conflito de interesse: empresa que é fornecedor do leilão (oferta em qualquer situação) não dá lance nele → `403 CONFLITO_DE_INTERESSE`. (D25) | Token `BUYER` da FORN-A em L1 → 403. |
| RN-09 | Sigilo: nas respostas da API-05 (`melhorLance.compradorId` e `compradorId`) e da API-06 (`items[].compradorId`), o `compradorId` aparece se for a empresa do token ou se a empresa do token for fornecedor do leilão; senão, é substituído por `"OCULTO"`. Valores nunca são ocultados. A resposta da API-07 e os eventos nunca são mascarados. (D14) | COMP-X vê o lance de COMP-Y como `"OCULTO"`; FORN-A vê `COMP-Y`. |
| RN-10 | Encerramento do leilão: na execução de `executar_prazos` com `agora >= dataFim`: leilão `ABERTO` com `totalLances >= 1` → `ENCERRADO`, `compradorId` = empresa do melhor lance, e a `negociacao_fechada` leva o rateio (RN-11) e o `demandaId` do melhor lance; leilão `ABERTO` sem lances, ou ainda `AGUARDANDO_RESERVAS` → `DESERTO`, `compradorId = null`, `alocacoes []`. Em todos os casos, `encerradoEm = agora` e publica uma única `negociacao_fechada`. (D8, D23, D35, D38) | 2 lances → `ENCERRADO`; 0 lances ou reserva pendente → `DESERTO`. |
| RN-11 | Rateio do lance vencedor `V` entre as ofertas do lote (`CONFIRMADA`, em ordem de `ordem`): `valorTotal_i` = `V × participacao_i` com 2 casas `ROUND_DOWN`; a diferença `V − Σ valorTotal_i` é somada à oferta de maior `valorItem` (empate: menor `ordem`). `valorUnitario_i` = `valorTotal_i / quantidade_i` com 2 casas `ROUND_HALF_UP`. `valorTotal` é o valor que vale para o pagamento; `valorUnitario` é informativo. (P5, D36, ADR 0002) | `V` 2600.00 com 0.2353 e 0.7647 → 611.78 e 1988.22; `V` 1000.01 com 0.3333, 0.3611 e 0.3056 → 333.30, 361.11 e 305.60. |
| RN-12 | Negociação direta aceitando ações: `status = "ABERTO"` e `agora < expiraEm`; senão → `409 NEGOCIACAO_ENCERRADA`. Só o participante da `vez` propõe ou aceita; o outro → `409 FORA_DA_VEZ`. Cada proposta inverte a `vez`, grava `precoAtual` e renova `expiraEm = agora + 48 h`. Qualquer participante pode recusar enquanto a negociação aceita ações. (D17, D20, D34) | Proposta às 2026-11-21T12:00 → `expiraEm` 2026-11-23T12:00. |
| RN-13 | O aceite exige reserva `CONFIRMADA` (senão → `409 RESERVA_PENDENTE`) e encerra como `ENCERRADO` com uma alocação {`fornecimentoId`, `fornecedorId`, `pedidoId`, `quantidade`, `valorUnitario` = `precoAtual`, `valorTotal` = `quantidade × precoAtual` com 2 casas `ROUND_HALF_UP`}; `compradorId` = comprador da direta e `demandaId = null`. (D20) | 10.00 × 125.00 → `valorTotal` 1250.00. |
| RN-14 | Recusa (API-11 ou reserva `RECUSADA`) → `RECUSADO`; prazo vencido em `executar_prazos` → `EXPIRADO`. Ambos gravam `encerradoEm` e publicam `negociacao_fechada` com `compradorId = null`, `demandaId = null` e `alocacoes []`. (D20, D23, D42) | D1 sem resposta até `expiraEm` → `EXPIRADO`. |
| RN-15 | Modo da rota: API-06 e API-07 só em `LEILAO_DIRETO`; API-08 a API-11 só em `VENDA_DIRETA`. Caso contrário → `409 MODO_INCOMPATIVEL`. | `POST /lances` em D1 → 409. |
| RN-16 | Escopos: rotas de negócio aceitam só `BUYER` e `SUPPLIER`; dar lance exige `BUYER`; escopo errado → `403 ACESSO_NEGADO`. Visibilidade: leilão visível a todos; direta só aos participantes; invisível → `404 NEGOCIACAO_NAO_ENCONTRADA`. (D3) | `CARRIER` em qualquer rota → 403. |
| RN-17 | `acoesPermitidas` lista, nesta ordem, as ações que o token pode fazer agora: `"DAR_LANCE"` (escopo `BUYER`, leilão aceitando lances e empresa que não é fornecedor do leilão); `"PROPOR"` (participante da `vez`, direta aceitando ações); `"ACEITAR"` (como `PROPOR` e reserva `CONFIRMADA`); `"RECUSAR"` (participante, direta aceitando ações). | Comprador logo após a abertura da direta → `["PROPOR", "RECUSAR"]`. |
| RN-18 | Idempotência HTTP: escopo = (empresa, `sub`, método, caminho canônico, `Idempotency-Key`). Só respostas 2xx são memorizadas, com status, corpo, `Location` e o hash do corpo da requisição, por 24 h, na mesma transação da mudança. Mesmo escopo e mesmo corpo → mesma resposta, sem efeito e sem evento, mesmo depois do estado final. Mesmo escopo e outro corpo → `409 IDEMPOTENCIA_CONFLITO`. Sem chave, nada é memorizado. A verificação ocorre depois da autenticação, da validação, da existência e do modo, e antes das regras de domínio. | "K1" repetido → mesma resposta, 1 lance. |
| RN-19 | Consumo: a validação (RN-20) vem antes da deduplicação. Mensagem válida com (`source`, `eventId`) já processado → sem efeito, sem publicação. Efeito e `registrar_evento_processado` na mesma transação. Se a transação falhar (`RepositorioIndisponivel` ou outra exceção), o handler repete o processamento até 4 execuções no total, chamando `clock.sleep(1)`, `clock.sleep(2)` e `clock.sleep(4)` antes da 2ª, da 3ª e da 4ª; falhando a 4ª, publica em `negociacao.dlt` com `error` `"PROCESSAMENTO_FALHOU"` e `failedAtAttempt` 4. | 5 entregas iguais → 1 efeito. |
| RN-20 | Mensagem consumida inválida (§7.3) não altera estado e é publicada em `negociacao.dlt` na hora, com `error` `"EVENTO_INVALIDO"`, `failedAtAttempt` 1 e chave conforme §7.4. Não é registrada como processada. O handler nunca propaga exceção. | Envelope sem `payload` → DLT com chave OF-A. |
| RN-21 | `executar_prazos` processa cada identificador de `listar_vencidas(agora, 100)` numa transação própria: obtém a negociação com `bloquear=True`, verifica de novo a condição e só então aplica RN-05, RN-10 ou RN-14. Estados finais (`ENCERRADO`, `DESERTO`, `RECUSADO`, `EXPIRADO`) não mudam mais por nenhum caminho. Chamar duas vezes no mesmo instante não repete efeitos. | Segunda chamada às 15:00 → devolve 0, sem novo evento. |
| RN-22 | Requisição rejeitada (qualquer 4xx ou 5xx) não altera estado, não memoriza idempotência e não publica evento. | Lance com 422 → `totalLances` igual. |

## 10. Catálogo de erros

Formato único de erro (`Content-Type: application/problem+json`), igual ao da ESP-PED-001 §6.3:

```json
{
  "type": "about:blank",
  "title": "Conflito",
  "status": 409,
  "codigo": "LEILAO_NAO_ABERTO",
  "detail": "O leilão não está aceitando lances.",
  "instance": "/api/v1/negociacoes/31000000-0000-4000-8000-000000000001/lances",
  "correlationId": "teste-neg-21",
  "timestamp": "2026-11-20T15:00:00.000Z"
}
```

`type` é sempre `"about:blank"`; `instance` é o caminho sem query string; `correlationId` é o valor efetivo do §7.1; `timestamp` é `clock.now()`. `title`, `status`, `codigo` e `detail` são os da tabela, byte a byte.

| Código | HTTP | `title` | `detail` | Quando ocorre |
|---|---|---|---|---|
| TOKEN_INVALIDO | 401 | Não autenticado | Token de acesso ausente ou inválido. | §7.1, passo 1. |
| CONTEXTO_NAO_SELECIONADO | 403 | Acesso negado | Selecione um contexto de empresa válido. | §7.1, passo 2. |
| ACESSO_NEGADO | 403 | Acesso negado | Você não tem permissão para esta operação. | RN-16 (escopo). |
| CONFLITO_DE_INTERESSE | 403 | Acesso negado | A empresa não pode negociar com uma oferta própria. | RN-08. |
| NEGOCIACAO_NAO_ENCONTRADA | 404 | Não encontrado | Negociação não encontrada. | `negociacaoId` inexistente ou invisível (RN-16). |
| LEILAO_NAO_ABERTO | 409 | Conflito | O leilão não está aceitando lances. | RN-06. |
| NEGOCIACAO_ENCERRADA | 409 | Conflito | A negociação não está aberta. | RN-12. |
| FORA_DA_VEZ | 409 | Conflito | Aguarde a resposta da outra parte. | RN-12. |
| RESERVA_PENDENTE | 409 | Conflito | A reserva do estoque ainda não foi confirmada. | RN-13. |
| MODO_INCOMPATIVEL | 409 | Conflito | Operação não disponível para o modo desta negociação. | RN-15. |
| IDEMPOTENCIA_CONFLITO | 409 | Conflito | A chave de idempotência já foi usada com outro corpo. | RN-18. |
| VALIDACAO_INVALIDA | 422 | Entrada inválida | Os dados da requisição são inválidos. | Corpo, ID na URL, query, `Idempotency-Key` ou paginação fora do contrato (§7.1, §7.2). |
| LANCE_INSUFICIENTE | 422 | Entrada inválida | O lance deve ser igual ou maior que o preço inicial e, havendo lances, igual ou maior que o melhor lance mais o incremento mínimo. | RN-07. |
| ERRO_INTERNO | 500 | Erro interno | Não foi possível processar a requisição. | Exceção não prevista. |
| DEPENDENCIA_INDISPONIVEL | 503 | Serviço indisponível | Uma dependência necessária está indisponível. | JWKS indisponível, `RepositorioIndisponivel`, ou prontidão falhando (API-02). |

Respostas fora do catálogo: rota inexistente (404) e método não permitido (405) mantêm a resposta padrão do framework; `/docs` e `/openapi.json` ficam habilitados. Nenhuma delas faz parte da avaliação de conformidade.

**Ordem de verificação** (quando mais de um erro se aplica, retorna-se o primeiro):

1. Correlação (§7.1; nunca é erro).
2. `TOKEN_INVALIDO`, ou `DEPENDENCIA_INDISPONIVEL` se as chaves públicas falharem.
3. `CONTEXTO_NAO_SELECIONADO`.
4. `ACESSO_NEGADO` pelo escopo da rota.
5. `VALIDACAO_INVALIDA` (ID na URL, query, `Idempotency-Key`, corpo).
6. `NEGOCIACAO_NAO_ENCONTRADA`.
7. `MODO_INCOMPATIVEL`.
8. Idempotência: replay da resposta memorizada ou `IDEMPOTENCIA_CONFLITO`.
9. Regras de domínio, na ordem da coluna "Erros possíveis" do §7.2 para cada rota.

`DEPENDENCIA_INDISPONIVEL` (banco) e `ERRO_INTERNO` podem ocorrer em qualquer passo que use o repositório. A validação automática de corpo do FastAPI não pode ser usada nas rotas com corpo, porque responderia antes dos passos 2 a 4.

## 11. Requisitos não funcionais

| ID | Característica (ISO/IEC 25010) | Requisito | Métrica e limiar | Verificação |
|---|---|---|---|---|
| RNF-01 | Adequação funcional — exatidão | Aritmética monetária com `Decimal` e saída com 2 casas. | Lote de 1.00 × 0.10 e 1.00 × 0.20 (preço inicial 0.30) → lance 0.30 aceito e alocações 0.09 e 0.21; lance `2550` devolvido como `2550.00`; soma das alocações = lance vencedor em 100% dos encerramentos. | CA-51 |
| RNF-02 | Interoperabilidade | Formato único de erro. | 100% das respostas 4xx e 5xx de negócio em `application/problem+json` com os 8 campos do §10. | CA-52 |
| RNF-03 | Confiabilidade — ordenação | Eventos da mesma negociação na mesma partição e envelope do kit. | 100% das mensagens publicadas (exceto DLT) com `key == aggregateId == negociacaoId`, `eventType` = tópico, `eventVersion` `"1.0"`, `source` `"negociacao-service"`, `timestamp` = relógio e `eventId` UUID distinto; 100% válidas contra `docs/negociacao/contratos/<topico>.schema.json`. | CA-53 |
| RNF-04 | Manutenibilidade — modularidade | Dependências permitidas e estado só nas portas. | Nenhum import fora do §5 nos módulos de domínio; duas aplicações de `create_app` com repositórios diferentes não compartilham negociações. | CA-54 |
| RNF-05 | Confiabilidade — idempotência do consumidor | Reentrega não repete efeito, com o serviço real. | 5 entregas do mesmo `pedido_atualizado` `RECUSADA` no Redpanda → 1 `negociacao_fechada` e 1 linha em `processed_events`. | CA-55 |
| RNF-06 | Confiabilidade — concorrência | Lances serializados com duas réplicas. | 50 lances concorrentes, com valores distintos de 2550.00 a 2599.00, enviados às duas réplicas pelo gateway → sequências aceitas 1..n sem buracos, cada lance aceito maior que o anterior, melhor lance = maior valor aceito, 0 respostas 5xx. | CA-56 |
| RNF-07 | Confiabilidade — prazos | Encerramento único e pontual com duas réplicas. | Com as duas réplicas e o agendador ativo, 1 leilão com `dataFim` em T → exatamente 1 `negociacao_fechada`, publicada em até 2 s após T; após reiniciar as réplicas paradas desde antes de T, o encerramento ocorre em até 5 s após a subida. | CA-57 |
| RNF-08 | Eficiência de desempenho | Latência no kit, via gateway, duas réplicas. | Escrita de lance p95 ≤ 1000 ms com 20 usuários simultâneos por 2 min; leitura (API-04, API-05, API-06) p95 ≤ 500 ms com 200 usuários simultâneos por 2 min, após 30 s de aquecimento; respostas 5xx < 1%. | CA-58 |
| RNF-09 | Segurança — controle de acesso | RBAC por escopo. | 100% das chamadas às rotas de negócio com token `CARRIER` ou `TENANT_ADMIN`, e de lance com `SUPPLIER`, → 403 `ACESSO_NEGADO`, sem mudança de estado. | CA-41, CA-17 |
| RNF-10 | Confiabilidade — publicação durável | Outbox com mesmo `eventId`. | Publicador interrompido antes da confirmação → após a retomada por outra réplica, toda cópia publicada tem o `eventId` gravado na outbox; 0 eventos perdidos. | CA-59 |
| RNF-11 | Segurança — isolamento de dados | Schema próprio e pool. | Com `svc_negociacao`, DML no schema `negociacao` funciona e `SELECT` em `pedidos` falha; 0 FKs para outros schemas; pool com no máximo 5 conexões por réplica sob a carga do RNF-08. | CA-60 |
| RNF-12 | Disponibilidade — prontidão | Degradação observável. | Banco ou Redpanda parados → `/health` 200 e `/health/ready` 503 em até 10 s; dependência de volta → `/health/ready` 200 em até 10 s. | CA-61 |

```gherkin
# CA-51
Cenário: Exatidão decimal
  Dado o leilão L4 aberto com itens OF-A (1.00 × 0.10 = 0.10, 0.3333) e OF-B (1.00 × 0.20 = 0.20, 0.6667), precoInicial 0.30 e incrementoMinimo 0.01, com as duas reservas confirmadas
  Quando token(BUYER, COMP-X) envia {"valor": 0.30}
  Então a resposta é 201 com valor 0.30
  Quando L4 é encerrado com esse lance
  Então alocacoes tem valorTotal 0.09 (OF-A) e 0.21 (OF-B), somando 0.30
  E no leilão L1 com precoInicial 2550.00, o lance {"valor": 2550} é devolvido com valor 2550.00

# CA-52
Cenário: Formato de erro
  Para cada resposta 4xx e 5xx produzida pelos CA-01 a CA-50
  Então o Content-Type é "application/problem+json" e o corpo tem exatamente as chaves type, title, status, codigo, detail, instance, correlationId e timestamp, com title, status e detail iguais à linha do codigo no §10

# CA-53
Cenário: Envelope, chave e schema
  Para cada mensagem publicada nos CA-01 a CA-50, exceto em "negociacao.dlt"
  Então key == aggregateId == payload.negociacaoId, eventType é o tópico, eventVersion "1.0", source "negociacao-service", timestamp igual ao relógio, eventId UUID diferente dos demais
  E a mensagem é válida contra docs/negociacao/contratos/<topico>.schema.json
  E cada arquivo docs/negociacao/contratos/<topico>.exemplo.json é válido contra o schema do mesmo tópico

# CA-54
Cenário: Dependências e isolamento
  Ao inspecionar os imports dos arquivos .py de app/, exceto app/adaptadores/
  Então só há biblioteca padrão, fastapi, starlette, pydantic, jwt, cryptography, simplejson, prometheus_client ou app
  E duas aplicações de create_app com repositórios em memória diferentes: L1 aberto pelo handler da primeira → GET na segunda responde 404

# CA-55
Cenário: Idempotência do consumidor no Redpanda (integração)
  Dado o serviço real com PostgreSQL e Redpanda e uma direta D com reserva PENDENTE
  Quando o mesmo envelope de pedido_atualizado RECUSADA para D é produzido 5 vezes no tópico
  Então, em até 10 s, há 1 mensagem lógica em "negociacao_fechada" para D e 1 linha em negociacao.processed_events com aquele eventId

# CA-56
Cenário: Lances concorrentes em duas réplicas (integração)
  Dado o leilão ABERTO com precoInicial 2550.00 e as réplicas app1 e app2 atrás do gateway
  Quando 50 compradores enviam ao mesmo tempo lances de 2550.00, 2551.00, … 2599.00
  Então todas as respostas são 201 ou 422 "LANCE_INSUFICIENTE", nenhuma é 5xx
  E os lances aceitos têm sequencias 1..n sem buracos, valores estritamente crescentes por sequencia, e melhorLance é o de maior valor aceito
  E há exatamente n mensagens em "lance_realizado"

# CA-57
Cenário: Encerramento com duas réplicas (integração)
  Dado um leilão ABERTO com 1 lance e dataFim T, e as duas réplicas com o agendador ativo
  Então em até 2 s após T há exatamente 1 mensagem em "negociacao_fechada" para ele
  Dado outro leilão com dataFim T2 e as duas réplicas paradas antes de T2
  Quando as réplicas sobem depois de T2
  Então em até 5 s há exatamente 1 mensagem em "negociacao_fechada" para ele

# CA-58
Cenário: Carga (integração, k6)
  Dado o kit com gateway e duas réplicas, 100 leilões ABERTO e 1.000 negociações
  Quando o roteiro k6 roda 2 min de lances com 20 usuários e 2 min de leituras com 200 usuários, após 30 s de aquecimento
  Então o p95 de POST /lances é ≤ 1000 ms, o p95 das leituras é ≤ 500 ms e a taxa de 5xx é < 1%

# CA-59
Cenário: Outbox (integração)
  Dado um lance aceito com a publicação travada antes da confirmação do broker
  Quando a réplica é encerrada e a outra assume a outbox
  Então "lance_realizado" chega ao tópico com o eventId gravado em negociacao.outbox e nenhuma cópia tem outro eventId

# CA-60
Cenário: Isolamento do banco (integração)
  Dado a conexão com a role svc_negociacao
  Então INSERT e SELECT em negociacao.lance funcionam, SELECT em pedidos.pedido falha por permissão, a migração não cria FK fora do schema negociacao e pg_stat_activity mostra no máximo 5 conexões por réplica durante o CA-58

# CA-61
Cenário: Prontidão com dependências reais (integração)
  Quando o PostgreSQL é parado
  Então em até 10 s GET /health responde 200 e GET /health/ready responde 503 com codigo "DEPENDENCIA_INDISPONIVEL"
  Quando o PostgreSQL volta
  Então em até 10 s GET /health/ready responde 200
  E o mesmo vale ao parar e religar o Redpanda
```

## 12. Matriz de rastreabilidade

| Requisito | Regras | Interfaces | Critérios de aceitação |
|---|---|---|---|
| RF-01 | RN-01, RN-03, RN-19, RN-20 | `leilao_iniciado`, `pedido_temporario_solicitado`, API-05 | CA-01 a CA-03 |
| RF-02 | RN-02, RN-03, RN-19, RN-20 | `modo_negociacao_definido`, `pedido_temporario_solicitado`, API-05, API-08 | CA-04, CA-05 |
| RF-03 | RN-04, RN-14, RN-19 | `pedido_atualizado`, `negociacao_fechada` | CA-06 a CA-08; erro: mensagens que não se aplicam são ignoradas (CA-07) e as inválidas são cobertas pelo RF-15 (CA-47) |
| RF-04 | RN-04, RN-05, RN-10, RN-21 | `pedido_atualizado`, `executar_prazos`, `negociacao_fechada` | CA-09 a CA-12; sem cenário de erro: processo interno sem entrada do usuário; os casos negativos são a recusa de todas as reservas (CA-11) e a reserva pendente em `dataFim` (CA-12) |
| RF-05 | RN-06, RN-07, RN-08, RN-15, RN-16, RN-22 | API-07, `lance_realizado` | CA-13 a CA-19 |
| RF-06 | RN-10, RN-11, RN-21 | `executar_prazos`, `negociacao_fechada` | CA-20 a CA-23; sem cenário de erro: processo interno; o caso negativo é o prazo ainda não vencido (CA-22) |
| RF-07 | RN-12, RN-15, RN-16, RN-22 | API-09 | CA-24 a CA-27 |
| RF-08 | RN-12, RN-13, RN-15, RN-16, RN-22 | API-10, `negociacao_fechada` | CA-28 a CA-30 |
| RF-09 | RN-12, RN-14, RN-15, RN-16, RN-22 | API-11, `negociacao_fechada` | CA-31, CA-32 |
| RF-10 | RN-14, RN-21 | `executar_prazos`, `negociacao_fechada` | CA-33; sem cenário de erro: processo interno; o caso negativo é o prazo não vencido (CA-33) |
| RF-11 | RN-09, RN-16, RN-17 | API-04, API-05 | CA-34 a CA-36 |
| RF-12 | RN-09, RN-15, RN-16 | API-06, API-08 | CA-37, CA-38 |
| RF-13 | RN-16, RN-22 | API-04 a API-11 | CA-39 a CA-43 |
| RF-14 | RN-18 | API-07, API-09, API-10, API-11 | CA-44, CA-45 |
| RF-15 | RN-19, RN-20 | tópicos consumidos, `negociacao.dlt` | CA-46 a CA-49 |
| RF-16 | — | API-01, API-02, API-03 | CA-50 |
| RNF-01 a RNF-12 | RN-05, RN-07, RN-11, RN-18 a RN-21 | todas | CA-51 a CA-61, CA-17, CA-41 |

Atributos, projeções e envelopes: os atributos das entidades estão no §6.1; os campos derivados das respostas (`melhorLance`, `agora`, `acoesPermitidas`) e as projeções (resumo, oferta do leilão, lance, proposta) estão no §7.2; os campos de envelope estão no §7.4. Não aparecem na API: `melhorLanceId`, `correlationId`, `pedidoId` (oferta e direta), `OfertaLeilao.ordem`, `Lance.usuarioId`, `Lance.demandaId` e `Proposta.empresaId`.

## 13. Premissas, decisões e questões em aberto

Decisões do autor (Caio Almeida Oliveira). Fontes: conversas gravadas com o professor em 08/10/2026 e 12/11/2026 (P1 a P12 abaixo), kit de Infraestrutura, ESP-MRC-001 v0.5, ESP-PED-001 v2.1, Identity-Service, DR-BD-002, planejamento inicial do grupo (orientação, não regra).

Regras do professor citadas: P1 (o leilão existe quando a demanda supera a oferta; verificação do Mercado, fora deste serviço); P2 (o fornecedor decide abrir o leilão); P3 (leilão entre `dataInicio` e `dataFim`; compradores disputam); P4 (pedido temporário sem comprador na criação do leilão; Professor, 12/11/2026); P5 (leilão conjunto com lance inicial pela soma ponderada e rateio pelo volume; Professor, 08/10/2026 e 12/11/2026); P6 (negociação direta parte do preço único da oferta; pagamento depois); P7 (pedido cancelado devolve o estoque); P8 (versão básica primeiro); P9 (toda rota autenticada com escopo); P10 (lance no lote inteiro, um vencedor; Professor, 12/11/2026); P11 ("leilão reverso" não é leilão; Professor, 12/11/2026); P12 (oferta com preço ou quantidade zero não é negociada; Professor, 12/11/2026).

| ID | Tipo | Descrição | Decisão |
|---|---|---|---|
| D1 | Decisão (Caio, 2026-11-16) | Envelope dos eventos | Envelope do `CONTRACTS.md` do kit (§7.4), `source` `"negociacao-service"`, `aggregateId` = `negociacaoId`, chave Kafka = `aggregateId`. Motivo: contrato comum da turma. |
| D2 | Decisão (Caio, 2026-11-16) | Formato de erro | RFC 7807 com `codigo` e `correlationId`, igual à ESP-PED-001 §6.3 (§10). Motivo: Pedidos, Fornecimento e Logística usam o mesmo; a divergência com o RFC 9457 do Identity fica na INT-NEG-07. |
| D3 | Decisão (Caio, 2026-11-16; revista pelas D33 e D34 em 2026-11-19) | Autenticação | Validação local do JWT RS256 do Identity (JWKS com cache de 1 h); `view_mode` `SINGLE` com `tenant_id`; `BUYER` dá lance e participa de diretas; `SUPPLIER` participa de diretas; `CARRIER` e `TENANT_ADMIN` → 403; `roles`, `permissions`, `platform_role` e `act` ignorados; JWKS indisponível → 503 (§7.1). |
| D4 | Decisão (Caio, 2026-11-16) | Persistência | PostgreSQL 16, schema `negociacao`, role `svc_negociacao`, outbox transacional, `processed_events`, duas réplicas, lances serializados pelo bloqueio da linha do leilão (§5.1, §6.1). |
| D5 | Decisão (Caio, 2026-11-16) | Modos da v1 | `LEILAO_DIRETO` e `VENDA_DIRETA`; sem `LEILAO_REVERSO` (P11). |
| D6 | Decisão (Caio, 2026-11-16; revista pela D35 em 2026-11-19) | Status | Leilão `AGUARDANDO_RESERVAS` → `ABERTO` → `ENCERRADO`/`DESERTO` (o `AGUARDANDO_INICIO` virou `AGUARDANDO_RESERVAS`); direta `ABERTO` → `ENCERRADO`/`RECUSADO`/`EXPIRADO`; sem `CANCELADO` (§6.2). |
| D7 | Decisão (Caio, 2026-11-16) | Pedido temporário | Tópico novo `pedido_temporario_solicitado`, um por oferta do leilão e um por direta; o resultado da reserva vem de `pedido_atualizado` do Pedidos; não consome eventos de reserva do Fornecimento (RN-03, RN-04). |
| D8 | Decisão (Caio, 2026-11-13) | Encerramento | Automático em `dataFim`, por agendador idempotente e seguro com duas réplicas, que também processa o que venceu com o serviço parado; sem rota de encerramento manual e sem administrador (RN-10, RN-21). |
| D9 | Decisão (Caio, 2026-11-13; corrigida pela D28 em 2026-11-17) | Dinheiro | Número JSON com até 2 casas na API, `Decimal` no código, `NUMERIC(18,4)` no banco (ADR 0001). O lance é o valor total do lote (D28). |
| D10 | Decisão (Caio, 2026-11-16; ampliada pela D29; **substituída pela D39** em 2026-11-19) | Dados da oferta | Cópia local das ofertas a partir dos eventos do Fornecimento. Não vale mais: dono, produto, preço e quantidade vêm do evento do Mercado (D39). |
| D11 | Decisão (Caio, 2026-11-16; corrigida pelas D38 e D42 em 2026-11-19) | `negociacao_fechada` | Um evento por negociação com `status`, `tipo`, `compradorId`, `demandaId`, `produtoId` e `alocacoes` já rateadas; fora de `ENCERRADO`, `compradorId` nulo e `alocacoes []`; destino da entrega fica com o Pedidos (§7.4). |
| D12 | Decisão (Caio, 2026-11-16) | Tempo real | Consulta periódica dos `GET`; SSE e WebSocket fora do escopo. |
| D13 | Decisão (Caio, 2026-11-13) | Cancelamento | A v1 não cancela leilão; ele termina só em `dataFim`. O cancelamento de demanda não afeta o leilão. |
| D14 | Decisão (Caio, 2026-11-13) | Sigilo | Valores visíveis a todos; nome `"OCULTO"` para compradores concorrentes; fornecedores do leilão veem os nomes; eventos sem máscara (RN-09). |
| D15 | Decisão (Caio, 2026-11-13; rateio revisto pela D36 em 2026-11-19) | Leilão conjunto | Na v1 (ADR 0002): várias ofertas do mesmo produto, um pedido temporário por oferta, preço inicial pela soma e rateio pela participação no valor (D36). O lote é formado pelo Mercado (D33). |
| D16 | Decisão (Caio, 2026-11-16; **substituída pela D33** em 2026-11-19) | Quem abre o leilão | O fornecedor criava o leilão neste serviço, que publicava `leilao_iniciado`. Não vale mais: o Mercado inicia o leilão (D33). |
| D17 | Decisão (Caio, 2026-11-13) | Negociação direta | Proposta e contraproposta de preço a partir do preço da oferta, antes de fechar (RN-12). |
| D18 | Decisão (Caio, 2026-11-16) | Tabelas | `processo_negociacao` (nomes do DR-BD-002: `modo`, `status`, `data_inicio`, `data_fim`, `valor_reserva` = preço inicial), `oferta_leilao`, `lance`, `proposta`, `idempotency_keys`, `processed_events`, `outbox` (§6.1). A `oferta_referencia` saiu com a D39. |
| D19 | Decisão (Caio, 2026-11-13; **substituída pelas D33 e D36** em 2026-11-19) | Entrada no conjunto | Fornecedores entravam neste serviço até `dataInicio`, com rateio pelo volume. Não vale mais: o lote é formado no Mercado (D33) e o rateio segue a participação no valor (D36). |
| D20 | Decisão (Caio, 2026-11-13; abertura revista pela D34 em 2026-11-19) | Fluxo da direta | A abertura vem do Mercado e pede o pedido temporário; a primeira proposta é o preço da oferta (D34); as partes alternam contrapropostas; só a parte da vez aceita; qualquer uma recusa; 48 h sem resposta expira; forma de pagamento fora da v1 (RN-12 a RN-14). |
| D21 | Decisão (Caio, 2026-11-16; revista pela D35 em 2026-11-19) | Reserva da oferta | Só a oferta com reserva confirmada fica no lote; a recusada sai e o leilão segue com as demais; sem oferta confirmada, termina deserto (RN-04, RN-05). |
| D22 | Decisão (Caio, 2026-11-16; incremento revisto pela D37 em 2026-11-19) | Quem dá lance | Qualquer empresa com escopo `BUYER`, sem exigir demanda; primeiro lance ≥ preço inicial; os seguintes ≥ melhor lance + incremento mínimo (RN-07). |
| D23 | Decisão (Caio, 2026-11-16) | Liberação dos pedidos temporários | `negociacao_fechada` em todo final; o Pedidos cancela os pedidos temporários fora de `alocacoes`; sem tópico de liberação (RN-05, RN-10, RN-14). |
| D24 | Decisão (Caio, 2026-11-16; **substituída pela D33** em 2026-11-19) | Datas | `dataInicio` e `dataFim` informados pelo fornecedor. Não vale mais: vêm do `leilao_iniciado` (`iniciaEm` e `encerraEm`). |
| D25 | Decisão (Caio, 2026-11-16; revista pela D34 em 2026-11-19) | Conflito de interesse | Empresa com oferta no leilão não dá lance nele; 403 (RN-08). Na direta, quem impede a oferta própria é o Mercado (ESP-MRC-001 RN-14). |
| D26 | Decisão (Caio, 2026-11-16; **substituída pela D34** em 2026-11-19) | Diretas simultâneas | Várias diretas na mesma oferta, uma aberta por comprador e oferta. Não vale mais aqui: quem limita as diretas abertas é o Mercado, pelo saldo da demanda (ESP-MRC-001 RN-14 e RN-15); a reserva continua decidindo o estoque (RN-04). |
| D27 | Decisão (Caio, 2026-11-16) | Empresa ou usuário bloqueado | Não consome eventos do Identity; token revogado é aceito até expirar (15 min no máximo) (§7.1). |
| D28 | Decisão (Caio, 2026-11-17) | Valor do lance | O lance é o **valor total pelo lote**, não o preço unitário. Motivo: coerente com P5 (o valor pago é rateado), P10 (lance no lote inteiro), D19, D22 e a ADR 0001. Corrige a D9 e o C13 do vault. Na negociação direta, a proposta continua sendo preço unitário (P6). |
| D29 | Decisão (Caio, 2026-11-17; **substituída pela D39** em 2026-11-19) | Mudança de preço da oferta | Consumir `fornecimento_atualizado` para a cópia local. Não vale mais: o preço vem congelado no evento do Mercado (D39). |
| D30 | Decisão (Caio, 2026-11-17; aceita pelo Pedidos em 2026-11-19) | Sinal da reserva | Campos `negociacaoId`, `fornecimentoId` e `reservaSituacao` (`PENDENTE`, `CONFIRMADA`, `RECUSADA`, `ENCERRADA`) no `pedido_atualizado`; só `CONFIRMADA` e `RECUSADA` mudam a oferta (RN-04). O Pedidos publica os três em toda versão (ESP-PED-001 §7.4). |
| D31 | Decisão (Caio, 2026-11-18; revista pela D35 em 2026-11-19) | Lote vazio | Sem nenhuma oferta confirmada, o leilão termina `DESERTO` assim que a última reserva é resolvida e publica `negociacao_fechada`, sem esperar `dataFim` (RN-05). Motivo: libera o estoque mais cedo. |
| D32 | Decisão (Caio, 2026-11-18) | Autor do melhor lance | Sem regra de "líder": a empresa do melhor lance pode dar outro lance, respeitando o incremento mínimo. Motivo: menor regra que funciona; quem cobre o próprio lance só paga mais (RN-07, CA-19). |
| D33 | Decisão (grupo, 2026-11-19) | Quem inicia o leilão | O Mercado forma o lote pela sugestão e publica `leilao_iniciado`; este serviço abre o leilão com o mesmo `negociacaoId`, as ofertas, o preço inicial, o incremento, as participações e as datas do evento (RN-01). Saem a criação de leilão por HTTP, a entrada de outros fornecedores e a publicação de `leilao_iniciado`. Motivo: diagrama da arquitetura (o Mercado "inicia leilões quando necessário") e ESP-MRC-001 DG-51. Substitui D16, D19 e D24. |
| D34 | Decisão (Caio, 2026-11-19) | Abertura da direta | O comprador pede a direta no Mercado, que publica `modo_negociacao_definido`; este serviço abre com o mesmo `negociacaoId`; a primeira proposta é o preço da oferta, registrada como do fornecedor, e a vez é do comprador (RN-02). Sai a abertura por HTTP. Motivo: o Mercado controla o saldo da demanda e as diretas abertas (ESP-MRC-001 DG-52); com a abertura em dois lugares, a mesma oferta poderia ser negociada pelos dois caminhos. Revê a D20 e substitui a D26. |
| D35 | Decisão (Caio, 2026-11-19) | Reservas antes dos lances | O leilão fica `AGUARDANDO_RESERVAS` até nenhuma reserva ficar pendente; as recusadas saem do lote e o preço inicial, o incremento e as participações são recalculados; todas recusadas → `DESERTO` na hora; reserva ainda pendente em `dataFim` → `DESERTO` (RN-05, RN-10). Motivo: regra já escrita pelo Mercado e pelo Pedidos (ESP-MRC-001 DG-51, ESP-PED-001 D-01); `dataFim` limita a espera. Revê D6, D21 e D31. |
| D36 | Decisão (Caio, 2026-11-19) | Rateio | Pela participação no valor enviada pelo Mercado, com o centavo que sobra para a oferta de maior valor (RN-11). Motivo: nenhum fornecedor recebe menos que o próprio preço; pelo volume, num lote de 5 × 120.00 e 15 × 130.00 com lance de 2550.00, a oferta de 130.00 receberia 127.50 por unidade. Substitui o rateio pelo volume da D19 e da ADR 0002. |
| D37 | Decisão (Caio, 2026-11-19) | Incremento mínimo | Lance seguinte ≥ melhor lance + `incrementoMinimo` do `leilao_iniciado`; se uma oferta sai do lote, o incremento é recalculado na proporção do novo preço inicial, nunca abaixo de 0.01 (RN-05, RN-07). Motivo: o Mercado envia o valor (ESP-MRC-001 DG-20), e a proporção reproduz a regra dele sem campo novo no contrato. Revê a D22. |
| D38 | Decisão (Caio, 2026-11-19) | Demanda no lance | `demandaId` opcional no lance, gravado sem conferência e enviado no `negociacao_fechada` do leilão vencido (RN-07, RN-10). Motivo: o Mercado só abate o saldo da demanda com esse campo (ESP-MRC-001 DG-27) e confere a demanda ao consumir. |
| D39 | Decisão (Caio, 2026-11-19) | Dados da oferta | Sem cópia local: dono, produto, preço e quantidade vêm do evento do Mercado; não consome `fornecimento_criado`, `fornecimento_atualizado` nem `estoque_atualizado`. Motivo: o Mercado já valida a oferta, e a reserva do Pedidos é a checagem final do estoque. Substitui D10 e D29. |
| D40 | Decisão (Caio, 2026-11-19) | Evento que abre o leilão | Só o `leilao_iniciado`; `modo_negociacao_definido` com `LEILAO_DIRETO` é registrado como processado e ignorado (RN-02). Motivo: o `leilao_iniciado` traz tudo o que o leilão usa; os dois eventos têm a mesma chave e chegam em ordem. |
| D41 | Decisão (Caio, 2026-11-19, combinada com o Pedidos) | Fechamento recusado pelo Pedidos | Sem republicação automática do `negociacao_fechada` que o Pedidos mandar para a DLT com `FECHAMENTO_INCOMPATIVEL`: o fechamento só usa `pedidoId`, oferta e quantidade recebidos do próprio Pedidos, então a recusa indica defeito e é tratada pela operação (INT-NEG-13). Este serviço não é avisado da recusa; quem precisar identificá-la consulta a `pedidos.dlt`. O Mercado também ignora fechamento republicado (ESP-MRC-001 DG-53). |
| D42 | Decisão (Caio, 2026-11-19) | Contrato com Pedidos e Mercado | `tipo` = `"VENDA_DIRETA"` (não `"DIRETA"`) em `pedido_temporario_solicitado` e `negociacao_fechada`; `compradorId` nulo fora de `ENCERRADO`. Motivo: schemas dos consumidores (ESP-PED-001 §7.3, ESP-MRC-001 §7.3); o formato anterior iria para a DLT do Pedidos. |
| PR-04 | Premissa (derivada) | Aceite e vez | Só a parte da vez aceita (aceita a proposta da outra parte); o aceite exige reserva confirmada, para não fechar venda sem estoque reservado; o prazo de 48 h conta da última proposta. |
| PR-05 | Premissa (alinhamento) | Quantidade | Decimal com até 2 casas, como no Fornecimento (`NUMERIC(12,2)`) e no Pedidos; o DR-BD-002 usa inteiro (INT-NEG-06). |
| PR-06 | Premissa (alinhamento) | `valorUnitario` no leilão | `valorTotal / quantidade` de cada alocação, com 2 casas; `valorTotal` é o valor que vale (RN-11). O Pedidos grava os dois sem recalcular (ESP-PED-001 RN-09). |
| PR-07 | Premissa (Identity, revisão `800f2d8`) | Tokens de serviço | `client_id` e `token_use = "service"` são ignorados; o token vale pelo `tenant_id` e `active_scope`, como os demais. Sem tolerância (`leeway`) na expiração. |
| PR-08 | Premissa (UX-04) | Tela de negociação | A API devolve prazos absolutos do servidor (`dataInicio`, `dataFim`, `expiraEm`), `agora` e `acoesPermitidas`, para a tela calcular a contagem regressiva e habilitar as ações. |
| INT-NEG-01 | Questão de integração → **Pedidos** (Daniel) | `negociacao_fechada` e pedidos temporários | **Fechada (2026-11-19):** a ESP-PED-001 v2.1 consome `pedido_temporario_solicitado`, cria um pedido por oferta (`UNIQUE(negociacao_id, fornecimento_id)`), aceita `alocacoes` e os status `RECUSADO` e `EXPIRADO`, e cancela os pedidos fora de `alocacoes`. Ajustes deste lado na D42. |
| INT-NEG-02 | Questão de integração → **Pedidos** (Daniel) | `pedido_atualizado` | **Fechada (2026-11-19):** o Pedidos publica `negociacaoId`, `fornecimentoId` e `reservaSituacao` em toda versão (D30). |
| INT-NEG-03 | Questão de integração → **Mercado** (Brunno) | Abertura das negociações | **Fechada (2026-11-19), em resposta à INT-MRC-01 da ESP-MRC-001:** (a) aceito: abre as negociações consumindo `leilao_iniciado` e `modo_negociacao_definido` (D33, D34, D40); (b) aceito (D35); (c) aceito (D33); (d) aceito (D36); (e) aceito, com recálculo proporcional (D37); (f) aceito no lance e no fechamento do leilão (D38); (g) aceito (D33); (h) mantido. Também: `tipo` `VENDA_DIRETA` e `compradorId` nulo fora de `ENCERRADO` (D42). |
| INT-NEG-04 | Questão de integração → **Fornecimento** | Consumo das ofertas | **Encerrada (2026-11-19):** este serviço não consome eventos do Fornecimento (D39). Nada a pedir. |
| INT-NEG-05 | Questão de integração → **Infra** (Pedro) | Tópicos e catálogo | Criar `pedido_temporario_solicitado` (3 partições, chave `negociacaoId`); registrar este serviço como consumidor de `leilao_iniciado`, `modo_negociacao_definido` e `pedido_atualizado`, e o Mercado como consumidor de `negociacao_fechada`; o produtor de `leilao_iniciado` continua sendo o Mercado; ACLs; rota do gateway para as duas réplicas. Situação: a pedir, junto com a INT-MRC-03 do Mercado. |
| INT-NEG-06 | Questão de integração → **Banco de Dados** | DDL do schema `negociacao` | Validar as tabelas do §6.1, `NUMERIC(18,4)` (ADR 0001), quantidade `NUMERIC(12,2)` (o DR-BD-002 usa `INT`), participação `NUMERIC(6,4)` e a tabela `lance`, que o DR-BD-002 não prevê no modelo único em `public`. Situação: pendente. |
| INT-NEG-07 | Questão de integração → **Autenticação** | Erro e contexto | Formato de erro `codigo`/`correlationId` (RFC 7807) × `code`/`correlation_id` (RFC 9457, D-ECO-07), mesma pendência da INT-PED-08; 403 `CONTEXTO_NAO_SELECIONADO` em vez de 401 para token sem empresa. O seed tem uma só empresa fornecedora e uma compradora sem oferta: **não será pedida mudança** (decisão do Caio, 2026-11-16); a demonstração usa o emissor de tokens de teste. Situação: registrada. |
| INT-NEG-08 | Questão de integração → **UX/UI** | Tela de negociação | A tela usa consulta periódica (D12), `agora`, `acoesPermitidas`, prazos absolutos e `"OCULTO"`; a referência UX-04 mostra "leilão reverso" e "selecionar fornecedor", que não existem neste serviço. Situação: a comunicar. |
| INT-NEG-09 | Questão de integração → **Demanda** | Nenhuma dependência | Este serviço não consome eventos da Demanda; o cancelamento de demanda não afeta leilão (D13). Situação: informativa. |
| INT-NEG-10 | Questão → **professor** | Confirmação de regras | Confirmar que o lance é o valor total do lote (D28) e o rateio pela participação no valor (D36). Situação: a perguntar. |
| INT-NEG-11 | Questão → **QA** | Gate G1 | Submeter esta ESP ao gate G1 e combinar a suíte de aceitação em memória (§5.1) e a de integração (CA-55 a CA-61). Situação: a submeter. |
| INT-NEG-12 | Questão de integração → **Mercado** (Brunno) | Tamanho dos IDs | O Mercado aceita `produtoId`, `fornecimentoId`, `fornecedorId`, `compradorId` e `demandaId` com até 128 caracteres; este serviço e o Pedidos, até 64, o tamanho das colunas de ID no banco do kit da Infra (`database/01-init.sql`, `VARCHAR(64)`). Proposta: o Mercado limitar a 64. Até lá, evento com ID maior vai para `negociacao.dlt` (§7.3). Situação: a comunicar. |
| INT-NEG-13 | Questão de integração → **Pedidos** (Daniel) | Fechamento recusado | Resposta à pendência do README do Pedidos: não há republicação automática de `negociacao_fechada` recusado com `FECHAMENTO_INCOMPATIVEL` (D41); a correção é manual, pela operação, e a recusa é identificada pela `pedidos.dlt`. Situação: **fechada** em 2026-11-19 com o Daniel. |

### 13.1 Casos de borda obrigatórios

| Caso de borda | Decisão |
|---|---|
| String vazia ou só com espaços em campos obrigatórios | Inválida: `422 VALIDACAO_INVALIDA` na API; mensagem inválida (DLT) no consumo. |
| Cabeçalho opcional presente porém vazio | `Idempotency-Key` e `X-Correlation-Id` vazios equivalem a ausentes. |
| Mensagem duplicada **e** inválida | A validação vem antes da deduplicação: vai para a DLT de novo (RN-19, RN-20). |
| Campo de chave/ID com tipo inesperado | Na API: `422 VALIDACAO_INVALIDA`. No consumo: mensagem inválida. Na chave da DLT, `eventId` não string é convertido com `str`; ausente, nulo ou vazio → `"desconhecido"` (§7.4). |
| Data/hora sem fuso horário | Na API e no consumo: inválida. `clock.now()` sem fuso: tratar como UTC. |
| Falha da porta de publicação / banco | Banco: `RepositorioIndisponivel` → 503 na API e novas tentativas no consumo (RN-19). `publish` dentro da transação: no adaptador real é a gravação na outbox, que falha junto com a transação; falhas do envio ao broker são tratadas pelo publicador da outbox (§5.1). Falha ao publicar na DLT no adaptador real: o offset não é confirmado e a mensagem é reprocessada. |
| Rotas e métodos fora do contrato | Resposta padrão do framework (§10). |
| Número monetário com mais de 2 casas ou como string | `422 VALIDACAO_INVALIDA`. |
| `pedido_atualizado` de negociação já encerrada | Ignorado, sem DLT (RN-04). |
| `pedido_atualizado` depois de o leilão terminar `DESERTO` com reserva pendente | Ignorado (RN-04); o Pedidos cancela o pedido pelo `negociacao_fechada`. |
| Evento do Mercado com `negociacaoId` já conhecido | Ignorado, sem DLT, mesmo com outro `eventId` ou outros valores (RN-01, RN-02). |
| ID de oferta, fornecedor, produto ou comprador com mais de 64 caracteres vindo do Mercado | Mensagem inválida (DLT), porque o Pedidos não aceitaria o `pedido_temporario_solicitado` (§7.3, INT-NEG-12). |
| Prazo vencido antes de `executar_prazos` rodar | As ações já são recusadas comparando com `agora` (RN-06, RN-12); o estado muda na próxima execução. |

## 14. Checklist de prontidão (Definition of Ready) — preenchido pelo QA

Pré-conferência do autor antes do gate G1; o QA confirma ou desmarca cada item.

- [x] Nenhum `{{ }}` restante; comentários de orientação removidos.
- [x] Todo RF tem enunciado EARS, prioridade e CAs de sucesso e erro quando aplicáveis; exceções justificadas na matriz (RF-04, RF-06, RF-10).
- [x] Todo CA usa valores concretos e resultado observável.
- [x] Todo código de erro citado em CA existe no §10 e vice-versa.
- [x] Ordem de verificação de erros definida para cada endpoint.
- [x] Campos de entidade, projeções de resposta e campos derivados estão mapeados; exemplos JSON respeitam os contratos.
- [x] Todo tópico citado tem produtor/consumidor, chave e exemplo.
- [x] Toda dependência externa está modelada como porta (§5.1).
- [x] Todo RNF tem métrica, limiar e forma de verificação.
- [x] Matriz de rastreabilidade completa (nenhum RF sem CA, nenhum CA órfão).
- [x] Não há palavras vagas sem quantificação ("rápido", "adequado", "etc.", "deve tratar erros").
- [ ] Seção 13 sem questões abertas e tabela 13.1 (casos de borda) toda decidida. A tabela 13.1 está decidida; continuam abertas as questões de integração INT-NEG-05 (tópico novo e consumidores na Infra), INT-NEG-06 (DDL com o Banco), INT-NEG-12 (tamanho dos IDs com o Mercado). INT-NEG-01 a INT-NEG-04 e INT-NEG-13 foram fechadas em 2026-11-19. Neste serviço vale o contrato desta ESP; os CAs de integração com os parceiros só rodam depois do aceite.
- [x] Comentários `# DECISÃO:` da última geração (se houver) analisados e incorporados como regra (não houve geração anterior).

## 15. Histórico de versões

| Versão | Data | Autor | Mudança |
|---|---|---|---|
| 1.0 | 2026-11-17 | Caio Almeida Oliveira | Criação, a partir das decisões D1 a D30 do autor e das regras do professor (08/10 e 12/11/2026), com os contratos em `docs/negociacao/contratos/`. |
| 1.1 | 2026-11-18 | Caio Almeida Oliveira | D31: leilão sem oferta confirmada em `dataInicio` termina `DESERTO` na hora (RF-04, RF-06, RN-09, RN-14, CA-18; sai o atributo `loteFixado`). D32: confirma que o autor do melhor lance pode cobri-lo (RN-11). |
| 1.2 | 2026-11-19 | Caio Almeida Oliveira | Alinhamento com a ESP-MRC-001 v0.5 e a ESP-PED-001 v2.1 (D33 a D42): o Mercado abre o leilão (`leilao_iniciado`) e a negociação direta (`modo_negociacao_definido`) e este serviço deixa de criá-los por HTTP; status `AGUARDANDO_RESERVAS` e liberação dos lances com as reservas resolvidas; rateio pela participação no valor; incremento mínimo; `demandaId` no lance e no fechamento; sem cópia local das ofertas; `tipo` `VENDA_DIRETA` e `compradorId` nulo fora de `ENCERRADO`; `demandaId` com até 64 caracteres. APIs, RFs, RNs e CAs renumerados; INT-NEG-01 a INT-NEG-04 fechadas; INT-NEG-12 nova; INT-NEG-13 nova e fechada com o Pedidos. |
