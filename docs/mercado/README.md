# Mercado / Matching Engine (`mercado-service`)

Responsável: Brunno Alexcson. Porta `8105`, rotas `/api/v1/mercado`, schema PostgreSQL `mercado`, role `svc_mercado`.

O serviço acompanha, por produto, a oferta disponível e a demanda aberta do portal e decide como cada compra é negociada: negociação direta entre um comprador e a oferta que ele escolher, ou leilão em lote quando a demanda supera a oferta e os fornecedores aceitam a sugestão de leilão. É o bloco 5 do diagrama da arquitetura ("calcula oferta vs demanda, define modo de negociação, inicia leilões quando necessário").

## Situação em 19/11/2026 (v0.5)

| Item | Situação |
|---|---|
| Especificação (ESP-MRC-001 v0.5) | Rascunho no modelo ERS-LLM v1.2 do QA, antes do gate G1; 17 RFs, 59 CAs, decisões DG-01 a DG-54 |
| Contratos de eventos | Schemas JSON e exemplos validados para os 12 tópicos; conferidos contra os contratos do Pedidos e do Fornecimento |
| Código (`services/mercado-service/`) | Não iniciado; segue a estrutura do `pedidos-service` |
| Alinhamento com Pedidos | Fechado em 19/11 (INT-MRC-02): a ESP-PED-001 não muda |
| Alinhamento com Negociação | Proposta de 19/11 na INT-MRC-01, **a confirmar com o Caio** |
| Status Report | Decisões marcadas com "Status Report: sim" na seção 13 da ESP |

## Documentos

- [Especificação ESP-MRC-001](requisitos.md): regras, rotas, eventos, erros, critérios de aceitação e decisões DG-01 a DG-54, pendências de integração INT-MRC-01 a INT-MRC-09 e pontos fora desta versão (seção 13).
- [Contratos de eventos](contratos/): um `<topico>.schema.json` (JSON Schema 2020-12) e um `<topico>.exemplo.json` por tópico consumido e publicado.

## Como o serviço funciona

**Balanço e vitrine**

1. Recebe produtos, ofertas e demandas pelos eventos dos serviços de origem.
2. A cada evento, recalcula o balanço do produto: oferta total (disponível livre das ofertas) × demanda total (saldo negociável das demandas abertas).
3. Mostra a vitrine do produto pelo menor preço (o "leilão reverso" do professor), pública para compradores e fornecedores, e indica quais ofertas servem para a demanda do comprador.
4. O balanço mostra só totais: nenhum fornecedor vê quem são os compradores.
5. Demanda com prazo vencido deixa de contar no balanço e não abre negociação direta; declarar a demanda expirada é do Demanda.

**Negociação direta (`VENDA_DIRETA`)**

1. O comprador escolhe uma oferta na vitrine e pede a negociação com a quantidade que quer (`POST /api/v1/mercado/negociacoes`).
2. O Mercado confere aptidão, preço máximo, saldo da demanda e disponível da oferta, e publica `modo_negociacao_definido`.
3. A Negociação abre a direta com o mesmo `negociacaoId` e pede o pedido temporário ao Pedidos (`pedido_temporario_solicitado`).
4. Uma demanda pode ter várias negociações abertas com ofertas diferentes, até o seu saldo (atendimento parcial).

**Leilão (`LEILAO_DIRETO`)**

1. Quando a demanda supera a oferta, abre uma sugestão de leilão para os fornecedores do produto.
2. Cada fornecedor decide se entra no lote e com quanto; pode alterar ou sair até T1.
3. O lote fecha (T1) no fim da janela de adesão, quando todos os fornecedores elegíveis entraram ou quando a oferta volta a cobrir a demanda.
4. Em T1, o Mercado calcula o preço inicial (soma de quantidade × preço), o incremento mínimo e a participação de cada fornecedor no valor do lote, e publica `modo_negociacao_definido` e `leilao_iniciado`. T2 = T1 + duração configurada.
5. A Negociação pede o pedido temporário de cada item e só libera os lances com todas as reservas confirmadas. Item com reserva recusada sai do lote antes do primeiro lance, e ela recalcula o preço inicial.
6. Lances, vencedor e rateio do valor são da Negociação. O Mercado recebe o `negociacao_fechada` para atualizar o saldo das demandas; só o primeiro fechamento de cada negociação é aplicado.

Fora do escopo: conduzir propostas e lances, criar pedidos, reservar estoque, compra conjunta, preço uniforme, notificações por e-mail ou push.

## Fronteira com os outros serviços

| Direção | Tópico | Com quem | Chave |
|---|---|---|---|
| Publica | `modo_negociacao_definido` | Negociação | `negociacaoId` |
| Publica | `leilao_iniciado` | Negociação, front-end | `negociacaoId` |
| Publica | `mercado.dlt` | operação, QA | chave original |
| Consome | `produto_cadastrado`, `produto_atualizado` | Produtos | `produtoId` |
| Consome | `fornecimento_criado`, `fornecimento_atualizado`, `estoque_atualizado` | Fornecimento | `fornecimentoId` |
| Consome | `demanda_criada`, `demanda_recorrente_gerada`, `demanda_cancelada` | Compra e Demanda | `demandaId` |
| Consome | `negociacao_fechada` | Negociação | `negociacaoId` |

## Fluxo combinado com Negociação e Pedidos (19/11)

Segue o diagrama: o Mercado inicia os leilões e publica `modo_negociacao_definido` e `leilao_iniciado`; a Negociação fica com lances, regras e vencedor.

| Etapa | Quem faz |
|---|---|
| Balanço, sugestão, vitrine e formação do lote (aceites e T1) | Mercado |
| Abrir a direta ou o leilão com o `negociacaoId` do Mercado | Negociação |
| Pedido temporário de cada oferta (`pedido_temporario_solicitado`) | Negociação pede; Pedidos cria e reserva |
| Lances só com todas as reservas confirmadas; item recusado sai antes do primeiro lance | Negociação |
| Vencedor, rateio e `negociacao_fechada` | Negociação |
| Destino da entrega | O comprador informa no Pedidos depois da venda |

**Pedidos à Negociação** (INT-MRC-01)
- Abrir a direta e o leilão consumindo `modo_negociacao_definido` e `leilao_iniciado`, em vez de criá-los por HTTP.
- Esperar as reservas antes dos lances e recalcular o lote se um item sair.
- Datas do leilão pelo sistema, rateio pela participação no valor e incremento mínimo enviados pelo Mercado.
- `demandaId` opcional no lance e no `negociacao_fechada`.

**Pedidos**: nada muda na ESP-PED-001; falta só atualizar a referência para a ESP-MRC-001 v0.5.

## Pendências com outros grupos

| Grupo | Pendência |
|---|---|
| Negociação | Itens da INT-MRC-01 acima |
| Infra | Criar `demanda_cancelada`, `produto_atualizado` e `fornecimento_atualizado`; registrar o Mercado como produtor e consumidor dos tópicos; retirar o Mercado dos consumidores de `lance_realizado`; rota do gateway |
| Compra e Demanda | Ajustes pedidos em 17/11 (`precoMaximo`, `frequencia`, `ocorrenciaId` no cancelamento, edição); cancelar demandas de produto excluído |
| Fornecimento | Atendida em 23/11: o Mercado está registrado como consumidor dos eventos de produto e oferta no contrato v2.5. Falta só o alinhamento da documentação deles, pedido na issue #44 de `davicalvarenga/Sistema-MicServ-SD` |
| Banco de Dados | DDL do schema `mercado` e quantidade decimal |
| Autenticação | Padrão de erro com `codigo` |
| Pedidos | Atualizar a referência para a ESP-MRC-001 v0.5 |
| UX/UI | Telas de vitrine, sugestão de leilão e contexto do leilão |
| QA | Revisar a ESP no gate G1 |
