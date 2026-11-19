# ESP-PED-001 — Especificação de Requisitos: Serviço de Pedidos

## 0. Metadados

| Campo | Valor |
|---|---|
| Identificador | ESP-PED-001 |
| Versão | 2.1 |
| Status | Em revisão (conteúdo aprovado pelo autor em 2026-11-19; aguarda o gate G1 do QA) |
| Serviço / Bounded context | `pedidos-service` (Portal B2B Distribuído): Pedidos. Porta `8107`, rotas `/api/v1/pedidos`, schema PostgreSQL `pedidos`, role `svc_pedidos` |
| Autores (PO / Dev / QA) | PO e Dev: Daniel Junio (grupo Mercado, Negociação/Leilão e Pedidos). QA: Grupo de Teste e Qualidade (revisão no gate G1) |
| Data | 2026-11-19 |
| Documentos relacionados | Diagrama "Arquitetura do Portal B2B Distribuído"; ESP-NEG-001 (branch `docs/esp-negociacao`, revisão `926dfb8`); ESP-MRC-001 v0.5 (`docs/mercado/requisitos.md`, revisão `5c8e8a2`); contratos de eventos em `docs/pedidos/contratos/`; kit de Infraestrutura (`CONTRACTS.md`, revisão `630515c`); contrato de eventos v2.0 do Fornecimento (revisão `56da541`); proposta da Logística para Pedidos (revisão `0ad3d21`); Identity-Service; DR-BD-002 v1.0; modelo ERS-LLM v1.2 do QA (revisão `efc05b6`). |

## 1. Diretivas para a LLM geradora

1. Implemente **exatamente** o que está especificado. Não adicione endpoints, campos, tópicos, perfis ou regras que não estejam neste documento.
2. **Ordem de precedência** quando houver conflito aparente: Catálogo de erros (§10) e Contratos (§7) > Critérios de aceitação (§8) > Regras de negócio (§9) > Requisitos funcionais (§8) > texto descritivo.
3. Nomes de campos, tópicos, códigos de erro, rotas e valores de enumeração são **literais**: use-os byte a byte, com a mesma grafia e caixa.
4. Quando algo não estiver definido, escolha o comportamento mais simples que **não viole** nenhum item deste documento e registre a decisão em um comentário `# DECISÃO:` no código.
5. Entregue código completo e executável. Proibido: `TODO`, `pass` como implementação, pseudocódigo, trechos omitidos ("...").
6. Grave todos os arquivos completos na estrutura do §5, dentro de `services/pedidos-service/`. Na resposta final, liste seus caminhos relativos.
7. Os arquivos de `docs/`, `services/mercado-service/` e `services/negociacao-service/` são somente leitura.

## 2. Contexto

### 2.1 Objetivo do serviço

Registrar o pedido que bloqueia o estoque de uma oferta durante uma negociação e acompanhar esse pedido até a entrega: o pedido nasce temporário, sem comprador, na abertura da negociação; é efetivado como venda no fechamento com vencedor ou cancelado sem ele; e segue para frete e entrega depois que o comprador informa o destino.

### 2.2 Posição na arquitetura

O serviço é o bloco "Pedidos" do diagrama e cobre os passos 7 a 10 do fluxo principal do negócio. Ele mantém cópias locais de produtos e ofertas a partir dos eventos do Fornecimento e cria um pedido temporário por oferta quando a Negociação abre um leilão ou uma negociação direta. O `pedido_criado` desse pedido é o que faz o Fornecimento reservar o estoque. No fechamento, Pedidos conclui a venda dos pedidos alocados e cancela os demais. Depois, o comprador informa o destino pela API, o que libera a cotação na Logística, e os eventos de frete e entrega levam o pedido até `ENTREGUE`.

```text
Negociação    ──[pedido_temporario_solicitado, negociacao_fechada]──────────────► Pedidos
Produtos      ──[produto_cadastrado, produto_atualizado]────────────────────────► Pedidos
Fornecimento  ──[fornecimento_criado, estoque_atualizado, reserva_estoque_recusada]► Pedidos
Logística     ──[solicitacao_frete_criada, frete_selecionado,
                 entrega_concluida, frete_indisponivel]─────────────────────────► Pedidos
Portal ─► Gateway Nginx ─REST /api/v1/pedidos (JWT do Identity)─────────────────► Pedidos
Identity      ──JWKS (HTTP)─────────────────────────────────────────────────────► Pedidos

Pedidos ──[pedido_criado]────► Fornecimento (reserva), Logística (ignora sem comprador)
Pedidos ──[pedido_atualizado]► Negociação (reservaSituacao), Fornecimento (liberação e baixa),
                               Logística (cotação a partir de AGUARDANDO_FRETE)
Pedidos ──[pedidos.dlt]──────► operação, QA
```

Passos do fluxo: (1) o Fornecimento publica as ofertas; (2–5) Demanda, Mercado e Negociação definem a negociação; (6) a Negociação abre o leilão ou a direta e pede um pedido temporário por oferta; (7) **este serviço cria o pedido temporário e acompanha a reserva**; (8) **no fechamento, conclui a venda ou cancela**; (9) **o comprador informa o destino** e a Logística cota e seleciona o frete; (10) **a entrega encerra o pedido**.

## 3. Escopo

### 3.1 Dentro do escopo

- Cópia local de produtos (`produto_cadastrado`, `produto_atualizado`) e ofertas (`fornecimento_criado`), usada para montar o snapshot de carga do pedido.
- Criação de um pedido temporário (`CRIADO`, sem comprador) por oferta a partir de `pedido_temporario_solicitado`, com unicidade por `(negociacaoId, fornecimentoId)` e espera por projeções ausentes.
- Acompanhamento da reserva por `estoque_atualizado` e `reserva_estoque_recusada`, com publicação de `reservaSituacao` para a Negociação.
- Efetivação (`VENDA_CONCLUIDA`) dos pedidos alocados e cancelamento dos demais a partir de `negociacao_fechada`, com validação do fechamento inteiro.
- Registro do destino pelo comprador (API-07).
- Registro da solicitação de frete, da seleção, da troca de transportadora, da indisponibilidade e da entrega a partir dos eventos da Logística.
- Consultas de pedidos e histórico com visibilidade por empresa e paginação.
- Histórico de versões, auditoria, publicação durável de `pedido_criado` e `pedido_atualizado`, DLT `pedidos.dlt`.
- Autenticação por JWT do Identity-Service, erros RFC 7807 com `codigo`, `X-Correlation-Id`, `Idempotency-Key`, `/health`, `/health/ready`, `/metrics`.
- Persistência no schema `pedidos`, outbox transacional, consumo idempotente, reconciliador de pendências e execução em duas réplicas.

### 3.2 Fora do escopo (não implementar)

- **Fora do Portal na v1 (nenhum serviço implementa):** emissão de nota fiscal; cancelamento manual pelo comprador ou fornecedor; pagamento; comissão calculada; estorno de venda concluída.
- Matching, sugestão de leilão e vitrine: `mercado-service`.
- Condução do leilão e da negociação direta, lances, vencedor e rateio do valor: `negociacao-service`.
- Reservar, liberar ou baixar estoque; manter saldo e preço de oferta: `fornecimentos-service`.
- Cotar, escolher transportadora e rastrear entrega: `logistica-service`.
- Criar pedido por HTTP; alterar status por HTTP; vários itens ou vários fornecedores no mesmo pedido.
- Consumir eventos de Demanda, de Mercado e do Identity; consumir `fornecimento_atualizado`, `produto_excluido` e `lance_realizado`.
- Notificação ativa (e-mail, SMS, push), SSE ou WebSocket.

## 4. Glossário

| Termo | Definição precisa |
|---|---|
| Agora | Valor de `clock.now()` (porta `Clock`). O serviço nunca lê o relógio do sistema diretamente. |
| Hoje | `(agora em UTC − 3 h).date()`: a data em Brasília (UTC−03:00; o Brasil não tem horário de verão desde 2019). Não usar `zoneinfo` nem base de fusos. Usada só na regra da data desejada (RN-15). |
| Instante | Data e hora. Na saída: UTC, ISO-8601 com milissegundos e sufixo `Z` (ex.: `"2026-11-20T12:00:00.000Z"`); microssegundos são truncados para milissegundos. Na entrada: ISO-8601 com `Z` ou offset explícito (`-03:00`), convertido para UTC; sem fuso é inválido. |
| Data | `YYYY-MM-DD`. |
| Valor monetário | Reais (BRL). Na API e nos eventos: número JSON (nunca string), com no máximo 2 casas na entrada e exatamente 2 casas na saída. Máximo `99999999999999.99`. No código, `decimal.Decimal`; `float` é proibido. No banco, `NUMERIC(18,4)`. |
| Quantidade | Número JSON > 0, com no máximo 2 casas na entrada e exatamente 2 na saída, na unidade do produto. Máximo `9999999999.99`. `Decimal` no código, `NUMERIC(12,2)` no banco. |
| Peso | `pesoKg`: número JSON > 0 em quilos, com exatamente 3 casas na saída (RN-03). `pesoUnitarioKg` na entrada: > 0, até 3 casas, máximo `99999.999`. |
| Comparação numérica | Os testes comparam números por valor: `5000`, `5000.0` e `5000.000` são iguais. A saída do serviço segue as casas deste glossário; os exemplos do documento e do Anexo A podem omitir zeros à direita e não definem o formato. |
| UUID | Texto no formato canônico `8-4-4-4-12` hexadecimal, qualquer versão, em qualquer caixa. Outras formas (sem hífens, com chaves) são inválidas. |
| Texto aparado | Resultado de `str.strip()` (remove todo espaço em branco Unicode das pontas). Campos obrigatórios aparados e vazios são inválidos. |
| Opcional | Campo opcional ausente e campo opcional com `null` são equivalentes, em todo o documento. |
| Local | Objeto `{"cidade": texto aparado de 1 a 120 caracteres, "uf": uma das 27 siglas brasileiras em maiúsculas}`. |
| Empresa | Valor do claim `tenant_id` do token. É a identidade usada em `compradorId` e `fornecedorId`. |
| Escopo | Valor do claim `active_scope`: `BUYER`, `SUPPLIER`, `TENANT_ADMIN` ou `CARRIER`. |
| Suporte | Token com `platform_role = "PLATFORM_ADMIN"`. Consulta qualquer pedido; não altera pedidos (RN-17). |
| Oferta | Fornecimento de um fornecedor para um produto (`fornecimentoId`). O estoque é controlado por oferta. |
| Pedido temporário | Pedido em `CRIADO` com `compradorId = null`. Existe para reservar a oferta durante a negociação. |
| Reserva / liberação / baixa | Operações do Fornecimento: reservar separa a quantidade da oferta; liberar devolve a reserva de um pedido cancelado; baixar retira a reserva de um pedido vendido. |
| Alocação | Item de `negociacao_fechada.alocacoes`: a parte do lote vendida a uma oferta, com `valorTotal` já rateado pela Negociação. |
| Solicitação vigente | A solicitação de frete atual do pedido (RN-19). As anteriores ficam `SUBSTITUIDA`. |
| Seleção vigente | O `frete` atual do pedido: a última seleção aplicada da solicitação vigente. |
| Repetição de negócio | Mensagem com outro `eventId` e os mesmos campos de negócio de um fato já aplicado (tabela da RN-27). Fica sem efeito e sem auditoria. |
| Estado terminal | `CANCELADO` ou `ENTREGUE`. |
| Pedido visível | Pedido que o token pode ver pela RN-17. |

## 5. Restrições técnicas

| Item | Valor obrigatório |
|---|---|
| Linguagem / versão | Python 3.12 |
| Framework | FastAPI com Pydantic 2 e Uvicorn |
| Dependências permitidas | As versões fixadas em `requirements.txt`: `fastapi==0.162.5`, `starlette==1.10.0`, `uvicorn==0.54.0`, `pydantic==2.14.3`, `psycopg[binary]==3.3.9`, `psycopg-pool==3.3.3`, `alembic==1.20.1`, `SQLAlchemy==2.1.8`, `aiokafka==0.15.0`, `PyJWT[crypto]==2.15.1`, `cryptography==50.0.2`, `simplejson==3.20.2`, `prometheus-client==0.26.0`, além da biblioteca padrão. Nenhuma outra. Testes: `httpx==0.28.1` e `pytest==9.2.0` em `requirements-test.txt`. A restrição vale para o pacote `app/`: os módulos de `app/` fora de `app/adaptadores/` importam somente biblioteca padrão, `fastapi`, `starlette`, `pydantic`, `jwt`, `cryptography`, `simplejson`, `prometheus_client` e o próprio pacote `app`. `tests/` usa também `pytest` e `httpx`; `validacao/gerar_contratos.py` é ferramenta do QA, usa `jsonschema` e não faz parte do serviço. |
| Persistência | Somente pela porta `PedidosRepositorio` (§5.1). Proibido guardar pedidos, projeções, pendências, chaves de idempotência ou eventos processados em variáveis de módulo ou da aplicação. |
| Estrutura de arquivos | Ver árvore abaixo. `app/__init__.py`, `app/portas.py` e `app/main.py` são obrigatórios. |
| Ponto de entrada (testes) | `app.main.create_app(event_bus: EventBus, clock: Clock, repositorio: PedidosRepositorio, chaves_publicas: ChavesPublicas, emissor: str = "identity-service", audiencia: str = "b2b-platform") -> fastapi.FastAPI` e `app.main.reconciliar(app: fastapi.FastAPI) -> int` (reavalia as solicitações pendentes e as entregas guardadas, numa transação por item, e devolve a soma de pedidos criados e de entregas aplicadas; pendências removidas não contam). |
| Ponto de entrada (execução) | `uvicorn app.main:criar_app_real --factory --host 0.0.0.0 --port 8107`: monta os adaptadores reais a partir das variáveis de ambiente e inicia, como tarefas de fundo, o consumidor Kafka, o publicador da outbox e o reconciliador (`reconciliar` a cada 60 s). |
| Formato de data/hora | Instante e Data (§4). |
| Formato monetário | Valor monetário (§4). Arredondamento `ROUND_HALF_UP` só onde a regra manda (RN-03, RN-04). |
| Configuração de infraestrutura | `DATABASE_URL` (role `svc_pedidos`), `MIGRATION_DATABASE_URL` (role `portal_ddl`; o `migracoes/env.py` força o driver `postgresql+psycopg`), `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_GROUP_ID` (`pedidos-service-v1`), `JWKS_URL`, `JWT_ISSUER` (`identity-service`), `JWT_AUDIENCE` (`b2b-platform`), `LOG_LEVEL` (`INFO`). Todas em `.env.example`. |

```text
services/pedidos-service/
├── app/
│   ├── __init__.py
│   ├── portas.py            # Protocols do §5.1 e RepositorioIndisponivel
│   ├── main.py              # create_app, reconciliar, criar_app_real
│   ├── dominio.py           # regras puras: criação, peso, transições, validação do fechamento, frete
│   ├── contratos.py         # validação de corpo HTTP e de eventos consumidos; montagem dos eventos publicados
│   ├── http.py              # rotas, autenticação, erros RFC 7807, correlação, idempotência
│   ├── jsonio.py            # JSON com Decimal (simplejson)
│   └── adaptadores/
│       ├── postgres.py      # PedidosRepositorio real (psycopg 3, pool ≤ 5)
│       ├── kafka.py         # EventBus real: outbox, publicador, consumidor, DLT
│       └── jwks.py          # ChavesPublicas real (PyJWKClient, cache de 1 h)
├── migracoes/
│   ├── env.py
│   └── versions/
├── demo/                    # init.sql (roles, schema pedidos e schema vizinho) e nginx.conf
├── tests/
├── validacao/               # relatório, JUnit, validar-operacao.ps1 e gerar_contratos.py
├── alembic.ini
├── requirements.txt
├── requirements-test.txt
├── .env.example
├── Dockerfile               # base python:3.12-slim
├── compose.demo.yml
├── compose.pedidos.yml
└── README.md
```

### 5.1 Portas (interfaces de infraestrutura)

```python
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Callable, Protocol

class RepositorioIndisponivel(Exception):
    """Lançada pelo repositório quando o banco não responde (falha de conexão)."""

class EventBus(Protocol):
    def publish(self, topic: str, key: str, message: dict) -> None: ...
    def subscribe(self, topic: str, handler: Callable[[dict], None]) -> None: ...
    def disponivel(self) -> bool: ...

class Clock(Protocol):
    def now(self) -> datetime: ...          # datetime com timezone
    def sleep(self, segundos: float) -> None: ...

class ChavesPublicas(Protocol):
    def obter(self, kid: str) -> object | None: ...   # chave pública RSA do kid, ou None se desconhecido

class PedidosRepositorio(Protocol):
    def transacao(self) -> AbstractContextManager[None]: ...
    def disponivel(self) -> bool: ...
    # consumo idempotente
    def evento_processado(self, source: str, event_id: str) -> bool: ...
    def registrar_evento_processado(self, source: str, event_id: str, topico: str) -> None: ...
    # projeções
    def obter_produto(self, produto_id: str) -> dict | None: ...
    def salvar_produto(self, produto: dict) -> None: ...
    def obter_oferta(self, fornecimento_id: str) -> dict | None: ...
    def salvar_oferta(self, oferta: dict) -> None: ...
    # negociação como unidade de concorrência
    def bloquear_negociacao(self, negociacao_id: str) -> None: ...
    def obter_negociacao_encerrada(self, negociacao_id: str) -> dict | None: ...
    def salvar_negociacao_encerrada(self, registro: dict) -> None: ...
    # pendências de criação
    def obter_pendente(self, negociacao_id: str, fornecimento_id: str) -> dict | None: ...
    def salvar_pendente(self, pendente: dict) -> None: ...
    def remover_pendente(self, negociacao_id: str, fornecimento_id: str) -> None: ...
    def listar_pendentes(self, limite: int, produto_id: str | None = None,
                         fornecimento_id: str | None = None) -> list[dict]: ...
    # pedidos
    def obter_pedido(self, pedido_id: str, bloquear: bool = False) -> dict | None: ...
    def obter_pedido_da_oferta(self, negociacao_id: str, fornecimento_id: str) -> dict | None: ...
    def listar_pedidos_da_negociacao(self, negociacao_id: str) -> list[dict]: ...
    def inserir_pedido(self, pedido: dict) -> None: ...
    def atualizar_pedido(self, pedido: dict) -> None: ...
    def listar_pedidos(self, empresa: str | None, escopo: str | None, status: str | None,
                       negociacao_id: str | None, offset: int, limite: int) -> tuple[list[dict], int]: ...
    # histórico e auditoria
    def registrar_historico(self, item: dict) -> None: ...
    def listar_historico(self, pedido_id: str, offset: int, limite: int) -> tuple[list[dict], int]: ...
    def registrar_auditoria(self, registro: dict) -> None: ...
    # frete
    def obter_solicitacao(self, solicitacao_frete_id: str) -> dict | None: ...
    def salvar_solicitacao(self, solicitacao: dict) -> None: ...
    def salvar_entrega_pendente(self, entrega: dict) -> None: ...
    def listar_entregas_pendentes(self, pedido_id: str | None, limite: int) -> list[dict]: ...
    def remover_entrega_pendente(self, pedido_id: str, entrega_id: str) -> None: ...
    # idempotência HTTP
    def obter_idempotencia(self, escopo: str) -> dict | None: ...
    def salvar_idempotencia(self, escopo: str, registro: dict) -> None: ...
```

Comportamento das portas:

- `app/portas.py` contém exatamente as definições acima. Quem chama `create_app` fornece as implementações; nos testes, dublês em memória.
- `create_app` chama, durante a criação da aplicação, `event_bus.subscribe` para os 11 tópicos do §7.3, um handler para cada. O handler recebe a mensagem completa (envelope) como `dict`. Ele só propaga `RepositorioIndisponivel` (RN-26); qualquer outra exceção é tratada dentro dele.
- `transacao()`: tudo o que é feito no repositório dentro de `with repositorio.transacao():` é gravado de forma atômica; se uma exceção sair do bloco, nada é gravado. Cada requisição HTTP de escrita, cada mensagem consumida e cada item processado por `reconciliar` usam uma única transação.
- `bloquear_negociacao(negociacao_id)` bloqueia a negociação até o fim da transação (no PostgreSQL, `pg_advisory_xact_lock` sobre o hash do ID). A criação de pedido temporário e a aplicação de `negociacao_fechada` chamam `bloquear_negociacao` antes de qualquer leitura. `obter_pedido(..., bloquear=True)` bloqueia a linha do pedido (`SELECT ... FOR UPDATE`) e é usado em toda transição.
- Os dicionários trocados com o repositório usam as chaves dos atributos do §6.1, com instantes em `datetime` UTC e valores em `Decimal`.
- `listar_pedidos` devolve `(itens, total)` em ordem de `criadoEm` decrescente e, em empate, `pedidoId` decrescente. `empresa = None` e `escopo = None` = sem filtro de visibilidade (suporte). Com escopo, aplica a RN-17 antes da paginação. `status` e `negociacao_id` `None` = sem filtro. `listar_historico` devolve em ordem de `versao` crescente.
- `listar_pendentes` e `listar_entregas_pendentes` devolvem em ordem de `recebidoEm` crescente. `reconciliar` usa `limite = 100` em cada uma.
- Qualquer método do repositório pode lançar `RepositorioIndisponivel`; a API responde `503 DEPENDENCIA_INDISPONIVEL` e o consumo segue a RN-26. Qualquer outra exceção vira `500 ERRO_INTERNO` na API. A captura é feita por um middleware HTTP do próprio serviço (`try/except` em volta de `call_next`), que devolve a resposta de erro sem relançar a exceção; assim a suíte pode usar `TestClient(app)` com as opções padrão.
- `publish` é chamado **dentro** da transação da mudança, como último passo antes do fim do bloco, e nunca chama handlers de forma síncrona. Ordem de toda escrita: validar → alterar o estado → registrar histórico e auditoria → publicar. Quando uma transação gera mais de uma versão do mesmo pedido, os `pedido_atualizado` são publicados em ordem de versão. A publicação em `pedidos.dlt` é feita fora da transação que falhou.
- `clock.now()` sempre devolve `datetime` com fuso; se vier sem fuso, tratar como UTC. `clock.sleep(segundos)` é a única forma de esperar; nos testes, o dublê retorna na hora e registra o valor.
- `chaves_publicas.obter(kid)` devolve a chave pública RSA ou `None` se o `kid` for desconhecido. Se lançar exceção, a resposta é `503 DEPENDENCIA_INDISPONIVEL`.
- `event_bus.disponivel()` e `repositorio.disponivel()` alimentam a API-02. Exceção em qualquer um dos dois conta como indisponível.
- Cada aplicação criada por `create_app` usa o seu próprio `prometheus_client.CollectorRegistry`; nada de métricas no registry global.
- Publicações em `pedidos.dlt` e o retorno de `RepositorioIndisponivel` são as únicas saídas do handler fora de uma transação concluída. Falha no commit depois de um `publish` está fora da suíte em memória (no adaptador real a outbox é desfeita junto).

Comportamento dos adaptadores reais (não usados pela suíte de aceitação em memória):

- **Repositório (`postgres.py`):** psycopg 3 com `psycopg_pool.ConnectionPool(max_size=5)`, role `svc_pedidos`, só o schema `pedidos`. `transacao()` abre uma conexão do pool numa variável de contexto e faz commit ao sair do bloco sem exceção. São falhas de conexão, convertidas em `RepositorioIndisponivel`: SQLSTATE da classe `08`, `57P01`, `57P02`, `57P03` e `PoolTimeout`. As demais exceções (inclusive `40001`, `40P01` e `57014`) sobem como estão.
- **Outbox (`kafka.py`):** dentro da transação, `publish` grava o envelope em `pedidos.outbox` pela mesma conexão, com `(pedido_id, versao)` único para `pedido_atualizado`. Um publicador em cada réplica faz polling a cada 1 s, lê o registro pendente mais antigo com `FOR UPDATE SKIP LOCKED`, envia com `acks=all`, timeout de 5 s e a chave do registro, marca `confirmado_em` e, em falha, reagenda com espera de 1, 2, 4, 8, 16 e depois 30 s fixos, sem jitter, a partir do instante da falha. Todo reenvio usa o mesmo `eventId`. Para o mesmo pedido, a versão seguinte só é enviada depois da confirmação da anterior.
- **Consumidor (`kafka.py`):** `aiokafka` com o grupo `KAFKA_GROUP_ID`, `enable_auto_commit=False`, em ordem dentro de cada partição. Bytes que não são JSON ou não são UTF-8 vão direto para `pedidos.dlt` (RN-25). O handler roda fora do laço de eventos (`asyncio.to_thread`). O offset só é confirmado depois do retorno do handler e do ACK da DLT, quando houver. Se o handler propagar `RepositorioIndisponivel`, ou se o broker falhar (`KafkaConnectionError`, `KafkaTimeoutError`), o offset não é confirmado e a mensagem é reentregue a cada 5 s, sem limite.
- **Reconciliador:** cada réplica chama `reconciliar` a cada 60 s. A segurança com duas réplicas vem de `bloquear_negociacao` e da nova verificação do estado dentro da transação.
- **JWKS (`jwks.py`):** `jwt.PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600, timeout=2)`. Falha de rede lança exceção (→ 503); `kid` ausente devolve `None` (→ 401).
- **Logs:** JSON em `stdout` com `instante`, `nivel`, `servico` (`pedidos-service`), `correlationId`, `mensagem` e, quando conhecidos, `eventId`, `pedidoId` e `negociacaoId`; avisos levam `codigo`. Pendência com mais de 24 h → `WARN` com `codigo` `PENDENCIA_ANTIGA`. Registro da outbox esperando mais de 60 s → `WARN` com `codigo` `OUTBOX_ATRASADA`. Nunca registrar o token.
- **Métricas (`/metrics`):** `pedidos_http_requisicoes_total` (`rota`, `metodo`, `status`), `pedidos_http_latencia_segundos` (histograma, `rota`, `metodo`), `pedidos_eventos_consumidos_total` (`topico`, `resultado` ∈ `aplicado`, `duplicado`, `ignorado`, `dlt`), `pedidos_pendencias`, `pedidos_pendencias_antigas`, `pedidos_outbox_pendentes` e `pedidos_outbox_idade_segundos`. IDs e tokens nunca são rótulos.

## 6. Modelo de domínio

### 6.1 Entidades

#### Pedido (tabela `pedidos.pedido`, chave `pedidoId`)
| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| pedidoId | string UUID | Sim | UUID4 gerado pelo serviço, em minúsculas. Recebido de fora (alocação, eventos, URL), é comparado normalizado em minúsculas. |
| negociacaoId | string | Sim | Da solicitação. `UNIQUE(negociacaoId, fornecimentoId)`. |
| fornecimentoId | string | Sim | Da solicitação. |
| tipoNegociacao | enum | Sim | `LEILAO` ou `VENDA_DIRETA`, da solicitação. |
| produtoId, fornecedorId | string | Sim | Da solicitação, iguais aos da projeção da oferta. |
| compradorId | string ou `null` | Sim | `null` no pedido temporário; na efetivação, o `compradorId` do topo de `negociacao_fechada` (RN-09). |
| quantidade | quantidade | Sim | Da solicitação. |
| unidade | enum | Sim | `KG`, `TON`, `UN`, `CX` ou `L`, do produto. |
| precoUnitarioOferta | valor monetário | Sim | `precoUnitario` da solicitação. |
| valorUnitario, valorTotal | valor monetário | Sim | No temporário: preço da oferta e RN-04. Na efetivação: os da alocação, sem recálculo. |
| produtoNome | string 1–255 | Sim | Do produto, na criação. |
| tipoTransporte | enum | Sim | `GRANEL`, `REFRIGERADO`, `CARGA_SECA` ou `PERIGOSO`, do produto. |
| pesoKg | peso | Sim | RN-03. |
| origem | Local | Sim | Da oferta. |
| destino | Local ou `null` | Sim | `null` até a API-07. |
| dataEntregaDesejada | data ou `null` | Sim | Da API-07. |
| status | enum | Sim | §6.2. |
| reservaSituacao | enum | Sim | `PENDENTE`, `CONFIRMADA`, `RECUSADA` ou `ENCERRADA`. |
| motivoCancelamento | enum ou `null` | Sim | `ESTOQUE_INSUFICIENTE`, `FORNECIMENTO_INEXISTENTE`, `FORA_DO_LOTE` ou `NEGOCIACAO_SEM_VENDA`. |
| solicitacaoFreteVigenteId | string ou `null` | Sim | RN-19. |
| frete | Frete ou `null` | Sim | Seleção vigente. |
| ocorrenciaFrete | objeto ou `null` | Sim | `{motivo, ocorridoEm}` do último `frete_indisponivel` da solicitação vigente. Volta a `null` quando outra solicitação se torna vigente. Atualiza `atualizadoEm`, não a versão. |
| versao | inteiro | Sim | Começa em 1; +1 a cada mudança de status, de reserva, de destino ou de frete. |
| criadoEm, atualizadoEm | instante | Sim | `agora`. |
| vendidoEm | instante ou `null` | Sim | `agora` na efetivação. |
| entregueEm | instante ou `null` | Sim | `entregueEm` da entrega aplicada. |
| correlationId | string | Sim | Correlação da mensagem que criou o pedido. |

Frete: `solicitacaoFreteId`, `entregaId`, `cotacaoId`, `transportadoraId` (strings 1–64), `tentativa` (inteiro ≥ 1), `preco` (valor monetário), `prazoDias` (inteiro ≥ 1), `dataEntregaPrevista` (data ou `null`).

#### SolicitacaoFrete (tabela `pedidos.solicitacao_frete`, chave `solicitacaoFreteId`)
| Atributo | Tipo | Obrigatório | Restrições / origem |
|---|---|---|---|
| solicitacaoFreteId | string 1–64 | Sim | Da Logística. |
| pedidoId | string UUID | Sim | Pedido correlacionado. |
| situacao | enum | Sim | `VIGENTE` ou `SUBSTITUIDA`. No máximo uma `VIGENTE` por pedido. |
| indisponivel | booleano | Sim | `true` depois de `frete_indisponivel` dela. |
| selecoes | lista de Frete com `situacao` | Sim | Todas as seleções aplicadas desta solicitação, em ordem de `tentativa`, cada uma com `situacao` `APLICADA` (a vigente) ou `SUBSTITUIDA`. Usada pela RN-20 e pela RN-21. |

#### Registros auxiliares
| Registro | Campos e finalidade |
|---|---|
| `produto` | `produtoId`, `nome`, `unidade`, `tipoTransporte`, `pesoUnitarioKg`, `timestamp` do evento aplicado (RN-01). |
| `oferta` | `fornecimentoId`, `fornecedorId`, `produtoId`, `unidade`, `origem` (RN-01). |
| `solicitacao_pendente` | Chave `(negociacaoId, fornecimentoId)`; payload, `eventId`, `correlationId`, causa (`PRODUTO_AUSENTE`, `OFERTA_AUSENTE` ou `PESO_AUSENTE`) e `recebidoEm` (RN-06). |
| `negociacao_encerrada` | `negociacaoId`, `status`, `compradorId`, `alocacoes` normalizadas e `recebidoEm` (RN-08, RN-12). |
| `entrega_pendente` | `pedidoId`, `entregaId`, `transportadoraId`, `entregueEm`, `eventId`, `correlationId`, `recebidoEm` (RN-21). |
| `historico` | `(pedidoId, versao)` único; `statusAnterior` (`null` na versão 1), `status`, `reservaSituacao`, `origem`, `motivo` (o `motivoCancelamento` na versão que cancela; `null` nas demais), `eventId` (mudança por evento) ou `usuario` (`sub` do token, mudança por HTTP), `correlationId`, `ocorridoEm`. `origem` é o tópico que causou a mudança ou `http_destino`; a criação por reavaliação (RN-01 ou `reconciliar`) mantém `pedido_temporario_solicitado`, com o `eventId` e o `correlationId` da solicitação guardada; a entrega guardada mantém `entrega_concluida`, com o `eventId` e o `correlationId` da entrega. |
| `auditoria` | `acao`, `instante` e `detalhe` (objeto com, no mínimo, o tópico e o ID de negócio) obrigatórios; `pedidoId`, `negociacaoId`, usuário, empresa e `eventId` opcionais. Lista fechada de `acao`: `PEDIDO_CRIADO`, `RESERVA_CONFIRMADA`, `PEDIDO_CANCELADO`, `VENDA_CONCLUIDA`, `DESTINO_INFORMADO`, `FRETE_APLICADO`, `ENTREGA_APLICADA`, `SOLICITACAO_DESCARTADA`, `SOLICITACAO_INVALIDA`, `ESTOQUE_REGISTRADO`, `EVENTO_CONFLITANTE`, `EVENTO_FORA_DE_ESTADO`, `EVENTO_NAO_CORRELACIONADO`, `ENTREGA_DESCARTADA`, `FRETE_INDISPONIVEL`. |

Tabelas técnicas: `pedidos.processed_events` (`source`, `event_id`, `topico`, `processado_em`; único em `source, event_id`; retenção mínima de 30 dias), `pedidos.idempotency_keys` (24 h) e `pedidos.outbox`.

### 6.2 Estados e transições

| Estado atual | Evento / comando | Condição | Próximo estado |
|---|---|---|---|
| — | `pedido_temporario_solicitado` | RN-05 e RN-06 (projeções presentes, negociação aberta) | `CRIADO`, reserva `PENDENTE`, versão 1 |
| `CRIADO`, reserva `PENDENTE` | `estoque_atualizado` `RESERVA` | RN-07 | `CRIADO`, reserva `CONFIRMADA` |
| `CRIADO`, reserva `PENDENTE` | `reserva_estoque_recusada` | RN-07 | `CANCELADO`, reserva `RECUSADA` |
| `CRIADO`, reserva `CONFIRMADA` | `negociacao_fechada` `ENCERRADO` com alocação do pedido | RN-08, RN-09 | `VENDA_CONCLUIDA` |
| `CRIADO` | `negociacao_fechada` sem o pedido em `alocacoes` | RN-10 | `CANCELADO`, reserva `ENCERRADA` |
| `VENDA_CONCLUIDA` | API-07 do comprador | RN-14, RN-15 | `AGUARDANDO_FRETE` |
| `AGUARDANDO_FRETE` | `frete_selecionado` da solicitação vigente | RN-19, RN-20 | `FRETE_SELECIONADO` |
| `FRETE_SELECIONADO` | `frete_selecionado` com tentativa maior, ou de nova solicitação vigente | RN-20 | `FRETE_SELECIONADO` (versão nova) |
| `FRETE_SELECIONADO` | `entrega_concluida` da seleção vigente | RN-21 | `ENTREGUE` |
| `AGUARDANDO_FRETE`, `FRETE_SELECIONADO` | `frete_indisponivel` da solicitação vigente | RN-22 | sem mudança de status nem de versão |
| `CANCELADO`, `ENTREGUE` | qualquer | — | sem mudança (estado final; RN-23) |

Não mudam o pedido: `REPOSICAO` e `ENCERRAMENTO` (RN-11), `LIBERACAO` e `BAIXA` (só auditoria), troca de solicitação vigente sem nova seleção (RN-19), reserva confirmada para pedido já cancelado.

## 7. Contratos de interface

### 7.1 Autenticação e contexto da requisição

Rotas públicas (sem token): `/health`, `/health/ready`, `/metrics`, `/docs`, `/openapi.json`. Todas as rotas `/api/v1/...` exigem `Authorization: Bearer <token>`, um JWT emitido pelo Identity-Service.

Validação do token, nesta ordem (decodificar com `algorithms=["RS256"]` e `options={"verify_exp": False, "verify_nbf": False, "verify_iat": False, "verify_aud": False, "verify_iss": False}`, conferindo `exp`, `nbf`, `iss` e `aud` manualmente contra `clock.now()` e os parâmetros de `create_app`, com tolerância de 0 s; `iat` é ignorado). O esquema `Bearer` é comparado sem diferenciar maiúsculas:

1. Cabeçalho ausente, vazio ou sem `Bearer ` seguido de texto; token malformado; cabeçalho do token sem `kid` ou com `alg` diferente de `"RS256"`; `chaves_publicas.obter(kid)` devolvendo `None`; assinatura inválida; `iss` diferente de `emissor`; `aud` que não contém `audiencia` (texto ou lista); `sub` ausente ou vazio; `exp` ausente ou `exp <= int(agora.timestamp())`; `nbf` presente e `nbf > int(agora.timestamp())` → `401 TOKEN_INVALIDO`, com o cabeçalho `WWW-Authenticate: Bearer`. Se `chaves_publicas.obter` lançar exceção → `503 DEPENDENCIA_INDISPONIVEL`.
2. Se `platform_role == "PLATFORM_ADMIN"`: o token é de suporte e dispensa `tenant_id` e `active_scope`. Senão: `tenant_id` ausente, nulo ou vazio, `active_scope` ausente ou nulo, ou `view_mode` presente e diferente de `"SINGLE"` → `403 CONTEXTO_NAO_SELECIONADO`.
3. Escopo não permitido na rota (tabela do §7.2) → `403 ACESSO_NEGADO`.

Claims ignorados na v1: `roles`, `permissions`, `group_id`, `allowed_tenants`, `act`, `fid`, `jti`, `client_id` e `token_use`.

`X-Correlation-Id`: 1 a 128 caracteres de `[A-Za-z0-9._-]`; ausente, vazio ou fora desse formato → o serviço gera um UUID4. O valor efetivo volta no cabeçalho `X-Correlation-Id` de toda resposta, no `correlationId` dos erros e dos eventos publicados pela requisição.

`Idempotency-Key` (opcional na API-07): 1 a 128 caracteres de `[A-Za-z0-9._-]`; vazio equivale a ausente; fora do formato → `422 VALIDACAO_INVALIDA`. Regras na RN-24.

### 7.2 API REST

Respostas de negócio em JSON, camelCase. O FastAPI não pode validar o corpo nem a query antes das verificações 2 a 4 do §10: o serviço lê `await request.body()` e os parâmetros e valida por conta própria. `pedidoId` na URL é UUID em qualquer caixa (outro formato → `422 VALIDACAO_INVALIDA`). Corpo com campos fora do contrato, campo obrigatório ausente, tipo errado (inclusive número como string) ou JSON malformado → `422 VALIDACAO_INVALIDA`. Listagens: `page` inteiro ≥ 1 (padrão 1), `size` inteiro de 1 a 100 (padrão 10); `status` um valor do enum do §6.2; `negociacaoId` texto de 1 a 64 caracteres; parâmetro desconhecido, repetido ou fora disso → `422 VALIDACAO_INVALIDA`. A API-05 e a API-07 não aceitam parâmetros de query; a API-06 aceita só `page` e `size`; qualquer outro → `422 VALIDACAO_INVALIDA`. Resposta `{"items": [...], "page": P, "size": S, "total": T}`, com `items: []` numa página vazia.

| ID | Método | Rota | Escopos | Sucesso | Erros possíveis (na ordem de verificação) |
|---|---|---|---|---|---|
| API-01 | GET | `/health` | público | 200 `{"status":"up"}` | — |
| API-02 | GET | `/health/ready` | público | 200 | 503 `{"status":"not_ready"}` |
| API-03 | GET | `/metrics` | público | 200 | — |
| API-04 | GET | `/api/v1/pedidos` | `BUYER`, `SUPPLIER`, `TENANT_ADMIN`, suporte | 200 | 401 `TOKEN_INVALIDO` / 503 → 403 `CONTEXTO_NAO_SELECIONADO` → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` |
| API-05 | GET | `/api/v1/pedidos/{pedidoId}` | `BUYER`, `SUPPLIER`, `TENANT_ADMIN`, suporte | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 `PEDIDO_NAO_ENCONTRADO` |
| API-06 | GET | `/api/v1/pedidos/{pedidoId}/historico` | `BUYER`, `SUPPLIER`, `TENANT_ADMIN`, suporte | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 `PEDIDO_NAO_ENCONTRADO` |
| API-07 | PUT | `/api/v1/pedidos/{pedidoId}/destino` | `BUYER` | 200 | 401 / 503 → 403 → 403 `ACESSO_NEGADO` → 422 `VALIDACAO_INVALIDA` → 404 `PEDIDO_NAO_ENCONTRADO` → idempotência (replay ou 409 `IDEMPOTENCIA_CONFLITO`) → repetição (RN-14: 200 ou 409 `TRANSICAO_INVALIDA`) → 409 `TRANSICAO_INVALIDA` → 422 `VALIDACAO_INVALIDA` (data no passado, RN-15) |

`CARRIER` recebe `403 ACESSO_NEGADO` em toda rota de negócio. Na API-07, `SUPPLIER`, `TENANT_ADMIN` e suporte recebem `403 ACESSO_NEGADO`. Em qualquer rota, `RepositorioIndisponivel` → `503 DEPENDENCIA_INDISPONIVEL`; outra exceção → `500 ERRO_INTERNO`. As rotas de negócio dependem só do repositório: com `event_bus.disponivel()` falso, elas respondem normalmente e só a API-02 responde 503.

#### API-02 — Prontidão

`200 {"status":"ready"}` quando `repositorio.disponivel()` e `event_bus.disponivel()` são verdadeiros; senão `503 {"status":"not_ready"}`. No adaptador real, o `EventBus` está disponível quando uma consulta de metadados ao broker, com timeout de 2 s, encontra os tópicos do §7.3 e do §7.4.

#### API-04 — Listar pedidos

Cada item: `pedidoId`, `negociacaoId`, `fornecimentoId`, `compradorId`, `fornecedorId`, `status`, `valorTotal`, `criadoEm`.

```json
{"items":[{"pedidoId":"32000000-0000-4000-8000-000000000001","negociacaoId":"31000000-0000-4000-8000-000000000001","fornecimentoId":"34000000-0000-4000-8000-000000000001","compradorId":null,"fornecedorId":"a1000000-0000-4000-8000-000000000001","status":"CRIADO","valorTotal":600.00,"criadoEm":"2026-11-20T12:00:00.000Z"}],"page":1,"size":10,"total":1}
```

#### API-05 — Detalhe do pedido

Resposta 200 com todos os atributos do §6.1, exceto `precoUnitarioOferta`, `solicitacaoFreteVigenteId` e `correlationId`. Exemplo de pedido efetivado, antes do destino:

```json
{
  "pedidoId": "32000000-0000-4000-8000-000000000001",
  "negociacaoId": "31000000-0000-4000-8000-000000000001",
  "tipoNegociacao": "LEILAO",
  "compradorId": "b2000000-0000-4000-8000-000000000002",
  "fornecedorId": "a1000000-0000-4000-8000-000000000001",
  "fornecimentoId": "34000000-0000-4000-8000-000000000001",
  "produtoId": "35000000-0000-4000-8000-000000000001",
  "produtoNome": "Milho em grão",
  "quantidade": 5.00,
  "unidade": "TON",
  "valorUnitario": 130.00,
  "valorTotal": 650.00,
  "pesoKg": 5000,
  "tipoTransporte": "GRANEL",
  "origem": {"cidade": "Rio Verde", "uf": "GO"},
  "destino": null,
  "dataEntregaDesejada": null,
  "status": "VENDA_CONCLUIDA",
  "reservaSituacao": "CONFIRMADA",
  "motivoCancelamento": null,
  "frete": null,
  "ocorrenciaFrete": null,
  "versao": 3,
  "criadoEm": "2026-11-20T12:00:00.000Z",
  "atualizadoEm": "2026-11-20T15:00:00.000Z",
  "vendidoEm": "2026-11-20T15:00:00.000Z",
  "entregueEm": null
}
```

#### API-06 — Histórico

Cada item: `versao`, `statusAnterior`, `status`, `reservaSituacao`, `origem`, `motivo` e `ocorridoEm`.

```json
{"items":[{"versao":1,"statusAnterior":null,"status":"CRIADO","reservaSituacao":"PENDENTE","origem":"pedido_temporario_solicitado","motivo":null,"ocorridoEm":"2026-11-20T12:00:00.000Z"},{"versao":2,"statusAnterior":"CRIADO","status":"CRIADO","reservaSituacao":"CONFIRMADA","origem":"estoque_atualizado","motivo":null,"ocorridoEm":"2026-11-20T12:00:05.000Z"}],"page":1,"size":10,"total":2}
```

#### API-07 — Informar o destino

Requisição (`cidade` e `uf` obrigatórios; `dataEntregaDesejada` opcional):

```json
{"cidade": "Anápolis", "uf": "GO", "dataEntregaDesejada": "2026-11-27"}
```

Resposta 200: o detalhe (API-05) com `status` `"AGUARDANDO_FRETE"`, `destino` e `dataEntregaDesejada` preenchidos.

### 7.3 Eventos consumidos

Envelope (kit, `CONTRACTS.md`): `eventId` (UUID), `eventType` (igual ao tópico), `eventVersion` (`"1.0"`), `timestamp` (instante), `source`, `correlationId`, `aggregateId` e `payload` (objeto). Campos adicionais no `payload` são aceitos e ignorados. Os payloads de exemplo de todos os tópicos estão no Anexo A. Os schemas JSON em `docs/pedidos/contratos/` são material de verificação do QA e dos parceiros; em caso de divergência, vale este documento.

| Tópico | Produtor | Chave | Ação | Mensagem inválida |
|---|---|---|---|---|
| `pedido_temporario_solicitado` | `negociacao-service` | `negociacaoId` | RF-02 | `pedidos.dlt` (RN-25) |
| `negociacao_fechada` | `negociacao-service` | `negociacaoId` | RF-04 | `pedidos.dlt` (RN-25) |
| `produto_cadastrado` | `produtos-service` | `produtoId` | RF-01 | `pedidos.dlt` (RN-25) |
| `produto_atualizado` | `produtos-service` | `produtoId` | RF-01 | `pedidos.dlt` (RN-25) |
| `fornecimento_criado` | `fornecimentos-service` | `fornecimentoId` | RF-01 | `pedidos.dlt` (RN-25) |
| `estoque_atualizado` | `fornecimentos-service` | `fornecimentoId` | RF-03 | `pedidos.dlt` (RN-25) |
| `reserva_estoque_recusada` | `fornecimentos-service` | `pedidoId` | RF-03 | `pedidos.dlt` (RN-25) |
| `solicitacao_frete_criada` | `logistica-service` | `solicitacaoFreteId` | RF-06 | `pedidos.dlt` (RN-25) |
| `frete_selecionado` | `logistica-service` | `pedidoId` | RF-06 | `pedidos.dlt` (RN-25) |
| `entrega_concluida` | `logistica-service` | `pedidoId` | RF-06 | `pedidos.dlt` (RN-25) |
| `frete_indisponivel` | `logistica-service` | `pedidoId` | RF-06 | `pedidos.dlt` (RN-25) |

Validação comum (falha → inválida): mensagem não é objeto; falta algum dos oito campos do envelope; `eventId` não é UUID; `eventType` diferente do tópico; `eventVersion` diferente de `"1.0"`; `source` diferente do produtor da tabela; `correlationId` fora de 1–128 caracteres de `[A-Za-z0-9._-]`; `timestamp` não é instante com fuso; `aggregateId` diferente do campo da coluna "Chave" no `payload`; `payload` não é objeto; campo obrigatório ausente, com tipo errado ou fora do domínio; string obrigatória vazia ou só com espaços; número com mais casas que o permitido ou acima do máximo (§4). IDs externos são texto de 1 a 64 caracteres, comparados como texto exato. `pedidoId` é UUID em qualquer caixa.

| Tópico | Campos usados (todos obrigatórios, salvo indicação) |
|---|---|
| `pedido_temporario_solicitado` | `negociacaoId`, `tipo` (`LEILAO`/`VENDA_DIRETA`), `fornecimentoId`, `fornecedorId`, `produtoId`, `quantidade`, `precoUnitario` (valor monetário). |
| `negociacao_fechada` | `negociacaoId`, `tipo`, `status` (`ENCERRADO`, `DESERTO`, `RECUSADO`, `EXPIRADO`), `compradorId` (texto se `ENCERRADO`, senão `null`), `produtoId`, `alocacoes` (≥ 1 item se `ENCERRADO`, senão `[]`). Cada alocação: `fornecimentoId`, `fornecedorId`, `pedidoId`, `quantidade`, `valorUnitario`, `valorTotal`. `demandaId` é aceito e ignorado. |
| `produto_cadastrado`, `produto_atualizado` | `produtoId`, `nome` (1–255), `unidade`, `tipoTransporte`. Opcional: `pesoUnitarioKg` (> 0 ou `null`). O `timestamp` do envelope ordena as versões (RN-01). |
| `fornecimento_criado` | `fornecimentoId`, `fornecedorId`, `produtoId`, `unidade`, `origem` (Local). |
| `estoque_atualizado` | `fornecimentoId`, `produtoId`, `fornecedorId`, `operacao` (`RESERVA`, `LIBERACAO`, `BAIXA`, `REPOSICAO`, `ENCERRAMENTO`), `quantidadeMovimentada` (> 0), `quantidadeDisponivel` e `quantidadeReservada` (≥ 0), `pedidoId` (UUID em `RESERVA`, `LIBERACAO` e `BAIXA`; `null` nas demais), `atualizadoEm`. |
| `reserva_estoque_recusada` | `pedidoId`, `fornecimentoId`, `quantidadeSolicitada`, `quantidadeDisponivel` (≥ 0 ou `null`), `motivo` (`ESTOQUE_INSUFICIENTE`, `FORNECIMENTO_INEXISTENTE`), `recusadoEm`. |
| `solicitacao_frete_criada` | `solicitacaoFreteId`, `pedidoId`, `status` = `"ABERTA"`. |
| `frete_selecionado` | `pedidoId`, `solicitacaoFreteId`, `entregaId`, `cotacaoId`, `transportadoraId`, `tentativa` (inteiro ≥ 1), `preco`, `prazoDias` (inteiro ≥ 1), `status` = `"FRETE_SELECIONADO"`. Opcional: `dataEntregaPrevista` (data). `criterio` é aceito e ignorado. |
| `entrega_concluida` | `pedidoId`, `entregaId`, `transportadoraId`, `entregueEm`. `prazoPrometido`, `noPrazo` e `atrasoHoras` são aceitos e ignorados. |
| `frete_indisponivel` | `pedidoId`, `solicitacaoFreteId`, `motivo` (`SEM_COTACAO`, `SEM_TRANSPORTADORA_DISPONIVEL`), `ocorridoEm`. Opcional: `detalhe` (texto até 500, só na auditoria). |

Exemplo completo (os payloads de todos os tópicos estão no Anexo A):

```json
{
  "eventId": "37000000-0000-4000-8000-000000000001",
  "eventType": "pedido_temporario_solicitado",
  "eventVersion": "1.0",
  "timestamp": "2026-11-20T12:00:00.000Z",
  "source": "negociacao-service",
  "correlationId": "teste-ped-01",
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

`negociacao_fechada` `ENCERRADO` de um leilão conjunto (payload):

```json
{"negociacaoId":"31000000-0000-4000-8000-000000000001","tipo":"LEILAO","status":"ENCERRADO","compradorId":"b2000000-0000-4000-8000-000000000002","produtoId":"35000000-0000-4000-8000-000000000001","alocacoes":[{"fornecimentoId":"34000000-0000-4000-8000-000000000001","fornecedorId":"a1000000-0000-4000-8000-000000000001","pedidoId":"32000000-0000-4000-8000-000000000001","quantidade":5.00,"valorUnitario":130.00,"valorTotal":650.00},{"fornecimentoId":"34000000-0000-4000-8000-000000000002","fornecedorId":"a1000000-0000-4000-8000-000000000002","pedidoId":"32000000-0000-4000-8000-000000000002","quantidade":15.00,"valorUnitario":130.00,"valorTotal":1950.00}]}
```

### 7.4 Eventos publicados

Envelope: `eventId` UUID4 novo, `eventType` = tópico, `eventVersion` `"1.0"`, `timestamp` = `agora`, `source` `"pedidos-service"`, `correlationId` da mensagem ou requisição que causou a mudança (na criação pelo reconciliador, o da solicitação guardada), `aggregateId` = chave. A chave Kafka é igual ao `aggregateId`.

| Tópico | Quando | Chave de partição | Payload |
|---|---|---|---|
| `pedido_criado` | Criação do pedido temporário (RN-05) | `pedidoId` | `pedidoId`, `negociacaoId`, `compradorId` (`null`), `fornecedorId`, `produtoId`, `fornecimentoId`, `produtoNome`, `quantidade`, `unidade`, `pesoKg`, `tipoTransporte`, `valorTotal`, `origem`, `destino` (`null`), `status` (`"CRIADO"`) |
| `pedido_atualizado` | Toda versão nova a partir da 2, exatamente uma por versão | `pedidoId` | Sempre: `pedidoId`, `negociacaoId`, `fornecimentoId`, `status`, `statusAnterior`, `reservaSituacao`, `versao`. `CANCELADO`: + `motivo`. `VENDA_CONCLUIDA`: + `compradorId`, `valorUnitario`, `valorTotal`. `AGUARDANDO_FRETE`: + `compradorId`, `fornecedorId`, `produtoId`, `produtoNome`, `quantidade`, `unidade`, `pesoKg`, `tipoTransporte`, `valorTotal`, `origem`, `destino` e `dataEntregaDesejada` (omitida se nula). `FRETE_SELECIONADO`: + `frete`. `ENTREGUE`: + `entregueEm`. |
| `pedidos.dlt` | Mensagem rejeitada (RN-25, RN-26, RN-08) | chave original se for texto UTF-8 de 1 a 128 caracteres; senão `topico:particao:offset` (dublê: ver abaixo) | `originalEvent`, `error`, `stackTrace`, `failedAtAttempt` |

Exemplos (payloads; todos os status no Anexo A):

```json
{"pedidoId":"32000000-0000-4000-8000-000000000001","negociacaoId":"31000000-0000-4000-8000-000000000001","compradorId":null,"fornecedorId":"a1000000-0000-4000-8000-000000000001","produtoId":"35000000-0000-4000-8000-000000000001","fornecimentoId":"34000000-0000-4000-8000-000000000001","produtoNome":"Milho em grão","quantidade":5.00,"unidade":"TON","pesoKg":5000,"tipoTransporte":"GRANEL","valorTotal":600.00,"origem":{"cidade":"Rio Verde","uf":"GO"},"destino":null,"status":"CRIADO"}
```

```json
{"pedidoId":"32000000-0000-4000-8000-000000000001","negociacaoId":"31000000-0000-4000-8000-000000000001","fornecimentoId":"34000000-0000-4000-8000-000000000001","status":"AGUARDANDO_FRETE","statusAnterior":"VENDA_CONCLUIDA","reservaSituacao":"CONFIRMADA","versao":4,"compradorId":"b2000000-0000-4000-8000-000000000002","fornecedorId":"a1000000-0000-4000-8000-000000000001","produtoId":"35000000-0000-4000-8000-000000000001","produtoNome":"Milho em grão","quantidade":5.00,"unidade":"TON","pesoKg":5000,"tipoTransporte":"GRANEL","valorTotal":650.00,"origem":{"cidade":"Rio Verde","uf":"GO"},"destino":{"cidade":"Anápolis","uf":"GO"},"dataEntregaDesejada":"2026-11-27"}
```

`pedidos.dlt`: `eventId` = UUIDv5 (namespace URL `6ba7b811-9dad-11d1-80b4-00c04fd430c8`, nome `pedidos.dlt:topico:particao:offset`; no dublê, `pedidos.dlt:<topico>:<eventId recebido>`, com `desconhecido` no lugar do `eventId` quando ele falta ou não é texto). Chave no dublê: o `aggregateId` recebido quando for texto de 1 a 128 caracteres, mesmo que a mensagem seja inválida por outro motivo; senão `"desconhecido"`. `correlationId` do original quando válido; senão UUID4. Payload:

- `originalEvent`: o objeto recebido, quando cabe em 64 KiB depois da remoção de segredos; texto UTF-8 (bytes inválidos trocados por U+FFFD) truncado em 64 KiB quando não é JSON ou passa do limite; `null` quando esse texto bruto (só o ramo não-JSON, sem reserializar) contém `authorization`, `bearer`, `token`, `senha`, `password`, `secret`, `api_key`, `api-key`, `apikey` ou `\u` (sem diferenciar maiúsculas). **Remoção de segredos:** chave de objeto cujo nome, sem `_` e `-` e em minúsculas, seja `token`, `authorization`, `senha`, `password`, `accesstoken`, `refreshtoken`, `secret`, `apikey` ou `clientsecret` tem o valor trocado por `"[omitido]"`.
- `error`: `EVENTO_INVALIDO` (RN-25), `CONTRATO_DIVERGENTE` (RN-06), `FECHAMENTO_INCOMPATIVEL` (RN-08) ou `TENTATIVAS_ESGOTADAS` (RN-26).
- `stackTrace`: nome da classe da exceção e mensagem, sem trechos do original; `null` em `CONTRATO_DIVERGENTE` e `FECHAMENTO_INCOMPATIVEL`.
- `failedAtAttempt`: `3` em `TENTATIVAS_ESGOTADAS`; `1` nos demais.

## 8. Requisitos funcionais

### 8.0 Massa de teste comum aos critérios

Relógio: sem indicação, `agora = 2026-11-20T12:00:00.000Z` (hoje = 2026-11-20, calculado como no §4).

Empresas (`tenant_id`): F1-EMP `a1000000-0000-4000-8000-000000000001`, F2-EMP `a1000000-0000-4000-8000-000000000002`, F3-EMP `a1000000-0000-4000-8000-000000000003`, COMP `b2000000-0000-4000-8000-000000000002`, OUTRA `c3000000-0000-4000-8000-000000000003`. Tokens válidos: cabeçalho `alg` `RS256` e `kid` `k1` (chave pública devolvida por `chaves_publicas.obter("k1")`); claims `iss` `identity-service`, `aud` `b2b-platform`, `exp` = agora + 900 s, sem `nbf`, e:

| Token | `sub` | `tenant_id` | `active_scope` | Outros |
|---|---|---|---|---|
| TK-F1 | `usr-f1` | F1-EMP | `SUPPLIER` | — |
| TK-F2 | `usr-f2` | F2-EMP | `SUPPLIER` | — |
| TK-COMP | `usr-comp` | COMP | `BUYER` | — |
| TK-OUTRA-B | `usr-outra-b` | OUTRA | `BUYER` | — |
| TK-OUTRA-S | `usr-outra-s` | OUTRA | `SUPPLIER` | — |
| TK-CARRIER | `usr-carrier` | OUTRA | `CARRIER` | — |
| TK-ADMIN | `usr-admin` | — | — | `platform_role` `PLATFORM_ADMIN` |
| TK-SEM-CONTEXTO | `usr-sem` | — | `BUYER` | — |

Produto PROD `35000000-0000-4000-8000-000000000001` ("Milho em grão", `TON`, `GRANEL`, `pesoUnitarioKg` `null`). Negociação N `31000000-0000-4000-8000-000000000001`.

Todas as ofertas são do produto PROD, com `unidade` `TON`.

| Oferta | `fornecimentoId` | Fornecedor | Origem | Quantidade na solicitação | `precoUnitario` | `pesoKg` | Valor temporário |
|---|---|---|---|---:|---:|---:|---:|
| F1 | `34000000-0000-4000-8000-000000000001` | F1-EMP | Rio Verde/GO | 5.00 | 120.00 | 5000 | 600.00 |
| F2 | `34000000-0000-4000-8000-000000000002` | F2-EMP | Jataí/GO | 15.00 | 125.00 | 15000 | 1875.00 |
| F3 | `34000000-0000-4000-8000-000000000003` | F3-EMP | Mineiros/GO | 10.00 | 118.00 | 10000 | 1180.00 |

Cenários base: **B** (base) = `produto_cadastrado` de PROD e `fornecimento_criado` de F1, F2 e F3 consumidos. **T** (temporários) = B + `pedido_temporario_solicitado` de N para F1, F2 e F3, gerando P1, P2 e P3. **R** (reservados) = T + `estoque_atualizado` `RESERVA` de P1, P2 e P3. **V** (vendido) = R + fechamento `ENCERRADO` do §7.3 com P1 e P2 (P3 fica fora). **A** (aguardando frete) = V + API-07 de P1 por TK-COMP com `{"cidade":"Anápolis","uf":"GO","dataEntregaDesejada":"2026-11-27"}` + `solicitacao_frete_criada` `sol-go-21` de P1.

`P1`, `P2` e `P3` são os `pedidoId` gerados, lidos do `pedido_criado` publicado. Nos exemplos do §7 e do Anexo A, `32000000-0000-4000-8000-000000000001`, `...0002` e `...0003` representam P1, P2 e P3: o teste os substitui pelos valores gerados antes de enviar ou comparar. **Valores-padrão:** campo não citado num CA usa o valor do exemplo correspondente do Anexo A, com `pedidoId`, `fornecimentoId`, `fornecedorId`, `produtoId` e quantidade do pedido do cenário (inclusive `quantidadeSolicitada` e `quantidadeMovimentada`), e instantes iguais a `agora`. Na comparação de eventos publicados, `eventId` e `timestamp` não entram. Eventos consumidos usam o envelope do §7.3 com `eventId` distintos e `correlationId` `teste-ped-01`. Onde o critério diz "publica", o dublê do `EventBus` registra a chamada a `publish`; "na DLT" = publicação em `pedidos.dlt`; "auditoria X" = um registro com `acao` X no dublê do repositório.

### RF-01 — Manter as projeções de produto e oferta
- **Prioridade:** Must
- **Enunciado (EARS):** Quando um `produto_cadastrado`, `produto_atualizado` ou `fornecimento_criado` válido for recebido, o serviço deve atualizar a cópia local e reavaliar as solicitações pendentes.
- **Regras aplicáveis:** RN-01, RN-06
- **Interface:** tópicos `produto_cadastrado`, `produto_atualizado`, `fornecimento_criado`

**Critérios de aceitação**
```gherkin
# CA-01
Cenário: Projeções alimentam o snapshot do pedido
  Dado nenhum produto e nenhuma oferta
  Quando o handler recebe produto_cadastrado de PROD e fornecimento_criado de F1
  E depois pedido_temporario_solicitado de N para F1
  Então publica pedido_criado com produtoNome "Milho em grão", unidade "TON", tipoTransporte "GRANEL", pesoKg 5000 e origem {"cidade":"Rio Verde","uf":"GO"}

# CA-02
Cenário: Produto com timestamp igual e dados diferentes é conflito
  Dado o cenário B com PROD gravado por um produto_cadastrado de timestamp 2026-11-20T11:00:00.000Z
  Quando o handler recebe produto_atualizado de PROD com nome "Milho tipo 2" e o mesmo timestamp
  Então há auditoria EVENTO_CONFLITANTE sem pedidoId
  E um pedido_temporario_solicitado posterior de N para F1 publica pedido_criado com produtoNome "Milho em grão"

# CA-03
Cenário: Oferta com UF inválida vai para a DLT
  Dado o cenário B
  Quando o handler recebe fornecimento_criado de 34000000-0000-4000-8000-000000000009 com origem {"cidade":"Goiânia","uf":"go"}
  Então publica em pedidos.dlt com chave "34000000-0000-4000-8000-000000000009", error "EVENTO_INVALIDO" e failedAtAttempt 1
  E a oferta não é gravada
```

### RF-02 — Criar o pedido temporário
- **Prioridade:** Must
- **Enunciado (EARS):** Quando um `pedido_temporario_solicitado` válido for recebido para uma negociação aberta, o serviço deve criar um único pedido `CRIADO` sem comprador para a oferta e publicar `pedido_criado`.
- **Regras aplicáveis:** RN-02 a RN-06, RN-12, RN-13, RN-28
- **Interface:** tópico `pedido_temporario_solicitado`; `pedido_criado`; `app.main.reconciliar`

**Critérios de aceitação**
```gherkin
# CA-04
Cenário: Pedido temporário criado
  Dado o cenário B
  Quando o handler recebe pedido_temporario_solicitado de N para F1 com quantidade 5.00 e precoUnitario 120.00
  Então existe um pedido P1 para (N, F1) em CRIADO, compradorId null, reservaSituacao PENDENTE, quantidade 5.00, pesoKg 5000, valorTotal 600.00 e versao 1
  E publica pedido_criado com chave P1, compradorId null, destino null e fornecimentoId de F1
  E o histórico de P1 tem versao 1 com statusAnterior null e origem "pedido_temporario_solicitado"

# CA-05
Cenário: Solicitação repetida não duplica o pedido
  Dado o cenário B
  Quando o handler recebe 5 vezes o mesmo envelope de pedido_temporario_solicitado de N para F1
  E recebe um sexto envelope com outro eventId e o mesmo payload
  Então existe 1 pedido para (N, F1)
  E publica pedido_criado 1 vez

# CA-06
Cenário: Leilão conjunto cria um pedido por oferta
  Dado o cenário B
  Quando o handler recebe pedido_temporario_solicitado de N para F1 e para F2
  Então existem 2 pedidos de N, com fornecimentoId F1 (valorTotal 600.00) e F2 (valorTotal 1875.00)
  E publica 2 pedido_criado com chaves distintas

# CA-07
Cenário: Solicitação espera a projeção e o reconciliador não duplica
  Dado somente fornecimento_criado de F1 consumido
  Quando o handler recebe pedido_temporario_solicitado de N para F1
  Então não existe pedido para (N, F1) e nada é publicado
  Quando o handler recebe produto_cadastrado de PROD com correlationId "teste-ped-02"
  Então existe 1 pedido para (N, F1)
  E publica pedido_criado 1 vez, com correlationId "teste-ped-01" (o da solicitação)
  Quando reconciliar(app) é chamado 2 vezes
  Então as duas chamadas devolvem 0 e nada mais é publicado

# CA-08
Cenário: Solicitação para negociação já encerrada é descartada
  Dado o cenário B e um negociacao_fechada de N com status DESERTO, compradorId null e alocacoes []
  Quando o handler recebe pedido_temporario_solicitado de N para F3
  Então não existe pedido para (N, F3)
  E há auditoria SOLICITACAO_DESCARTADA com negociacaoId N

# CA-09
Cenário: Fornecedor divergente da projeção vai para a DLT
  Dado o cenário B
  Quando o handler recebe pedido_temporario_solicitado de N para F1 com fornecedorId "a1000000-0000-4000-8000-000000000002"
  Então publica em pedidos.dlt com chave N, error "CONTRATO_DIVERGENTE", stackTrace null e failedAtAttempt 1
  E não existe pedido para (N, F1)

# CA-10
Cenário: Outra solicitação para a mesma oferta com dados diferentes é conflito
  Dado o cenário T
  Quando o handler recebe pedido_temporario_solicitado de N para F1 com outro eventId e quantidade 6.00
  Então P1 continua com quantidade 5.00 e versao 1
  E há auditoria EVENTO_CONFLITANTE com pedidoId P1
```

### RF-03 — Acompanhar a reserva
- **Prioridade:** Must
- **Enunciado (EARS):** Quando a confirmação ou a recusa da reserva de um pedido temporário for recebida, o serviço deve atualizar a reserva, cancelar o pedido na recusa e publicar `pedido_atualizado`.
- **Regras aplicáveis:** RN-07, RN-11, RN-23
- **Interface:** tópicos `estoque_atualizado`, `reserva_estoque_recusada`; `pedido_atualizado`

**Critérios de aceitação**
```gherkin
# CA-11
Cenário: Reserva confirmada
  Dado o cenário T
  Quando o handler recebe estoque_atualizado de F1 com operacao RESERVA, pedidoId P1 e quantidadeMovimentada 5.00
  Então P1 continua em CRIADO com reservaSituacao CONFIRMADA e versao 2
  E publica pedido_atualizado com status "CRIADO", statusAnterior "CRIADO", reservaSituacao "CONFIRMADA", versao 2, negociacaoId N e fornecimentoId de F1

# CA-12
Cenário: Reserva recusada cancela o pedido uma vez
  Dado o cenário T
  Quando o handler recebe 2 vezes reserva_estoque_recusada de P1 com quantidadeSolicitada 5.00 e motivo ESTOQUE_INSUFICIENTE, com eventId distintos
  Então P1 fica CANCELADO, reservaSituacao RECUSADA, motivoCancelamento ESTOQUE_INSUFICIENTE e versao 2
  E publica 1 pedido_atualizado com status "CANCELADO", reservaSituacao "RECUSADA" e motivo "ESTOQUE_INSUFICIENTE"

# CA-13
Cenário: Reposição e liberação não mudam o pedido
  Dado o cenário V (P1 em VENDA_CONCLUIDA e P3 em CANCELADO por FORA_DO_LOTE)
  Quando o handler recebe estoque_atualizado de F1 com operacao REPOSICAO e pedidoId null
  E estoque_atualizado de F3 com operacao LIBERACAO e pedidoId P3
  Então nada é publicado em pedidos.dlt nem em pedido_atualizado
  E P1 e P3 mantêm a versão
  E há 1 auditoria ESTOQUE_REGISTRADO com pedidoId P3

# CA-14
Cenário: Reserva com quantidade diferente é conflito
  Dado o cenário T
  Quando o handler recebe estoque_atualizado de F1 com operacao RESERVA, pedidoId P1 e quantidadeMovimentada 4.00
  Então P1 continua com reservaSituacao PENDENTE e versao 1
  E há auditoria EVENTO_CONFLITANTE com pedidoId P1
```

### RF-04 — Efetivar ou cancelar no fechamento
- **Prioridade:** Must
- **Enunciado (EARS):** Quando um `negociacao_fechada` válido for recebido, o serviço deve concluir a venda dos pedidos alocados e cancelar os demais pedidos da negociação, ou rejeitar o fechamento inteiro se alguma alocação não corresponder.
- **Regras aplicáveis:** RN-08, RN-09, RN-10, RN-12, RN-13
- **Interface:** tópico `negociacao_fechada`; `pedido_atualizado`

**Critérios de aceitação**
```gherkin
# CA-15
Cenário: Venda concluída no leilão conjunto
  Dado o cenário R
  Quando o handler recebe o negociacao_fechada ENCERRADO do §7.3 com alocações de P1 (valorTotal 650.00) e P2 (valorTotal 1950.00)
  Então P1 fica VENDA_CONCLUIDA com compradorId COMP, valorUnitario 130.00, valorTotal 650.00, vendidoEm 2026-11-20T12:00:00.000Z e versao 3
  E P2 fica VENDA_CONCLUIDA com valorTotal 1950.00
  E publica pedido_atualizado com status "VENDA_CONCLUIDA" para P1 e para P2

# CA-16
Cenário: Oferta fora do lote é cancelada
  Dado o cenário R
  Quando o handler recebe o mesmo negociacao_fechada ENCERRADO, que não inclui P3
  Então P3 fica CANCELADO com motivoCancelamento FORA_DO_LOTE e reservaSituacao ENCERRADA
  E publica pedido_atualizado de P3 com status "CANCELADO", motivo "FORA_DO_LOTE", reservaSituacao "ENCERRADA" e versao 3

# CA-17
Cenário: Leilão deserto cancela todos uma vez
  Dado o cenário R
  Quando o handler recebe 2 vezes negociacao_fechada de N com status DESERTO, compradorId null e alocacoes [], com eventId distintos
  Então P1, P2 e P3 ficam CANCELADO com motivoCancelamento NEGOCIACAO_SEM_VENDA
  E publica 1 pedido_atualizado de cancelamento por pedido

# CA-18
Cenário: Fechamento incompatível é rejeitado inteiro
  Dado o cenário R
  Quando o handler recebe o negociacao_fechada ENCERRADO do §7.3 com a alocação de P1 alterada para quantidade 4.00
  Então publica em pedidos.dlt com chave N, error "FECHAMENTO_INCOMPATIVEL", stackTrace null e failedAtAttempt 1
  E P1, P2 e P3 continuam em CRIADO com versao 2
  Quando o handler recebe o negociacao_fechada ENCERRADO correto, com outro eventId
  Então P1 e P2 ficam VENDA_CONCLUIDA e P3 fica CANCELADO

# CA-19
Cenário: Segundo fechamento diferente não desfaz o primeiro
  Dado o cenário V
  Quando o handler recebe negociacao_fechada de N com status DESERTO, compradorId null e alocacoes []
  Então P1 e P2 continuam VENDA_CONCLUIDA
  E há auditoria EVENTO_CONFLITANTE com negociacaoId N
  E nada é publicado em pedido_atualizado
```

### RF-05 — Informar o destino
- **Prioridade:** Must
- **Enunciado (EARS):** Quando o comprador de um pedido em `VENDA_CONCLUIDA` informar o destino pela API-07, o serviço deve gravar o destino, mover o pedido para `AGUARDANDO_FRETE` e publicar o snapshot de carga completo.
- **Regras aplicáveis:** RN-14, RN-15, RN-16, RN-17, RN-24
- **Interface:** API-07; `pedido_atualizado`

**Critérios de aceitação**
```gherkin
# CA-20
Cenário: Destino informado e repetido
  Dado o cenário V
  Quando TK-COMP envia PUT /api/v1/pedidos/P1/destino com {"cidade":"Anápolis","uf":"GO","dataEntregaDesejada":"2026-11-27"}
  Então a resposta é 200 com status "AGUARDANDO_FRETE", destino {"cidade":"Anápolis","uf":"GO"}, dataEntregaDesejada "2026-11-27" e versao 4
  E publica pedido_atualizado igual ao exemplo AGUARDANDO_FRETE do §7.4
  Quando TK-COMP repete o mesmo PUT
  Então a resposta é 200 com versao 4
  E nada mais é publicado

# CA-21
Cenário: Quem não pode informar o destino
  Dado o cenário V
  Quando TK-F1 envia o PUT de P1 com o corpo do CA-20
  Então a resposta é 403 com codigo "ACESSO_NEGADO"
  Quando TK-OUTRA-B envia o PUT de P1 com o corpo do CA-20
  Então a resposta é 404 com codigo "PEDIDO_NAO_ENCONTRADO"
  Quando TK-COMP envia o PUT de P3 com o corpo do CA-20, e P3 está CANCELADO e sem comprador
  Então a resposta é 404 com codigo "PEDIDO_NAO_ENCONTRADO"
  Quando TK-COMP envia o PUT do CA-20 para P1 e depois outro PUT de P1 com {"cidade":"Goiânia","uf":"GO"}
  Então a primeira resposta é 200 e a segunda é 409 com codigo "TRANSICAO_INVALIDA"

# CA-22
Cenário: Destino inválido
  Dado o cenário V
  Quando TK-COMP envia o PUT de P1 com {"cidade":"Anápolis","uf":"GO","dataEntregaDesejada":"2026-11-19"}
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando TK-COMP envia o PUT de P1 com {"cidade":"Anápolis","uf":"go"}
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando TK-COMP envia o PUT de P1 com {"cidade":"   ","uf":"GO"}
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando TK-COMP envia o PUT de P1 com {"cidade":"Anápolis","uf":"GO","frete":1}
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
  E P1 continua VENDA_CONCLUIDA com versao 3

# CA-23
Cenário: Chave de idempotência
  Dado o cenário V
  Quando TK-COMP envia o PUT de P1 com Idempotency-Key "dest-1" e {"cidade":"Anápolis","uf":"GO"}
  E repete com a mesma chave e o mesmo corpo
  Então as duas respostas são 200 com o mesmo corpo
  Quando TK-COMP envia com a chave "dest-1" e {"cidade":"Goiânia","uf":"GO"}
  Então a resposta é 409 com codigo "IDEMPOTENCIA_CONFLITO"
```

### RF-06 — Registrar frete e entrega
- **Prioridade:** Must
- **Enunciado (EARS):** Quando um evento de frete válido da Logística for recebido para um pedido em `AGUARDANDO_FRETE` ou `FRETE_SELECIONADO`, o serviço deve registrar a solicitação, a seleção, a troca, a indisponibilidade ou a entrega conforme a solicitação e a seleção vigentes.
- **Regras aplicáveis:** RN-18 a RN-23
- **Interface:** tópicos `solicitacao_frete_criada`, `frete_selecionado`, `entrega_concluida`, `frete_indisponivel`; `pedido_atualizado`

**Critérios de aceitação**
```gherkin
# CA-24
Cenário: Frete selecionado
  Dado o cenário A
  Quando o handler recebe frete_selecionado de P1 da solicitação sol-go-21, tentativa 1, entregaId "ent-go-31", cotacaoId "cot-go-18", transportadoraId "tra-go-8", preco 150.00 e prazoDias 2
  Então P1 fica FRETE_SELECIONADO com frete.cotacaoId "cot-go-18", frete.tentativa 1 e versao 5
  E publica pedido_atualizado com status "FRETE_SELECIONADO" e frete igual ao do detalhe

# CA-25
Cenário: Troca de transportadora
  Dado P1 em FRETE_SELECIONADO com tentativa 1 (CA-24)
  Quando o handler recebe, nesta ordem, frete_selecionado de sol-go-21 tentativa 2 com entregaId "ent-go-32" e transportadoraId "tra-go-9", a repetição da tentativa 1 e entrega_concluida de "ent-go-31" da "tra-go-8"
  Então P1 continua FRETE_SELECIONADO com frete.tentativa 2 e versao 6
  E publica 1 pedido_atualizado novo
  E há auditoria ENTREGA_DESCARTADA com pedidoId P1

# CA-26
Cenário: Entrega antes da seleção
  Dado o cenário A
  Quando o handler recebe entrega_concluida de P1 com entregaId "ent-go-31", transportadoraId "tra-go-8" e entregueEm 2026-11-23T16:00:00.000Z
  Então P1 continua AGUARDANDO_FRETE
  Quando o handler recebe o frete_selecionado do CA-24
  Então P1 fica ENTREGUE com entregueEm "2026-11-23T16:00:00.000Z" e versao 6
  E o histórico de P1 termina com FRETE_SELECIONADO (versao 5) e ENTREGUE (versao 6, origem "entrega_concluida")

# CA-27
Cenário: Frete indisponível não cancela e libera nova solicitação
  Dado o cenário A
  Quando o handler recebe frete_indisponivel de P1 da solicitação sol-go-21 com motivo SEM_COTACAO e ocorridoEm 2026-11-21T02:00:00.000Z
  Então P1 continua AGUARDANDO_FRETE com versao 4
  E o detalhe tem ocorrenciaFrete {"motivo":"SEM_COTACAO","ocorridoEm":"2026-11-21T02:00:00.000Z"}
  E há auditoria FRETE_INDISPONIVEL
  E nada é publicado em pedido_atualizado
  Quando o handler recebe solicitacao_frete_criada sol-go-22 de P1
  E frete_selecionado de P1 da solicitação sol-go-22, tentativa 1
  Então P1 fica FRETE_SELECIONADO com frete.solicitacaoFreteId "sol-go-22"

# CA-28
Cenário: Evento de frete fora de estado ou sem pedido
  Dado o cenário V
  Quando o handler recebe frete_selecionado de P1
  Então P1 continua VENDA_CONCLUIDA e há auditoria EVENTO_FORA_DE_ESTADO
  Quando o handler recebe entrega_concluida de pedidoId "32000000-0000-4000-8000-000000000099"
  Então há auditoria EVENTO_NAO_CORRELACIONADO e nada é publicado em pedidos.dlt
```

### RF-07 — Consultar pedidos e histórico
- **Prioridade:** Must
- **Enunciado (EARS):** Quando um token autorizado consultar a API-04, a API-05 ou a API-06, o serviço deve devolver só os pedidos visíveis, com paginação.
- **Regras aplicáveis:** RN-16, RN-17
- **Interface:** API-04, API-05, API-06

**Critérios de aceitação**
```gherkin
# CA-29
Cenário: Visibilidade do fornecedor e do comprador
  Dado o cenário T
  Quando TK-F1 lista GET /api/v1/pedidos?negociacaoId=N
  Então a resposta é 200 com total 1 e o item P1 com compradorId null
  Quando TK-COMP lista GET /api/v1/pedidos?negociacaoId=N
  Então a resposta é 200 com {"items":[],"page":1,"size":10,"total":0}
  Dado o cenário V
  Quando TK-COMP lista, abre o detalhe e o histórico de P1
  Então a lista tem total 2 (P1 e P2), o detalhe tem status "VENDA_CONCLUIDA" e o histórico tem versões [1,2,3] nessa ordem
  Quando TK-ADMIN abre o detalhe de P3
  Então a resposta é 200 com status "CANCELADO"

# CA-30
Cenário: Paginação e parâmetros inválidos
  Dado o cenário T
  Quando TK-F1 envia GET /api/v1/pedidos?page=1&size=101
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA" e Content-Type application/problem+json
  Quando TK-F1 envia GET /api/v1/pedidos?status=CRIADO&status=CANCELADO
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"
  Quando TK-F1 envia GET /api/v1/pedidos/abc
  Então a resposta é 422 com codigo "VALIDACAO_INVALIDA"

# CA-31
Cenário: Pedido de outra empresa
  Dado o cenário T
  Quando TK-OUTRA-S lista GET /api/v1/pedidos e consulta o detalhe e o histórico de P1
  Então a lista tem total 0 e as consultas diretas respondem 404 com codigo "PEDIDO_NAO_ENCONTRADO"
```

### RF-08 — Autenticar e autorizar
- **Prioridade:** Must
- **Enunciado (EARS):** Se uma requisição de negócio chegar sem token válido, sem contexto ou com escopo não permitido, então o serviço deve recusá-la com o erro do §7.1, sem alterar dados.
- **Regras aplicáveis:** RN-17, RN-29
- **Interface:** §7.1, API-04 a API-07

**Critérios de aceitação**
```gherkin
# CA-32
Cenário: Token inválido e chaves indisponíveis
  Dado o cenário T
  Quando GET /api/v1/pedidos é enviado sem Authorization
  Então a resposta é 401 com codigo "TOKEN_INVALIDO" e o cabeçalho WWW-Authenticate "Bearer"
  Quando é enviado com TK-F1 expirado (exp = agora)
  Então a resposta é 401 com codigo "TOKEN_INVALIDO"
  Quando chaves_publicas.obter lança exceção e a requisição usa TK-F1
  Então a resposta é 503 com codigo "DEPENDENCIA_INDISPONIVEL"

# CA-33
Cenário: Contexto e escopo
  Dado o cenário V
  Quando TK-SEM-CONTEXTO envia GET /api/v1/pedidos
  Então a resposta é 403 com codigo "CONTEXTO_NAO_SELECIONADO"
  Quando TK-CARRIER envia GET /api/v1/pedidos
  Então a resposta é 403 com codigo "ACESSO_NEGADO"
  Quando TK-ADMIN envia GET /api/v1/pedidos?negociacaoId=N
  Então a resposta é 200 com total 3
  Quando TK-ADMIN envia o PUT de destino de P1
  Então a resposta é 403 com codigo "ACESSO_NEGADO" e P1 mantém a versao 3
```

### RF-09 — Consumir com segurança
- **Prioridade:** Must
- **Enunciado (EARS):** Se uma mensagem consumida for inválida, repetida ou não puder ser processada, então o serviço deve publicá-la em `pedidos.dlt`, ignorá-la ou devolver a falha ao adaptador conforme a RN-25 e a RN-26, sem efeito parcial.
- **Regras aplicáveis:** RN-25, RN-26, RN-27
- **Interface:** §7.3, `pedidos.dlt`

**Critérios de aceitação**
```gherkin
# CA-34
Cenário: Envelope sem payload
  Dado o cenário T
  Quando o handler de frete_selecionado recebe um envelope com aggregateId P1 e sem payload
  Então publica em pedidos.dlt com chave P1, error "EVENTO_INVALIDO" e failedAtAttempt 1
  E P1 mantém a versao 1

# CA-35
Cenário: Falha de execução transitória e esgotada
  Dado o cenário T e repositorio.atualizar_pedido lançando ValueError na primeira chamada
  Quando o handler recebe reserva_estoque_recusada de P1
  Então clock.sleep é chamado com 1 e P1 fica CANCELADO uma vez, sem publicação em pedidos.dlt
  Dado o cenário T e repositorio.atualizar_pedido lançando ValueError em todas as chamadas
  Quando o handler recebe reserva_estoque_recusada de P2 com fornecimentoId de F2 e quantidadeSolicitada 15.00
  Então clock.sleep é chamado com 1 e com 2
  E publica em pedidos.dlt com error "TENTATIVAS_ESGOTADAS" e failedAtAttempt 3
  E P2 continua CRIADO com versao 1

# CA-36
Cenário: Banco indisponível volta ao adaptador
  Dado o cenário T e repositorio.transacao lançando RepositorioIndisponivel
  Quando o handler recebe reserva_estoque_recusada de P1
  Então o handler propaga RepositorioIndisponivel
  E nada é publicado

# CA-37
Cenário: Mensagem duplicada e inválida
  Dado o cenário T e um envelope de frete_indisponivel de P1 sem o campo motivo
  Quando o handler recebe esse envelope 2 vezes com o mesmo eventId
  Então publica em pedidos.dlt 2 vezes com error "EVENTO_INVALIDO"
```

### RF-10 — Saúde e erros inesperados
- **Prioridade:** Must
- **Enunciado (EARS):** O serviço deve expor a API-01 a API-03 sem token e responder `500 ERRO_INTERNO` a uma falha não prevista.
- **Regras aplicáveis:** RN-29
- **Interface:** API-01, API-02, API-03

**Critérios de aceitação**
```gherkin
# CA-38
Cenário: Prontidão
  Dado repositorio.disponivel() e event_bus.disponivel() verdadeiros
  Quando GET /health/ready é enviado
  Então a resposta é 200 com {"status":"ready"}
  Dado event_bus.disponivel() falso
  Quando GET /health/ready é enviado
  Então a resposta é 503 com {"status":"not_ready"}
  E GET /health responde 200 com {"status":"up"}
  E GET /api/v1/pedidos com TK-F1 responde 200

# CA-39
Cenário: Falha inesperada
  Dado o cenário T e repositorio.listar_pedidos lançando ValueError
  Quando TK-F1 envia GET /api/v1/pedidos
  Então a resposta é 500 com codigo "ERRO_INTERNO" e Content-Type application/problem+json
```

## 9. Regras de negócio

| ID | Regra | Exemplo concreto |
|---|---|---|
| RN-01 | Projeções: `produto_cadastrado` e `produto_atualizado` gravam `nome`, `unidade`, `tipoTransporte`, `pesoUnitarioKg` e o `timestamp` do envelope; só se aplicam com `timestamp` maior que o gravado; igual com algum desses quatro campos diferente → auditoria `EVENTO_CONFLITANTE` e mantém o primeiro; igual com os mesmos campos ou menor → ignorado. `fornecimento_criado` grava a oferta; um segundo da mesma oferta com `fornecedorId`, `produtoId`, `unidade` ou `origem` (cidade aparada e `uf`) diferente → `EVENTO_CONFLITANTE`, mantém o primeiro. Depois da transação da projeção, reavalia (RN-06) as pendências daquele produto ou daquela oferta (`listar_pendentes(100, produto_id=...)` ou `(100, fornecimento_id=...)`), cada uma na sua própria transação; o que passar de 100 fica para o `reconciliar`; uma falha nessa reavaliação é registrada em log e não propaga (a pendência fica para o `reconciliar`). Atualizações não alteram pedidos já criados. | `produto_atualizado` com o mesmo `timestamp` e outro nome → conflito. |
| RN-02 | Unicidade: no máximo um pedido por `(negociacaoId, fornecimentoId)`. Uma solicitação com outro `eventId` para uma chave que já tem pedido ou pendência é repetição se `tipo`, `fornecedorId`, `produtoId`, `quantidade` e `precoUnitario` forem iguais; se algum diferir → `EVENTO_CONFLITANTE`, sem mudança. | Mesmo payload 6 vezes → 1 pedido. |
| RN-03 | Peso: `KG` → `quantidade`; `TON` → `quantidade × 1000`; `UN`, `CX` e `L` → `quantidade × pesoUnitarioKg` (`ROUND_HALF_UP`, 3 casas). Sem `pesoUnitarioKg` nessas unidades → pendente (`PESO_AUSENTE`). | 5.00 TON → 5000. |
| RN-04 | Valor do pedido temporário: `valorUnitario = precoUnitario`; `valorTotal = quantidade × precoUnitario` (`ROUND_HALF_UP`, 2 casas). Resultado acima de `99999999999999.99` → mensagem inválida (RN-25). | 5.00 × 120.00 = 600.00. |
| RN-05 | Criação: pedido `CRIADO`, `compradorId = null`, `destino = null`, reserva `PENDENTE`, versão 1, histórico e auditoria `PEDIDO_CRIADO`, e `pedido_criado` (§7.4). | CA-04. |
| RN-06 | Ordem de avaliação de uma solicitação válida, depois de `bloquear_negociacao`: (1) já existe pedido ou pendência para a chave → RN-02; (2) negociação em `negociacao_encerrada` → auditoria `SOLICITACAO_DESCARTADA`, sem pedido; (3) alguma projeção presente diverge (`fornecedorId` ou `produtoId` da oferta, ou `unidade` da oferta diferente da do produto) → DLT `CONTRATO_DIVERGENTE`; (4) projeção de produto ou de oferta ausente, ou peso sem `pesoUnitarioKg` → grava `solicitacao_pendente` com a causa; (5) senão, cria (RN-05). Na reavaliação (RN-01 ou `reconciliar`), a pendência passa de novo pelos passos 2 a 5: no passo 2, a pendência é removida com a auditoria `SOLICITACAO_DESCARTADA`; no passo 3, é removida com `SOLICITACAO_INVALIDA`, sem DLT; no passo 4, continua com o `recebidoEm` original; no passo 5, o pedido é criado com o `eventId` e o `correlationId` da solicitação guardada. | Produto ausente → pendência; produto chega → 1 pedido. |
| RN-07 | Reserva: `estoque_atualizado` `RESERVA` com o `pedidoId`, o `fornecimentoId` e `quantidadeMovimentada` iguais aos do pedido em `CRIADO` com reserva `PENDENTE` → reserva `CONFIRMADA`, status `CRIADO`, versão nova, auditoria `RESERVA_CONFIRMADA`. `reserva_estoque_recusada` com `pedidoId`, `fornecimentoId` e `quantidadeSolicitada` iguais, reserva `PENDENTE` → `CANCELADO`, reserva `RECUSADA`, `motivoCancelamento` = `motivo` recebido, auditoria `PEDIDO_CANCELADO`. As duas publicam `pedido_atualizado`. `RESERVA` igual com reserva já `CONFIRMADA` → sem efeito, sem auditoria. Oferta ou quantidade diferente, ou recusa com reserva `CONFIRMADA` → `EVENTO_CONFLITANTE`. `pedidoId` desconhecido → sem efeito, sem auditoria. Pedido em estado terminal: vale a RN-23, avaliada antes desta regra. | Recusa de P1 → `CANCELADO`, `ESTOQUE_INSUFICIENTE`. |
| RN-08 | Validação do fechamento, depois de `bloquear_negociacao`: se `negociacao_encerrada` já existe, vale a RN-12. Senão, com `ENCERRADO`, cada alocação precisa apontar para um pedido existente da mesma negociação, em `CRIADO`, com reserva `CONFIRMADA`, mesmo `fornecimentoId`, `fornecedorId` e `quantidade`, com `produtoId` e `tipo` do fechamento iguais aos do pedido, e nenhum `pedidoId` ou `fornecimentoId` pode se repetir. Se qualquer alocação falhar, nenhum pedido muda, a mensagem vai para a DLT com `FECHAMENTO_INCOMPATIVEL` e a negociação não é gravada como encerrada. | Alocação de P1 com 4.00 → DLT; o fechamento correto depois é aplicado. |
| RN-09 | Efetivação: cada pedido de `alocacoes` recebe o `compradorId` do topo do fechamento e o `valorUnitario` e o `valorTotal` da sua alocação, `vendidoEm = agora`, vai a `VENDA_CONCLUIDA`, auditoria `VENDA_CONCLUIDA` e `pedido_atualizado`. O valor não é recalculado. | Alocação com quantidade 60.00, `valorUnitario` 9.00 e `valorTotal` 553.86 → grava 553.86. |
| RN-10 | Cancelamento no fechamento: todo pedido não terminal da negociação fora de `alocacoes` vai a `CANCELADO`; `motivoCancelamento` `FORA_DO_LOTE` em `ENCERRADO` e `NEGOCIACAO_SEM_VENDA` em `DESERTO`, `RECUSADO` ou `EXPIRADO`; reserva `ENCERRADA` se estava `PENDENTE` ou `CONFIRMADA`; auditoria `PEDIDO_CANCELADO` e `pedido_atualizado`. O fechamento inteiro é uma transação. | Deserto → P1, P2 e P3 cancelados. |
| RN-11 | `estoque_atualizado` com `REPOSICAO` ou `ENCERRAMENTO` → sem efeito, sem auditoria. `LIBERACAO` e `BAIXA` de pedido conhecido, em qualquer status → uma auditoria `ESTOQUE_REGISTRADO` por `eventId` distinto, sem versão nova (a RN-27 não se aplica a elas). | `LIBERACAO` de P3 cancelado → auditoria. |
| RN-12 | Todo fechamento aplicado, inclusive de negociação sem pedidos, grava `negociacao_encerrada`. Um fechamento seguinte da mesma negociação com o mesmo `status`, `compradorId` e `alocacoes` (ordenadas por `pedidoId`, números por valor) → repetição; diferente → `EVENTO_CONFLITANTE`, sem desfazer o primeiro. | `DESERTO` depois de `ENCERRADO` → conflito. |
| RN-13 | Criação e fechamento da mesma negociação são serializados por `bloquear_negociacao`: uma criação concorrente termina antes e entra no cancelamento, ou vê a negociação encerrada e é descartada. | Nunca sobra pedido `CRIADO` de negociação encerrada. |
| RN-14 | Destino (API-07): só `BUYER` cujo `tenant_id` é o `compradorId` do pedido (outro comprador ou pedido sem comprador → `404 PEDIDO_NAO_ENCONTRADO`). Em `VENDA_CONCLUIDA` → grava `destino` (cidade sem espaços nas pontas) e `dataEntregaDesejada`, vai a `AGUARDANDO_FRETE`, auditoria `DESTINO_INFORMADO`, histórico com origem `http_destino` e `pedido_atualizado`. Em `AGUARDANDO_FRETE`, `FRETE_SELECIONADO` ou `ENTREGUE`: mesma cidade (comparação exata após remover os espaços das pontas), mesma `uf` e mesma `dataEntregaDesejada` (ausente = `null`) → `200` com o detalhe atual, sem versão nova; diferente → `409 TRANSICAO_INVALIDA`. Em `CRIADO` ou `CANCELADO` → `409 TRANSICAO_INVALIDA` (quando visível). | PUT repetido → 200, versão 4. |
| RN-15 | `dataEntregaDesejada` informada precisa ser ≥ hoje; senão `422 VALIDACAO_INVALIDA`. A regra só vale para destino novo: a repetição de um destino já gravado responde pela RN-14. | Hoje 2026-11-20, data 2026-11-19 → 422. |
| RN-16 | O pedido temporário não aparece para o comprador e não aceita destino. | CA-21, CA-29. |
| RN-17 | Visibilidade, aplicada antes da paginação: `BUYER` vê pedidos com `compradorId` = empresa; `SUPPLIER` vê pedidos com `fornecedorId` = empresa; `TENANT_ADMIN` vê os dois; suporte vê todos (só leitura). Pedido invisível → `404 PEDIDO_NAO_ENCONTRADO`. `CARRIER` → `403 ACESSO_NEGADO`. | OUTRA em P1 → 404. |
| RN-18 | Eventos de frete (`solicitacao_frete_criada`, `frete_selecionado`, `entrega_concluida`, `frete_indisponivel`) só valem com o pedido em `AGUARDANDO_FRETE` ou `FRETE_SELECIONADO`. Em `CRIADO` ou `VENDA_CONCLUIDA` → auditoria `EVENTO_FORA_DE_ESTADO`, sem efeito. Pedido desconhecido → `EVENTO_NAO_CORRELACIONADO`, sem DLT. | Frete em `VENDA_CONCLUIDA` → fora de estado. |
| RN-19 | Solicitação vigente: uma `solicitacao_frete_criada` ou um `frete_selecionado` com `solicitacaoFreteId` desconhecido torna essa solicitação `VIGENTE` só se o pedido não tem solicitação vigente ou se a vigente já recebeu `frete_indisponivel`; a anterior vira `SUBSTITUIDA`. Se o pedido já tem outra vigente sem indisponibilidade → `EVENTO_CONFLITANTE`, sem efeito. Uma `SUBSTITUIDA` nunca volta; seus eventos → `EVENTO_FORA_DE_ESTADO`. Trocar a vigente não cria versão nem evento; em `FRETE_SELECIONADO`, o `frete` continua com a seleção anterior até chegar uma seleção da nova solicitação. | `sol-go-22` depois de indisponibilidade de `sol-go-21` → vigente. |
| RN-20 | Seleção: `frete_selecionado` da solicitação vigente → grava `frete`, vai a `FRETE_SELECIONADO` (de `AGUARDANDO_FRETE`) ou gera versão nova no mesmo status (troca), auditoria `FRETE_APLICADO` e `pedido_atualizado`. Na mesma solicitação: tentativa maior substitui; menor → ignorada; mesma tentativa com os mesmos dados → repetição; com dados diferentes → `EVENTO_CONFLITANTE`. Uma nova solicitação vigente aceita qualquer tentativa a partir de 1. Toda seleção aplicada entra em `selecoes`; a anterior vira `SUBSTITUIDA`. Depois de aplicar uma seleção, reavalia na mesma transação a entrega guardada do pedido (RN-21). | Tentativa 2 depois da 1 → troca. |
| RN-21 | Entrega, avaliada nesta ordem sobre as `selecoes` de todas as solicitações do pedido: (1) `entregaId` e `transportadoraId` iguais aos da seleção vigente → `ENTREGUE`, `entregueEm` recebido, auditoria `ENTREGA_APLICADA`, `pedido_atualizado`; (2) iguais aos de uma seleção `SUBSTITUIDA` ou de solicitação `SUBSTITUIDA` → auditoria `ENTREGA_DESCARTADA`; (3) `entregaId` igual ao da seleção vigente com outra `transportadoraId` → `EVENTO_CONFLITANTE`; (4) qualquer outro par → guardada em `entrega_pendente` e aplicada quando chegar a seleção com esse par. Na reavaliação, entrega guardada de pedido em estado terminal é removida sem auditoria. | Entrega antes da seleção → aplicada depois. |
| RN-22 | `frete_indisponivel` da solicitação vigente → `indisponivel = true` na solicitação, `ocorrenciaFrete = {motivo, ocorridoEm}`, `atualizadoEm = agora`, auditoria `FRETE_INDISPONIVEL`; sem versão nova, sem `pedido_atualizado`, sem cancelamento (a baixa já ocorreu). A Logística reabre a cotação com nova `solicitacao_frete_criada`. Um segundo `frete_indisponivel` da mesma solicitação com outro `motivo` atualiza `ocorrenciaFrete` e gera outra auditoria; com o mesmo `motivo` é repetição. De solicitação substituída → `EVENTO_FORA_DE_ESTADO`; desconhecida → `EVENTO_NAO_CORRELACIONADO`. | CA-27. |
| RN-23 | Estado terminal (`CANCELADO`, `ENTREGUE`), avaliado antes das regras de cada tópico: nenhuma mudança. Repetição de negócio → sem auditoria; `LIBERACAO` e `BAIXA` → RN-11; `estoque_atualizado` `RESERVA` para pedido `CANCELADO` → sem efeito e sem auditoria (o Fornecimento libera); demais eventos → `EVENTO_FORA_DE_ESTADO`. | Recusa repetida em P1 cancelado → sem efeito. |
| RN-24 | Idempotência HTTP (API-07): escopo = `sub` + `tenant_id` + `active_scope` + método + caminho com `pedidoId` em minúsculas + `Idempotency-Key`. Só respostas 2xx são memorizadas (status e corpo, com o hash do corpo da requisição) por 24 h, na mesma transação da mudança. O hash é o SHA-256 do JSON canônico do corpo (`json.dumps(corpo, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`). A resposta 200 de uma repetição (RN-14) também é memorizada. Mesmo escopo e corpo → mesma resposta, sem efeito. Mesmo escopo e outro corpo → `409 IDEMPOTENCIA_CONFLITO`. Requisições simultâneas com a mesma chave são serializadas; a segunda recebe a resposta da primeira. | CA-23. |
| RN-25 | Mensagem consumida inválida (§7.3) não altera estado, não é registrada como processada e é publicada em `pedidos.dlt` na hora com `EVENTO_INVALIDO` e `failedAtAttempt` 1. A validação vem antes da deduplicação. Mensagens enviadas à DLT por `CONTRATO_DIVERGENTE`, `FECHAMENTO_INCOMPATIVEL` ou `TENTATIVAS_ESGOTADAS` também não são registradas como processadas: uma reentrega com o mesmo `eventId` é avaliada de novo. | Envelope sem `payload` → DLT. |
| RN-26 | Processamento: efeito e `registrar_evento_processado` na mesma transação. `RepositorioIndisponivel` → o handler propaga a exceção, sem publicar nada (o adaptador não confirma o offset e reentrega a cada 5 s). Outra exceção → o handler repete a transação até 3 execuções no total, chamando `clock.sleep(1)` e `clock.sleep(2)` entre elas; se a terceira falhar, publica em `pedidos.dlt` com `TENTATIVAS_ESGOTADAS`, `failedAtAttempt` 3 e `stackTrace`, fora da transação que falhou. | 3 falhas → DLT com tentativa 3. |
| RN-27 | Deduplicação: (`source`, `eventId`) já processado → sem efeito. Repetição de negócio com outro `eventId` é comparada pelos campos: `pedido_temporario_solicitado` (RN-02); `negociacao_fechada` (RN-12); `estoque_atualizado` `RESERVA`: `pedidoId`, `fornecimentoId`, `quantidadeMovimentada`; `reserva_estoque_recusada`: `pedidoId`, `fornecimentoId`, `quantidadeSolicitada`, `motivo`; `solicitacao_frete_criada`: `solicitacaoFreteId`, `pedidoId`; `frete_selecionado`: `solicitacaoFreteId`, `tentativa`, `entregaId`, `cotacaoId`, `transportadoraId`, `preco`, `prazoDias`, `dataEntregaPrevista`; `entrega_concluida`: `entregaId`, `transportadoraId`, `entregueEm`; `frete_indisponivel`: `solicitacaoFreteId`, `motivo`. Repetição → sem efeito e sem auditoria, em qualquer status. | 2 recusas iguais → 1 cancelamento. |
| RN-28 | Versão e eventos: toda mudança de status, de reserva, de destino ou de frete incrementa `versao`, grava uma linha de histórico e publica exatamente um `pedido_atualizado` (exceto a criação, que publica `pedido_criado`). | Fechamento com 3 pedidos → 3 `pedido_atualizado`. |
| RN-29 | Requisição rejeitada (qualquer 4xx ou 5xx) não altera estado, não memoriza idempotência e não publica evento. | PUT com 422 → versão igual. |

## 10. Catálogo de erros

Formato único de erro (`Content-Type: application/problem+json`), igual ao da ESP-NEG-001 e da ESP-MRC-001:

```json
{
  "type": "about:blank",
  "title": "Conflito",
  "status": 409,
  "codigo": "TRANSICAO_INVALIDA",
  "detail": "O estado atual do pedido não permite essa transição.",
  "instance": "/api/v1/pedidos/32000000-0000-4000-8000-000000000001/destino",
  "correlationId": "teste-ped-01",
  "timestamp": "2026-11-20T12:00:00.000Z"
}
```

`type` é sempre `"about:blank"`; `instance` é o caminho sem query string; `correlationId` é o valor efetivo do §7.1; `timestamp` é `clock.now()`. `title`, `status`, `codigo` e `detail` são os da tabela, byte a byte.

| Código | HTTP | `title` | `detail` | Quando ocorre |
|---|---|---|---|---|
| TOKEN_INVALIDO | 401 | Não autenticado | Token de acesso ausente ou inválido. | §7.1, passo 1. |
| CONTEXTO_NAO_SELECIONADO | 403 | Acesso negado | Selecione um contexto de empresa válido. | §7.1, passo 2. |
| ACESSO_NEGADO | 403 | Acesso negado | Você não tem permissão para esta operação. | §7.1, passo 3; RN-17. |
| PEDIDO_NAO_ENCONTRADO | 404 | Não encontrado | Pedido não encontrado. | RN-14, RN-17. |
| TRANSICAO_INVALIDA | 409 | Conflito | O estado atual do pedido não permite essa transição. | RN-14. |
| IDEMPOTENCIA_CONFLITO | 409 | Conflito | A chave de idempotência já foi usada com outro corpo. | RN-24. |
| VALIDACAO_INVALIDA | 422 | Entrada inválida | Os dados da requisição são inválidos. | ID na URL, query, `Idempotency-Key`, corpo (§7.1, §7.2) e data no passado (RN-15). |
| ERRO_INTERNO | 500 | Erro interno | Não foi possível processar a requisição. | Exceção não prevista. |
| DEPENDENCIA_INDISPONIVEL | 503 | Serviço indisponível | Uma dependência necessária está indisponível. | Chaves públicas indisponíveis ou `RepositorioIndisponivel`. |

Respostas fora do catálogo: rota inexistente (404) e método não permitido (405) mantêm a resposta padrão do framework (`{"detail": ...}`); `/docs` e `/openapi.json` ficam habilitados. A API-02 responde `{"status":"not_ready"}` com 503, fora do formato de erro.

**Ordem de verificação** (quando mais de um erro se aplica, retorna-se o primeiro):

1. Correlação (§7.1; nunca é erro).
2. `TOKEN_INVALIDO`, ou `DEPENDENCIA_INDISPONIVEL` se as chaves públicas falharem.
3. `CONTEXTO_NAO_SELECIONADO`.
4. `ACESSO_NEGADO` pelo escopo da rota.
5. `VALIDACAO_INVALIDA` de formato (ID na URL, query, `Idempotency-Key`, corpo).
6. `PEDIDO_NAO_ENCONTRADO` (visibilidade, RN-17; na API-07, também RN-14).
7. Idempotência (API-07): replay ou `IDEMPOTENCIA_CONFLITO`.
8. Regras de domínio da API-07: repetição (RN-14), `TRANSICAO_INVALIDA`, data no passado (`VALIDACAO_INVALIDA`, RN-15).

`DEPENDENCIA_INDISPONIVEL` (banco) e `ERRO_INTERNO` podem ocorrer em qualquer passo que use o repositório. A validação automática de corpo e de query do FastAPI não pode ser usada, porque responderia antes dos passos 2 a 4.

## 11. Requisitos não funcionais

| ID | Característica (ISO/IEC 25010) | Requisito | Métrica e limiar | Verificação |
|---|---|---|---|---|
| RNF-01 | Adequação funcional — exatidão | Aritmética com `Decimal`. | 0.10 TON × 0.20 → `valorTotal` 0.02; 1.25 TON × 8.00 → 10.00 e `pesoKg` 1250; alocação com `valorTotal` 553.86 gravada como 553.86 em 100% dos casos. | Teste automatizado de domínio (RN-03, RN-04, RN-09) |
| RNF-02 | Interoperabilidade | Formato único de erro. | 100% das respostas 4xx e 5xx de negócio em `application/problem+json` com os 8 campos do §10. | CAs de erro do §8 |
| RNF-03 | Confiabilidade — ordenação e contrato | Eventos do mesmo pedido na mesma partição e envelope do kit. | 100% das mensagens publicadas (exceto DLT) com `key == aggregateId == pedidoId`, `eventType` = tópico, `eventVersion` `"1.0"`, `source` `"pedidos-service"`, `timestamp` = relógio e `eventId` UUID distinto, com os campos do §7.4 para o status. O QA também valida as mensagens contra `docs/pedidos/contratos/<topico>.schema.json` (fora da entrada da LLM). | CA-04, CA-11, CA-15, CA-20 e `validacao/gerar_contratos.py` |
| RNF-04 | Manutenibilidade — modularidade | Dependências permitidas e estado só nas portas. | Nenhum import fora do §5 nos módulos de domínio; duas aplicações de `create_app` com repositórios diferentes não compartilham pedidos. | Inspeção e teste automatizado |
| RNF-05 | Segurança — isolamento de dados | Role própria e schema próprio. | O `demo/init.sql` cria o schema `vizinho` com `vizinho.registro(id int)` sem permissão para `svc_pedidos`. Com `svc_pedidos`: `SELECT` e `INSERT` em `pedidos.pedido` com sucesso, os dois em `vizinho.registro` com SQLSTATE `42501`, e zero FKs para fora de `pedidos`. | Script SQL na stack de demonstração |
| RNF-06 | Eficiência de desempenho | Latência pelo gateway com duas réplicas. | Na stack de demonstração (sem Fornecimento real), massa publicada por eventos (projeções, solicitação, reserva e fechamento): leitura com 1.000 pedidos de 10 empresas compradoras, 200 usuários virtuais (20 por empresa, `sleep(1)` entre requisições, ciclo lista → detalhe → histórico), 30 s de aquecimento e 2 min de coleta, p95 ≤ 500 ms; escrita com 3.000 pedidos em `VENDA_CONCLUIDA`, 20 usuários virtuais (2 por empresa, 150 pedidos cada, 1 API-07 e `sleep(1)`), mesma janela, p95 ≤ 1000 ms; 5xx < 1% em cada cenário. Tokens do emissor de teste com `active_scope` `BUYER`. | k6 (`grafana/k6:0.54.0`), evidência no G2 |
| RNF-07 | Confiabilidade — unicidade com duas réplicas | Sem pedido duplicado sob concorrência. | Com `app1` e `app2` ativas, 6 entregas da mesma solicitação (5 com o mesmo `eventId`, 1 com outro) e `reconciliar` nas duas réplicas → 1 pedido por `(negociacaoId, fornecimentoId)` e 1 `pedido_criado`. Solicitação e fechamento concorrentes da mesma negociação → nenhum pedido `CRIADO` depois do fechamento. | Teste de integração na stack de demonstração |
| RNF-08 | Confiabilidade — publicação durável | Outbox com mesmo `eventId` e sem efeito parcial. | (a) Erro injetado na gravação da outbox depois da alteração do pedido → rollback completo e offset não confirmado. (b) Com o broker parado, API-07 grava o `pedido_atualizado` na outbox e responde 200; parada a `app1` e religado o broker, a `app2` publica com o `eventId` gravado em até 60 s após o fim do comando que religa o broker; 0 eventos perdidos. | `validacao/validar-operacao.ps1` |
| RNF-09 | Confiabilidade — prontidão | Degradação observável. | Banco ou broker parado → em até 10 s, `/health` 200 e `/health/ready` 503; restabelecido → `/health/ready` 200 em até 60 s. | `validacao/validar-operacao.ps1` |
| RNF-10 | Segurança — controle de acesso | Sigilo entre empresas. | Contra a API-05 e a API-07 de P1: token expirado e assinatura inválida → 401 `TOKEN_INVALIDO`; sem `tenant_id` → 403 `CONTEXTO_NAO_SELECIONADO`; `BUYER` de OUTRA → 404; comprador de P1 → 200. 100% dos erros sem campos do pedido e versão de P1 inalterada. | CA-21, CA-31, CA-32, CA-33 |
| RNF-11 | Rastreabilidade | Correlação ponta a ponta. | Executando CA-04, CA-11, CA-15 e CA-20 com um `correlationId` por entrada: 100% das linhas de histórico e auditoria com `eventId` ou usuário; cada `correlationId` presente no log, no repositório e no evento de saída correspondente; nenhum log ou evento com o JWT. | Teste automatizado e inspeção de logs |

## 12. Matriz de rastreabilidade

| Requisito | Regras | Interfaces | Critérios de aceitação |
|---|---|---|---|
| RF-01 | RN-01, RN-06 | `produto_cadastrado`, `produto_atualizado`, `fornecimento_criado` | CA-01, CA-02, CA-03 |
| RF-02 | RN-02 a RN-06, RN-12, RN-13, RN-28 | `pedido_temporario_solicitado`, `pedido_criado`, `reconciliar` | CA-04, CA-05, CA-06, CA-07, CA-08, CA-09, CA-10 |
| RF-03 | RN-07, RN-11, RN-23 | `estoque_atualizado`, `reserva_estoque_recusada`, `pedido_atualizado` | CA-11, CA-12, CA-13, CA-14 |
| RF-04 | RN-08, RN-09, RN-10, RN-12, RN-13 | `negociacao_fechada`, `pedido_atualizado` | CA-15, CA-16, CA-17, CA-18, CA-19 |
| RF-05 | RN-14, RN-15, RN-16, RN-17, RN-24 | API-07, `pedido_atualizado` | CA-20, CA-21, CA-22, CA-23 |
| RF-06 | RN-18 a RN-23 | `solicitacao_frete_criada`, `frete_selecionado`, `entrega_concluida`, `frete_indisponivel`, `pedido_atualizado` | CA-24, CA-25, CA-26, CA-27, CA-28 |
| RF-07 | RN-16, RN-17 | API-04, API-05, API-06 | CA-29, CA-30, CA-31 |
| RF-08 | RN-17, RN-29 | §7.1 | CA-32, CA-33 |
| RF-09 | RN-25, RN-26, RN-27 | §7.3, `pedidos.dlt` | CA-34, CA-35, CA-36, CA-37 |
| RF-10 | RN-29 | API-01, API-02, API-03 | CA-38, CA-39 |
| RNF-01 a RNF-11 | §11 | §11 | §11, coluna "Verificação" |

Campos da entidade × projeções: o detalhe (API-05) expõe o Pedido sem `precoUnitarioOferta`, `solicitacaoFreteVigenteId` e `correlationId`; a lista (API-04) e o histórico (API-06) são projeções reduzidas. `eventId`, `eventType`, `timestamp`, `source`, `correlationId` e `aggregateId` pertencem só ao envelope.

## 13. Premissas, decisões e questões em aberto

Decisões do autor (Daniel Junio), tomadas a partir das respostas do professor, dos repositórios dos outros grupos e das discussões do grupo. A versão 2.0 foi revisada em 5 rodadas independentes do processo AED (perfil de sistemas distribuídos) e aprovada pelo autor em 2026-11-19; esta versão 2.1 só converte o formato para o ERS-LLM v1.2.

**Regras do professor citadas**

| ID | O que o professor disse | Onde entra |
|---|---|---|
| PROF-01 | Ao criar o leilão ou abrir a negociação direta, o estoque é bloqueado por um **pedido temporário, ainda sem comprador**. Sem lance (ou sem acordo), o pedido é cancelado e o estoque volta; com vencedor, o pedido é efetivado (aula "Leilão e cadastro de produtos"; Teams, 12/11, registrado na issue #13 do Fornecimento). | RN-05, RN-09, RN-10 |
| PROF-02 | A venda é concluída quando o leilão fecha, não na entrega. | RN-09 |
| PROF-03 | Leilão conjunto: várias ofertas do mesmo produto num lote, lance no lote inteiro, valor rateado proporcionalmente. | RN-02, RN-08, RN-09 |
| PROF-04 | O estoque é controlado por oferta (fornecimento), não por produto. | §6.1 (`fornecimentoId`) |
| PROF-05 | Se um serviço parceiro não estiver pronto, a apresentação usa eventos de teste no formato combinado. | §8.0, RNF-06 a RNF-09 |

**Decisões**

| ID | Tipo | Descrição | Decisão |
|---|---|---|---|
| D-01 | Decisão | Gatilho do pedido temporário | Consumir `pedido_temporario_solicitado` da ESP-NEG-001, com um pedido por oferta, `alocacoes` no fechamento e `reservaSituacao` no `pedido_atualizado`. Motivo: os lances só começam com a reserva de todos os itens confirmada; o Mercado forma o lote (`leilao_iniciado`) e a Negociação pede um pedido temporário por item e filtra o lote pela reserva (ESP-MRC-001 v0.5, DG-37 e DG-51). Pedidos não consome eventos do Mercado. |
| D-02 | Decisão | Destino da entrega | O comprador informa pela API-07 depois da venda. Motivo: a Negociação não envia destino e o leilão não está ligado a uma demanda. ↻ Destino da demanda indicada no lance (`demandaId`, ESP-MRC-001 DG-27/DG-28) ou endereço da empresa no Identity. |
| D-03 | Decisão | Início da cotação | A Logística cota a partir do `pedido_atualizado` `AGUARDANDO_FRETE` e ignora `pedido_criado` com `compradorId` nulo. ↻ Cotar no `VENDA_CONCLUIDA` (sem destino). |
| D-04 | Decisão | API de escrita | Sem `POST` de criação e sem `PATCH` de status: quem cria é a Negociação e a venda é concluída no fechamento. A única escrita HTTP é a API-07. |
| D-05 | Decisão | Frete indisponível depois da venda | Não cancela; o pedido aguarda nova solicitação da Logística (RN-22). Motivo: a baixa já ocorreu e o estorno está fora da v1. |
| D-06 | Decisão | Conclusão da venda | No fechamento (PROF-02). Recusa a proposta da Logística de concluir em `entrega_concluida`. |
| D-07 | Decisão | Fechamento incompatível | Vai inteiro para a DLT, sem efeito parcial; a negociação continua aberta em Pedidos até um fechamento corrigido (RN-08). |
| D-08 | Decisão | Contrato de trabalho | Os campos do §7 são o contrato de Pedidos. Uma mudança pedida por um parceiro gera nova versão desta especificação. |
| D-09 | Decisão | Formato do documento | Modelo ERS-LLM v1.2 do QA, em `docs/pedidos/requisitos.md`, com contratos em `docs/pedidos/contratos/`, como a ESP-MRC-001. Suíte de aceitação em memória pelas portas do §5.1; comportamentos de infraestrutura real (outbox, réplicas, banco) viraram RNFs com evidência na stack de demonstração. |
| PR-01 | Premissa | Execução da apresentação | Sem os serviços parceiros, os eventos de entrada são publicados por fixtures no formato do §7.3 (PROF-05). |
| PR-02 | Premissa | Ordem entre tópicos | A Negociação só inclui em `alocacoes` pedidos que ela recebeu pelo `pedido_atualizado` com reserva `CONFIRMADA`; por isso a efetivação exige reserva confirmada (RN-08). |

### 13.1 Casos de borda obrigatórios

| Caso de borda | Decisão |
|---|---|
| String vazia ou só com espaços em campos obrigatórios | Inválida: `422 VALIDACAO_INVALIDA` na API; mensagem inválida (DLT) no consumo. A cidade tem os espaços das pontas removidos antes de validar. |
| Cabeçalho opcional presente porém vazio | `Idempotency-Key` e `X-Correlation-Id` vazios equivalem a ausentes. |
| Mensagem duplicada **e** inválida | A validação vem antes da deduplicação: vai para a DLT de novo (RN-25, CA-37). |
| Campo de chave/ID com tipo inesperado | Na API: `422 VALIDACAO_INVALIDA`. No consumo: mensagem inválida; a chave da DLT é o `aggregateId` se for texto de 1 a 128 caracteres, senão `"desconhecido"` no dublê. |
| Data/hora sem fuso horário | Na API e no consumo: inválida. `clock.now()` sem fuso: tratar como UTC. |
| Falha da porta de publicação / banco | `RepositorioIndisponivel` → 503 na API; no consumo, o handler propaga (RN-26). Outra exceção → 500 na API; no consumo, 3 tentativas e DLT. `publish` dentro da transação é a gravação na outbox no adaptador real e falha junto com a transação. Falha ao publicar na DLT no adaptador real: o offset não é confirmado e a mensagem é reprocessada. |
| Rotas e métodos fora do contrato | Resposta padrão do framework (§10). |
| Número com mais casas que o permitido, como string ou acima do máximo | `422 VALIDACAO_INVALIDA` na API; mensagem inválida no consumo. |
| UUID de pedido em maiúsculas | Aceito e normalizado em minúsculas (§6.1). |
| Fechamento com alocação incompatível | DLT inteira, sem efeito parcial (RN-08). |
| Entrega antes da seleção | Guardada e aplicada depois (RN-21). |
| Solicitação de frete atrasada depois de outra vigente | `EVENTO_CONFLITANTE`, sem efeito (RN-19). |
| Projeção ausente na solicitação | Pendência reavaliada pela projeção e pelo `reconciliar` (RN-06). |

## 14. Checklist de prontidão (Definition of Ready) — preenchido pelo QA

Pré-conferência do autor antes do gate G1; o QA confirma ou desmarca cada item.

- [x] Nenhum `{{ }}` restante; comentários de orientação removidos.
- [x] Todo RF tem enunciado EARS, prioridade e CAs de sucesso e erro quando aplicáveis.
- [x] Todo CA usa valores concretos e resultado observável.
- [x] Todo código de erro citado em CA existe no §10 e vice-versa.
- [x] Ordem de verificação de erros definida para cada endpoint.
- [x] Campos de entidade, projeções de resposta e campos derivados estão mapeados; exemplos JSON respeitam os contratos.
- [x] Todo tópico citado tem produtor/consumidor, chave e exemplo.
- [x] Toda dependência externa está modelada como porta (§5.1).
- [x] Todo RNF tem métrica, limiar e forma de verificação.
- [x] Matriz de rastreabilidade completa (nenhum RF sem CA, nenhum CA órfão).
- [x] Não há palavras vagas sem quantificação.
- [x] Seção 13 sem questões abertas e tabela 13.1 (casos de borda) toda decidida.
- [x] Comentários `# DECISÃO:` da última geração (se houver) analisados e incorporados como regra (a geração do primeiro fluxo, v1.1, não usou o modelo ERS-LLM).

## 15. Histórico de versões

| Versão | Data | Autor | Mudança |
|---|---|---|---|
| 1.1 | 2026-11-12 | Daniel Junio | Pedido criado depois de `negociacao_fechada`, reserva por `pedido_criado` e conclusão por `PATCH` (formato AED). Primeiro fluxo implementado. |
| 2.0 | 2026-11-19 | Daniel Junio | Pedido temporário na abertura (PROF-01), leilão conjunto, venda no fechamento, destino pelo comprador, sem `POST` e `PATCH`; decisões D-01 a D-08; 5 rodadas de revisão AED; aprovada pelo autor. |
| 2.1 | 2026-11-19 | Daniel Junio | Conversão para o modelo ERS-LLM v1.2 do QA (D-09), como a ESP-MRC-001: portas e ponto de entrada `create_app`, 10 RFs em EARS, 39 CAs em Gherkin, 29 RNs, catálogo de erros, 11 RNFs e matriz. Comportamentos de infraestrutura real viraram RNFs. Correções da revisão independente: JWT conferido pelo `clock`; "hoje" sem base de fusos; histórico de seleções (`selecoes`); ordem da RN-21; valores-padrão e substituição de P1–P3 na massa; Anexo A com os payloads de todos os tópicos; regras de repetição, DLT, idempotência e formatos. Segunda rodada: `compradorId` do topo do fechamento (RN-09); tokens da massa com `sub`, `tenant_id` e `active_scope`; corpos e quantidades explícitos no CA-21 e no CA-35; 500 por middleware; filtro em `listar_pendentes`. Revisão do mesmo dia: referência à ESP-MRC-001 v0.5 e motivo da D-01 alinhados ao fluxo combinado com Mercado e Negociação, sem mudança de regra ou contrato. |

## Anexo A — Exemplos de payload por tópico

Gerados de `docs/pedidos/contratos/*.exemplo.json` e válidos contra os schemas da mesma pasta. Todos usam o envelope do §7.3 ou do §7.4. Nos testes, os `pedidoId` `32000000-0000-4000-8000-000000000001`, `...0002` e `...0003` representam P1, P2 e P3 e são substituídos pelos valores gerados (§8.0). Os números podem aparecer sem os zeros à direita; o formato de saída é o do §4.

`entrega_concluida` (chave: `32000000-0000-4000-8000-000000000001`)

```json
{"pedidoId":"32000000-0000-4000-8000-000000000001","entregaId":"ent-go-31","transportadoraId":"tra-go-8","entregueEm":"2026-11-23T16:00:00.000Z","prazoPrometido":"2026-11-23T21:00:00.000Z","noPrazo":true,"atrasoHoras":0}
```

`estoque_atualizado` (chave: `34000000-0000-4000-8000-000000000001`)

```json
{"fornecimentoId":"34000000-0000-4000-8000-000000000001","produtoId":"35000000-0000-4000-8000-000000000001","fornecedorId":"a1000000-0000-4000-8000-000000000001","operacao":"RESERVA","quantidadeMovimentada":5.0,"quantidadeDisponivel":95.0,"quantidadeReservada":5.0,"pedidoId":"32000000-0000-4000-8000-000000000001","atualizadoEm":"2026-11-20T12:00:04.000Z"}
```

`estoque_atualizado.reposicao` (chave: `34000000-0000-4000-8000-000000000001`)

```json
{"fornecimentoId":"34000000-0000-4000-8000-000000000001","produtoId":"35000000-0000-4000-8000-000000000001","fornecedorId":"a1000000-0000-4000-8000-000000000001","operacao":"REPOSICAO","quantidadeMovimentada":10.0,"quantidadeDisponivel":105.0,"quantidadeReservada":5.0,"pedidoId":null,"atualizadoEm":"2026-11-20T13:00:00.000Z"}
```

`fornecimento_criado` (chave: `34000000-0000-4000-8000-000000000001`)

```json
{"fornecimentoId":"34000000-0000-4000-8000-000000000001","fornecedorId":"a1000000-0000-4000-8000-000000000001","produtoId":"35000000-0000-4000-8000-000000000001","unidade":"TON","origem":{"cidade":"Rio Verde","uf":"GO"}}
```

`frete_indisponivel` (chave: `32000000-0000-4000-8000-000000000001`)

```json
{"pedidoId":"32000000-0000-4000-8000-000000000001","solicitacaoFreteId":"sol-go-21","motivo":"SEM_COTACAO","detalhe":"Prazo de cotação expirado.","ocorridoEm":"2026-11-21T02:00:00.000Z"}
```

`frete_selecionado` (chave: `32000000-0000-4000-8000-000000000001`)

```json
{"pedidoId":"32000000-0000-4000-8000-000000000001","solicitacaoFreteId":"sol-go-21","entregaId":"ent-go-31","cotacaoId":"cot-go-18","transportadoraId":"tra-go-8","tentativa":1,"preco":150.0,"prazoDias":2,"dataEntregaPrevista":"2026-11-23","criterio":"ESCOLHA_COMPRADOR","status":"FRETE_SELECIONADO"}
```

`negociacao_fechada.deserto` (chave: `31000000-0000-4000-8000-000000000001`)

```json
{"negociacaoId":"31000000-0000-4000-8000-000000000001","tipo":"LEILAO","status":"DESERTO","compradorId":null,"produtoId":"35000000-0000-4000-8000-000000000001","alocacoes":[]}
```

`negociacao_fechada` (chave: `31000000-0000-4000-8000-000000000001`)

```json
{"negociacaoId":"31000000-0000-4000-8000-000000000001","tipo":"LEILAO","status":"ENCERRADO","compradorId":"b2000000-0000-4000-8000-000000000002","produtoId":"35000000-0000-4000-8000-000000000001","alocacoes":[{"fornecimentoId":"34000000-0000-4000-8000-000000000001","fornecedorId":"a1000000-0000-4000-8000-000000000001","pedidoId":"32000000-0000-4000-8000-000000000001","quantidade":5.0,"valorUnitario":130.0,"valorTotal":650.0},{"fornecimentoId":"34000000-0000-4000-8000-000000000002","fornecedorId":"a1000000-0000-4000-8000-000000000002","pedidoId":"32000000-0000-4000-8000-000000000002","quantidade":15.0,"valorUnitario":130.0,"valorTotal":1950.0}]}
```

`pedido_atualizado.aguardando_frete` (chave: `32000000-0000-4000-8000-000000000001`)

```json
{"pedidoId":"32000000-0000-4000-8000-000000000001","negociacaoId":"31000000-0000-4000-8000-000000000001","fornecimentoId":"34000000-0000-4000-8000-000000000001","status":"AGUARDANDO_FRETE","statusAnterior":"VENDA_CONCLUIDA","reservaSituacao":"CONFIRMADA","versao":4,"compradorId":"b2000000-0000-4000-8000-000000000002","fornecedorId":"a1000000-0000-4000-8000-000000000001","produtoId":"35000000-0000-4000-8000-000000000001","produtoNome":"Milho em grão","quantidade":5.0,"unidade":"TON","pesoKg":5000,"tipoTransporte":"GRANEL","valorTotal":650.0,"origem":{"cidade":"Rio Verde","uf":"GO"},"destino":{"cidade":"Anápolis","uf":"GO"},"dataEntregaDesejada":"2026-11-27"}
```

`pedido_atualizado.cancelado` (chave: `32000000-0000-4000-8000-000000000003`)

```json
{"pedidoId":"32000000-0000-4000-8000-000000000003","negociacaoId":"31000000-0000-4000-8000-000000000001","fornecimentoId":"34000000-0000-4000-8000-000000000003","status":"CANCELADO","statusAnterior":"CRIADO","reservaSituacao":"ENCERRADA","versao":3,"motivo":"FORA_DO_LOTE"}
```

`pedido_atualizado.entregue` (chave: `32000000-0000-4000-8000-000000000001`)

```json
{"pedidoId":"32000000-0000-4000-8000-000000000001","negociacaoId":"31000000-0000-4000-8000-000000000001","fornecimentoId":"34000000-0000-4000-8000-000000000001","status":"ENTREGUE","statusAnterior":"FRETE_SELECIONADO","reservaSituacao":"CONFIRMADA","versao":6,"entregueEm":"2026-11-23T16:00:00.000Z"}
```

`pedido_atualizado` (chave: `32000000-0000-4000-8000-000000000001`)

```json
{"pedidoId":"32000000-0000-4000-8000-000000000001","negociacaoId":"31000000-0000-4000-8000-000000000001","fornecimentoId":"34000000-0000-4000-8000-000000000001","status":"CRIADO","statusAnterior":"CRIADO","reservaSituacao":"CONFIRMADA","versao":2}
```

`pedido_atualizado.frete_selecionado` (chave: `32000000-0000-4000-8000-000000000001`)

```json
{"pedidoId":"32000000-0000-4000-8000-000000000001","negociacaoId":"31000000-0000-4000-8000-000000000001","fornecimentoId":"34000000-0000-4000-8000-000000000001","status":"FRETE_SELECIONADO","statusAnterior":"AGUARDANDO_FRETE","reservaSituacao":"CONFIRMADA","versao":5,"frete":{"solicitacaoFreteId":"sol-go-21","entregaId":"ent-go-31","cotacaoId":"cot-go-18","transportadoraId":"tra-go-8","tentativa":1,"preco":150.0,"prazoDias":2,"dataEntregaPrevista":"2026-11-23"}}
```

`pedido_atualizado.venda_concluida` (chave: `32000000-0000-4000-8000-000000000001`)

```json
{"pedidoId":"32000000-0000-4000-8000-000000000001","negociacaoId":"31000000-0000-4000-8000-000000000001","fornecimentoId":"34000000-0000-4000-8000-000000000001","status":"VENDA_CONCLUIDA","statusAnterior":"CRIADO","reservaSituacao":"CONFIRMADA","versao":3,"compradorId":"b2000000-0000-4000-8000-000000000002","valorUnitario":130.0,"valorTotal":650.0}
```

`pedido_criado` (chave: `32000000-0000-4000-8000-000000000001`)

```json
{"pedidoId":"32000000-0000-4000-8000-000000000001","negociacaoId":"31000000-0000-4000-8000-000000000001","compradorId":null,"fornecedorId":"a1000000-0000-4000-8000-000000000001","produtoId":"35000000-0000-4000-8000-000000000001","fornecimentoId":"34000000-0000-4000-8000-000000000001","produtoNome":"Milho em grão","quantidade":5.0,"unidade":"TON","pesoKg":5000,"tipoTransporte":"GRANEL","valorTotal":600.0,"origem":{"cidade":"Rio Verde","uf":"GO"},"destino":null,"status":"CRIADO"}
```

`pedido_temporario_solicitado` (chave: `31000000-0000-4000-8000-000000000001`)

```json
{"negociacaoId":"31000000-0000-4000-8000-000000000001","tipo":"LEILAO","fornecimentoId":"34000000-0000-4000-8000-000000000001","fornecedorId":"a1000000-0000-4000-8000-000000000001","produtoId":"35000000-0000-4000-8000-000000000001","quantidade":5.0,"precoUnitario":120.0}
```

`pedidos.dlt` (chave: `32000000-0000-4000-8000-000000000001`)

```json
{"originalEvent":{"eventId":"37000000-0000-4000-8000-000000000201","eventType":"frete_selecionado","eventVersion":"1.0","timestamp":"2026-11-20T14:00:00.000Z","source":"logistica-service","correlationId":"teste-ped-01","aggregateId":"32000000-0000-4000-8000-000000000001"},"error":"EVENTO_INVALIDO","stackTrace":"ValidationError: payload ausente","failedAtAttempt":1}
```

`produto_atualizado` (chave: `35000000-0000-4000-8000-000000000001`)

```json
{"produtoId":"35000000-0000-4000-8000-000000000001","nome":"Milho em grão tipo 1","unidade":"TON","tipoTransporte":"GRANEL","pesoUnitarioKg":null}
```

`produto_cadastrado` (chave: `35000000-0000-4000-8000-000000000001`)

```json
{"produtoId":"35000000-0000-4000-8000-000000000001","nome":"Milho em grão","unidade":"TON","tipoTransporte":"GRANEL","pesoUnitarioKg":null}
```

`reserva_estoque_recusada` (chave: `32000000-0000-4000-8000-000000000001`)

```json
{"pedidoId":"32000000-0000-4000-8000-000000000001","fornecimentoId":"34000000-0000-4000-8000-000000000001","quantidadeSolicitada":5.0,"quantidadeDisponivel":2.0,"motivo":"ESTOQUE_INSUFICIENTE","recusadoEm":"2026-11-20T12:00:04.000Z"}
```

`solicitacao_frete_criada` (chave: `sol-go-21`)

```json
{"solicitacaoFreteId":"sol-go-21","pedidoId":"32000000-0000-4000-8000-000000000001","status":"ABERTA"}
```
