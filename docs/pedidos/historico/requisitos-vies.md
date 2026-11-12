# Requisitos — Pedidos

Perfil de especificação: sistemas-distribuidos 1.0

| Versão | Data | Situação | Mudança |
|---|---|---|---|
| 1.0 | 2026-11-05 | Rascunho | RFs aprovados e contratos em detalhamento |

## 1. Objetivo e atores

Pedidos deve registrar uma única compra para cada negociação fechada com vencedor e acompanhar a reserva de estoque, o frete e a conclusão da venda.

| Ator | Necessidade |
|---|---|
| Empresa compradora | Consultar a compra e acompanhar sua situação e seu histórico. |
| Empresa fornecedora | Consultar seus pedidos e vincular as operações de estoque ao fornecimento correto. |
| Operador autorizado pelo Portal | Consultar pedidos e acionar transições autorizadas. |
| Negociação | Entregar a identidade e o resultado da negociação fechada. |
| Fornecimento | Receber eventos do pedido, controlar estoque e comunicar a recusa da reserva. |
| Logística | Iniciar a cotação a partir do pedido e comunicar a seleção do frete. |

## 3. Glossário

| Termo | Definição |
|---|---|
| `negociacaoId` | UUID da negociação de origem, chave da unicidade de negócio do pedido. |
| `fornecimentoId` | UUID da oferta no serviço Fornecimento, obrigatório nos eventos publicados por Pedidos. |
| Reserva | Separação do saldo disponível por Fornecimento para um pedido. |
| Compensação | Cancelamento do pedido em resposta à recusa da reserva e liberação aplicável de saldo por Fornecimento após evento de cancelamento. |

## 4. Tecnologia e execução

| Item | Valor |
|---|---|
| Linguagem e framework | Python 3.12 e FastAPI. [A ESCLARECER: fixar versões compatíveis e lista de bibliotecas.] |
| Persistência | PostgreSQL 16, banco `portal_b2b`, schema `pedidos`, role `svc_pedidos`. Sem FKs para outros domínios e sem acesso aos seus schemas. |
| Mensageria | Redpanda real, compatível com Kafka, em Docker Compose. |
| Acesso | Porta `8107`, gateway Nginx na porta `8080`, prefixo `/api/v1/pedidos`. |
| Operação | `GET /health`, `GET /health/ready`, `GET /metrics` e `GET /docs`. [A ESCLARECER: confirmar respostas, autenticação e dependências de prontidão.] |
| Inicialização e testes | [A ESCLARECER: fixar revisão e comando do Compose, diretório, credenciais, tópicos, variáveis e forma de inicializar e limpar massa isolada.] |

~~~text
services/pedidos-service/
tests/test_pedidos.py
~~~

Na homologação, PostgreSQL e Redpanda devem ser reais. Cada cenário deve usar identidades próprias para pedido e negociação, consumidores de observação isolados e verificações persistentes. [A ESCLARECER: confirmar injeção de eventos externos no ambiente, evidência de cada entrega, prazo e método de simular falhas de reserva, frete e publicação.]

## 5. Modelo de dados

| Registro | Atributos e restrições de domínio | Fonte oficial |
|---|---|---|
| `pedidos.pedido` | `id` UUID como PK. `negociacao_id` UUID obrigatório e `UNIQUE`. `fornecimento_id`, `comprador_id`, `fornecedor_id` e `produto_id` UUIDs obrigatórios. `quantidade` inteira > 0. O DR-BD-002 referencia `valor_unitario` e `valor_total` com `DECIMAL(18,4)`, `valor_comissao` opcional e `endereco_destino_id` UUID opcional. Estado inicial `CRIADO`. [A ESCLARECER: fixar origem e validação dos valores, frete e endereços, limites e arredondamento.] | Pedidos, com as referências identificadas pertencentes aos domínios de origem |
| `pedidos.pedido_historico` | Histórico ordenável das mudanças do pedido. [A ESCLARECER: definir colunas, estados anterior e novo, instante, motivo, referência à operação e ordenação.] | Pedidos |
| `pedidos.audit_log` | Trilha de mudanças e atores. [A ESCLARECER: definir campos, ações auditáveis e retenção.] | Pedidos |
| `pedidos.idempotency_keys` | Persistência de chaves HTTP nas operações em que a chave for exigida. [A ESCLARECER: definir operações, escopo, validade e conflito de corpo.] | Pedidos |
| `pedidos.processed_events` | Registro durável de eventos aplicados. [A ESCLARECER: definir chave de deduplicação, retenção e evidência por entrega.] | Pedidos |
| `pedidos.outbox` | Evento a publicar, persistido com a mutação correspondente. [A ESCLARECER: definir colunas, estados e retentativas.] | Pedidos |

O serviço deve poder verificar localmente o resultado recebido de Negociação antes de aceitar a criação por HTTP. Também deve preservar a seleção de frete recebida antes da confirmação de estoque para aplicá-la posteriormente. [A ESCLARECER: definir a persistência, os identificadores, a retenção e o tratamento de resultados ou seleções conflitantes.]

Transições descritas no planejamento, sujeitas às precondições indicadas.

| Origem | Gatilho | Destino | Condição |
|---|---|---|---|
| Sem pedido | `negociacao_fechada` ou `POST /api/v1/pedidos` | `CRIADO` | Primeiro pedido para o `negociacaoId` de uma negociação encerrada com vencedor. O `POST` exige que o resultado Kafka já esteja registrado localmente. |
| `CRIADO` | `frete_selecionado` antes da confirmação da reserva | `CRIADO` | O frete do pedido fica registrado como seleção pendente. |
| `CRIADO` | Confirmação da reserva | `AGUARDANDO_FRETE` | O serviço aplica a seleção pendente, se houver, pela próxima transição. [A ESCLARECER: fixar o contrato da confirmação e a observabilidade do estado intermediário.] |
| `AGUARDANDO_FRETE` | `frete_selecionado` ou aplicação de seleção pendente | `FRETE_SELECIONADO` | O frete pertence ao pedido e a reserva está confirmada. |
| `FRETE_SELECIONADO` | Transição autorizada de conclusão | `VENDA_CONCLUIDA` | O status conclui a venda na v1. [A ESCLARECER: fixar ator, contrato HTTP e outras precondições.] |
| `VENDA_CONCLUIDA` | Confirmação de entrega | `ENTREGUE` | [A ESCLARECER: definir origem, autorização e efeito observável.] |
| `CRIADO` com ou sem seleção pendente | `reserva_estoque_recusada` | `CANCELADO` | A seleção pendente perde efeito no fluxo. [A ESCLARECER: enumerar outros estados canceláveis, resolver sinais de estoque conflitantes e definir se existe cancelamento manual.] |

## 6. Regras de negócio e contratos

| ID | Regra | RFs |
|---|---|---|
| RN-PED-01 | A criação por `negociacao_fechada` e por `POST /api/v1/pedidos` deve compartilhar a restrição persistida `UNIQUE` de `negociacao_id`, inclusive em concorrência. Uma criação válida deve resultar em exatamente um pedido e um registro lógico de `pedido_criado` por `negociacaoId`. Negociação `DESERTO` não deve gerar pedido. O `POST` deve rejeitar a tentativa se Pedidos ainda não tiver registrado localmente um resultado Kafka com vencedor para a negociação. Reentregas e novas tentativas não devem criar outro pedido. [A ESCLARECER: definir o contrato HTTP da rejeição e da repetição e o tratamento de resultados conflitantes.] | PED-01 |
| RN-PED-02 | Consultas devem aplicar a visibilidade do solicitante e paginação da listagem. `size > 100` deve resultar em `422`. [A ESCLARECER: definir papéis, propriedade do pedido, ordenação e filtros. Confirmar valores padrão da paginação quando os parâmetros forem omitidos e limites inferiores, incluindo `page=0` e `size=0`.] | PED-02 |
| RN-PED-03 | Recusa de estoque deve acionar cancelamento do pedido correspondente e a publicação de `pedido_atualizado` com estado `CANCELADO`. Fornecimento libera reserva existente de acordo com seu contrato. [A ESCLARECER: fixar estados aplicáveis, correlação e efeito de eventos duplicados, conflitantes ou tardios.] | PED-03, PED-05 |
| RN-PED-04 | Se `frete_selecionado` chegar antes da confirmação da reserva, Pedidos deve guardar a seleção e permanecer em `CRIADO`. Deve aplicar a seleção pendente quando a reserva for confirmada e avançar para `FRETE_SELECIONADO`. Se a reserva for recusada, deve cancelar o pedido e impedir que a seleção de frete o reative. A conclusão válida leva a `VENDA_CONCLUIDA` e sinaliza a baixa definitiva ao Fornecimento. [A ESCLARECER: definir o evento de confirmação de reserva, divergências entre sinais de estoque, timeouts e autoridade da conclusão.] | PED-03, PED-04, PED-05 |
| RN-PED-05 | A mudança de estado, o histórico, a auditoria e a inclusão do evento na outbox devem ser atômicos. `pedido_criado` e `pedido_atualizado` devem incluir `fornecimentoId`. [A ESCLARECER: definir os pontos de gravação de cada trilha, identificadores, confirmação de consumo e recuperação.] | PED-01, PED-03, PED-04, PED-05 |

O planejamento atribui ao `CONTRACTS.md` o envelope JSON com `eventId`, `eventType`, `eventVersion`, `timestamp`, `source`, `correlationId`, `aggregateId` e `payload`. [A ESCLARECER: incorporar a revisão do kit, tipos, versão, chave de partição, nomes físicos de tópicos, exemplos completos e regra de correlação.]

| Evento | Produtor → consumidor | Conteúdo e efeito a especificar |
|---|---|---|
| `negociacao_fechada` | Negociação → Pedidos | Identidade `negociacaoId`, resultado `ENCERRADO` ou `DESERTO` e, quando houver vencedor, dados do resultado e `fornecimentoId`. |
| `reserva_estoque_recusada` | Fornecimento → Pedidos | Identidades para correlacionar recusa, pedido e fornecimento, além do motivo. O planejamento prevê alinhamento do tópico com Infra. |
| `frete_selecionado` | Logística → Pedidos | Referências para correlacionar pedido, solicitação de frete e cotação selecionada. |
| `pedido_criado` | Pedidos → Fornecimento e Logística | Identidade do pedido e `fornecimentoId`, quantidade para reserva e dados necessários para iniciar o frete. Estado `CRIADO`. |
| `pedido_atualizado` | Pedidos → Fornecimento. [A ESCLARECER: confirmar se Logística ou outros serviços devem consumir este evento na v1.] | Identidade do pedido, `fornecimentoId` e novo estado. `CANCELADO` é o sinal de liberação e `VENDA_CONCLUIDA` o de baixa definitiva. |

[A ESCLARECER: confirmar com Negociação, Fornecimento e Logística os contratos completos, origem de todos os dados do pedido e se a confirmação de reserva chega por `estoque_atualizado` ou outro evento. Definir retentativas, falha parcial, destino de mensagens inválidas, retenção e recuperação após queda.]

## 7. Erros globais

A API deve validar JWT Bearer, aplicar RBAC, propagar `X-Correlation-Id` e usar `application/problem+json` conforme o padrão RFC 7807 adotado pelo kit. O planejamento cita `401`, `403` e `422` para falta de autenticação, falta de autorização e tamanho de página superior a 100, respectivamente. [A ESCLARECER: fixar claims, matriz de autorização, corpo literal dos erros, precedência, resposta à transição inválida e efeitos de rejeição.]

## 8. Requisitos funcionais

### PED-01 — Criar um pedido por negociação com vencedor

- **Interface e regras.** Consumo de `negociacao_fechada` e `POST /api/v1/pedidos`. RN-PED-01 e RN-PED-05. [A ESCLARECER: fixar autorização, corpo e respostas da rota.]
- **CA-PED-01.1.** Dado que a negociação `31000000-0000-4000-8000-000000000001` não tem pedido, quando for consumido seu resultado válido `ENCERRADO` com vencedor, então deve existir exatamente um pedido com esse `negociacaoId`, estado `CRIADO` e um registro de outbox de `pedido_criado` com o `fornecimentoId` da mensagem. [A ESCLARECER: completar payload, sinal e prazo.]
- **CA-PED-01.2.** Dada a mesma negociação inicialmente sem pedido, quando o mesmo evento válido for entregue cinco vezes, então a contagem persistida por `negociacaoId` após processamento das cinco entregas deve ser exatamente 1, com uma única criação lógica na outbox. [A ESCLARECER: definir evidência por entrega e prazo após a quinta.]
- **CA-PED-01.3.** Dado que a negociação `31000000-0000-4000-8000-000000000002` não tem pedido, quando chegar `negociacao_fechada` com estado `DESERTO`, então a contagem de pedidos e registros de criação para esse UUID deve continuar 0 após a confirmação de processamento. [A ESCLARECER: fixar representação do vencedor ausente, confirmação e prazo.]
- **CA-PED-01.4.** Dado que a negociação `31000000-0000-4000-8000-000000000001` já gerou pedido pelo consumo de `negociacao_fechada`, quando um ator autorizado enviar `POST /api/v1/pedidos` para a mesma negociação, então deve existir exatamente o pedido original para esse UUID e um único registro de criação na outbox. [A ESCLARECER: fixar requisição, status e corpo para pedido existente.]
- **CA-PED-01.5.** Dado que a negociação `31000000-0000-4000-8000-000000000003` não tem resultado com vencedor registrado localmente, quando um ator autorizado chamar `POST /api/v1/pedidos` para ela, então a requisição deve ser rejeitada e a contagem de pedidos e registros de criação para o UUID deve continuar 0. [A ESCLARECER: fixar status, corpo HTTP e fixture da chamada.]

### PED-02 — Consultar pedidos e histórico

- **Interface e regras.** `GET /api/v1/pedidos`, `GET /api/v1/pedidos/{id}` e `GET /api/v1/pedidos/{id}/historico`. RN-PED-02. [A ESCLARECER: fixar parâmetros, paginação do histórico, visibilidade e JSON.]
- **CA-PED-02.1.** Dado o pedido visível `32000000-0000-4000-8000-000000000001` com uma transição registrada, quando o ator autorizado listar, abrir o detalhe e consultar o histórico, então os resultados devem referir-se ao mesmo UUID e o histórico deve incluir a transição persistida. [A ESCLARECER: fixar estado inicial, transição, status e corpos literais.]
- **CA-PED-02.2.** Dado o mesmo ator, quando listar com `?page=1&size=101`, então a API deve retornar `422` com `application/problem+json`. [A ESCLARECER: fixar JSON literal.]

### PED-03 — Cancelar por recusa de estoque

- **Interface e regras.** Consumo de `reserva_estoque_recusada`. RN-PED-03 e RN-PED-05. [A ESCLARECER: fixar tipo do motivo, origem e nomes físicos dos tópicos.]
- **CA-PED-03.1.** Dado um pedido em `CRIADO` com frete pendente e sem reserva confirmada, quando Fornecimento recusar a reserva vinculada a esse pedido, então ele deve ficar `CANCELADO`, a seleção não deve avançar o pedido para `FRETE_SELECIONADO` e deve haver histórico e `pedido_atualizado` com o mesmo `fornecimentoId`. [A ESCLARECER: fixar fixture, JSON e prazo.]
- **CA-PED-03.2.** Dado um pedido já cancelado pela mesma mensagem, quando o mesmo `eventId` for reentregue, então o estado deve continuar `CANCELADO` com uma única transição por recusa e um único registro lógico de atualização para ela. [A ESCLARECER: fixar confirmação por entrega e prazo.]

### PED-04 — Registrar frete e aplicar transições autorizadas

- **Interface e regras.** Consumo de `frete_selecionado` e `PATCH /api/v1/pedidos/{id}/status`. RN-PED-04 e RN-PED-05. [A ESCLARECER: fechar tabela de transições, origem de entrega, corpo e respostas HTTP.]
- **CA-PED-04.1.** Dado um pedido em `CRIADO` sem reserva confirmada e a cotação `33000000-0000-4000-8000-000000000001`, quando Logística selecionar essa cotação e depois Fornecimento confirmar a reserva do mesmo pedido, então após o primeiro evento o pedido deve permanecer `CRIADO` com essa cotação pendente e, após o segundo, deve estar `FRETE_SELECIONADO` com a mesma cotação aplicada. [A ESCLARECER: fixar JSON, sinal de confirmação da reserva e evidência e prazo de cada etapa.]
- **CA-PED-04.2.** Dado um pedido elegível em `FRETE_SELECIONADO`, quando a conclusão autorizada for solicitada, então o estado deve ficar `VENDA_CONCLUIDA` e deve existir `pedido_atualizado` com esse estado, sem dependência de emissão fiscal. [A ESCLARECER: fixar corpo, ator, precondições, resposta, evento e prazo.]
- **CA-PED-04.3.** Dado um pedido em `FRETE_SELECIONADO` e um ator autenticado sem permissão de conclusão, quando tentar concluir o pedido por HTTP, então deve receber `403` com `application/problem+json`, com estado e outbox preservados. [A ESCLARECER: fixar papel, requisição e corpo literal.]

### PED-05 — Registrar histórico, auditoria e publicar mudanças

- **Interface e regras.** Mutação do pedido, histórico, auditoria e publicação pela outbox. RN-PED-05.
- **CA-PED-05.1.** Dado um pedido em `CRIADO`, quando uma recusa de estoque válida causar a transição para `CANCELADO`, então a consulta de teste deve encontrar o pedido cancelado, uma entrada de histórico, uma de auditoria e um registro de outbox da mudança com o `fornecimentoId` do pedido. [A ESCLARECER: fixar os campos de comparação e a evidência no broker.]
- **CA-PED-05.2.** Dado um pedido em `CRIADO`, quando uma falha de banco impedir a inclusão da atualização na outbox na transação de cancelamento, então o pedido deve permanecer `CRIADO`, sem novo registro de histórico, auditoria ou evento dessa tentativa. [A ESCLARECER: fixar mecanismo de indução da falha, evidência e política de reprocessamento.]

## 9. Requisitos não funcionais

| ID | Requisito | Como verificar e limite |
|---|---|---|
| RNF-PED-01 | Isolamento dos dados | Usar `svc_pedidos` para executar operação no próprio schema e tentar ler ou gravar outro schema. Exigir rejeição de acessos externos e zero FKs entre domínios. |
| RNF-PED-02 | Latência e erros | Metas do planejamento em Compose são p95 de leitura ≤ 500 ms, p95 de escrita ≤ 1000 ms e 5xx < 1%. [A ESCLARECER: fixar operações, carga, dados, duração, instrumento e cálculo.] |
| RNF-PED-03 | Recuperação e unicidade | Reentregar eventos concorrentes para o mesmo `negociacaoId` e repetir após reinício do consumidor. Exigir um pedido e um evento lógico de criação. [A ESCLARECER: fixar cardinalidade, prazo, evidência de cada entrega e mecanismo para simular interrupções.] |

## 10. Restrições de geração

1. Este rascunho deve passar pela resolução das pendências e pela revisão independente antes da geração.
2. A versão aprovada deve conter todos os contratos no próprio documento. Preserve estados, nomes, rotas, campos e erros literais.
3. Use a stack e os arquivos da seção 4, com mutações no schema `pedidos` e comunicação de negócio interserviços por Kafka.
4. Isole o estado por CA, verifique as mudanças no banco e os eventos observáveis e meça os RNFs separadamente.
