# Mercado / Matching Engine (`mercado-service`)

Responsável: Brunno Alexcson. Porta `8105`, rotas `/api/v1/mercado`, schema PostgreSQL `mercado`, role `svc_mercado`.

O serviço acompanha, por produto, a oferta disponível e a demanda aberta do portal e decide como cada compra é negociada: negociação direta entre um comprador e a oferta que ele escolher, ou leilão em lote quando a demanda supera a oferta e os fornecedores aceitam a sugestão de leilão. É o bloco 5 do diagrama da arquitetura ("calcula oferta vs demanda, define modo de negociação, inicia leilões quando necessário").

## Situação em 18/11/2026

| Item | Situação |
|---|---|
| Especificação (ESP-MRC-001 v0.3) | Rascunho no modelo ERS-LLM v1.2 do QA, antes do gate G1 |
| Contratos de eventos | Schemas JSON e exemplos validados para os 12 tópicos |
| Código (`services/mercado-service/`) | Não iniciado; segue a estrutura do `pedidos-service` |
| Alinhamento com Negociação e Pedidos | Divergências na INT-MRC-01 e na INT-MRC-02, **a discutir no grupo** |
| Status Report | Decisões marcadas com "Status Report: sim" na seção 13 da ESP |

## Documentos

- [Especificação ESP-MRC-001](requisitos.md): regras, rotas, eventos, erros, critérios de aceitação e decisões DG-01 a DG-36 (seção 13).
- [Contratos de eventos](contratos/): um `<topico>.schema.json` (JSON Schema 2020-12) e um `<topico>.exemplo.json` por tópico consumido e publicado.

## Como o serviço funciona

**Balanço e vitrine**

1. Recebe produtos, ofertas e demandas pelos eventos dos serviços de origem.
2. A cada evento, recalcula o balanço do produto: oferta total (disponível livre das ofertas) × demanda total (saldo negociável das demandas abertas).
3. Mostra a vitrine do produto pelo menor preço (o "leilão reverso" do professor) e indica quais ofertas servem para a demanda do comprador.

**Negociação direta (`VENDA_DIRETA`)**

1. O comprador escolhe uma oferta na vitrine e pede a negociação com a quantidade que quer (`POST /api/v1/mercado/negociacoes`).
2. O Mercado confere aptidão, preço máximo, saldo da demanda e disponível da oferta, e publica `modo_negociacao_definido`.
3. Uma demanda pode ter várias negociações abertas com ofertas diferentes, até o seu saldo (atendimento parcial).

**Leilão (`LEILAO_DIRETO`)**

1. Quando a demanda supera a oferta, abre uma sugestão de leilão para os fornecedores do produto.
2. Cada fornecedor decide se entra no lote e com quanto; pode alterar ou sair até T1.
3. O lote fecha (T1) no fim da janela de adesão, quando todos os fornecedores elegíveis entraram ou quando a oferta volta a cobrir a demanda.
4. Em T1, o Mercado calcula o preço inicial (soma de quantidade × preço), o incremento mínimo e a participação de cada fornecedor no valor do lote, e publica `modo_negociacao_definido` e `leilao_iniciado`. T2 = T1 + duração configurada.
5. Lances, vencedor e rateio do valor são da Negociação. O Mercado só recebe o `negociacao_fechada` para atualizar o saldo das demandas.

Fora do escopo: conduzir propostas e lances, criar pedidos, reservar estoque, compra conjunta, preço uniforme, notificações por e-mail ou push.

## Fronteira com os outros serviços

| Direção | Tópico | Com quem | Chave |
|---|---|---|---|
| Publica | `modo_negociacao_definido` | Negociação, Pedidos | `negociacaoId` |
| Publica | `leilao_iniciado` | Negociação, Pedidos, front-end | `negociacaoId` |
| Publica | `mercado.dlt` | operação, QA | chave original |
| Consome | `produto_cadastrado`, `produto_atualizado` | Produtos | `produtoId` |
| Consome | `fornecimento_criado`, `fornecimento_atualizado`, `estoque_atualizado` | Fornecimento | `fornecimentoId` |
| Consome | `demanda_criada`, `demanda_recorrente_gerada`, `demanda_cancelada` | Compra e Demanda | `demandaId` |
| Consome | `negociacao_fechada` | Negociação | `negociacaoId` |

## O que muda para Negociação e Pedidos (a discutir no grupo)

Detalhes na seção 13 da ESP (INT-MRC-01 e INT-MRC-02).

**Negociação**
- Consumir `modo_negociacao_definido` e `leilao_iniciado` do Mercado, em vez de criar leilão e direta por HTTP.
- Datas do leilão definidas pelo sistema (janela de adesão e duração configuradas).
- Rateio pela `participacao` no valor enviada pelo Mercado, e incremento mínimo enviado no `leilao_iniciado`.
- `demandaId` opcional no lance e no `negociacao_fechada`, para o Mercado saber qual demanda o vencedor atendeu.

**Pedidos**
- Criar o pedido da direta a partir de `modo_negociacao_definido` e um pedido temporário sem comprador por item a partir de `leilao_iniciado`; efetivar ou cancelar com `negociacao_fechada`.

## Pendências com outros grupos

| Grupo | Pendência |
|---|---|
| Infra | Criar `demanda_cancelada`, `produto_atualizado` e `fornecimento_atualizado`; registrar o Mercado como produtor e consumidor dos tópicos; rota do gateway |
| Compra e Demanda | Ajustes pedidos em 17/11 (`precoMaximo`, `frequencia`, `ocorrenciaId` no cancelamento, edição) |
| Fornecimento | Registrar o Mercado como consumidor dos eventos de produto e oferta |
| Banco de Dados | DDL do schema `mercado` e quantidade decimal |
| Autenticação | Padrão de erro com `codigo` |
| UX/UI | Telas de vitrine, sugestão de leilão e contexto do leilão |
| QA | Revisar a ESP no gate G1 |
