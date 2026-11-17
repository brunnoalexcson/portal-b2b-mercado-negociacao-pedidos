# ESP-NEG-001 — Especificação de Requisitos: Serviço de Negociação e Leilão

## 0. Metadados

| Campo | Valor |
|---|---|
| Identificador | ESP-NEG-001 |
| Versão | 1.0 |
| Status | Rascunho |
| Serviço / Bounded context | `negociacao-service` (microsserviço 6 do Portal B2B Distribuído): Negociação e Leilão. Porta `8106`, rotas `/api/v1/negociacoes`, schema PostgreSQL `negociacao`, role `svc_negociacao` |
| Autores (PO / Dev / QA) | PO e Dev: Caio Almeida Oliveira (grupo Mercado, Negociação/Leilão e Pedidos). QA: Grupo de Teste e Qualidade (revisão no gate G1) |
| Data | 2026-11-17 |
| Documentos relacionados | ESP-PED-001 (`docs/pedidos/requisitos.md`); contratos de eventos em `docs/contratos/negociacao/`; kit de Infraestrutura (`CONTRACTS.md` e `ORIENTACOES-ARQUITETURA-GRUPOS.md`, revisão `630515c`); ESP-FRN-001 e contrato de eventos v2.0 do Fornecimento (revisão `56da541`); Identity-Service (revisão `800f2d8`); DR-BD-002 v1.0; modelo ERS-LLM v1.2 do QA (revisão `efc05b6`) |

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

Conduzir as negociações do portal: leilões em que compradores disputam o lote de uma ou mais ofertas do mesmo produto, e negociações diretas de preço entre um comprador e um fornecedor. Ao fim, o serviço informa o resultado ao Pedidos, que efetiva ou cancela os pedidos temporários criados na abertura.

### 2.2 Posição na arquitetura

O fornecedor cria o leilão por HTTP neste serviço, e outros fornecedores do mesmo produto entram com suas ofertas até `dataInicio`. Cada oferta que entra num leilão, e cada negociação direta aberta, pede ao Pedidos um **pedido temporário** sem comprador, que reserva o estoque no Fornecimento. O Pedidos informa o resultado da reserva por `pedido_atualizado`. Entre `dataInicio` e `dataFim`, compradores dão lances no lote inteiro. Em `dataFim`, o serviço encerra o leilão sozinho, rateia o valor entre as ofertas e publica `negociacao_fechada`, que o Pedidos usa para efetivar os pedidos das ofertas vendidas e cancelar os demais. O serviço mantém uma cópia local das ofertas a partir dos eventos do Fornecimento. Ele não consome eventos do Mercado, da Demanda nem do Identity.

```text
Fornecimento ──[fornecimento_criado, fornecimento_atualizado, estoque_atualizado]──► Negociação
Pedidos      ──[pedido_atualizado]────────────────────────────────────────────────► Negociação
Portal ─► Gateway Nginx ─REST /api/v1/negociacoes (JWT do Identity)───────────────► Negociação
Identity     ──JWKS (HTTP)────────────────────────────────────────────────────────► Negociação

Negociação ──[pedido_temporario_solicitado]──► Pedidos (cria o pedido sem comprador; reserva via pedido_criado → Fornecimento)
Negociação ──[leilao_iniciado]───────────────► Pedidos, front-end
Negociação ──[lance_realizado]───────────────► front-end, auditoria
Negociação ──[negociacao_fechada]────────────► Pedidos (efetiva os pedidos de alocacoes e cancela os demais)
Negociação ──[negociacao.dlt]────────────────► operação, QA
```

Passos do fluxo principal: (1) o Fornecimento publica as ofertas; (2) o Mercado sugere o leilão ao fornecedor quando a demanda supera a oferta (fora deste serviço); (3) o fornecedor cria o leilão ou o comprador abre a negociação direta **neste serviço**; (4) pedidos temporários e reserva; (5) lances ou propostas; (6) fechamento e `negociacao_fechada`; (7) o Pedidos segue com frete, venda e entrega.

## 3. Escopo

### 3.1 Dentro do escopo

- Criação de leilão (`LEILAO_DIRETO`) pelo fornecedor, com a sua oferta, a quantidade, `dataInicio` e `dataFim`.
- Entrada de outros fornecedores no leilão até `dataInicio`, com ofertas do mesmo produto (leilão conjunto).
- Um pedido temporário por oferta que entra num leilão e por negociação direta aberta (`pedido_temporario_solicitado`).
- Registro da confirmação ou da recusa da reserva a partir de `pedido_atualizado`.
- Fixação do lote e do preço inicial em `dataInicio`.
- Lances de compradores no lote inteiro, com sigilo do nome dos concorrentes.
- Encerramento automático do leilão em `dataFim`, com vencedor (`ENCERRADO`) ou sem lance (`DESERTO`), e rateio do valor entre as ofertas do lote.
- Negociação direta (`VENDA_DIRETA`) aberta pelo comprador, com contrapropostas de preço, aceite, recusa e expiração em 48 h.
- Consultas de negociações, lances e propostas, com paginação.
- Cópia local das ofertas a partir de `fornecimento_criado`, `fornecimento_atualizado` e `estoque_atualizado`.
- Autenticação por JWT do Identity-Service, erros RFC 7807, `X-Correlation-Id`, `Idempotency-Key`, `/health`, `/health/ready`, `/metrics`.
- Persistência no schema `negociacao`, outbox transacional, consumo idempotente (`processed_events`), DLT `negociacao.dlt` e execução em duas réplicas.

### 3.2 Fora do escopo (não implementar)

- Comparar oferta × demanda, sugerir leilão ao fornecedor e verificar se a demanda supera a oferta (Mercado). Este serviço cria o leilão sem essa verificação.
- "Leilão reverso" (`LEILAO_REVERSO`): não é leilão; mostrar o menor preço quando a oferta supera a demanda é do Mercado ou da vitrine.
- Consumir `modo_negociacao_definido`, `leilao_iniciado` ou qualquer evento do Mercado, da Demanda ou do Identity (`tenant_bloqueado`, `usuario_bloqueado`, `sessao_comprometida`).
- Criar, efetivar ou cancelar pedidos; estados do pedido; destino da entrega; nota fiscal (Pedidos).
- Reservar, baixar ou liberar estoque; alterar preço ou saldo de oferta (Fornecimento).
- Demandas, frete, cotação, emissão de token, cadastro de empresas e usuários, telas.
- DDL oficial do banco (a proposta do schema `negociacao` é deste serviço; a aprovação é do grupo de Banco).
- Cancelamento de leilão; encerramento manual ou antecipado; papel de administrador da plataforma; forma de pagamento; incremento mínimo de lance; lance para parte do lote; retirada de oferta de um leilão; edição ou exclusão de lances e propostas; SSE ou WebSocket.
- Bloqueio imediato de empresa ou usuário revogado no Identity: o token é aceito até expirar.

## 4. Glossário

| Termo | Definição precisa |
|---|---|
| Agora | Valor de `clock.now()` (porta `Clock`). O serviço nunca lê o relógio do sistema diretamente. |
| Instante | Data e hora em UTC, ISO-8601 com milissegundos e sufixo `Z` na saída (ex.: `"2026-11-20T13:00:00.000Z"`). Na entrada, aceita qualquer ISO-8601 **com** fuso (`Z` ou `±hh:mm`) e converte para UTC; sem fuso é inválido. |
| Valor monetário | Reais (BRL). Na API e nos eventos: número JSON (nunca string), maior que zero, com no máximo 2 casas decimais e no máximo 12 dígitos na parte inteira. Na saída, sempre com exatamente 2 casas (`2550` recebido → `2550.00` devolvido). No código, `decimal.Decimal`; `float` é proibido. No banco, `NUMERIC(18,4)`. |
| Quantidade | Número JSON maior que zero com no máximo 2 casas decimais e no máximo 10 dígitos na parte inteira, na unidade do produto. Saída com exatamente 2 casas. `Decimal` no código, `NUMERIC(12,2)` no banco. |
| Empresa | Valor do claim `tenant_id` do token. É a identidade usada em `compradorId`, `fornecedorId` e `criadoPor`. |
| Escopo | Valor do claim `active_scope`: `BUYER` (comprador) ou `SUPPLIER` (fornecedor) nas rotas de negócio. |
| Oferta | Um fornecimento do Fornecimento, identificado por `fornecimentoId`, com dono (`fornecedorId`), produto, preço unitário e saldo disponível. |
| Cópia local da oferta | Registro `OfertaReferencia` (§6.1) mantido a partir dos eventos do Fornecimento. É a única fonte de dono, produto, preço e saldo que este serviço consulta. |
| Oferta negociável | Oferta presente na cópia local com `preco > 0` e `quantidadeDisponivel > 0`. |
| Leilão | Negociação com `modo = "LEILAO_DIRETO"`. Compradores disputam um lote; vence o maior lance. |
| Oferta do leilão | Registro `OfertaLeilao` (§6.1): uma oferta que entrou num leilão, com quantidade, preço unitário copiado da oferta no momento da entrada e situação da reserva. |
| Lote | Conjunto das ofertas do leilão com `noLote = true`, fixado uma única vez, em `dataInicio`. `quantidadeTotal` = soma das quantidades do lote. |
| Preço inicial | `precoInicial` = Σ (`quantidade` × `precoUnitario`) das ofertas do lote, com 2 casas. Valor mínimo do primeiro lance. |
| Lance | Valor monetário **total** oferecido por um comprador pelo lote inteiro. |
| Melhor lance | Lance de maior valor do leilão. Pela RN-11, é sempre o último lance aceito. |
| Entrada aberta | `status == "AGUARDANDO_INICIO"` **e** `agora < dataInicio`. |
| Leilão aceitando lances | `status == "ABERTO"` **e** `agora < dataFim`. |
| Fornecedor do leilão | Empresa dona de alguma oferta do leilão, em qualquer situação. |
| Pedido temporário | Pedido criado pelo Pedidos sem comprador, a partir de `pedido_temporario_solicitado`, que reserva o estoque de uma oferta. Este serviço só conhece o seu `pedidoId`, recebido em `pedido_atualizado`. |
| Reserva | Situação do pedido temporário: `PENDENTE` (pedido enviado), `CONFIRMADA` ou `RECUSADA` (informadas por `pedido_atualizado`). |
| Rateio | Divisão do valor do lance vencedor entre as ofertas do lote, na proporção da quantidade de cada uma (RN-15). |
| Negociação direta | Negociação com `modo = "VENDA_DIRETA"`: um comprador e uma oferta, com troca de propostas de preço unitário. |
| Proposta | Preço unitário proposto numa negociação direta por uma das partes. A primeira é do comprador, na abertura. |
| Vez | Parte (`BUYER` ou `SUPPLIER`) que deve responder à última proposta. Só a parte da vez propõe ou aceita. |
| Prazo de resposta | `expiraEm` = instante da última proposta + 48 h. Negociação direta aceitando ações: `status == "ABERTO"` **e** `agora < expiraEm`. |
| Participante | Na negociação direta: o comprador (escopo `BUYER` e empresa = `compradorId`) ou o fornecedor (escopo `SUPPLIER` e empresa = `fornecedorId`). |
| Conflito de interesse | Empresa negociando com oferta própria: dar lance num leilão em que é fornecedor do leilão, ou abrir negociação direta com oferta própria. |
| `"OCULTO"` | String literal que substitui o `compradorId` nas respostas, conforme o sigilo (RN-13). |

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
| Formato monetário | Valor monetário (§4). Arredondamento sempre explícito, conforme RN-09 e RN-15. |
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
    # cópia local das ofertas
    def obter_oferta_referencia(self, fornecimento_id: str) -> dict | None: ...
    def salvar_oferta_referencia(self, oferta: dict) -> None: ...
    # negociações
    def inserir_negociacao(self, negociacao: dict) -> None: ...
    def obter_negociacao(self, negociacao_id: str, bloquear: bool = False) -> dict | None: ...
    def atualizar_negociacao(self, negociacao: dict) -> None: ...
    def listar_negociacoes(self, empresa: str, escopo: str, modo: str | None, status: str | None,
                           offset: int, limite: int) -> tuple[list[dict], int]: ...
    def obter_direta_aberta(self, fornecimento_id: str, comprador_id: str) -> dict | None: ...
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
- `create_app` chama, durante a criação da aplicação, `event_bus.subscribe` para os tópicos `pedido_atualizado`, `fornecimento_criado`, `fornecimento_atualizado` e `estoque_atualizado`, um handler para cada. O handler recebe a mensagem completa (envelope, §7.3) como `dict` e **nunca** propaga exceção.
- `transacao()`: tudo o que é feito no repositório dentro de `with repositorio.transacao():` é gravado de forma atômica; se uma exceção sair do bloco, nada é gravado. Cada requisição HTTP de escrita, cada mensagem consumida e cada negociação processada por `executar_prazos` usam uma única transação.
- Os dicionários trocados com o repositório usam as chaves dos atributos do §6.1, com instantes em `datetime` UTC e valores monetários e quantidades em `Decimal`.
- `obter_negociacao(id, bloquear=True)` bloqueia a negociação até o fim da transação (no PostgreSQL, `SELECT ... FOR UPDATE`). Toda escrita sobre uma negociação existente (oferta, lance, proposta, aceite, recusa, reserva, prazo) obtém a negociação com `bloquear=True` antes de validar as regras de domínio. É isso que serializa os lances do mesmo leilão entre as duas réplicas.
- `listar_negociacoes` devolve `(itens, total)`: no máximo `limite` itens a partir de `offset`, em ordem de `criadoEm` decrescente e, em empate, `negociacaoId` decrescente; `total` conta todos os itens do filtro. Entram todas as negociações com `modo = "LEILAO_DIRETO"` e as com `modo = "VENDA_DIRETA"` em que (`escopo == "BUYER"` e `compradorId == empresa`) ou (`escopo == "SUPPLIER"` e `fornecedorId == empresa`). `modo` e `status` `None` = sem filtro.
- `obter_direta_aberta` devolve a negociação `VENDA_DIRETA` com `status = "ABERTO"` daquela oferta e daquele comprador, ou `None`.
- `listar_vencidas(agora, limite)` devolve até `limite` identificadores de negociações em que: (`modo = "LEILAO_DIRETO"`, `status = "AGUARDANDO_INICIO"`, `loteFixado = false` e `dataInicio <= agora`) ou (`modo = "LEILAO_DIRETO"`, `status` ∈ {`AGUARDANDO_INICIO`, `ABERTO`} e `dataFim <= agora`) ou (`modo = "VENDA_DIRETA"`, `status = "ABERTO"` e `expiraEm <= agora`), em ordem do prazo mais antigo.
- `listar_ofertas_leilao` devolve as ofertas do leilão em ordem de `entrouEm` crescente e, em empate, `fornecimentoId` crescente. `listar_lances` e `listar_propostas` devolvem em ordem de `sequencia` crescente, com o total.
- `salvar_oferta_referencia` insere ou substitui pelo `fornecimentoId`. `salvar_idempotencia` insere ou substitui pelo escopo.
- Qualquer método do repositório pode lançar `RepositorioIndisponivel`; o serviço responde `503 DEPENDENCIA_INDISPONIVEL` na API e, no consumo, aplica as novas tentativas da RN-25. Qualquer outra exceção vira `500 ERRO_INTERNO` na API.
- `publish` é chamado **dentro** da transação da mudança, como último passo antes do fim do bloco, e nunca chama handlers de forma síncrona. Ordem de toda escrita: validar → alterar o estado → publicar. Falha lançada por `publish` está fora do escopo das portas (§13.1).
- `clock.now()` sempre devolve `datetime` com fuso; se vier sem fuso, tratar como UTC. `clock.sleep(segundos)` é a única forma de esperar; nos testes, o dublê retorna na hora e registra o valor.
- `chaves_publicas.obter(kid)` devolve a chave pública RSA (objeto aceito por `jwt.decode`) ou `None` se o `kid` for desconhecido. Se lançar exceção, a resposta é `503 DEPENDENCIA_INDISPONIVEL`. O serviço nunca faz requisição HTTP para obter chaves fora desse adaptador.
- `event_bus.disponivel()` e `repositorio.disponivel()` alimentam `/health/ready`.

Comportamento dos adaptadores reais (não usados pela suíte de aceitação em memória):

- **Repositório (`postgres.py`):** psycopg 3 com `psycopg_pool.ConnectionPool(max_size=5)`, role `svc_negociacao`, só o schema `negociacao`. `transacao()` abre uma conexão do pool numa variável de contexto e faz commit ao sair do bloco sem exceção. Erros de conexão viram `RepositorioIndisponivel`.
- **Outbox (`kafka.py`):** dentro da transação, `publish` grava o envelope em `negociacao.outbox` pela mesma conexão. Um publicador em cada réplica lê o registro pendente mais antigo com `FOR UPDATE SKIP LOCKED`, envia com `acks=all` e a chave do registro, marca `confirmado_em` e, em falha, reagenda com espera `min(30, 2^tentativas)` segundos. Todo reenvio usa o mesmo `eventId`.
- **Consumidor (`kafka.py`):** `aiokafka` com grupo `negociacao-service-v1`, `enable_auto_commit=False`. Cada mensagem é decodificada como JSON; bytes que não são JSON vão direto para `negociacao.dlt` (RN-26). O handler roda fora do laço de eventos (`asyncio.to_thread`). O offset é confirmado só depois do retorno do handler. `processed_events` (`source`, `event_id`) é gravado na mesma transação do efeito.
- **Agendador:** cada réplica chama `executar_prazos` a cada 1 s. A segurança com duas réplicas vem do `bloquear=True` e da nova verificação do estado dentro da transação (RN-27).
- **JWKS (`jwks.py`):** `jwt.PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600, timeout=2)`. Falha de rede lança exceção (→ 503); `kid` ausente no conjunto devolve `None` (→ 401).
- **Logs:** JSON em `stdout` com `timestamp`, `level`, `service` (`negociacao-service`), `correlationId`, `message` e, quando conhecidos, `negociacaoId` e `eventId`. Sem `print()`. Nunca registrar o token.

## 6. Modelo de domínio

### 6.1 Entidades

#### Negociacao (tabela `negociacao.processo_negociacao`)

| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| negociacaoId | UUID (string) | Sim | UUID4 gerado pelo serviço na criação. Coluna `id`. |
| modo | enum | Sim | `"LEILAO_DIRETO"` ou `"VENDA_DIRETA"`, do corpo da criação. Nos eventos, `tipo` = `"LEILAO"` ou `"DIRETA"`, respectivamente. |
| status | enum | Sim | Leilão: `"AGUARDANDO_INICIO"`, `"ABERTO"`, `"ENCERRADO"`, `"DESERTO"`. Direta: `"ABERTO"`, `"ENCERRADO"`, `"RECUSADO"`, `"EXPIRADO"`. |
| produtoId | string 1–64 | Sim | Produto da oferta que criou a negociação (cópia local). |
| dataInicio | instante | Sim | Leilão: do corpo. Direta: igual a `criadoEm` no banco (coluna `NOT NULL` do DR-BD-002), exposta como `null` no resumo da API-05. |
| dataFim | instante | Leilão | Leilão: do corpo. Direta: `null`. |
| loteFixado | booleano | Sim | `false` na criação; `true` depois da fixação do lote (RN-09). Direta: sempre `false`. |
| quantidadeTotal | quantidade | Não | Leilão: definida na fixação do lote; `null` antes ou se o lote ficou vazio. Direta: `null`. |
| precoInicial | valor monetário | Não | Leilão: definido na fixação do lote (RN-09); `null` antes ou se o lote ficou vazio. Coluna `valor_reserva` (DR-BD-002). Direta: `null`. |
| melhorLanceId | UUID | Não | Último lance aceito; `null` sem lances. |
| totalLances | inteiro ≥ 0 | Sim | Inicial 0. |
| compradorId | string 1–64 | Não | Leilão: empresa do lance vencedor, preenchida no encerramento `ENCERRADO`. Direta: empresa do comprador que abriu. |
| fornecimentoId | string 1–64 | Direta | Direta: oferta negociada. Leilão: `null`. |
| fornecedorId | string 1–64 | Direta | Direta: dono da oferta (cópia local). Leilão: `null`. |
| quantidade | quantidade | Direta | Direta: do corpo. Leilão: `null`. |
| precoOferta | valor monetário | Direta | Direta: preço da cópia local na abertura. Leilão: `null`. |
| precoAtual | valor monetário | Direta | Direta: preço da última proposta. Leilão: `null`. |
| vez | enum | Direta | `"BUYER"` ou `"SUPPLIER"`. Direta: `"SUPPLIER"` na abertura. Leilão: `null`. |
| expiraEm | instante | Direta | Direta: instante da última proposta + 48 h. Leilão: `null`. |
| reservaSituacao | enum | Direta | `"PENDENTE"`, `"CONFIRMADA"` ou `"RECUSADA"`. Direta: `"PENDENTE"` na abertura. Leilão: `null` (a reserva fica em cada `OfertaLeilao`). |
| pedidoId | string 1–64 | Não | Direta: pedido temporário, recebido em `pedido_atualizado` `CONFIRMADA`. Leilão: `null`. |
| totalPropostas | inteiro ≥ 0 | Sim | Direta: 1 na abertura. Leilão: 0. |
| criadoPor | string 1–64 | Sim | Empresa do token que criou. |
| usuarioCriador | string | Sim | `sub` do token que criou. |
| correlationId | string 1–128 | Sim | Correlação da requisição de criação; reutilizada nos eventos publicados por `executar_prazos`. |
| criadoEm | instante | Sim | `clock.now()` na criação. |
| encerradoEm | instante | Não | `clock.now()` quando a negociação chega a um estado final. |

#### OfertaLeilao (tabela `negociacao.oferta_leilao`, chave `(negociacaoId, fornecimentoId)`)

| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| negociacaoId | UUID | Sim | Leilão ao qual pertence. |
| fornecimentoId | string 1–64 | Sim | Do corpo. Único por leilão. |
| fornecedorId | string 1–64 | Sim | Dono da oferta (cópia local); igual à empresa do token. |
| quantidade | quantidade | Sim | Do corpo. |
| precoUnitario | valor monetário | Sim | `preco` da cópia local no momento da entrada; não muda depois. |
| situacao | enum | Sim | `"PENDENTE"` na entrada; `"CONFIRMADA"` ou `"RECUSADA"` por `pedido_atualizado`. |
| noLote | booleano | Sim | `false` na entrada; `true` se estava `CONFIRMADA` na fixação do lote. |
| pedidoId | string 1–64 | Não | Recebido em `pedido_atualizado` `CONFIRMADA`. Não aparece na API. |
| entrouEm | instante | Sim | `clock.now()` na entrada. |

#### Lance (tabela `negociacao.lance`, `UNIQUE(negociacaoId, sequencia)`)

| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| lanceId | UUID | Sim | UUID4 gerado pelo serviço. |
| negociacaoId | UUID | Sim | Leilão. |
| compradorId | string 1–64 | Sim | Empresa do token. |
| usuarioId | string | Sim | `sub` do token. Não aparece na API nem nos eventos. |
| valor | valor monetário | Sim | Do corpo: valor total pelo lote. |
| sequencia | inteiro ≥ 1 | Sim | 1 no primeiro lance do leilão; +1 a cada lance aceito, sem buracos. |
| criadoEm | instante | Sim | `clock.now()` na aceitação. |

#### Proposta (tabela `negociacao.proposta`, `UNIQUE(negociacaoId, sequencia)`)

| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| propostaId | UUID | Sim | UUID4 gerado pelo serviço. |
| negociacaoId | UUID | Sim | Negociação direta. |
| autor | enum | Sim | `"BUYER"` ou `"SUPPLIER"`: parte que propôs. |
| empresaId | string 1–64 | Sim | Empresa do token. Não aparece na API. |
| precoUnitario | valor monetário | Sim | Do corpo (na abertura, `precoUnitario` do corpo da criação). |
| sequencia | inteiro ≥ 1 | Sim | 1 na abertura; +1 a cada contraproposta. |
| criadoEm | instante | Sim | `clock.now()`. |

#### OfertaReferencia (tabela `negociacao.oferta_referencia`, chave `fornecimentoId`)

| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| fornecimentoId | string 1–64 | Sim | Do evento. |
| fornecedorId | string 1–64 | Sim | Do evento. |
| produtoId | string 1–64 | Sim | Do evento. |
| preco | `Decimal` ≥ 0 | Sim | `fornecimento_criado.preco` ou `fornecimento_atualizado.preco`. |
| quantidadeDisponivel | `Decimal` ≥ 0 | Sim | De qualquer um dos três eventos do Fornecimento. |
| atualizadoEm | instante | Sim | `criadoEm` ou `atualizadoEm` do payload do último evento aplicado (RN-23). |

Tabelas técnicas do schema `negociacao`: `idempotency_keys` (escopo, hash do corpo, status, corpo e `Location` da resposta, `criado_em`; validade de 24 h), `processed_events` (`source`, `event_id` únicos, `topico`, `processado_em`) e `outbox` (`event_id` UUID único, `topico`, `chave`, `envelope` JSONB, `tentativas`, `proximo_envio`, `confirmado_em`, `criado_em`). Índice único parcial em `processo_negociacao (fornecimento_id, comprador_id) WHERE modo = 'VENDA_DIRETA' AND status = 'ABERTO'` (RN-16). Nenhuma FK para outro schema. Dinheiro `NUMERIC(18,4)`; quantidade `NUMERIC(12,2)`; instantes `TIMESTAMPTZ`.

### 6.2 Estados e transições

Leilão (`LEILAO_DIRETO`):

| Estado atual | Evento / comando | Condição | Próximo estado |
|---|---|---|---|
| (inexistente) | API-04 com `modo = "LEILAO_DIRETO"` | RN-01 a RN-04 | `AGUARDANDO_INICIO` |
| `AGUARDANDO_INICIO` | API-07 entrar com oferta | entrada aberta e RN-02 a RN-06 | `AGUARDANDO_INICIO` (nova oferta `PENDENTE`) |
| `AGUARDANDO_INICIO` | `pedido_atualizado` da oferta | RN-08 | `AGUARDANDO_INICIO` (oferta `CONFIRMADA` ou `RECUSADA`) |
| `AGUARDANDO_INICIO` | `executar_prazos` | `agora >= dataInicio`, `loteFixado = false`, ≥ 1 oferta `CONFIRMADA` | `ABERTO` (lote fixado) |
| `AGUARDANDO_INICIO` | `executar_prazos` | `agora >= dataInicio`, `loteFixado = false`, 0 oferta `CONFIRMADA` | `AGUARDANDO_INICIO` (`loteFixado = true`, lote vazio) |
| `AGUARDANDO_INICIO` | `executar_prazos` | `agora >= dataFim` | `DESERTO` |
| `ABERTO` | API-09 lance | RN-10 a RN-12 | `ABERTO` (novo melhor lance) |
| `ABERTO` | `executar_prazos` | `agora >= dataFim` e `totalLances >= 1` | `ENCERRADO` |
| `ABERTO` | `executar_prazos` | `agora >= dataFim` e `totalLances = 0` | `DESERTO` |
| `ENCERRADO` / `DESERTO` | qualquer | — | (final, sem transições) |

Negociação direta (`VENDA_DIRETA`):

| Estado atual | Evento / comando | Condição | Próximo estado |
|---|---|---|---|
| (inexistente) | API-04 com `modo = "VENDA_DIRETA"` | RN-02, RN-04, RN-12, RN-16 | `ABERTO` (`vez = "SUPPLIER"`, reserva `PENDENTE`) |
| `ABERTO` | API-11 contraproposta | RN-17 | `ABERTO` (`vez` invertida, `expiraEm` renovado) |
| `ABERTO` | API-12 aceitar | RN-17, RN-18 | `ENCERRADO` |
| `ABERTO` | API-13 recusar | RN-17 (prazo) | `RECUSADO` |
| `ABERTO` | `pedido_atualizado` `RECUSADA` | reserva `PENDENTE` | `RECUSADO` |
| `ABERTO` | `pedido_atualizado` `CONFIRMADA` | reserva `PENDENTE` | `ABERTO` (reserva `CONFIRMADA`) |
| `ABERTO` | `executar_prazos` | `agora >= expiraEm` | `EXPIRADO` |
| `ENCERRADO` / `RECUSADO` / `EXPIRADO` | qualquer | — | (final, sem transições) |

O prazo vencido não altera o `status` sozinho: até `executar_prazos` rodar, as ações já são recusadas pela RN-06, RN-10 e RN-17, que comparam com `agora`.

## 7. Contratos de interface

### 7.1 Autenticação e contexto da requisição

Rotas públicas (sem token): `/health`, `/health/ready`, `/metrics`, `/docs`, `/openapi.json`. Todas as rotas `/api/v1/...` exigem `Authorization: Bearer <token>`, um JWT emitido pelo Identity-Service.

Validação do token, nesta ordem (decodificar sempre com `algorithms=["RS256"]` e `options={"verify_exp": False}`, comparando a expiração com `clock.now()`):

1. Cabeçalho ausente, vazio ou sem `Bearer ` seguido de texto; token malformado; cabeçalho do token sem `kid` ou com `alg` diferente de `"RS256"`; `chaves_publicas.obter(kid)` devolvendo `None`; assinatura inválida; `iss` diferente de `emissor`; `aud` que não contém `audiencia` (texto ou lista); `sub` ausente ou vazio; `exp` ausente, não inteiro ou `exp <= int(agora.timestamp())` → `401 TOKEN_INVALIDO`, com o cabeçalho `WWW-Authenticate: Bearer`. Se `chaves_publicas.obter` lançar exceção → `503 DEPENDENCIA_INDISPONIVEL`.
2. `view_mode` diferente de `"SINGLE"`, `tenant_id` ausente, nulo ou vazio, ou `active_scope` ausente ou nulo → `403 CONTEXTO_NAO_SELECIONADO`. É o token do login antes do `switch-context` (`view_mode = "NONE"`) ou o da visão de grupo econômico (`"MACRO_GROUP"`).
3. Escopo não permitido na rota (tabela do §7.2): `CARRIER` e `TENANT_ADMIN` em qualquer rota de negócio, e o escopo errado para a operação → `403 ACESSO_NEGADO`.

Claims ignorados na v1: `platform_role`, `roles`, `permissions`, `group_id`, `allowed_tenants`, `act`, `fid`, `jti`, `client_id` e `token_use`. Um token com `platform_role = "PLATFORM_ADMIN"` não tem nenhum poder adicional. Um token revogado no Identity é aceito até `exp` (no máximo 15 min).

`X-Correlation-Id`: valor de 1 a 128 caracteres de `[A-Za-z0-9._-]`; ausente, vazio ou fora desse formato → o serviço gera um UUID4. O valor efetivo volta no cabeçalho `X-Correlation-Id` de toda resposta, no `correlationId` dos erros e no `correlationId` dos eventos publicados pela requisição.

`Idempotency-Key` (opcional em todo `POST` de negócio): 1 a 128 caracteres de `[A-Za-z0-9._-]`; vazio equivale a ausente; fora do formato → `422 VALIDACAO_INVALIDA`. Regras na RN-24.

### 7.2 API REST

Todas as respostas de negócio são JSON com nomes de campo em camelCase. O FastAPI não pode validar o corpo antes das verificações 1 a 6 do §10: o corpo é lido com `await request.body()` e validado pelo próprio serviço. IDs na URL são UUID; outro formato → `422 VALIDACAO_INVALIDA`. Corpos com campos fora do contrato, campos ausentes, tipos errados (inclusive número enviado como string ou booleano), número com mais de 2 casas ou JSON malformado → `422 VALIDACAO_INVALIDA`. Listagens: `page` inteiro ≥ 1 (padrão 1) e `size` inteiro de 1 a 100 (padrão 10); fora disso → `422 VALIDACAO_INVALIDA`; resposta `{"items": [...], "page": P, "size": S, "total": T}`, com `items: []` numa página vazia.

| ID | Método | Rota | Escopos | Sucesso | Erros possíveis (na ordem de verificação) |
|---|---|---|---|---|---|
| API-01 | GET | `/health` | público | 200 | — |
| API-02 | GET | `/health/ready` | público | 200 | 503 `DEPENDENCIA_INDISPONIVEL` |
| API-03 | GET | `/metrics` | público | 200 | — |
| API-04 | POST | `/api/v1/negociacoes` | `SUPPLIER` (leilão), `BUYER` (direta) | 201 | 401 `TOKEN_INVALIDO` / 503 → 403 `CONTEXTO_NAO_SELECIONADO` → 403 `ACESSO_NEGADO` (escopo fora de `BUYER`/`SUPPLIER`) → 422 `VALIDACAO_INVALIDA` → 403 `ACESSO_NEGADO` (escopo × `modo`) → idempotência (replay ou 409 `IDEMPOTENCIA_CONFLITO`) → **leilão:** 422 `DATAS_INVALIDAS` → 422 `OFERTA_INDISPONIVEL` → 403 `OFERTA_DE_OUTRA_EMPRESA` → 422 `QUANTIDADE_INDISPONIVEL`; **direta:** 422 `OFERTA_INDISPONIVEL` → 403 `CONFLITO_DE_INTERESSE` → 422 `QUANTIDADE_INDISPONIVEL` → 409 `NEGOCIACAO_JA_ABERTA` |
| API-05 | GET | `/api/v1/negociacoes` | `BUYER`, `SUPPLIER` | 200 | 401 / 503 → 403 `CONTEXTO_NAO_SELECIONADO` → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` |
| API-06 | GET | `/api/v1/negociacoes/{negociacaoId}` | `BUYER`, `SUPPLIER` | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 `NEGOCIACAO_NAO_ENCONTRADA` |
| API-07 | POST | `/api/v1/negociacoes/{negociacaoId}/ofertas` | `SUPPLIER` | 201 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` → idempotência → 409 `ENTRADA_ENCERRADA` → 422 `OFERTA_INDISPONIVEL` → 403 `OFERTA_DE_OUTRA_EMPRESA` → 422 `PRODUTO_DIFERENTE` → 409 `OFERTA_JA_NO_LEILAO` → 422 `QUANTIDADE_INDISPONIVEL` |
| API-08 | GET | `/api/v1/negociacoes/{negociacaoId}/lances` | `BUYER`, `SUPPLIER` | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` |
| API-09 | POST | `/api/v1/negociacoes/{negociacaoId}/lances` | `BUYER` | 201 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` → idempotência → 403 `CONFLITO_DE_INTERESSE` → 409 `LEILAO_NAO_ABERTO` → 422 `LANCE_INSUFICIENTE` |
| API-10 | GET | `/api/v1/negociacoes/{negociacaoId}/propostas` | `BUYER`, `SUPPLIER` (participantes) | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` |
| API-11 | POST | `/api/v1/negociacoes/{negociacaoId}/propostas` | `BUYER`, `SUPPLIER` (participantes) | 201 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` → idempotência → 409 `NEGOCIACAO_ENCERRADA` → 409 `FORA_DA_VEZ` |
| API-12 | POST | `/api/v1/negociacoes/{negociacaoId}/aceitar` | `BUYER`, `SUPPLIER` (participantes) | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` → idempotência → 409 `NEGOCIACAO_ENCERRADA` → 409 `FORA_DA_VEZ` → 409 `RESERVA_PENDENTE` |
| API-13 | POST | `/api/v1/negociacoes/{negociacaoId}/recusar` | `BUYER`, `SUPPLIER` (participantes) | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 → 409 `MODO_INCOMPATIVEL` → idempotência → 409 `NEGOCIACAO_ENCERRADA` |

Visibilidade (RN-21): um leilão é visível a todo token `BUYER` ou `SUPPLIER`; uma negociação direta só aos participantes. Negociação inexistente ou invisível → `404 NEGOCIACAO_NAO_ENCONTRADA`. Em qualquer rota, banco indisponível → `503 DEPENDENCIA_INDISPONIVEL`; falha inesperada → `500 ERRO_INTERNO`.

#### Projeções de resposta

**Detalhe de leilão** (API-04 leilão, API-06). Campos derivados: `melhorLance` (lance de `melhorLanceId`, ou `null`), `agora` (`clock.now()`), `acoesPermitidas` (RN-22). `compradorId` é `null` até o encerramento `ENCERRADO`. `melhorLance.compradorId` e `compradorId` seguem o sigilo (RN-13). Exemplo visto por COMP-X em `2026-11-20T13:20:00.000Z`:

```json
{
  "negociacaoId": "31000000-0000-4000-8000-000000000001",
  "modo": "LEILAO_DIRETO",
  "status": "ABERTO",
  "produtoId": "35000000-0000-4000-8000-000000000001",
  "dataInicio": "2026-11-20T13:00:00.000Z",
  "dataFim": "2026-11-20T15:00:00.000Z",
  "quantidadeTotal": 20.00,
  "precoInicial": 2550.00,
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
      "situacao": "CONFIRMADA",
      "noLote": true,
      "entrouEm": "2026-11-20T12:00:00.000Z"
    },
    {
      "negociacaoId": "31000000-0000-4000-8000-000000000001",
      "fornecimentoId": "34000000-0000-4000-8000-000000000002",
      "fornecedorId": "a1000000-0000-4000-8000-000000000002",
      "quantidade": 15.00,
      "precoUnitario": 130.00,
      "situacao": "CONFIRMADA",
      "noLote": true,
      "entrouEm": "2026-11-20T12:30:00.000Z"
    }
  ],
  "criadoPor": "a1000000-0000-4000-8000-000000000001",
  "criadoEm": "2026-11-20T12:00:00.000Z",
  "encerradoEm": null,
  "agora": "2026-11-20T13:20:00.000Z",
  "acoesPermitidas": ["DAR_LANCE"]
}
```

**Detalhe de negociação direta** (API-04 direta, API-06, API-12, API-13). Exemplo visto por COMP-X logo após abrir:

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
  "precoAtual": 125.00,
  "vez": "SUPPLIER",
  "expiraEm": "2026-11-22T12:00:00.000Z",
  "reservaSituacao": "PENDENTE",
  "totalPropostas": 1,
  "criadoPor": "b2000000-0000-4000-8000-000000000001",
  "criadoEm": "2026-11-20T12:00:00.000Z",
  "encerradoEm": null,
  "agora": "2026-11-20T12:00:00.000Z",
  "acoesPermitidas": ["RECUSAR"]
}
```

**Resumo** (itens da API-05): `negociacaoId`, `modo`, `status`, `produtoId`, `dataInicio` (`null` na direta), `dataFim` (`null` na direta), `expiraEm` (`null` no leilão), `criadoEm`.

**Oferta do leilão** (API-07 e itens de `ofertas`): `negociacaoId`, `fornecimentoId`, `fornecedorId`, `quantidade`, `precoUnitario`, `situacao`, `noLote`, `entrouEm`.

**Lance** (API-08 e API-09): `lanceId`, `negociacaoId`, `compradorId`, `valor`, `sequencia`, `criadoEm`. Na API-08 o `compradorId` segue o sigilo; na API-09 nunca é mascarado.

**Proposta** (API-10 e API-11): `propostaId`, `negociacaoId`, `autor`, `precoUnitario`, `sequencia`, `criadoEm`.

#### API-01 — Saúde

Resposta 200: `{"status": "UP", "service": "negociacao-service", "timestamp": "2026-11-20T12:00:00.000Z"}` (`timestamp` = `clock.now()`).

#### API-02 — Prontidão

Resposta 200 quando `repositorio.disponivel()` e `event_bus.disponivel()` são verdadeiros: `{"status": "READY", "service": "negociacao-service"}`. Caso contrário, `503 DEPENDENCIA_INDISPONIVEL` no formato do §10.

#### API-03 — Métricas

Resposta 200 em `text/plain; version=0.0.4` (formato Prometheus), com ao menos `http_requests_total{metodo,rota,status}` e `http_request_duration_seconds{metodo,rota}`; `rota` é o modelo da rota (ex.: `/api/v1/negociacoes/{negociacaoId}/lances`), nunca o caminho com IDs.

#### API-04 — Criar leilão ou abrir negociação direta

Leilão (escopo `SUPPLIER`):

```json
{
  "modo": "LEILAO_DIRETO",
  "fornecimentoId": "34000000-0000-4000-8000-000000000001",
  "quantidade": 5.00,
  "dataInicio": "2026-11-20T13:00:00.000Z",
  "dataFim": "2026-11-20T15:00:00.000Z"
}
```

Resposta 201 com `Location: /api/v1/negociacoes/{negociacaoId}` e o detalhe de leilão em `AGUARDANDO_INICIO`, com `quantidadeTotal`, `precoInicial`, `melhorLance` e `compradorId` nulos, `totalLances` 0, `encerradoEm` nulo e uma oferta `PENDENTE` com `noLote` falso e `precoUnitario` igual ao preço da cópia local. Publica `pedido_temporario_solicitado` e `leilao_iniciado` (§7.4).

Negociação direta (escopo `BUYER`):

```json
{
  "modo": "VENDA_DIRETA",
  "fornecimentoId": "34000000-0000-4000-8000-000000000002",
  "quantidade": 10.00,
  "precoUnitario": 125.00
}
```

Resposta 201 com `Location` e o detalhe de negociação direta do exemplo acima. Publica `pedido_temporario_solicitado`.

#### API-05 — Listar negociações

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
      "dataInicio": "2026-11-20T13:00:00.000Z",
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

#### API-06 — Detalhar negociação

Resposta 200 com o detalhe de leilão ou de negociação direta, conforme o `modo`.

#### API-07 — Entrar com oferta no leilão

```json
{ "fornecimentoId": "34000000-0000-4000-8000-000000000002", "quantidade": 15.00 }
```

Resposta 201:

```json
{
  "negociacaoId": "31000000-0000-4000-8000-000000000001",
  "fornecimentoId": "34000000-0000-4000-8000-000000000002",
  "fornecedorId": "a1000000-0000-4000-8000-000000000002",
  "quantidade": 15.00,
  "precoUnitario": 130.00,
  "situacao": "PENDENTE",
  "noLote": false,
  "entrouEm": "2026-11-20T12:30:00.000Z"
}
```

Publica `pedido_temporario_solicitado` da oferta.

#### API-08 — Listar lances

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

#### API-09 — Dar lance

```json
{ "valor": 2600.00 }
```

Resposta 201:

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

#### API-10 — Listar propostas

Resposta 200 (ordem de `sequencia` crescente):

```json
{
  "items": [
    {
      "propostaId": "36000000-0000-4000-8000-000000000001",
      "negociacaoId": "31000000-0000-4000-8000-000000000002",
      "autor": "BUYER",
      "precoUnitario": 125.00,
      "sequencia": 1,
      "criadoEm": "2026-11-20T12:00:00.000Z"
    },
    {
      "propostaId": "36000000-0000-4000-8000-000000000002",
      "negociacaoId": "31000000-0000-4000-8000-000000000002",
      "autor": "SUPPLIER",
      "precoUnitario": 128.00,
      "sequencia": 2,
      "criadoEm": "2026-11-21T12:00:00.000Z"
    }
  ],
  "page": 1,
  "size": 10,
  "total": 2
}
```

#### API-11 — Contraproposta

```json
{ "precoUnitario": 128.00 }
```

Resposta 201: a proposta criada (segundo item do exemplo da API-10). Não publica evento.

#### API-12 — Aceitar e API-13 — Recusar

Sem corpo, ou corpo `{}`; qualquer outro corpo → `422 VALIDACAO_INVALIDA`. Resposta 200 com o detalhe de negociação direta já em `ENCERRADO` (API-12) ou `RECUSADO` (API-13), `encerradoEm` preenchido e `acoesPermitidas` vazio. Publicam `negociacao_fechada`.

### 7.3 Eventos consumidos

Envelope (kit, `CONTRACTS.md`): `eventId`, `eventType` (igual ao tópico), `eventVersion` (`"1.0"`), `timestamp`, `source`, `correlationId`, `aggregateId` (igual à chave Kafka) e `payload` (objeto). Campos adicionais no `payload` são aceitos e ignorados. Os schemas estão em `docs/contratos/negociacao/<topico>.schema.json` e listam só os campos que este serviço usa.

| Tópico | Produtor | Chave | Ação | Em caso de mensagem inválida |
|---|---|---|---|---|
| `pedido_atualizado` | `pedidos-service` | `pedidoId` | RF-03 | `negociacao.dlt` (RN-26) |
| `fornecimento_criado` | `fornecimentos-service` | `fornecimentoId` | RF-14 | `negociacao.dlt` (RN-26) |
| `fornecimento_atualizado` | `fornecimentos-service` | `fornecimentoId` | RF-14 | `negociacao.dlt` (RN-26) |
| `estoque_atualizado` | `fornecimentos-service` | `fornecimentoId` | RF-14 | `negociacao.dlt` (RN-26) |

Validação comum (falha → inválida): mensagem não é objeto; falta algum dos oito campos do envelope; `eventId` não é string não vazia; `eventType` diferente do tópico; `eventVersion` diferente de `"1.0"`; `source` diferente do produtor da tabela; `correlationId` fora de 1–128 caracteres de `[A-Za-z0-9._-]`; `timestamp` não é instante com fuso; `payload` não é objeto; campo obrigatório do payload ausente, com tipo errado ou fora do domínio do schema; string obrigatória vazia ou só com espaços.

`pedido_atualizado` (contrato candidato, INT-NEG-02). Obrigatórios: `pedidoId`, `status`. Usados quando presentes: `negociacaoId` (UUID), `fornecimentoId`, `reservaSituacao` (`PENDENTE`, `CONFIRMADA`, `RECUSADA` ou `ENCERRADA`). Se `negociacaoId` estiver presente, `fornecimentoId` e `reservaSituacao` passam a ser obrigatórios.

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
    "reservaSituacao": "CONFIRMADA",
    "statusAnterior": "CRIADO",
    "versao": 2
  }
}
```

`fornecimento_criado`. Obrigatórios: `fornecimentoId`, `fornecedorId`, `produtoId`, `preco` (≥ 0), `quantidadeDisponivel` (≥ 0), `criadoEm`.

```json
{
  "eventId": "37000000-0000-4000-8000-000000000007",
  "eventType": "fornecimento_criado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-19T10:00:00.000Z",
  "source": "fornecimentos-service",
  "correlationId": "teste-neg-forn",
  "aggregateId": "34000000-0000-4000-8000-000000000001",
  "payload": {
    "fornecimentoId": "34000000-0000-4000-8000-000000000001",
    "fornecedorId": "a1000000-0000-4000-8000-000000000001",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "unidade": "TON",
    "preco": 120.00,
    "quantidadeDisponivel": 100.00,
    "origem": {
      "cidade": "Rio Verde",
      "uf": "GO"
    },
    "criadoPor": {
      "empresaId": "a1000000-0000-4000-8000-000000000001",
      "usuarioId": "c3000000-0000-4000-8000-000000000001"
    },
    "criadoEm": "2026-11-19T10:00:00.000Z"
  }
}
```

`fornecimento_atualizado`. Obrigatórios: `fornecimentoId`, `fornecedorId`, `produtoId`, `preco` (≥ 0), `quantidadeDisponivel` (≥ 0), `atualizadoEm`.

```json
{
  "eventId": "37000000-0000-4000-8000-000000000008",
  "eventType": "fornecimento_atualizado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-19T18:00:00.000Z",
  "source": "fornecimentos-service",
  "correlationId": "teste-neg-forn",
  "aggregateId": "34000000-0000-4000-8000-000000000001",
  "payload": {
    "fornecimentoId": "34000000-0000-4000-8000-000000000001",
    "fornecedorId": "a1000000-0000-4000-8000-000000000001",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "unidade": "TON",
    "preco": 118.50,
    "quantidadeDisponivel": 100.00,
    "origem": {
      "cidade": "Rio Verde",
      "uf": "GO"
    },
    "atualizadoPor": {
      "empresaId": "a1000000-0000-4000-8000-000000000001",
      "usuarioId": "c3000000-0000-4000-8000-000000000001"
    },
    "atualizadoEm": "2026-11-19T18:00:00.000Z"
  }
}
```

`estoque_atualizado`. Obrigatórios: `fornecimentoId`, `produtoId`, `fornecedorId`, `operacao` (`RESERVA`, `LIBERACAO`, `BAIXA`, `REPOSICAO` ou `ENCERRAMENTO`), `quantidadeDisponivel` (≥ 0), `atualizadoEm`.

```json
{
  "eventId": "37000000-0000-4000-8000-000000000009",
  "eventType": "estoque_atualizado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:01:00.000Z",
  "source": "fornecimentos-service",
  "correlationId": "teste-neg-01",
  "aggregateId": "34000000-0000-4000-8000-000000000001",
  "payload": {
    "fornecimentoId": "34000000-0000-4000-8000-000000000001",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "fornecedorId": "a1000000-0000-4000-8000-000000000001",
    "operacao": "RESERVA",
    "quantidadeMovimentada": 5.00,
    "quantidadeDisponivel": 95.00,
    "quantidadeReservada": 5.00,
    "pedidoId": "32000000-0000-4000-8000-000000000001",
    "atualizadoEm": "2026-11-20T12:01:00.000Z"
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
| `leilao_iniciado` | Leilão criado (RF-01) | `negociacaoId` | `negociacaoId`, `produtoId`, `status` (`"AGUARDANDO_INICIO"`), `dataInicio`, `dataFim`, `criadoPor`, `oferta` {`fornecimentoId`, `fornecedorId`, `quantidade`, `precoUnitario`} | Pedidos, front-end |
| `pedido_temporario_solicitado` | Oferta entra num leilão (RF-01, RF-02) ou negociação direta aberta (RF-07) | `negociacaoId` | `negociacaoId`, `tipo`, `fornecimentoId`, `fornecedorId`, `produtoId`, `quantidade`, `precoUnitario` (preço da oferta) | Pedidos |
| `lance_realizado` | Lance aceito (RF-05) | `negociacaoId` | `negociacaoId`, `lanceId`, `compradorId` (sem máscara), `valor`, `sequencia`, `criadoEm` | front-end, auditoria |
| `negociacao_fechada` | Negociação chega a estado final (RF-06, RF-03, RF-09, RF-10, RF-11) | `negociacaoId` | `negociacaoId`, `tipo`, `status`, `compradorId` (`null` só em `DESERTO`), `produtoId`, `alocacoes[]` {`fornecimentoId`, `fornecedorId`, `pedidoId`, `quantidade`, `valorUnitario`, `valorTotal`} (vazia fora de `ENCERRADO`) | Pedidos |
| `negociacao.dlt` | Mensagem consumida inválida ou com falha após 4 execuções (RF-17) | `aggregateId` original se for string de 1 a 128 caracteres; senão `eventId` original convertido com `str` se presente e não vazio; senão `"desconhecido"` | `topicoOrigem`, `originalEvent` (mensagem recebida, intacta), `error` (`"EVENTO_INVALIDO"` ou `"PROCESSAMENTO_FALHOU"`), `detalhe` (motivo, 1–500 caracteres), `stackTrace` (`""`), `failedAtAttempt` (1 a 4) | operação, QA |

Exemplos (iguais a `docs/contratos/negociacao/<topico>.exemplo.json`):

`leilao_iniciado`:

```json
{
  "eventId": "37000000-0000-4000-8000-000000000001",
  "eventType": "leilao_iniciado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:00:00.000Z",
  "source": "negociacao-service",
  "correlationId": "teste-neg-01",
  "aggregateId": "31000000-0000-4000-8000-000000000001",
  "payload": {
    "negociacaoId": "31000000-0000-4000-8000-000000000001",
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "status": "AGUARDANDO_INICIO",
    "dataInicio": "2026-11-20T13:00:00.000Z",
    "dataFim": "2026-11-20T15:00:00.000Z",
    "criadoPor": "a1000000-0000-4000-8000-000000000001",
    "oferta": {
      "fornecimentoId": "34000000-0000-4000-8000-000000000001",
      "fornecedorId": "a1000000-0000-4000-8000-000000000001",
      "quantidade": 5.00,
      "precoUnitario": 120.00
    }
  }
}
```

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

`negociacao_fechada` (leilão conjunto encerrado; rateio do lance de 2600.00 entre 5 e 15 unidades):

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
    "produtoId": "35000000-0000-4000-8000-000000000001",
    "alocacoes": [
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000001",
        "fornecedorId": "a1000000-0000-4000-8000-000000000001",
        "pedidoId": "32000000-0000-4000-8000-000000000001",
        "quantidade": 5.00,
        "valorUnitario": 130.00,
        "valorTotal": 650.00
      },
      {
        "fornecimentoId": "34000000-0000-4000-8000-000000000002",
        "fornecedorId": "a1000000-0000-4000-8000-000000000002",
        "pedidoId": "32000000-0000-4000-8000-000000000002",
        "quantidade": 15.00,
        "valorUnitario": 130.00,
        "valorTotal": 1950.00
      }
    ]
  }
}
```

`negociacao_fechada` deserto (payload): `{"negociacaoId": "31000000-0000-4000-8000-000000000001", "tipo": "LEILAO", "status": "DESERTO", "compradorId": null, "produtoId": "35000000-0000-4000-8000-000000000001", "alocacoes": []}`.

`negociacao.dlt`:

```json
{
  "eventId": "37000000-0000-4000-8000-000000000005",
  "eventType": "negociacao.dlt",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:05:00.000Z",
  "source": "negociacao-service",
  "correlationId": "teste-neg-dlt",
  "aggregateId": "34000000-0000-4000-8000-000000000001",
  "payload": {
    "topicoOrigem": "estoque_atualizado",
    "originalEvent": {
      "eventId": "38000000-0000-4000-8000-000000000009",
      "eventType": "estoque_atualizado",
      "eventVersion": "1.0",
      "timestamp": "2026-11-20T12:04:59.000Z",
      "source": "fornecimentos-service",
      "correlationId": "teste-neg-dlt",
      "aggregateId": "34000000-0000-4000-8000-000000000001"
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

Os critérios usam esta massa. `token(ESCOPO, EMPRESA)` é um JWT RS256 assinado com a chave de teste registrada em `chaves_publicas` (`kid` `"teste-1"`), com `iss` `"identity-service"`, `aud` `"b2b-platform"`, `view_mode` `"SINGLE"`, `tenant_id` = EMPRESA, `active_scope` = ESCOPO, `sub` `"c3000000-0000-4000-8000-000000000001"` e `exp` = agora + 900 s. "Relógio em H" significa `clock.now()` = `2026-11-20T` H `.000Z`. `L1`, `L2`, `D1`… são os `negociacaoId` devolvidos pelo serviço. Toda resposta de erro tem o corpo do §10 com os valores da linha do código citado.

| Nome | Valor |
|---|---|
| FORN-A, FORN-B, FORN-C (empresas fornecedoras) | `a1000000-0000-4000-8000-000000000001`, `…000000000002`, `…000000000003` |
| COMP-X, COMP-Y (empresas compradoras) | `b2000000-0000-4000-8000-000000000001`, `…000000000002` |
| Produto P, produto Q | `35000000-0000-4000-8000-000000000001`, `35000000-0000-4000-8000-000000000002` |
| OF-A | `34000000-0000-4000-8000-000000000001`: FORN-A, produto P, `preco` 120.00, `quantidadeDisponivel` 100.00 |
| OF-B | `34000000-0000-4000-8000-000000000002`: FORN-B, produto P, `preco` 130.00, `quantidadeDisponivel` 50.00 |
| OF-C | `34000000-0000-4000-8000-000000000003`: FORN-B, produto Q, `preco` 10.00, `quantidadeDisponivel` 10.00 |
| OF-Z | `34000000-0000-4000-8000-000000000004`: FORN-B, produto P, `preco` 0.00, `quantidadeDisponivel` 30.00 |
| OF-W | `34000000-0000-4000-8000-000000000005`: FORN-B, produto P, `preco` 140.00, `quantidadeDisponivel` 0.00 |
| OF-D | `34000000-0000-4000-8000-000000000006`: FORN-C, produto P, `preco` 110.00, `quantidadeDisponivel` 40.00 |
| OF-E, OF-F | `…000000000007` (FORN-A, P, 0.10, 10.00) e `…000000000008` (FORN-B, P, 0.20, 10.00) |
| PT-A, PT-B, PT-D | pedidos temporários `32000000-0000-4000-8000-000000000001`, `…000000000002`, `…000000000003` |
| Leilão L1 | criado por FORN-A às `12:00:00` com OF-A, `quantidade` 5.00, `dataInicio` `2026-11-20T13:00:00.000Z`, `dataFim` `2026-11-20T15:00:00.000Z` |
| Direta D1 | aberta por COMP-X às `12:00:00` com OF-B, `quantidade` 10.00, `precoUnitario` 125.00 |

Quando um CA compara uma resposta com um exemplo do §7, os identificadores gerados pelo serviço (`negociacaoId`, `lanceId`, `propostaId`, `eventId`) são comparados com os valores realmente devolvidos, e não com os UUIDs ilustrativos do exemplo. As ofertas existem na cópia local (por `fornecimento_criado`) antes de cada cenário. "Confirmar a reserva de X com PT" significa entregar ao handler um `pedido_atualizado` com `negociacaoId` da negociação, `fornecimentoId` X, `pedidoId` PT, `status` `"CRIADO"` e `reservaSituacao` `"CONFIRMADA"` (eventId novo). "Recusar a reserva" é igual, com `"RECUSADA"`.

### RF-01 — Criar leilão

- **Prioridade:** Must
- **Enunciado (EARS):** Quando um token `SUPPLIER` enviar `POST /api/v1/negociacoes` com `modo` `"LEILAO_DIRETO"` e dados que satisfaçam as regras, o serviço deve criar o leilão em `AGUARDANDO_INICIO` com a oferta como primeira oferta do leilão, responder 201 e publicar `pedido_temporario_solicitado` e `leilao_iniciado`.
- **Regras aplicáveis:** RN-01, RN-02, RN-03, RN-04, RN-07, RN-28
- **Interface:** API-04; tópicos `pedido_temporario_solicitado`, `leilao_iniciado`

```gherkin
# CA-01
Cenário: Fornecedor cria um leilão
  Dado o relógio em 12:00:00
  Quando token(SUPPLIER, FORN-A) envia POST /api/v1/negociacoes com {"modo": "LEILAO_DIRETO", "fornecimentoId": OF-A, "quantidade": 5.00, "dataInicio": "2026-11-20T13:00:00.000Z", "dataFim": "2026-11-20T15:00:00.000Z"}
  Então a resposta é 201 com Location "/api/v1/negociacoes/L1", status "AGUARDANDO_INICIO", produtoId P, quantidadeTotal null, precoInicial null, melhorLance null, totalLances 0, compradorId null, criadoPor FORN-A e acoesPermitidas ["ENTRAR_COM_OFERTA"]
  E ofertas tem 1 item com fornecimentoId OF-A, fornecedorId FORN-A, quantidade 5.00, precoUnitario 120.00, situacao "PENDENTE", noLote false e entrouEm "2026-11-20T12:00:00.000Z"
  E foram publicadas exatamente 2 mensagens, nesta ordem, ambas com chave L1:
    | tópico                       | payload                                                                                                   |
    | pedido_temporario_solicitado | negociacaoId L1, tipo "LEILAO", fornecimentoId OF-A, fornecedorId FORN-A, produtoId P, quantidade 5.00, precoUnitario 120.00 |
    | leilao_iniciado              | negociacaoId L1, produtoId P, status "AGUARDANDO_INICIO", dataInicio e dataFim do corpo, criadoPor FORN-A, oferta {OF-A, FORN-A, 5.00, 120.00} |

# CA-02
Cenário: Datas do leilão
  Dado o relógio em 12:00:00
  Quando token(SUPPLIER, FORN-A) cria o leilão de OF-A com dataInicio "2026-11-20T11:59:59.999Z"
  Então a resposta é 422 com codigo "DATAS_INVALIDAS"
  Quando cria com dataInicio "2026-11-20T13:00:00.000Z" e dataFim "2026-11-20T13:00:00.000Z"
  Então a resposta é 422 com codigo "DATAS_INVALIDAS"
  Quando cria com dataInicio "2026-11-20T12:00:00.000Z" e dataFim "2026-11-20T12:00:00.001Z"
  Então a resposta é 201
  Quando cria com dataInicio "2026-11-20T10:00:00-03:00" e dataFim "2026-11-20T12:00:00-03:00"
  Então a resposta é 201 com dataInicio "2026-11-20T13:00:00.000Z" e dataFim "2026-11-20T15:00:00.000Z"

# CA-03
Cenário: Oferta não negociável
  Quando token(SUPPLIER, FORN-B) cria o leilão com OF-Z (preço 0.00), com OF-W (disponível 0.00) ou com fornecimentoId "34000000-0000-4000-8000-000000000099" (ausente da cópia local)
  Então cada resposta é 422 com codigo "OFERTA_INDISPONIVEL"

# CA-04
Cenário: Oferta de outra empresa
  Quando token(SUPPLIER, FORN-A) cria o leilão com OF-B
  Então a resposta é 403 com codigo "OFERTA_DE_OUTRA_EMPRESA"

# CA-05
Cenário: Quantidade acima do saldo
  Quando token(SUPPLIER, FORN-A) cria o leilão com OF-A e quantidade 100.01
  Então a resposta é 422 com codigo "QUANTIDADE_INDISPONIVEL"
  Quando cria com OF-A e quantidade 100.00
  Então a resposta é 201

# CA-06
Cenário: Escopo e corpo inválidos
  Quando token(BUYER, COMP-X) envia o corpo de leilão do CA-01
  Então a resposta é 403 com codigo "ACESSO_NEGADO"
  Quando token(SUPPLIER, FORN-A) envia o corpo do CA-01 com qualquer uma destas alterações: modo "LEILAO_REVERSO"; quantidade "5.00" (string); quantidade 5.001; quantidade 0; dataInicio "2026-11-20T13:00:00" (sem fuso); campo extra "incrementoMinimo": 10.00; sem dataFim; corpo "abc"
  Então cada resposta é 422 com codigo "VALIDACAO_INVALIDA"

# CA-07
Cenário: Precedência de erros na criação de leilão
  Dado o relógio em 12:00:00
  Quando token(SUPPLIER, FORN-A) cria o leilão com OF-B e dataInicio "2026-11-20T11:00:00.000Z"
  Então a resposta é 422 com codigo "DATAS_INVALIDAS"
  Quando token(SUPPLIER, FORN-A) cria o leilão com OF-Z, que é de FORN-B
  Então a resposta é 422 com codigo "OFERTA_INDISPONIVEL"
  Quando token(SUPPLIER, FORN-A) cria o leilão com OF-B e quantidade 999.00
  Então a resposta é 403 com codigo "OFERTA_DE_OUTRA_EMPRESA"
  E depois dos CA-02 a CA-07, as respostas de erro não publicaram nenhuma mensagem
```

### RF-02 — Entrar com oferta no leilão

- **Prioridade:** Must
- **Enunciado (EARS):** Enquanto a entrada do leilão estiver aberta, quando um token `SUPPLIER` enviar `POST /api/v1/negociacoes/{id}/ofertas` com uma oferta própria, negociável e do mesmo produto, o serviço deve registrar a oferta como `PENDENTE`, responder 201 e publicar `pedido_temporario_solicitado`.
- **Regras aplicáveis:** RN-02, RN-03, RN-04, RN-05, RN-06, RN-07, RN-20, RN-28
- **Interface:** API-07; tópico `pedido_temporario_solicitado`

```gherkin
# CA-08
Cenário: Segundo fornecedor entra no leilão
  Dado o leilão L1 em AGUARDANDO_INICIO e o relógio em 12:30:00
  Quando token(SUPPLIER, FORN-B) envia POST /api/v1/negociacoes/L1/ofertas com {"fornecimentoId": OF-B, "quantidade": 15.00}
  Então a resposta é 201 com negociacaoId L1, fornecimentoId OF-B, fornecedorId FORN-B, quantidade 15.00, precoUnitario 130.00, situacao "PENDENTE", noLote false e entrouEm "2026-11-20T12:30:00.000Z"
  E foi publicada exatamente 1 mensagem em "pedido_temporario_solicitado" com chave L1 e payload {negociacaoId L1, tipo "LEILAO", fornecimentoId OF-B, fornecedorId FORN-B, produtoId P, quantidade 15.00, precoUnitario 130.00}
  E GET /api/v1/negociacoes/L1 mostra ofertas OF-A e OF-B, nesta ordem

# CA-09
Cenário: Entrada depois de dataInicio
  Dado o leilão L1 em AGUARDANDO_INICIO e o relógio em 13:00:00, sem executar_prazos
  Quando token(SUPPLIER, FORN-B) entra com OF-B
  Então a resposta é 409 com codigo "ENTRADA_ENCERRADA"
  Dado o leilão L1 em ABERTO
  Quando token(SUPPLIER, FORN-C) entra com OF-D
  Então a resposta é 409 com codigo "ENTRADA_ENCERRADA"

# CA-10
Cenário: Produto diferente e oferta repetida
  Dado o leilão L1 em AGUARDANDO_INICIO, relógio em 12:30:00, com OF-B já no leilão
  Quando token(SUPPLIER, FORN-B) entra com OF-C
  Então a resposta é 422 com codigo "PRODUTO_DIFERENTE"
  Quando token(SUPPLIER, FORN-B) entra novamente com OF-B
  Então a resposta é 409 com codigo "OFERTA_JA_NO_LEILAO"
  Quando token(SUPPLIER, FORN-C) entra com OF-D e quantidade 40.01
  Então a resposta é 422 com codigo "QUANTIDADE_INDISPONIVEL"

# CA-11
Cenário: Precedência e erros de rota na entrada
  Dado o leilão L1 com o relógio em 13:00:00 e a direta D1 aberta
  Quando token(SUPPLIER, FORN-A) entra em L1 com OF-B
  Então a resposta é 409 com codigo "ENTRADA_ENCERRADA"
  Quando token(BUYER, COMP-X) entra em L1 com OF-B
  Então a resposta é 403 com codigo "ACESSO_NEGADO"
  Quando token(SUPPLIER, FORN-B) envia POST /api/v1/negociacoes/D1/ofertas com OF-B
  Então a resposta é 409 com codigo "MODO_INCOMPATIVEL"
  Quando token(SUPPLIER, FORN-B) envia POST /api/v1/negociacoes/abc/ofertas
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando token(SUPPLIER, FORN-B) envia POST /api/v1/negociacoes/31000000-0000-4000-8000-000000000099/ofertas
  Então a resposta é 404 com codigo "NEGOCIACAO_NAO_ENCONTRADA"
```

### RF-03 — Registrar o resultado da reserva

- **Prioridade:** Must
- **Enunciado (EARS):** Quando um `pedido_atualizado` válido com `negociacaoId`, `fornecimentoId` e `reservaSituacao` `"CONFIRMADA"` ou `"RECUSADA"` chegar para uma oferta com reserva `PENDENTE`, o serviço deve registrar a nova situação e o `pedidoId`; numa negociação direta, a recusa encerra a negociação como `RECUSADO` e publica `negociacao_fechada`.
- **Regras aplicáveis:** RN-08, RN-19, RN-25
- **Interface:** tópico `pedido_atualizado`; tópico `negociacao_fechada`

```gherkin
# CA-12
Cenário: Reserva confirmada no leilão
  Dado o leilão L1 com OF-A PENDENTE
  Quando o handler recebe pedido_atualizado {pedidoId PT-A, negociacaoId L1, fornecimentoId OF-A, status "CRIADO", reservaSituacao "CONFIRMADA"}
  Então GET /api/v1/negociacoes/L1 mostra OF-A com situacao "CONFIRMADA" e noLote false
  E nenhuma mensagem é publicada

# CA-13
Cenário: Reserva recusada no leilão
  Dado o leilão L1 com OF-A CONFIRMADA e OF-B PENDENTE
  Quando o handler recebe a recusa da reserva de OF-B em L1
  Então OF-B fica com situacao "RECUSADA", L1 continua "AGUARDANDO_INICIO" e nenhuma mensagem é publicada

# CA-14
Cenário: pedido_atualizado que não muda nada
  Dado o leilão L1 com OF-A CONFIRMADA e OF-B PENDENTE
  Quando o handler recebe, cada um com eventId novo: pedido_atualizado sem negociacaoId; com reservaSituacao "PENDENTE" para OF-B; com reservaSituacao "ENCERRADA" para OF-B; com negociacaoId "31000000-0000-4000-8000-000000000099"; com fornecimentoId OF-D em L1; e a recusa de OF-A em L1
  Então OF-A continua "CONFIRMADA", OF-B continua "PENDENTE", nenhuma mensagem é publicada e nada vai para "negociacao.dlt"

# CA-15
Cenário: Reserva recusada na negociação direta
  Dado a direta D1 em ABERTO com reserva PENDENTE e o relógio em 12:05:00
  Quando o handler recebe a recusa da reserva de OF-B em D1, com correlationId "teste-neg-15"
  Então GET /api/v1/negociacoes/D1 por token(BUYER, COMP-X) mostra status "RECUSADO", reservaSituacao "RECUSADA" e encerradoEm "2026-11-20T12:05:00.000Z"
  E foi publicada exatamente 1 mensagem em "negociacao_fechada" com chave D1, correlationId "teste-neg-15" e payload {negociacaoId D1, tipo "DIRETA", status "RECUSADO", compradorId COMP-X, produtoId P, alocacoes []}
```

### RF-04 — Fixar o lote em dataInicio

- **Prioridade:** Must
- **Enunciado (EARS):** Quando `executar_prazos` rodar com `agora >= dataInicio` num leilão `AGUARDANDO_INICIO` com `loteFixado = false`, o serviço deve fixar o lote com as ofertas `CONFIRMADA`, calcular `quantidadeTotal` e `precoInicial` e passar o leilão a `ABERTO`; sem oferta confirmada, o leilão fica sem lote e não abre para lances.
- **Regras aplicáveis:** RN-09, RN-27
- **Interface:** `app.main.executar_prazos`

```gherkin
# CA-16
Cenário: Lote do leilão conjunto
  Dado o leilão L1 com OF-A (5.00) CONFIRMADA com PT-A e OF-B (15.00) CONFIRMADA com PT-B
  E o relógio em 13:00:00
  Quando executar_prazos é chamado
  Então devolve 1
  E GET /api/v1/negociacoes/L1 mostra status "ABERTO", quantidadeTotal 20.00, precoInicial 2550.00 e as duas ofertas com noLote true
  E nenhuma mensagem é publicada

# CA-17
Cenário: Confirmação depois da fixação do lote
  Dado o leilão L1 com OF-A CONFIRMADA e OF-B PENDENTE, e o lote fixado às 13:00:00
  Quando, às 13:05:00, o handler recebe a confirmação da reserva de OF-B com PT-B
  Então OF-B fica "CONFIRMADA" com noLote false
  E L1 continua com quantidadeTotal 5.00 e precoInicial 600.00

# CA-18
Cenário: Nenhuma oferta confirmada em dataInicio
  Dado o leilão L1 com OF-A PENDENTE e o relógio em 13:00:00
  Quando executar_prazos é chamado
  Então L1 continua "AGUARDANDO_INICIO" com quantidadeTotal null e precoInicial null
  Quando, às 13:30:00, token(BUYER, COMP-X) envia POST /api/v1/negociacoes/L1/lances com {"valor": 1000.00}
  Então a resposta é 409 com codigo "LEILAO_NAO_ABERTO"
  Quando, às 15:00:00, executar_prazos é chamado
  Então L1 fica "DESERTO" e é publicada 1 mensagem em "negociacao_fechada" com chave L1 e payload {negociacaoId L1, tipo "LEILAO", status "DESERTO", compradorId null, produtoId P, alocacoes []}
```

### RF-05 — Dar lance

- **Prioridade:** Must
- **Enunciado (EARS):** Enquanto o leilão estiver aceitando lances, quando um token `BUYER` de empresa que não é fornecedora do leilão enviar `POST /api/v1/negociacoes/{id}/lances` com valor válido, o serviço deve registrar o lance, responder 201 e publicar `lance_realizado`.
- **Regras aplicáveis:** RN-10, RN-11, RN-12, RN-20, RN-28
- **Interface:** API-09; tópico `lance_realizado`

```gherkin
# CA-19
Cenário: Primeiro lance igual ao preço inicial
  Dado o leilão L1 ABERTO com precoInicial 2550.00 e o relógio em 13:05:00
  Quando token(BUYER, COMP-X) envia {"valor": 2550.00} com X-Correlation-Id "teste-neg-19"
  Então a resposta é 201 com negociacaoId L1, compradorId COMP-X, valor 2550.00, sequencia 1 e criadoEm "2026-11-20T13:05:00.000Z"
  E foi publicada exatamente 1 mensagem em "lance_realizado" com chave L1, correlationId "teste-neg-19" e payload {negociacaoId L1, lanceId igual ao da resposta, compradorId COMP-X, valor 2550.00, sequencia 1, criadoEm "2026-11-20T13:05:00.000Z"}

# CA-20
Cenário: Valor mínimo do lance
  Dado o leilão L1 ABERTO com precoInicial 2550.00 e sem lances
  Quando token(BUYER, COMP-X) envia {"valor": 2549.99}
  Então a resposta é 422 com codigo "LANCE_INSUFICIENTE"
  Quando token(BUYER, COMP-X) envia {"valor": 2550.00}
  Então a resposta é 201
  Quando token(BUYER, COMP-Y) envia {"valor": 2550.00}
  Então a resposta é 422 com codigo "LANCE_INSUFICIENTE"
  Quando token(BUYER, COMP-Y) envia {"valor": 2550.01}
  Então a resposta é 201 com sequencia 2

# CA-21
Cenário: Lance fora da janela
  Dado o leilão L1 AGUARDANDO_INICIO e o relógio em 12:59:59
  Quando token(BUYER, COMP-X) envia {"valor": 3000.00}
  Então a resposta é 409 com codigo "LEILAO_NAO_ABERTO"
  Dado o leilão L1 ABERTO e o relógio em 15:00:00, sem executar_prazos
  Quando token(BUYER, COMP-X) envia {"valor": 3000.00}
  Então a resposta é 409 com codigo "LEILAO_NAO_ABERTO"
  Dado o leilão L1 ENCERRADO
  Quando token(BUYER, COMP-X) envia {"valor": 3000.00}
  Então a resposta é 409 com codigo "LEILAO_NAO_ABERTO"

# CA-22
Cenário: Conflito de interesse no leilão
  Dado o leilão L1 ABERTO com OF-A (FORN-A) CONFIRMADA e OF-B (FORN-B) RECUSADA
  Quando token(BUYER, FORN-A) envia {"valor": 3000.00}
  Então a resposta é 403 com codigo "CONFLITO_DE_INTERESSE"
  Quando token(BUYER, FORN-B) envia {"valor": 3000.00}
  Então a resposta é 403 com codigo "CONFLITO_DE_INTERESSE"

# CA-23
Cenário: Escopo, corpo e modo no lance
  Dado o leilão L1 ABERTO e a direta D1 ABERTO
  Quando token(SUPPLIER, FORN-C) envia {"valor": 3000.00} para L1
  Então a resposta é 403 com codigo "ACESSO_NEGADO"
  Quando token(BUYER, COMP-X) envia para L1 qualquer um destes corpos: {"valor": "3000.00"}; {"valor": 3000.001}; {"valor": 0}; {"valor": -1.00}; {"valor": true}; {}; {"valor": 3000.00, "comprador": "x"}
  Então cada resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando token(BUYER, COMP-X) envia {"valor": 3000.00} para D1
  Então a resposta é 409 com codigo "MODO_INCOMPATIVEL"

# CA-24
Cenário: Precedência de erros no lance
  Dado o leilão L1 com OF-A de FORN-A, ENCERRADO
  Quando token(BUYER, FORN-A) envia {"valor": "abc"}
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando token(BUYER, FORN-A) envia {"valor": 1.00}
  Então a resposta é 403 com codigo "CONFLITO_DE_INTERESSE"
  Dado o leilão L1 ABERTO com precoInicial 2550.00 e o relógio em 15:00:00
  Quando token(BUYER, COMP-X) envia {"valor": 1.00}
  Então a resposta é 409 com codigo "LEILAO_NAO_ABERTO"

# CA-25
Cenário: O autor do melhor lance pode cobrir o próprio lance
  Dado o leilão L1 ABERTO cujo melhor lance é 2550.00 de COMP-X
  Quando token(BUYER, COMP-X) envia {"valor": 2560.00}
  Então a resposta é 201 com sequencia 2
```

### RF-06 — Encerrar o leilão em dataFim e ratear

- **Prioridade:** Must
- **Enunciado (EARS):** Quando `executar_prazos` rodar com `agora >= dataFim` num leilão `AGUARDANDO_INICIO` ou `ABERTO`, o serviço deve encerrá-lo como `ENCERRADO` (com lance) ou `DESERTO` (sem lance) e publicar uma única `negociacao_fechada`, com as alocações do rateio no caso `ENCERRADO`.
- **Regras aplicáveis:** RN-14, RN-15, RN-27
- **Interface:** `app.main.executar_prazos`; tópico `negociacao_fechada`

```gherkin
# CA-26
Cenário: Leilão conjunto encerrado com vencedor
  Dado o leilão L1 ABERTO com lote OF-A (5.00, PT-A) e OF-B (15.00, PT-B), precoInicial 2550.00
  E os lances 2550.00 de COMP-X (sequencia 1) e 2600.00 de COMP-Y (sequencia 2)
  E o relógio em 15:00:00
  Quando executar_prazos é chamado
  Então GET /api/v1/negociacoes/L1 por token(BUYER, COMP-Y) mostra status "ENCERRADO", compradorId COMP-Y e encerradoEm "2026-11-20T15:00:00.000Z"
  E foi publicada exatamente 1 mensagem em "negociacao_fechada" com chave L1 e payload igual ao de docs/contratos/negociacao/negociacao_fechada.exemplo.json: compradorId COMP-Y e alocacoes [{OF-A, FORN-A, PT-A, 5.00, 130.00, 650.00}, {OF-B, FORN-B, PT-B, 15.00, 130.00, 1950.00}]

# CA-27
Cenário: Leilão aberto sem lances
  Dado o leilão L1 ABERTO com lote fixado e nenhum lance, e o relógio em 15:00:00
  Quando executar_prazos é chamado
  Então L1 fica "DESERTO" com compradorId null
  E foi publicada 1 mensagem em "negociacao_fechada" com payload {negociacaoId L1, tipo "LEILAO", status "DESERTO", compradorId null, produtoId P, alocacoes []}

# CA-28
Cenário: Encerramento uma única vez e no instante certo
  Dado o leilão L1 ABERTO com 2 lances e o relógio em 14:59:59.999
  Quando executar_prazos é chamado
  Então devolve 0 e L1 continua "ABERTO"
  Dado o relógio em 15:00:00
  Quando executar_prazos é chamado duas vezes
  Então a primeira chamada devolve 1, a segunda devolve 0 e há exatamente 1 mensagem em "negociacao_fechada" para L1

# CA-29
Cenário: Rateio com centavo que sobra
  Dado o leilão L2 com lote OF-A (1.00, entrou às 12:00:00) e OF-B (2.00, entrou às 12:10:00), precoInicial 380.00, e o lance vencedor 1000.00
  Quando L2 é encerrado
  Então alocacoes é [{OF-A, quantidade 1.00, valorUnitario 333.33, valorTotal 333.33}, {OF-B, quantidade 2.00, valorUnitario 333.33, valorTotal 666.67}]
  Dado o leilão L3 com lote OF-A (1.00, 12:00:00), OF-B (1.00, 12:10:00) e OF-D (1.00, 12:20:00), precoInicial 360.00, e o lance vencedor 1000.00
  Quando L3 é encerrado
  Então os valorTotal são 333.34 (OF-A), 333.33 (OF-B) e 333.33 (OF-D), somando 1000.00

# CA-30
Cenário: Oferta confirmada depois de dataInicio fica fora das alocações
  Dado o leilão L1 com lote só com OF-A (5.00, PT-A), OF-B CONFIRMADA com noLote false, e o lance vencedor 700.00 de COMP-X
  Quando L1 é encerrado
  Então alocacoes tem 1 item {OF-A, FORN-A, PT-A, quantidade 5.00, valorUnitario 140.00, valorTotal 700.00}
```

### RF-07 — Abrir negociação direta

- **Prioridade:** Must
- **Enunciado (EARS):** Quando um token `BUYER` enviar `POST /api/v1/negociacoes` com `modo` `"VENDA_DIRETA"` para uma oferta negociável de outra empresa, o serviço deve abrir a negociação com a primeira proposta do comprador, `vez` `"SUPPLIER"` e prazo de 48 h, responder 201 e publicar `pedido_temporario_solicitado`.
- **Regras aplicáveis:** RN-02, RN-04, RN-07, RN-12, RN-16, RN-17, RN-28
- **Interface:** API-04; tópico `pedido_temporario_solicitado`

```gherkin
# CA-31
Cenário: Comprador abre negociação direta
  Dado o relógio em 12:00:00
  Quando token(BUYER, COMP-X) envia POST /api/v1/negociacoes com {"modo": "VENDA_DIRETA", "fornecimentoId": OF-B, "quantidade": 10.00, "precoUnitario": 125.00}
  Então a resposta é 201 com Location "/api/v1/negociacoes/D1" e o corpo igual ao exemplo "Detalhe de negociação direta" do §7.2
  E foi publicada exatamente 1 mensagem, em "pedido_temporario_solicitado", com chave D1 e payload {negociacaoId D1, tipo "DIRETA", fornecimentoId OF-B, fornecedorId FORN-B, produtoId P, quantidade 10.00, precoUnitario 130.00}
  E GET /api/v1/negociacoes/D1/propostas tem 1 item com autor "BUYER", precoUnitario 125.00 e sequencia 1

# CA-32
Cenário: Comprador negociando oferta da própria empresa
  Quando token(BUYER, FORN-B) abre negociação direta com OF-B
  Então a resposta é 403 com codigo "CONFLITO_DE_INTERESSE"

# CA-33
Cenário: Uma negociação aberta por comprador e oferta
  Dado a direta D1 em ABERTO
  Quando token(BUYER, COMP-X) abre outra negociação direta com OF-B
  Então a resposta é 409 com codigo "NEGOCIACAO_JA_ABERTA"
  Quando token(BUYER, COMP-Y) abre negociação direta com OF-B, quantidade 10.00
  Então a resposta é 201
  Dado D1 em RECUSADO
  Quando token(BUYER, COMP-X) abre negociação direta com OF-B
  Então a resposta é 201

# CA-34
Cenário: Erros na abertura da negociação direta
  Quando token(SUPPLIER, FORN-A) envia o corpo do CA-31
  Então a resposta é 403 com codigo "ACESSO_NEGADO"
  Quando token(BUYER, COMP-X) envia o corpo do CA-31 com precoUnitario 0, ou sem precoUnitario, ou com "dataInicio": "2026-11-20T13:00:00.000Z" a mais
  Então cada resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando token(BUYER, COMP-X) abre com OF-Z
  Então a resposta é 422 com codigo "OFERTA_INDISPONIVEL"
  Quando token(BUYER, COMP-X) abre com OF-B e quantidade 50.01
  Então a resposta é 422 com codigo "QUANTIDADE_INDISPONIVEL"
  Quando token(BUYER, FORN-B) abre com OF-B e quantidade 50.01
  Então a resposta é 403 com codigo "CONFLITO_DE_INTERESSE"
```

### RF-08 — Enviar contraproposta

- **Prioridade:** Must
- **Enunciado (EARS):** Enquanto a negociação direta estiver aceitando ações, quando o participante da vez enviar `POST /api/v1/negociacoes/{id}/propostas` com `precoUnitario` válido, o serviço deve registrar a proposta, passar a vez à outra parte, renovar `expiraEm` para agora + 48 h e responder 201.
- **Regras aplicáveis:** RN-17, RN-20, RN-21, RN-28
- **Interface:** API-11

```gherkin
# CA-35
Cenário: Fornecedor responde com contraproposta
  Dado a direta D1 com vez "SUPPLIER" e o relógio em "2026-11-21T12:00:00.000Z"
  Quando token(SUPPLIER, FORN-B) envia POST /api/v1/negociacoes/D1/propostas com {"precoUnitario": 128.00}
  Então a resposta é 201 com autor "SUPPLIER", precoUnitario 128.00, sequencia 2 e criadoEm "2026-11-21T12:00:00.000Z"
  E GET /api/v1/negociacoes/D1 mostra precoAtual 128.00, vez "BUYER", expiraEm "2026-11-23T12:00:00.000Z" e totalPropostas 2
  E nenhuma mensagem é publicada

# CA-36
Cenário: Proposta fora da vez
  Dado a direta D1 com vez "SUPPLIER"
  Quando token(BUYER, COMP-X) envia {"precoUnitario": 124.00}
  Então a resposta é 409 com codigo "FORA_DA_VEZ"

# CA-37
Cenário: Proposta em negociação encerrada ou vencida
  Dado a direta D1 com expiraEm "2026-11-22T12:00:00.000Z" e o relógio em "2026-11-22T12:00:00.000Z", sem executar_prazos
  Quando token(SUPPLIER, FORN-B) envia {"precoUnitario": 128.00}
  Então a resposta é 409 com codigo "NEGOCIACAO_ENCERRADA"
  Dado D1 em RECUSADO
  Quando token(SUPPLIER, FORN-B) envia {"precoUnitario": 128.00}
  Então a resposta é 409 com codigo "NEGOCIACAO_ENCERRADA"

# CA-38
Cenário: Quem não participa não vê a negociação direta
  Dado a direta D1 e o leilão L1
  Quando token(BUYER, COMP-Y) ou token(SUPPLIER, FORN-A) envia {"precoUnitario": 128.00} para D1
  Então cada resposta é 404 com codigo "NEGOCIACAO_NAO_ENCONTRADA"
  Quando token(SUPPLIER, FORN-A) envia {"precoUnitario": 128.00} para L1
  Então a resposta é 409 com codigo "MODO_INCOMPATIVEL"
```

### RF-09 — Aceitar a proposta

- **Prioridade:** Must
- **Enunciado (EARS):** Enquanto a negociação direta estiver aceitando ações e com reserva `CONFIRMADA`, quando o participante da vez enviar `POST /api/v1/negociacoes/{id}/aceitar`, o serviço deve encerrar a negociação como `ENCERRADO` pelo `precoAtual`, responder 200 e publicar `negociacao_fechada` com uma alocação.
- **Regras aplicáveis:** RN-17, RN-18, RN-20, RN-21, RN-28
- **Interface:** API-12; tópico `negociacao_fechada`

```gherkin
# CA-39
Cenário: Fornecedor aceita a proposta do comprador
  Dado a direta D1 com vez "SUPPLIER", precoAtual 125.00, reserva CONFIRMADA com PT-D e o relógio em 13:00:00
  Quando token(SUPPLIER, FORN-B) envia POST /api/v1/negociacoes/D1/aceitar sem corpo
  Então a resposta é 200 com status "ENCERRADO", encerradoEm "2026-11-20T13:00:00.000Z" e acoesPermitidas []
  E foi publicada exatamente 1 mensagem em "negociacao_fechada" com chave D1 e payload {negociacaoId D1, tipo "DIRETA", status "ENCERRADO", compradorId COMP-X, produtoId P, alocacoes [{fornecimentoId OF-B, fornecedorId FORN-B, pedidoId PT-D, quantidade 10.00, valorUnitario 125.00, valorTotal 1250.00}]}

# CA-40
Cenário: Aceite fora da vez ou com reserva pendente
  Dado a direta D1 com vez "SUPPLIER" e reserva PENDENTE
  Quando token(BUYER, COMP-X) envia aceitar
  Então a resposta é 409 com codigo "FORA_DA_VEZ"
  Quando token(SUPPLIER, FORN-B) envia aceitar
  Então a resposta é 409 com codigo "RESERVA_PENDENTE"

# CA-41
Cenário: Corpo inválido e aceite repetido
  Dado a direta D1 com vez "SUPPLIER" e reserva CONFIRMADA
  Quando token(SUPPLIER, FORN-B) envia aceitar com corpo {"precoUnitario": 1.00}
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando token(SUPPLIER, FORN-B) envia aceitar com corpo {}
  Então a resposta é 200
  Quando token(SUPPLIER, FORN-B) envia aceitar de novo, sem Idempotency-Key
  Então a resposta é 409 com codigo "NEGOCIACAO_ENCERRADA" e nenhuma nova mensagem é publicada
```

### RF-10 — Recusar a negociação direta

- **Prioridade:** Must
- **Enunciado (EARS):** Enquanto a negociação direta estiver aceitando ações, quando qualquer participante enviar `POST /api/v1/negociacoes/{id}/recusar`, o serviço deve encerrá-la como `RECUSADO`, responder 200 e publicar `negociacao_fechada` sem alocações.
- **Regras aplicáveis:** RN-17, RN-19, RN-20, RN-21, RN-28
- **Interface:** API-13; tópico `negociacao_fechada`

```gherkin
# CA-42
Cenário: Comprador recusa fora da sua vez
  Dado a direta D1 com vez "SUPPLIER"
  Quando token(BUYER, COMP-X) envia POST /api/v1/negociacoes/D1/recusar
  Então a resposta é 200 com status "RECUSADO"
  E foi publicada exatamente 1 mensagem em "negociacao_fechada" com chave D1 e payload {negociacaoId D1, tipo "DIRETA", status "RECUSADO", compradorId COMP-X, produtoId P, alocacoes []}

# CA-43
Cenário: Erros na recusa
  Dado a direta D1 em EXPIRADO e o leilão L1
  Quando token(SUPPLIER, FORN-B) envia recusar para D1
  Então a resposta é 409 com codigo "NEGOCIACAO_ENCERRADA"
  Quando token(BUYER, COMP-X) envia recusar para L1
  Então a resposta é 409 com codigo "MODO_INCOMPATIVEL"
  Quando token(CARRIER, COMP-X) envia recusar para D1
  Então a resposta é 403 com codigo "ACESSO_NEGADO"
```

### RF-11 — Expirar a negociação direta

- **Prioridade:** Must
- **Enunciado (EARS):** Quando `executar_prazos` rodar com `agora >= expiraEm` numa negociação direta `ABERTO`, o serviço deve encerrá-la como `EXPIRADO` e publicar `negociacao_fechada` sem alocações.
- **Regras aplicáveis:** RN-19, RN-27
- **Interface:** `app.main.executar_prazos`; tópico `negociacao_fechada`

```gherkin
# CA-44
Cenário: Negociação direta sem resposta por 48 h
  Dado a direta D1 com expiraEm "2026-11-22T12:00:00.000Z" e correlationId de criação "teste-neg-31"
  E o relógio em "2026-11-22T11:59:59.999Z"
  Quando executar_prazos é chamado
  Então D1 continua "ABERTO"
  Dado o relógio em "2026-11-22T12:00:00.000Z"
  Quando executar_prazos é chamado
  Então D1 fica "EXPIRADO" com encerradoEm "2026-11-22T12:00:00.000Z"
  E foi publicada 1 mensagem em "negociacao_fechada" com chave D1, correlationId "teste-neg-31" e payload {negociacaoId D1, tipo "DIRETA", status "EXPIRADO", compradorId COMP-X, produtoId P, alocacoes []}
```

### RF-12 — Consultar negociações

- **Prioridade:** Must
- **Enunciado (EARS):** Quando um token `BUYER` ou `SUPPLIER` solicitar `GET /api/v1/negociacoes` ou `GET /api/v1/negociacoes/{id}`, o serviço deve devolver as negociações visíveis, com paginação, sigilo, `agora` e `acoesPermitidas`.
- **Regras aplicáveis:** RN-13, RN-21, RN-22
- **Interface:** API-05, API-06

```gherkin
# CA-45
Cenário: Detalhe do leilão com sigilo e ações
  Dado o leilão L1 ABERTO com lances 2550.00 de COMP-X e 2600.00 de COMP-Y, e o relógio em 13:20:00
  Quando token(BUYER, COMP-X) consulta GET /api/v1/negociacoes/L1
  Então a resposta é 200 igual ao exemplo "Detalhe de leilão" do §7.2, com melhorLance.compradorId "OCULTO", agora "2026-11-20T13:20:00.000Z" e acoesPermitidas ["DAR_LANCE"]
  Quando token(BUYER, COMP-Y) consulta
  Então melhorLance.compradorId é COMP-Y
  Quando token(SUPPLIER, FORN-A) consulta
  Então melhorLance.compradorId é COMP-Y e acoesPermitidas é []
  Dado o leilão L1 AGUARDANDO_INICIO e o relógio em 12:30:00
  Quando token(SUPPLIER, FORN-C) consulta
  Então acoesPermitidas é ["ENTRAR_COM_OFERTA"]

# CA-46
Cenário: Listagem com visibilidade e paginação
  Dado o leilão L1 e a direta D1 de COMP-X, ambos criados às 12:00:00, e nenhuma outra negociação
  Quando token(BUYER, COMP-X) consulta GET /api/v1/negociacoes
  Então a resposta é 200 com page 1, size 10, total 2 e um item para L1 (status "AGUARDANDO_INICIO", dataInicio e dataFim do leilão, expiraEm null) e outro para D1 (dataInicio null, dataFim null, expiraEm "2026-11-22T12:00:00.000Z"), com os campos do exemplo da API-05
  Quando token(BUYER, COMP-Y) consulta GET /api/v1/negociacoes
  Então a resposta é 200 com total 1 e só o item de L1
  Quando token(BUYER, COMP-Y) consulta GET /api/v1/negociacoes?modo=VENDA_DIRETA
  Então a resposta é 200 com {"items": [], "page": 1, "size": 10, "total": 0}
  Quando token(BUYER, COMP-X) consulta com ?size=101, ?size=0, ?page=0, ?page=a ou ?status=CANCELADO
  Então cada resposta é 422 com codigo "VALIDACAO_INVALIDA"

# CA-47
Cenário: Detalhe invisível ou inexistente
  Dado a direta D1 de COMP-X
  Quando token(BUYER, COMP-Y) consulta GET /api/v1/negociacoes/D1
  Então a resposta é 404 com codigo "NEGOCIACAO_NAO_ENCONTRADA"
  Quando token(SUPPLIER, FORN-B) consulta GET /api/v1/negociacoes/D1 com vez "SUPPLIER" e reserva CONFIRMADA
  Então a resposta é 200 com acoesPermitidas ["PROPOR", "ACEITAR", "RECUSAR"]
  Quando token(BUYER, COMP-X) consulta GET /api/v1/negociacoes/123
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
```

### RF-13 — Listar lances e propostas

- **Prioridade:** Must
- **Enunciado (EARS):** Quando um token visível à negociação solicitar `GET /api/v1/negociacoes/{id}/lances` (leilão) ou `GET /api/v1/negociacoes/{id}/propostas` (direta), o serviço deve devolver os itens em ordem de `sequencia`, com paginação, aplicando o sigilo nos lances.
- **Regras aplicáveis:** RN-13, RN-20, RN-21
- **Interface:** API-08, API-10

```gherkin
# CA-48
Cenário: Lances com sigilo e paginação
  Dado o leilão L1 com lances 2550.00 de COMP-X e 2600.00 de COMP-Y
  Quando token(BUYER, COMP-X) consulta GET /api/v1/negociacoes/L1/lances
  Então a resposta é 200 igual ao exemplo da API-08
  Quando token(SUPPLIER, FORN-B) consulta a mesma rota
  Então os itens têm compradorId COMP-X e COMP-Y
  Quando token(BUYER, COMP-X) consulta ?page=2&size=1
  Então a resposta tem 1 item com sequencia 2, page 2, size 1 e total 2

# CA-49
Cenário: Propostas e rotas de outro modo
  Dado a direta D1 com as propostas 125.00 (BUYER) e 128.00 (SUPPLIER), e o leilão L1
  Quando token(SUPPLIER, FORN-B) consulta GET /api/v1/negociacoes/D1/propostas
  Então a resposta é 200 igual ao exemplo da API-10
  Quando token(BUYER, COMP-Y) consulta GET /api/v1/negociacoes/D1/propostas
  Então a resposta é 404 com codigo "NEGOCIACAO_NAO_ENCONTRADA"
  Quando token(BUYER, COMP-X) consulta GET /api/v1/negociacoes/L1/propostas ou GET /api/v1/negociacoes/D1/lances
  Então cada resposta é 409 com codigo "MODO_INCOMPATIVEL"
```

### RF-14 — Manter a cópia local das ofertas

- **Prioridade:** Must
- **Enunciado (EARS):** Quando uma mensagem válida de `fornecimento_criado`, `fornecimento_atualizado` ou `estoque_atualizado` chegar, o serviço deve atualizar a cópia local da oferta se o evento for mais novo que o último aplicado, sem alterar negociações já abertas.
- **Regras aplicáveis:** RN-23, RN-25
- **Interface:** tópicos `fornecimento_criado`, `fornecimento_atualizado`, `estoque_atualizado`

```gherkin
# CA-50
Cenário: Preço novo da oferta
  Dado OF-A na cópia local com preco 120.00 e atualizadoEm "2026-11-19T10:00:00.000Z"
  Quando o handler recebe o fornecimento_atualizado de docs/contratos/negociacao/fornecimento_atualizado.exemplo.json (preco 118.50, atualizadoEm "2026-11-19T18:00:00.000Z")
  E token(SUPPLIER, FORN-A) cria um leilão com OF-A e quantidade 5.00
  Então a oferta do leilão tem precoUnitario 118.50
  Quando o handler recebe um fornecimento_atualizado de OF-A com preco 99.00 e atualizadoEm "2026-11-19T17:00:00.000Z"
  Então a cópia local continua com preco 118.50

# CA-51
Cenário: Saldo da oferta
  Dado OF-A na cópia local com quantidadeDisponivel 100.00
  Quando o handler recebe o estoque_atualizado de docs/contratos/negociacao/estoque_atualizado.exemplo.json (quantidadeDisponivel 95.00)
  E token(SUPPLIER, FORN-A) cria um leilão com OF-A e quantidade 95.01
  Então a resposta é 422 com codigo "QUANTIDADE_INDISPONIVEL"
  Quando o handler recebe estoque_atualizado de OF-A com operacao "ENCERRAMENTO", quantidadeDisponivel 0 e atualizadoEm "2026-11-20T12:02:00.000Z"
  E token(SUPPLIER, FORN-A) cria um leilão com OF-A e quantidade 1.00
  Então a resposta é 422 com codigo "OFERTA_INDISPONIVEL"
  Quando o handler recebe estoque_atualizado de fornecimentoId "34000000-0000-4000-8000-000000000098", ausente da cópia local
  Então a cópia local não ganha registro novo e nada vai para "negociacao.dlt"

# CA-52
Cenário: Mudança de preço não altera o leilão em andamento
  Dado o leilão L1 com OF-A (5.00) que entrou com precoUnitario 120.00
  Quando o handler recebe fornecimento_atualizado de OF-A com preco 118.50 e atualizadoEm "2026-11-20T12:10:00.000Z"
  E a reserva de OF-A é confirmada e o lote é fixado às 13:00:00
  Então L1 mostra OF-A com precoUnitario 120.00 e precoInicial 600.00
```

### RF-15 — Autenticar e autorizar

- **Prioridade:** Must
- **Enunciado (EARS):** Se uma requisição a `/api/v1/...` chegar sem token válido, sem contexto de empresa ou com escopo não permitido, então o serviço deve responder 401, 403 ou 503 conforme o §7.1, sem alterar estado.
- **Regras aplicáveis:** RN-21, RN-28
- **Interface:** API-04 a API-13

```gherkin
# CA-53
Cenário: Token inválido
  Quando GET /api/v1/negociacoes é chamado: sem Authorization; com "Bearer " vazio; com token assinado por outra chave; com alg "HS256"; com kid "desconhecido"; com iss "outro"; com aud "outra"; sem sub; com exp igual a agora
  Então cada resposta é 401 com codigo "TOKEN_INVALIDO" e cabeçalho WWW-Authenticate "Bearer"

# CA-54
Cenário: Token sem contexto de empresa
  Quando GET /api/v1/negociacoes é chamado com token de view_mode "NONE" e tenant_id null; com view_mode "MACRO_GROUP" e tenant_id null; ou com view_mode "SINGLE" e active_scope null
  Então cada resposta é 403 com codigo "CONTEXTO_NAO_SELECIONADO"

# CA-55
Cenário: Escopos sem acesso
  Quando token(CARRIER, COMP-X) ou token(TENANT_ADMIN, COMP-X) chama GET /api/v1/negociacoes
  Então cada resposta é 403 com codigo "ACESSO_NEGADO"
  Quando token(BUYER, COMP-X) com platform_role "PLATFORM_ADMIN", roles ["SUPPLIER_SALES"] e client_id "svc-teste" cria um leilão com OF-A
  Então a resposta é 403 com codigo "ACESSO_NEGADO"

# CA-56
Cenário: Dependência indisponível e falha inesperada
  Quando chaves_publicas.obter lança exceção numa chamada a GET /api/v1/negociacoes
  Então a resposta é 503 com codigo "DEPENDENCIA_INDISPONIVEL"
  Quando repositorio.obter_negociacao lança RepositorioIndisponivel em GET /api/v1/negociacoes/L1
  Então a resposta é 503 com codigo "DEPENDENCIA_INDISPONIVEL"
  Quando repositorio.obter_negociacao lança RuntimeError em GET /api/v1/negociacoes/L1
  Então a resposta é 500 com codigo "ERRO_INTERNO"

# CA-57
Cenário: Correlação
  Quando GET /api/v1/negociacoes/L1 é chamado com X-Correlation-Id "teste-neg-57"
  Então a resposta tem X-Correlation-Id "teste-neg-57"
  Quando é chamado sem X-Correlation-Id, ou com "a b", ou com 129 caracteres "a"
  Então a resposta tem X-Correlation-Id com um UUID novo
  Quando GET /api/v1/negociacoes/123 é chamado com X-Correlation-Id "teste-neg-57"
  Então o corpo do erro tem correlationId "teste-neg-57" e instance "/api/v1/negociacoes/123"
```

### RF-16 — Idempotência das escritas HTTP

- **Prioridade:** Must
- **Enunciado (EARS):** Onde um `POST` de negócio trouxer `Idempotency-Key`, o serviço deve devolver a resposta original de uma chamada bem-sucedida com o mesmo escopo, a mesma chave e o mesmo corpo, sem repetir o efeito; com corpo diferente, deve responder 409.
- **Regras aplicáveis:** RN-24
- **Interface:** API-04, API-07, API-09, API-11, API-12, API-13

```gherkin
# CA-58
Cenário: Reenvio do mesmo lance
  Dado o leilão L1 ABERTO com precoInicial 2550.00
  E token(BUYER, COMP-X) enviou {"valor": 2550.00} com Idempotency-Key "K1" e recebeu 201
  Quando token(BUYER, COMP-X) reenvia {"valor": 2550.00} com Idempotency-Key "K1"
  Então a resposta é 201 com corpo idêntico ao da primeira
  E totalLances é 1 e há só 1 mensagem em "lance_realizado"
  Dado L1 ENCERRADO
  Quando token(BUYER, COMP-X) reenvia a mesma requisição com "K1"
  Então a resposta é 201 com o mesmo corpo

# CA-59
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

### RF-17 — Consumir mensagens com idempotência e DLT

- **Prioridade:** Must
- **Enunciado (EARS):** Quando uma mensagem consumida já processada chegar de novo, o serviço deve ignorá-la; se a mensagem for inválida ou continuar falhando após 4 execuções, então o serviço deve publicá-la em `negociacao.dlt` sem propagar exceção.
- **Regras aplicáveis:** RN-25, RN-26
- **Interface:** tópicos consumidos; tópico `negociacao.dlt`

```gherkin
# CA-60
Cenário: Cinco entregas do mesmo evento
  Dado a direta D1 com reserva PENDENTE
  Quando o handler recebe 5 vezes a mesma recusa da reserva de OF-B em D1, com o mesmo eventId "38000000-0000-4000-8000-000000000060"
  Então D1 fica "RECUSADO", há exatamente 1 mensagem em "negociacao_fechada" para D1 e evento_processado("pedidos-service", "38000000-0000-4000-8000-000000000060") é verdadeiro

# CA-61
Cenário: Mensagem inválida vai para a DLT
  Quando o handler de estoque_atualizado recebe o originalEvent de docs/contratos/negociacao/negociacao.dlt.exemplo.json (sem payload)
  Então o handler não lança exceção
  E é publicada 1 mensagem em "negociacao.dlt" com chave OF-A e payload {topicoOrigem "estoque_atualizado", originalEvent igual à mensagem recebida, error "EVENTO_INVALIDO", detalhe não vazio, stackTrace "", failedAtAttempt 1}
  Quando o handler recebe a mesma mensagem de novo
  Então é publicada outra mensagem em "negociacao.dlt"
  Quando o handler de pedido_atualizado recebe mensagens com eventType "pedido_criado", com eventVersion "2.0", com source "outro-service", com payload sem status, ou com negociacaoId e sem reservaSituacao
  Então cada uma gera 1 mensagem em "negociacao.dlt" e nenhum estado muda

# CA-62
Cenário: Chave da DLT sem aggregateId
  Quando o handler de fornecimento_criado recebe {"eventId": "E-62", "payload": {}}
  Então a mensagem em "negociacao.dlt" tem chave "E-62"
  Quando recebe {"payload": {}}
  Então a mensagem em "negociacao.dlt" tem chave "desconhecido"

# CA-63
Cenário: Falha transitória e falha persistente
  Dado o repositório lançando RepositorioIndisponivel nas 2 primeiras transações e funcionando depois
  Quando o handler recebe a confirmação da reserva de OF-A em L1
  Então OF-A fica "CONFIRMADA", clock.sleep foi chamado com 1 e 2, e nada vai para "negociacao.dlt"
  Dado o repositório lançando RepositorioIndisponivel em toda transação de processamento
  Quando o handler recebe a confirmação da reserva de OF-A em L1
  Então o handler executa o processamento 4 vezes, clock.sleep é chamado com 1, 2 e 4, e é publicada 1 mensagem em "negociacao.dlt" com error "PROCESSAMENTO_FALHOU" e failedAtAttempt 4
```

### RF-18 — Saúde, prontidão e métricas

- **Prioridade:** Must
- **Enunciado (EARS):** Quando `/health`, `/health/ready` ou `/metrics` forem chamados, o serviço deve responder sem exigir token, com prontidão 503 se o banco ou o barramento estiverem indisponíveis.
- **Regras aplicáveis:** —
- **Interface:** API-01, API-02, API-03

```gherkin
# CA-64
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
| RN-01 | Datas do leilão: `dataInicio >= agora` e `dataFim > dataInicio`, comparadas em UTC. Sem duração mínima nem máxima. Caso contrário → `422 DATAS_INVALIDAS`. (D24) | Agora 12:00: `dataInicio` 11:59:59.999 → 422; `dataInicio` 12:00:00.000 e `dataFim` 12:00:00.001 → 201. |
| RN-02 | Só oferta negociável entra num leilão ou numa negociação direta: presente na cópia local, `preco > 0` e `quantidadeDisponivel > 0`. Caso contrário → `422 OFERTA_INDISPONIVEL`. (P12, D10) | OF-Z com preço 0.00 → 422. |
| RN-03 | No leilão (criar ou entrar), o `fornecedorId` da oferta deve ser a empresa do token. Caso contrário → `403 OFERTA_DE_OUTRA_EMPRESA`. (D10) | FORN-A com OF-B → 403. |
| RN-04 | `quantidade <= quantidadeDisponivel` da cópia local. Caso contrário → `422 QUANTIDADE_INDISPONIVEL`. (D10) | OF-A com 100.00 disponíveis: 100.01 → 422; 100.00 → 201. |
| RN-05 | A oferta que entra num leilão deve ter o mesmo `produtoId` do leilão. Caso contrário → `422 PRODUTO_DIFERENTE`. (D15) | OF-C (produto Q) em L1 (produto P) → 422. |
| RN-06 | Só se entra num leilão com a entrada aberta (`status = "AGUARDANDO_INICIO"` e `agora < dataInicio`); senão → `409 ENTRADA_ENCERRADA`. Cada `fornecimentoId` entra uma vez por leilão; repetido → `409 OFERTA_JA_NO_LEILAO`. Uma mesma empresa pode entrar com ofertas diferentes. (D19) | Agora = `dataInicio` → 409. |
| RN-07 | Cada oferta que entra num leilão (inclusive a da criação) e cada negociação direta aberta publicam exatamente um `pedido_temporario_solicitado`, com chave `negociacaoId`, `tipo` (`"LEILAO"` ou `"DIRETA"`) e `precoUnitario` = preço da cópia local no momento. Esse preço fica copiado em `OfertaLeilao.precoUnitario` ou `Negociacao.precoOferta` e não muda depois. (P4, D7, D29) | Entrada de OF-B com 15.00 → 1 evento com 15.00 e 130.00. |
| RN-08 | Um `pedido_atualizado` válido com `negociacaoId`, `fornecimentoId` e `reservaSituacao` ∈ {`CONFIRMADA`, `RECUSADA`} muda a reserva da oferta correspondente (oferta do leilão ou negociação direta com aquele `fornecimentoId`) **somente se ela estiver `PENDENTE`**. `CONFIRMADA` grava também o `pedidoId`. Numa direta, `RECUSADA` leva a negociação a `RECUSADO` (RN-19). Num leilão, `RECUSADA` só marca a oferta; o leilão continua. É ignorado, sem DLT: evento sem `negociacaoId`, com outra `reservaSituacao`, de negociação ou oferta desconhecida, de negociação em estado final, ou de oferta que já não está `PENDENTE`. (D7, D21, D26, D30) | Recusa de OF-A já confirmada → ignorada. |
| RN-09 | Na primeira execução de `executar_prazos` com `agora >= dataInicio` e `loteFixado = false`: as ofertas `CONFIRMADA` naquele momento recebem `noLote = true`; `quantidadeTotal` = soma das quantidades delas; `precoInicial` = Σ (`quantidade` × `precoUnitario`), arredondado para 2 casas com `ROUND_HALF_UP`; `loteFixado = true`; com ≥ 1 oferta no lote, `status = "ABERTO"`; sem nenhuma, o status continua `AGUARDANDO_INICIO`, os campos ficam `null` e o leilão termina `DESERTO` em `dataFim`. Oferta confirmada depois fica `noLote = false` e não entra nas alocações. (D19, D21, D23) | 5 × 120.00 + 15 × 130.00 → `precoInicial` 2550.00, `quantidadeTotal` 20.00. |
| RN-10 | Lance só é aceito com o leilão aceitando lances (`status = "ABERTO"` e `agora < dataFim`). Caso contrário → `409 LEILAO_NAO_ABERTO`. (P3) | Agora = `dataFim` → 409. |
| RN-11 | O lance é o valor total pelo lote inteiro. Sem lances, `valor >= precoInicial`; com lances, `valor >` melhor lance. Sem incremento mínimo. Caso contrário → `422 LANCE_INSUFICIENTE`. A empresa do melhor lance pode dar outro lance maior. (P10, D22, D28) | Preço inicial 2550.00: 2549.99 → 422; 2550.00 → 201; depois 2550.00 → 422 e 2550.01 → 201. |
| RN-12 | Conflito de interesse: empresa que é fornecedor do leilão (oferta em qualquer situação) não dá lance nele; comprador não abre negociação direta com oferta cujo `fornecedorId` é a própria empresa. → `403 CONFLITO_DE_INTERESSE`. (D25) | Token `BUYER` da FORN-A em L1 → 403. |
| RN-13 | Sigilo: nas respostas da API-06 (`melhorLance.compradorId` e `compradorId`) e da API-08 (`items[].compradorId`), o `compradorId` aparece se for a empresa do token ou se a empresa do token for fornecedor do leilão; senão, é substituído por `"OCULTO"`. Valores nunca são ocultados. A resposta da API-09 e os eventos nunca são mascarados. (D14) | COMP-X vê o lance de COMP-Y como `"OCULTO"`; FORN-A vê `COMP-Y`. |
| RN-14 | Na execução de `executar_prazos` com `agora >= dataFim` num leilão `AGUARDANDO_INICIO` ou `ABERTO`: com `totalLances >= 1` → `ENCERRADO`, `compradorId` = empresa do melhor lance; sem lances → `DESERTO`, `compradorId` = `null`. Em ambos, `encerradoEm = agora` e publica uma única `negociacao_fechada` (D23). Se no mesmo `executar_prazos` também for a vez da RN-09, aplica primeiro a RN-09 e depois esta. (D8, D13) | 2 lances → `ENCERRADO`; 0 → `DESERTO` com `alocacoes []`. |
| RN-15 | Rateio do lance vencedor `V` entre as ofertas do lote (ordem de `entrouEm`, depois `fornecimentoId`), com `Q` = `quantidadeTotal`: `valorTotal_i` = `V × quantidade_i / Q` truncado para 2 casas (`ROUND_DOWN`); a diferença `V − Σ valorTotal_i` é somada à oferta de maior `quantidade` (empate: a primeira na ordem). `valorUnitario` de todas as alocações = `V / Q` arredondado para 2 casas com `ROUND_HALF_UP`. `valorTotal` é o valor que vale para o pagamento. (P5, D19, D28, ADR 0002) | `V` 1000.00, quantidades 1.00 e 2.00 → 333.33 e 666.67; quantidades 1, 1, 1 → 333.34, 333.33, 333.33. |
| RN-16 | No máximo uma negociação direta `ABERTO` por par (oferta, comprador). Outra abertura do mesmo comprador → `409 NEGOCIACAO_JA_ABERTA`. Compradores diferentes podem negociar a mesma oferta ao mesmo tempo; a reserva decide quem fica com o estoque. (D26) | COMP-Y em OF-B enquanto D1 está aberta → 201. |
| RN-17 | Negociação direta aceitando ações: `status = "ABERTO"` e `agora < expiraEm`; senão → `409 NEGOCIACAO_ENCERRADA`. Só o participante da `vez` propõe ou aceita; o outro → `409 FORA_DA_VEZ`. Cada proposta inverte a `vez`, grava `precoAtual` e renova `expiraEm = agora + 48 h`. Qualquer participante pode recusar enquanto a negociação aceita ações. (D17, D20) | Proposta às 2026-11-21T12:00 → `expiraEm` 2026-11-23T12:00. |
| RN-18 | O aceite exige reserva `CONFIRMADA` (senão → `409 RESERVA_PENDENTE`) e encerra como `ENCERRADO` com uma alocação {`fornecimentoId`, `fornecedorId`, `pedidoId`, `quantidade`, `valorUnitario` = `precoAtual`, `valorTotal` = `quantidade × precoAtual` com 2 casas `ROUND_HALF_UP`}. (D20) | 10.00 × 125.00 → `valorTotal` 1250.00. |
| RN-19 | Recusa (API-13 ou reserva `RECUSADA`) → `RECUSADO`; prazo vencido em `executar_prazos` → `EXPIRADO`. Ambos gravam `encerradoEm` e publicam `negociacao_fechada` com `alocacoes []` e o `compradorId` do comprador. (D20, D23) | D1 sem resposta até `expiraEm` → `EXPIRADO`. |
| RN-20 | Modo da rota: API-07, API-08 e API-09 só em `LEILAO_DIRETO`; API-10 a API-13 só em `VENDA_DIRETA`. Caso contrário → `409 MODO_INCOMPATIVEL`. | `POST /lances` em D1 → 409. |
| RN-21 | Escopos: rotas de negócio aceitam só `BUYER` e `SUPPLIER`; criar leilão e entrar com oferta exigem `SUPPLIER`; abrir direta e dar lance exigem `BUYER`; escopo errado → `403 ACESSO_NEGADO`. Visibilidade: leilão visível a todos; direta só aos participantes; invisível → `404 NEGOCIACAO_NAO_ENCONTRADA`. (D3) | `CARRIER` em qualquer rota → 403. |
| RN-22 | `acoesPermitidas` lista, nesta ordem, as ações que o token pode fazer agora: `"ENTRAR_COM_OFERTA"` (escopo `SUPPLIER` e entrada aberta); `"DAR_LANCE"` (escopo `BUYER`, leilão aceitando lances e empresa que não é fornecedor do leilão); `"PROPOR"` (participante da `vez`, direta aceitando ações); `"ACEITAR"` (como `PROPOR` e reserva `CONFIRMADA`); `"RECUSAR"` (participante, direta aceitando ações). | Comprador logo após abrir a direta → `["RECUSAR"]`. |
| RN-23 | Cópia local: `fornecimento_criado` e `fornecimento_atualizado` gravam `fornecedorId`, `produtoId`, `preco`, `quantidadeDisponivel` e `atualizadoEm` (o `criadoEm` ou `atualizadoEm` do payload); `estoque_atualizado` grava só `quantidadeDisponivel` e `atualizadoEm`. Um evento só é aplicado se o seu instante for **maior** que o `atualizadoEm` gravado (igual ou menor → ignorado). `estoque_atualizado` de oferta ausente da cópia é ignorado. A cópia nunca altera negociações já existentes. (D10, D29) | Atualização com instante anterior → ignorada. |
| RN-24 | Idempotência HTTP: escopo = (empresa, `sub`, método, caminho canônico, `Idempotency-Key`). Só respostas 2xx são memorizadas, com status, corpo, `Location` e o hash do corpo da requisição, por 24 h, na mesma transação da mudança. Mesmo escopo e mesmo corpo → mesma resposta, sem efeito e sem evento, mesmo depois do estado final. Mesmo escopo e outro corpo → `409 IDEMPOTENCIA_CONFLITO`. Sem chave, nada é memorizado. A verificação ocorre depois da autenticação, da validação, da existência e do modo, e antes das regras de domínio. | "K1" repetido → mesma resposta, 1 lance. |
| RN-25 | Consumo: a validação (RN-26) vem antes da deduplicação. Mensagem válida com (`source`, `eventId`) já processado → sem efeito, sem publicação. Efeito e `registrar_evento_processado` na mesma transação. Se a transação falhar (`RepositorioIndisponivel` ou outra exceção), o handler repete o processamento até 4 execuções no total, chamando `clock.sleep(1)`, `clock.sleep(2)` e `clock.sleep(4)` antes da 2ª, da 3ª e da 4ª; falhando a 4ª, publica em `negociacao.dlt` com `error` `"PROCESSAMENTO_FALHOU"` e `failedAtAttempt` 4. | 5 entregas iguais → 1 efeito. |
| RN-26 | Mensagem consumida inválida (§7.3) não altera estado e é publicada em `negociacao.dlt` na hora, com `error` `"EVENTO_INVALIDO"`, `failedAtAttempt` 1 e chave conforme §7.4. Não é registrada como processada. O handler nunca propaga exceção. | Envelope sem `payload` → DLT com chave OF-A. |
| RN-27 | `executar_prazos` processa cada identificador de `listar_vencidas(agora, 100)` numa transação própria: obtém a negociação com `bloquear=True`, verifica de novo a condição e só então aplica RN-09, RN-14 ou RN-19. Estados finais (`ENCERRADO`, `DESERTO`, `RECUSADO`, `EXPIRADO`) não mudam mais por nenhum caminho. Chamar duas vezes no mesmo instante não repete efeitos. | Segunda chamada às 15:00 → devolve 0, sem novo evento. |
| RN-28 | Requisição rejeitada (qualquer 4xx ou 5xx) não altera estado, não memoriza idempotência e não publica evento. | Lance com 422 → `totalLances` igual. |

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
| ACESSO_NEGADO | 403 | Acesso negado | Você não tem permissão para esta operação. | RN-21 (escopo). |
| CONFLITO_DE_INTERESSE | 403 | Acesso negado | A empresa não pode negociar com uma oferta própria. | RN-12. |
| OFERTA_DE_OUTRA_EMPRESA | 403 | Acesso negado | A oferta pertence a outra empresa. | RN-03. |
| NEGOCIACAO_NAO_ENCONTRADA | 404 | Não encontrado | Negociação não encontrada. | `negociacaoId` inexistente ou invisível (RN-21). |
| ENTRADA_ENCERRADA | 409 | Conflito | O leilão não aceita novas ofertas. | RN-06. |
| OFERTA_JA_NO_LEILAO | 409 | Conflito | A oferta já está neste leilão. | RN-06. |
| LEILAO_NAO_ABERTO | 409 | Conflito | O leilão não está aceitando lances. | RN-10. |
| NEGOCIACAO_JA_ABERTA | 409 | Conflito | Já existe uma negociação aberta desta empresa para esta oferta. | RN-16. |
| NEGOCIACAO_ENCERRADA | 409 | Conflito | A negociação não está aberta. | RN-17. |
| FORA_DA_VEZ | 409 | Conflito | Aguarde a resposta da outra parte. | RN-17. |
| RESERVA_PENDENTE | 409 | Conflito | A reserva do estoque ainda não foi confirmada. | RN-18. |
| MODO_INCOMPATIVEL | 409 | Conflito | Operação não disponível para o modo desta negociação. | RN-20. |
| IDEMPOTENCIA_CONFLITO | 409 | Conflito | A chave de idempotência já foi usada com outro corpo. | RN-24. |
| VALIDACAO_INVALIDA | 422 | Entrada inválida | Os dados da requisição são inválidos. | Corpo, ID na URL, query, `Idempotency-Key` ou paginação fora do contrato (§7.1, §7.2). |
| DATAS_INVALIDAS | 422 | Entrada inválida | A data de início deve ser igual ou posterior ao momento atual e a data de fim, posterior à data de início. | RN-01. |
| OFERTA_INDISPONIVEL | 422 | Entrada inválida | A oferta não existe ou está com preço ou quantidade zerados. | RN-02. |
| QUANTIDADE_INDISPONIVEL | 422 | Entrada inválida | A quantidade é maior que o saldo disponível da oferta. | RN-04. |
| PRODUTO_DIFERENTE | 422 | Entrada inválida | A oferta é de um produto diferente do leilão. | RN-05. |
| LANCE_INSUFICIENTE | 422 | Entrada inválida | O lance deve ser maior que o melhor lance atual e não menor que o preço inicial. | RN-11. |
| ERRO_INTERNO | 500 | Erro interno | Não foi possível processar a requisição. | Exceção não prevista. |
| DEPENDENCIA_INDISPONIVEL | 503 | Serviço indisponível | Uma dependência necessária está indisponível. | JWKS indisponível, `RepositorioIndisponivel`, ou prontidão falhando (API-02). |

Respostas fora do catálogo: rota inexistente (404) e método não permitido (405) mantêm a resposta padrão do framework; `/docs` e `/openapi.json` ficam habilitados. Nenhuma delas faz parte da avaliação de conformidade.

**Ordem de verificação** (quando mais de um erro se aplica, retorna-se o primeiro):

1. Correlação (§7.1; nunca é erro).
2. `TOKEN_INVALIDO`, ou `DEPENDENCIA_INDISPONIVEL` se as chaves públicas falharem.
3. `CONTEXTO_NAO_SELECIONADO`.
4. `ACESSO_NEGADO` pelo escopo da rota (na API-04, primeiro "escopo fora de `BUYER`/`SUPPLIER`"; o confronto escopo × `modo` vem logo depois do passo 5).
5. `VALIDACAO_INVALIDA` (ID na URL, query, `Idempotency-Key`, corpo).
6. `NEGOCIACAO_NAO_ENCONTRADA`.
7. `MODO_INCOMPATIVEL`.
8. Idempotência: replay da resposta memorizada ou `IDEMPOTENCIA_CONFLITO`.
9. Regras de domínio, na ordem da coluna "Erros possíveis" do §7.2 para cada rota.

`DEPENDENCIA_INDISPONIVEL` (banco) e `ERRO_INTERNO` podem ocorrer em qualquer passo que use o repositório. A validação automática de corpo do FastAPI não pode ser usada nas rotas com corpo, porque responderia antes dos passos 2 a 4.

## 11. Requisitos não funcionais

| ID | Característica (ISO/IEC 25010) | Requisito | Métrica e limiar | Verificação |
|---|---|---|---|---|
| RNF-01 | Adequação funcional — exatidão | Aritmética monetária com `Decimal` e saída com 2 casas. | Lote de 1.00 × 0.10 e 1.00 × 0.20 → `precoInicial` 0.30 e lance 0.30 aceito; lance `2550` devolvido como `2550.00`; soma das alocações = lance vencedor em 100% dos encerramentos. | CA-65 |
| RNF-02 | Interoperabilidade | Formato único de erro. | 100% das respostas 4xx e 5xx de negócio em `application/problem+json` com os 8 campos do §10. | CA-66 |
| RNF-03 | Confiabilidade — ordenação | Eventos da mesma negociação na mesma partição e envelope do kit. | 100% das mensagens publicadas (exceto DLT) com `key == aggregateId == negociacaoId`, `eventType` = tópico, `eventVersion` `"1.0"`, `source` `"negociacao-service"`, `timestamp` = relógio e `eventId` UUID distinto; 100% válidas contra `docs/contratos/negociacao/<topico>.schema.json`. | CA-67 |
| RNF-04 | Manutenibilidade — modularidade | Dependências permitidas e estado só nas portas. | Nenhum import fora do §5 nos módulos de domínio; duas aplicações de `create_app` com repositórios diferentes não compartilham negociações. | CA-68 |
| RNF-05 | Confiabilidade — idempotência do consumidor | Reentrega não repete efeito, com o serviço real. | 5 entregas do mesmo `pedido_atualizado` `RECUSADA` no Redpanda → 1 `negociacao_fechada` e 1 linha em `processed_events`. | CA-69 |
| RNF-06 | Confiabilidade — concorrência | Lances serializados com duas réplicas. | 50 lances concorrentes, com valores distintos de 2550.00 a 2599.00, enviados às duas réplicas pelo gateway → sequências aceitas 1..n sem buracos, cada lance aceito maior que o anterior, melhor lance = maior valor aceito, 0 respostas 5xx. | CA-70 |
| RNF-07 | Confiabilidade — prazos | Encerramento único e pontual com duas réplicas. | Com as duas réplicas e o agendador ativo, 1 leilão com `dataFim` em T → exatamente 1 `negociacao_fechada`, publicada em até 2 s após T; após reiniciar as réplicas paradas desde antes de T, o encerramento ocorre em até 5 s após a subida. | CA-71 |
| RNF-08 | Eficiência de desempenho | Latência no kit, via gateway, duas réplicas. | Escrita de lance p95 ≤ 1000 ms com 20 usuários simultâneos por 2 min; leitura (API-05, API-06, API-08) p95 ≤ 500 ms com 200 usuários simultâneos por 2 min, após 30 s de aquecimento; respostas 5xx < 1%. | CA-72 |
| RNF-09 | Segurança — controle de acesso | RBAC por escopo. | 100% das chamadas às rotas de negócio com token `CARRIER` ou `TENANT_ADMIN`, e de lance com `SUPPLIER`, → 403 `ACESSO_NEGADO`, sem mudança de estado. | CA-55, CA-23 |
| RNF-10 | Confiabilidade — publicação durável | Outbox com mesmo `eventId`. | Publicador interrompido antes da confirmação → após a retomada por outra réplica, toda cópia publicada tem o `eventId` gravado na outbox; 0 eventos perdidos. | CA-73 |
| RNF-11 | Segurança — isolamento de dados | Schema próprio e pool. | Com `svc_negociacao`, DML no schema `negociacao` funciona e `SELECT` em `pedidos` falha; 0 FKs para outros schemas; pool com no máximo 5 conexões por réplica sob a carga do RNF-08. | CA-74 |
| RNF-12 | Disponibilidade — prontidão | Degradação observável. | Banco ou Redpanda parados → `/health` 200 e `/health/ready` 503 em até 10 s; dependência de volta → `/health/ready` 200 em até 10 s. | CA-75 |

```gherkin
# CA-65
Cenário: Exatidão decimal
  Dado o leilão L4 de FORN-A com OF-E (1.00) e FORN-B com OF-F (1.00), ambas confirmadas, e o lote fixado
  Então precoInicial é 0.30
  Quando token(BUYER, COMP-X) envia {"valor": 0.30}
  Então a resposta é 201 com valor 0.30
  E no leilão L1 com precoInicial 2550.00, o lance {"valor": 2550} é devolvido com valor 2550.00

# CA-66
Cenário: Formato de erro
  Para cada resposta 4xx e 5xx produzida pelos CA-01 a CA-64
  Então o Content-Type é "application/problem+json" e o corpo tem exatamente as chaves type, title, status, codigo, detail, instance, correlationId e timestamp, com title, status e detail iguais à linha do codigo no §10

# CA-67
Cenário: Envelope, chave e schema
  Para cada mensagem publicada nos CA-01 a CA-64, exceto em "negociacao.dlt"
  Então key == aggregateId == payload.negociacaoId, eventType é o tópico, eventVersion "1.0", source "negociacao-service", timestamp igual ao relógio, eventId UUID diferente dos demais
  E a mensagem é válida contra docs/contratos/negociacao/<topico>.schema.json
  E cada arquivo docs/contratos/negociacao/<topico>.exemplo.json é válido contra o schema do mesmo tópico

# CA-68
Cenário: Dependências e isolamento
  Ao inspecionar os imports dos arquivos .py de app/, exceto app/adaptadores/
  Então só há biblioteca padrão, fastapi, starlette, pydantic, jwt, cryptography, simplejson, prometheus_client ou app
  E duas aplicações de create_app com repositórios em memória diferentes: L1 criado na primeira → GET na segunda responde 404

# CA-69
Cenário: Idempotência do consumidor no Redpanda (integração)
  Dado o serviço real com PostgreSQL e Redpanda e uma direta D com reserva PENDENTE
  Quando o mesmo envelope de pedido_atualizado RECUSADA para D é produzido 5 vezes no tópico
  Então, em até 10 s, há 1 mensagem lógica em "negociacao_fechada" para D e 1 linha em negociacao.processed_events com aquele eventId

# CA-70
Cenário: Lances concorrentes em duas réplicas (integração)
  Dado o leilão ABERTO com precoInicial 2550.00 e as réplicas app1 e app2 atrás do gateway
  Quando 50 compradores enviam ao mesmo tempo lances de 2550.00, 2551.00, … 2599.00
  Então todas as respostas são 201 ou 422 "LANCE_INSUFICIENTE", nenhuma é 5xx
  E os lances aceitos têm sequencias 1..n sem buracos, valores estritamente crescentes por sequencia, e melhorLance é o de maior valor aceito
  E há exatamente n mensagens em "lance_realizado"

# CA-71
Cenário: Encerramento com duas réplicas (integração)
  Dado um leilão ABERTO com 1 lance e dataFim T, e as duas réplicas com o agendador ativo
  Então em até 2 s após T há exatamente 1 mensagem em "negociacao_fechada" para ele
  Dado outro leilão com dataFim T2 e as duas réplicas paradas antes de T2
  Quando as réplicas sobem depois de T2
  Então em até 5 s há exatamente 1 mensagem em "negociacao_fechada" para ele

# CA-72
Cenário: Carga (integração, k6)
  Dado o kit com gateway e duas réplicas, 100 leilões ABERTO e 1.000 negociações
  Quando o roteiro k6 roda 2 min de lances com 20 usuários e 2 min de leituras com 200 usuários, após 30 s de aquecimento
  Então o p95 de POST /lances é ≤ 1000 ms, o p95 das leituras é ≤ 500 ms e a taxa de 5xx é < 1%

# CA-73
Cenário: Outbox (integração)
  Dado um lance aceito com a publicação travada antes da confirmação do broker
  Quando a réplica é encerrada e a outra assume a outbox
  Então "lance_realizado" chega ao tópico com o eventId gravado em negociacao.outbox e nenhuma cópia tem outro eventId

# CA-74
Cenário: Isolamento do banco (integração)
  Dado a conexão com a role svc_negociacao
  Então INSERT e SELECT em negociacao.lance funcionam, SELECT em pedidos.pedido falha por permissão, a migração não cria FK fora do schema negociacao e pg_stat_activity mostra no máximo 5 conexões por réplica durante o CA-72

# CA-75
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
| RF-01 | RN-01, RN-02, RN-03, RN-04, RN-07, RN-28 | API-04, `pedido_temporario_solicitado`, `leilao_iniciado` | CA-01 a CA-07 |
| RF-02 | RN-02 a RN-07, RN-20, RN-28 | API-07, `pedido_temporario_solicitado` | CA-08 a CA-11 |
| RF-03 | RN-08, RN-19, RN-25 | `pedido_atualizado`, `negociacao_fechada` | CA-12 a CA-15; erro: mensagens que não se aplicam são ignoradas (CA-14) e as inválidas são cobertas pelo RF-17 (CA-61) |
| RF-04 | RN-09, RN-27 | `executar_prazos` | CA-16 a CA-18; sem cenário de erro: é um processo interno sem entrada do usuário; o caso negativo é o lote vazio (CA-18) |
| RF-05 | RN-10, RN-11, RN-12, RN-20, RN-28 | API-09, `lance_realizado` | CA-19 a CA-25 |
| RF-06 | RN-14, RN-15, RN-27 | `executar_prazos`, `negociacao_fechada` | CA-26 a CA-30; sem cenário de erro: processo interno; o caso negativo é o prazo ainda não vencido (CA-28) |
| RF-07 | RN-02, RN-04, RN-07, RN-12, RN-16, RN-17, RN-28 | API-04, `pedido_temporario_solicitado` | CA-31 a CA-34 |
| RF-08 | RN-17, RN-20, RN-21, RN-28 | API-11 | CA-35 a CA-38 |
| RF-09 | RN-17, RN-18, RN-20, RN-21, RN-28 | API-12, `negociacao_fechada` | CA-39 a CA-41 |
| RF-10 | RN-17, RN-19, RN-20, RN-21, RN-28 | API-13, `negociacao_fechada` | CA-42, CA-43 |
| RF-11 | RN-19, RN-27 | `executar_prazos`, `negociacao_fechada` | CA-44; sem cenário de erro: processo interno; o caso negativo é o prazo não vencido (CA-44) |
| RF-12 | RN-13, RN-21, RN-22 | API-05, API-06 | CA-45 a CA-47 |
| RF-13 | RN-13, RN-20, RN-21 | API-08, API-10 | CA-48, CA-49 |
| RF-14 | RN-23, RN-25 | `fornecimento_criado`, `fornecimento_atualizado`, `estoque_atualizado` | CA-50 a CA-52; mensagens inválidas pelo RF-17 (CA-61, CA-62) |
| RF-15 | RN-21, RN-28 | API-04 a API-13 | CA-53 a CA-57 |
| RF-16 | RN-24 | API-04, API-07, API-09, API-11, API-12, API-13 | CA-58, CA-59 |
| RF-17 | RN-25, RN-26 | tópicos consumidos, `negociacao.dlt` | CA-60 a CA-63 |
| RF-18 | — | API-01, API-02, API-03 | CA-64 |
| RNF-01 a RNF-12 | RN-09, RN-11, RN-15, RN-24 a RN-27 | todas | CA-65 a CA-75, CA-23, CA-55 |

Atributos, projeções e envelopes: os atributos das entidades estão no §6.1; os campos derivados das respostas (`melhorLance`, `agora`, `acoesPermitidas`) e as projeções (resumo, oferta do leilão, lance, proposta) estão no §7.2; os campos de envelope estão no §7.4. Não aparecem na API: `loteFixado`, `melhorLanceId`, `usuarioCriador`, `correlationId`, `pedidoId` (oferta e direta), `Lance.usuarioId` e `Proposta.empresaId`.

## 13. Premissas, decisões e questões em aberto

Decisões do autor (Caio Almeida Oliveira). Fontes: conversas gravadas com o professor em 08/10/2026 e 12/11/2026 (P1 a P12 abaixo), kit de Infraestrutura, ESP-PED-001, contratos do Fornecimento v2.0, Identity-Service, DR-BD-002, planejamento inicial do grupo (orientação, não regra).

Regras do professor citadas: P1 (o leilão existe quando a demanda supera a oferta; verificação do Mercado, fora deste serviço); P2 (o fornecedor decide abrir o leilão); P3 (leilão entre `dataInicio` e `dataFim`; compradores disputam); P4 (pedido temporário sem comprador na criação do leilão; Professor, 12/11/2026); P5 (leilão conjunto com lance inicial pela soma ponderada e rateio pelo volume; Professor, 08/10/2026 e 12/11/2026); P6 (negociação direta parte do preço único da oferta; pagamento depois); P7 (pedido cancelado devolve o estoque); P8 (versão básica primeiro); P9 (toda rota autenticada com escopo); P10 (lance no lote inteiro, um vencedor; Professor, 12/11/2026); P11 ("leilão reverso" não é leilão; Professor, 12/11/2026); P12 (oferta com preço ou quantidade zero não é negociada; Professor, 12/11/2026).

| ID | Tipo | Descrição | Decisão |
|---|---|---|---|
| D1 | Decisão (Caio, 2026-11-16) | Envelope dos eventos | Envelope do `CONTRACTS.md` do kit (§7.4), `source` `"negociacao-service"`, `aggregateId` = `negociacaoId`, chave Kafka = `aggregateId`. Motivo: contrato comum da turma. |
| D2 | Decisão (Caio, 2026-11-16) | Formato de erro | RFC 7807 com `codigo` e `correlationId`, igual à ESP-PED-001 §6.3 (§10). Motivo: Pedidos, Fornecimento e Logística usam o mesmo; a divergência com o RFC 9457 do Identity fica na INT-NEG-07. |
| D3 | Decisão (Caio, 2026-11-16) | Autenticação | Validação local do JWT RS256 do Identity (JWKS com cache de 1 h); `view_mode` `SINGLE` com `tenant_id`; `BUYER` dá lance e abre direta; `SUPPLIER` cria e entra em leilão e responde propostas; `CARRIER` e `TENANT_ADMIN` → 403; `roles`, `permissions`, `platform_role` e `act` ignorados; JWKS indisponível → 503 (§7.1). |
| D4 | Decisão (Caio, 2026-11-16) | Persistência | PostgreSQL 16, schema `negociacao`, role `svc_negociacao`, outbox transacional, `processed_events`, duas réplicas, lances serializados pelo bloqueio da linha do leilão (§5.1, §6.1). |
| D5 | Decisão (Caio, 2026-11-16) | Modos da v1 | `LEILAO_DIRETO` e `VENDA_DIRETA`; sem `LEILAO_REVERSO` (P11). |
| D6 | Decisão (Caio, 2026-11-16) | Status | Leilão `AGUARDANDO_INICIO` → `ABERTO` → `ENCERRADO`/`DESERTO`; direta `ABERTO` → `ENCERRADO`/`RECUSADO`/`EXPIRADO`; sem `CANCELADO` (§6.2). |
| D7 | Decisão (Caio, 2026-11-16) | Pedido temporário | Tópico novo `pedido_temporario_solicitado`, um por oferta do leilão e um por direta; o resultado da reserva vem de `pedido_atualizado` do Pedidos; não consome eventos de reserva do Fornecimento (RN-07, RN-08). |
| D8 | Decisão (Caio, 2026-11-13) | Encerramento | Automático em `dataFim`, por agendador idempotente e seguro com duas réplicas, que também processa o que venceu com o serviço parado; sem rota de encerramento manual e sem administrador (RN-14, RN-27). |
| D9 | Decisão (Caio, 2026-11-13; corrigida pela D28 em 2026-11-17) | Dinheiro | Número JSON com até 2 casas na API, `Decimal` no código, `NUMERIC(18,4)` no banco (ADR 0001). O lance é o valor total do lote (D28). |
| D10 | Decisão (Caio, 2026-11-16; ampliada pela D29) | Dados da oferta | Cópia local das ofertas a partir dos eventos do Fornecimento; validação de dono, produto, preço e quantidade > 0 e saldo; preço sempre o da oferta (RN-02 a RN-05, RN-23). |
| D11 | Decisão (Caio, 2026-11-16) | `negociacao_fechada` | Um evento por negociação com `status`, `tipo`, `compradorId`, `produtoId` e `alocacoes` já rateadas; deserto com `alocacoes []`; destino da entrega fica com o Pedidos (§7.4). |
| D12 | Decisão (Caio, 2026-11-16) | Tempo real | Consulta periódica dos `GET`; SSE e WebSocket fora do escopo. |
| D13 | Decisão (Caio, 2026-11-13) | Cancelamento | A v1 não cancela leilão; ele termina só em `dataFim`. O cancelamento de demanda não afeta o leilão. |
| D14 | Decisão (Caio, 2026-11-13) | Sigilo | Valores visíveis a todos; nome `"OCULTO"` para compradores concorrentes; fornecedores do leilão veem os nomes; eventos sem máscara (RN-13). |
| D15 | Decisão (Caio, 2026-11-13) | Leilão conjunto | Na v1 (ADR 0002): várias ofertas do mesmo produto, um pedido temporário por oferta, preço inicial pela soma e rateio pelo volume. |
| D16 | Decisão (Caio, 2026-11-16) | Quem abre o leilão | O fornecedor cria o leilão neste serviço; outros entram até `dataInicio`; este serviço publica `leilao_iniciado`; a sugestão de leilão é do Mercado, fora do escopo; nenhum evento do Mercado é consumido (INT-NEG-03). |
| D17 | Decisão (Caio, 2026-11-13) | Negociação direta | Proposta e contraproposta de preço a partir do preço da oferta, antes de fechar (RN-17). |
| D18 | Decisão (Caio, 2026-11-16) | Tabelas | `processo_negociacao` (nomes do DR-BD-002: `modo`, `status`, `data_inicio`, `data_fim`, `valor_reserva` = preço inicial), `oferta_leilao`, `lance`, `proposta`, `oferta_referencia`, `idempotency_keys`, `processed_events`, `outbox` (§6.1). |
| D19 | Decisão (Caio, 2026-11-13) | Entrada no conjunto | Fornecedores entram até `dataInicio`; depois o lote fica fixo; preço inicial = Σ quantidade × preço; centavo do rateio para a oferta de maior volume, empate para a que entrou primeiro (RN-06, RN-09, RN-15). |
| D20 | Decisão (Caio, 2026-11-13) | Fluxo da direta | O comprador abre com quantidade e preço, o que pede o pedido temporário; partes alternam contrapropostas; qualquer uma aceita ou recusa; 48 h sem resposta expira; forma de pagamento fora da v1 (RN-17 a RN-19). |
| D21 | Decisão (Caio, 2026-11-16) | Reserva da oferta | Só a oferta com reserva confirmada entra no lote; pendente não conta; recusada não entra e o leilão continua; sem oferta confirmada em `dataInicio`, termina deserto em `dataFim` (RN-08, RN-09). |
| D22 | Decisão (Caio, 2026-11-16) | Quem dá lance | Qualquer empresa com escopo `BUYER`, sem exigir demanda; sem incremento mínimo; primeiro lance ≥ preço inicial, os seguintes estritamente maiores (RN-11). |
| D23 | Decisão (Caio, 2026-11-16) | Liberação dos pedidos temporários | `negociacao_fechada` em todo final; o Pedidos cancela os pedidos temporários fora de `alocacoes`; sem tópico de liberação; a oferta confirmada depois de `dataInicio` fica reservada até o fechamento (RN-09, RN-14, RN-19). |
| D24 | Decisão (Caio, 2026-11-16) | Datas | `dataInicio` ≥ agora e `dataFim` > `dataInicio`; sem duração mínima nem máxima (RN-01). |
| D25 | Decisão (Caio, 2026-11-16) | Conflito de interesse | Empresa com oferta no leilão não dá lance; comprador não negocia oferta da própria empresa; ambos 403 (RN-12). |
| D26 | Decisão (Caio, 2026-11-16) | Diretas simultâneas | Várias diretas na mesma oferta; a reserva decide; uma aberta por comprador e oferta (RN-16, RN-08). |
| D27 | Decisão (Caio, 2026-11-16) | Empresa ou usuário bloqueado | Não consome eventos do Identity; token revogado é aceito até expirar (15 min no máximo) (§7.1). |
| D28 | Decisão (Caio, 2026-11-17) | Valor do lance | O lance é o **valor total pelo lote**, não o preço unitário. Motivo: coerente com P5 (o valor pago é rateado), P10 (lance no lote inteiro), D19, D22 e a ADR 0001. Corrige a D9 e o C13 do vault. Na negociação direta, a proposta continua sendo preço unitário (P6). |
| D29 | Decisão (Caio, 2026-11-17) | Mudança de preço da oferta | Consumir também `fornecimento_atualizado` (contrato v2.0 do Fornecimento, 16/11/2026), para a cópia local ter o preço vigente. O preço fica congelado na oferta do leilão ou na direta no momento da entrada, porque é com ele que o pedido temporário é pedido (RN-07, RN-23). |
| D30 | Decisão (Caio, 2026-11-17) | Sinal da reserva | Campo novo `reservaSituacao` (`PENDENTE`, `CONFIRMADA`, `RECUSADA`, `ENCERRADA`) no `pedido_atualizado`, junto com `negociacaoId` e `fornecimentoId`; só `CONFIRMADA` e `RECUSADA` mudam a oferta. Motivo: aditivo, não mexe nos status do Pedidos, e o Pedidos já guarda essa situação (ESP-PED-001 §4). Contrato candidato (INT-NEG-02). |
| PR-01 | Premissa (derivada) | Momento do `leilao_iniciado` | Publicado na criação do leilão, com a primeira oferta, porque é a "abertura do leilão" de P4. A fixação do lote e o encerramento não publicam `leilao_iniciado`. Revisar com o Caio no G1. |
| PR-02 | Premissa (derivada) | Lote vazio em `dataInicio` | O leilão fica `AGUARDANDO_INICIO` com `loteFixado` verdadeiro até `dataFim` e termina `DESERTO` (leitura literal da D21: "os lances não abrem"). |
| PR-03 | Premissa (derivada) | Autor do melhor lance | Sem regra de "líder": a empresa do melhor lance pode dar outro maior (D22 não proíbe). |
| PR-04 | Premissa (derivada) | Aceite e vez | Só a parte da vez aceita (aceita a proposta da outra parte); o aceite exige reserva confirmada, para não fechar venda sem estoque reservado; o prazo de 48 h conta da última proposta. |
| PR-05 | Premissa (alinhamento) | Quantidade | Decimal com até 2 casas, como no Fornecimento (`NUMERIC(12,2)`) e no Pedidos; o DR-BD-002 usa inteiro (INT-NEG-06). |
| PR-06 | Premissa (alinhamento) | `valorUnitario` no leilão | `V / Q` com 2 casas, igual para todas as alocações; `valorTotal` é o valor que vale (RN-15). O Pedidos não deve recalcular o total a partir do unitário (INT-NEG-01). |
| PR-07 | Premissa (Identity, revisão `800f2d8`) | Tokens de serviço | `client_id` e `token_use = "service"` são ignorados; o token vale pelo `tenant_id` e `active_scope`, como os demais. Sem tolerância (`leeway`) na expiração. |
| PR-08 | Premissa (UX-04) | Tela de negociação | A API devolve prazos absolutos do servidor (`dataInicio`, `dataFim`, `expiraEm`), `agora` e `acoesPermitidas`, para a tela calcular a contagem regressiva e habilitar as ações. |
| INT-NEG-01 | Questão de integração → **Pedidos** (Brunno, Daniel) | `negociacao_fechada` e pedidos temporários | Diferenças em relação à ESP-PED-001 §7.2 (INT-PED-01): (a) uma lista `alocacoes` em vez de `fornecedorId`, `fornecimentoId`, `quantidade` e `valorUnitario` no topo; (b) campo `tipo`; (c) status `RECUSADO` e `EXPIRADO`, além de `ENCERRADO` e `DESERTO`; (d) sem `demandaId`, `destino` e `dataEntregaDesejada`: o destino fica com o Pedidos; (e) `compradorId` `null` no deserto; (f) `valorTotal` de cada alocação é o valor a cobrar. Também: consumir `pedido_temporario_solicitado` e criar o pedido sem comprador (INT-PED-04); vários pedidos por negociação (`UNIQUE(negociacao_id, fornecimento_id)` em vez de `UNIQUE(negociacao_id)`); cancelar os pedidos temporários fora de `alocacoes`. Situação: mensagem escrita, a enviar. Neste serviço vale o contrato desta ESP. |
| INT-NEG-02 | Questão de integração → **Pedidos** | `pedido_atualizado` | Incluir `negociacaoId`, `fornecimentoId` e `reservaSituacao` (D30) nos eventos dos pedidos temporários. Situação: a confirmar. |
| INT-NEG-03 | Questão de integração → **Mercado** (Brunno, Daniel) | Quem abre o leilão | O Mercado deixa de publicar `leilao_iniciado` e só sugere o leilão; este serviço não consome `modo_negociacao_definido`; a checagem demanda > oferta fica só na sugestão; o "leilão reverso" (mostrar o menor preço) sai deste serviço; corrigir "leilões (direto/reverso)" no planejamento. Situação: mensagem escrita, a enviar. |
| INT-NEG-04 | Questão de integração → **Fornecimento** (Davi e grupo) | Consumo das ofertas | Registrar o `negociacao-service` como consumidor de `fornecimento_criado`, `fornecimento_atualizado` e `estoque_atualizado`. A reserva continua pelo `pedido_criado` com `compradorId` nulo (contrato v1.3 do Fornecimento). Situação: a comunicar. |
| INT-NEG-05 | Questão de integração → **Infra** (Pedro) | Tópicos e catálogo | Criar `pedido_temporario_solicitado` e `fornecimento_atualizado` (este pedido também pelo Fornecimento, P-37) com 3 partições; trocar o produtor de `leilao_iniciado` para `negociacao-service`; registrar este serviço como consumidor de `pedido_atualizado`, `fornecimento_criado`, `fornecimento_atualizado` e `estoque_atualizado`; ACLs; rota do gateway para as duas réplicas. Situação: última etapa, depois das respostas da INT-NEG-01 a 03. |
| INT-NEG-06 | Questão de integração → **Banco de Dados** | DDL do schema `negociacao` | Validar as tabelas do §6.1, `NUMERIC(18,4)` (ADR 0001), quantidade `NUMERIC(12,2)` (o DR-BD-002 usa `INT`) e a tabela `lance`, que o DR-BD-002 não prevê no modelo único em `public`. Situação: pendente. |
| INT-NEG-07 | Questão de integração → **Autenticação** | Erro e contexto | Formato de erro `codigo`/`correlationId` (RFC 7807) × `code`/`correlation_id` (RFC 9457, D-ECO-07), mesma pendência da INT-PED-08; 403 `CONTEXTO_NAO_SELECIONADO` em vez de 401 para token sem empresa. O seed tem uma só empresa fornecedora e uma compradora sem oferta: **não será pedida mudança** (decisão do Caio, 2026-11-16); a demonstração usa o emissor de tokens de teste. Situação: registrada. |
| INT-NEG-08 | Questão de integração → **UX/UI** | Tela de negociação | A tela usa consulta periódica (D12), `agora`, `acoesPermitidas`, prazos absolutos e `"OCULTO"`; a referência UX-04 mostra "leilão reverso" e "selecionar fornecedor", que não existem neste serviço. Situação: a comunicar. |
| INT-NEG-09 | Questão de integração → **Demanda** | Nenhuma dependência | Este serviço não consome eventos da Demanda; o cancelamento de demanda não afeta leilão (D13). Situação: informativa. |
| INT-NEG-10 | Questão → **professor** | Confirmação de regras | Confirmar que o lance é o valor total do lote (D28) e a regra do centavo que sobra no rateio (D19). Situação: a perguntar. |
| INT-NEG-11 | Questão → **QA** | Gate G1 | Submeter esta ESP ao gate G1 e combinar a suíte de aceitação em memória (§5.1) e a de integração (CA-69 a CA-75). Situação: a submeter. |

### 13.1 Casos de borda obrigatórios

| Caso de borda | Decisão |
|---|---|
| String vazia ou só com espaços em campos obrigatórios | Inválida: `422 VALIDACAO_INVALIDA` na API; mensagem inválida (DLT) no consumo. |
| Cabeçalho opcional presente porém vazio | `Idempotency-Key` e `X-Correlation-Id` vazios equivalem a ausentes. |
| Mensagem duplicada **e** inválida | A validação vem antes da deduplicação: vai para a DLT de novo (RN-25, RN-26). |
| Campo de chave/ID com tipo inesperado | Na API: `422 VALIDACAO_INVALIDA`. No consumo: mensagem inválida. Na chave da DLT, `eventId` não string é convertido com `str`; ausente, nulo ou vazio → `"desconhecido"` (§7.4). |
| Data/hora sem fuso horário | Na API e no consumo: inválida. `clock.now()` sem fuso: tratar como UTC. |
| Falha da porta de publicação / banco | Banco: `RepositorioIndisponivel` → 503 na API e novas tentativas no consumo (RN-25). `publish` dentro da transação: no adaptador real é a gravação na outbox, que falha junto com a transação; falhas do envio ao broker são tratadas pelo publicador da outbox (§5.1). Falha ao publicar na DLT no adaptador real: o offset não é confirmado e a mensagem é reprocessada. |
| Rotas e métodos fora do contrato | Resposta padrão do framework (§10). |
| Número monetário com mais de 2 casas ou como string | `422 VALIDACAO_INVALIDA`. |
| Evento do Fornecimento com instante igual ao gravado | Ignorado (RN-23). |
| `pedido_atualizado` de negociação já encerrada | Ignorado, sem DLT (RN-08). |
| Prazo vencido antes de `executar_prazos` rodar | As ações já são recusadas comparando com `agora` (RN-06, RN-10, RN-17); o estado muda na próxima execução. |

## 14. Checklist de prontidão (Definition of Ready) — preenchido pelo QA

Pré-conferência do autor antes do gate G1; o QA confirma ou desmarca cada item.

- [x] Nenhum `{{ }}` restante; comentários de orientação removidos.
- [x] Todo RF tem enunciado EARS, prioridade e CAs de sucesso e erro quando aplicáveis; exceções justificadas na matriz (RF-03, RF-04, RF-06, RF-11, RF-14).
- [x] Todo CA usa valores concretos e resultado observável.
- [x] Todo código de erro citado em CA existe no §10 e vice-versa.
- [x] Ordem de verificação de erros definida para cada endpoint.
- [x] Campos de entidade, projeções de resposta e campos derivados estão mapeados; exemplos JSON respeitam os contratos.
- [x] Todo tópico citado tem produtor/consumidor, chave e exemplo.
- [x] Toda dependência externa está modelada como porta (§5.1).
- [x] Todo RNF tem métrica, limiar e forma de verificação.
- [x] Matriz de rastreabilidade completa (nenhum RF sem CA, nenhum CA órfão).
- [x] Não há palavras vagas sem quantificação ("rápido", "adequado", "etc.", "deve tratar erros").
- [ ] Seção 13 sem questões abertas e tabela 13.1 (casos de borda) toda decidida. A tabela 13.1 está decidida; continuam abertas as questões de integração INT-NEG-01, INT-NEG-02 e INT-NEG-03 (formato e consumo dos eventos com o Pedidos e o Mercado), INT-NEG-05 (tópicos novos na Infra) e INT-NEG-06 (DDL com o Banco). Neste serviço vale o contrato desta ESP; os CAs de integração com os parceiros só rodam depois do aceite.
- [x] Comentários `# DECISÃO:` da última geração (se houver) analisados e incorporados como regra (não houve geração anterior).

## 15. Histórico de versões

| Versão | Data | Autor | Mudança |
|---|---|---|---|
| 1.0 | 2026-11-17 | Caio Almeida Oliveira | Criação, a partir das decisões D1 a D30 do autor e das regras do professor (08/10 e 12/11/2026), com os contratos em `docs/contratos/negociacao/`. |
