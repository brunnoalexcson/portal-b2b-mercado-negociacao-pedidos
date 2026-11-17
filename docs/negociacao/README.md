# Negociação e Leilão (`negociacao-service`)

Responsável: Caio Almeida Oliveira. Porta `8106`, rotas `/api/v1/negociacoes`, schema PostgreSQL `negociacao`, role `svc_negociacao`.

O serviço conduz as negociações do portal: leilões em que compradores disputam o lote de uma ou mais ofertas do mesmo produto, e negociações diretas de preço entre um comprador e um fornecedor. No fim, ele informa o resultado ao Pedidos, que efetiva ou cancela os pedidos temporários criados na abertura.

## Situação em 17/11/2026

| Item | Situação |
|---|---|
| Especificação (ESP-NEG-001 v1.0) | Rascunho, ainda não submetido ao gate G1 do QA |
| Contratos de eventos | Schemas JSON e exemplos validados para os 9 tópicos |
| Código (`services/negociacao-service/`) | Não iniciado; começa depois do G1 |
| Alinhamento com Mercado e Pedidos | Propostas abaixo, **a confirmar** |
| Tópicos novos na Infra | Pedido feito só depois da resposta de Mercado e Pedidos |

## Documentos

- [Especificação ESP-NEG-001](../especificacoes/ESP-NEG-001_Negociacao.md): regras, rotas, eventos, erros, critérios de aceitação e decisões (seção 13).
- [Contratos de eventos](../contratos/negociacao/): um `<topico>.schema.json` (JSON Schema 2020-12) e um `<topico>.exemplo.json` por tópico.
- Decisões de arquitetura: [ADR 0001: dinheiro com 2 casas na API e 4 no banco](adr/0001-dinheiro-2-casas-na-api-4-no-banco.md) e [ADR 0002: leilão conjunto na v1](adr/0002-leilao-conjunto-na-v1.md).

## Como o serviço funciona

**Leilão (`LEILAO_DIRETO`)**

1. O fornecedor cria o leilão com a sua oferta, a quantidade, `dataInicio` e `dataFim`.
2. Até `dataInicio`, outros fornecedores do mesmo produto entram com suas ofertas (leilão conjunto).
3. Cada oferta pede um **pedido temporário** sem comprador, que reserva o estoque. A oferta só entra no lote depois que a reserva é confirmada.
4. Em `dataInicio`, o lote é fixado. O preço inicial é a soma de quantidade × preço das ofertas.
5. Entre `dataInicio` e `dataFim`, compradores dão lances. O lance é o **valor total** do lote: o primeiro precisa ser maior ou igual ao preço inicial, e cada lance seguinte, maior que o melhor lance.
6. Em `dataFim`, o leilão fecha sozinho:
   - com lance, fica `ENCERRADO`, e o valor é rateado entre as ofertas pelo volume de cada uma (o centavo que sobra vai para a oferta de maior volume);
   - sem lance, fica `DESERTO`.

**Negociação direta (`VENDA_DIRETA`)**

1. O comprador abre a negociação com uma oferta, a quantidade e um preço proposto. Isso também pede um pedido temporário.
2. Comprador e fornecedor alternam contrapropostas de preço unitário. Qualquer um pode aceitar (`ENCERRADO`) ou recusar (`RECUSADO`).
3. Depois de 48 h sem resposta, a negociação vira `EXPIRADO`.

Fora do escopo:
- comparar oferta × demanda e sugerir o leilão (Mercado);
- o "leilão reverso", que pela regra do professor não é leilão;
- criar ou cancelar pedidos (Pedidos);
- reservar estoque (Fornecimento);
- cancelar leilão, encerrar manualmente ou ter um administrador;
- SSE ou WebSocket.

## Fronteira com os outros serviços

| Direção | Tópico | Com quem | Chave |
|---|---|---|---|
| Publica | `pedido_temporario_solicitado` (tópico novo) | Pedidos | `negociacaoId` |
| Publica | `leilao_iniciado` | Pedidos, front-end | `negociacaoId` |
| Publica | `lance_realizado` | front-end, auditoria | `negociacaoId` |
| Publica | `negociacao_fechada` | Pedidos | `negociacaoId` |
| Publica | `negociacao.dlt` | operação, QA | chave original |
| Consome | `pedido_atualizado` | Pedidos | `pedidoId` |
| Consome | `fornecimento_criado`, `fornecimento_atualizado`, `estoque_atualizado` | Fornecimento | `fornecimentoId` |

Não consome eventos do Mercado, da Demanda nem do Identity. A autenticação usa o JWT RS256 do Identity-Service, validado localmente pelo JWKS.

## O que muda para Mercado e Pedidos (propostas a confirmar)

Detalhes na seção 13 da ESP (INT-NEG-01 a INT-NEG-03).

**Mercado**
- Continua *sugerindo* o leilão ao fornecedor quando a demanda supera a oferta.
- Deixa de publicar `leilao_iniciado`, que passa a ser publicado pelo Negociação.
- O Negociação não consome `modo_negociacao_definido`.
- O "leilão reverso" (mostrar o menor preço) não fica no Negociação.

**Pedidos**
- **Consumir `pedido_temporario_solicitado`** e criar o pedido sem comprador. O payload é `negociacaoId`, `tipo`, `fornecimentoId`, `fornecedorId`, `produtoId`, `quantidade` e `precoUnitario`. A reserva segue o fluxo atual (`pedido_criado` → Fornecimento).
- **Incluir no `pedido_atualizado` dos pedidos temporários** os campos `negociacaoId`, `fornecimentoId` e um campo novo `reservaSituacao` (`PENDENTE`, `CONFIRMADA`, `RECUSADA` ou `ENCERRADA`). Só `CONFIRMADA` e `RECUSADA` mudam algo no Negociação.
- **Novo formato de `negociacao_fechada`:** um evento por negociação, com `status` (`ENCERRADO`, `DESERTO`, `RECUSADO` ou `EXPIRADO`), `tipo`, `compradorId`, `produtoId` e a lista `alocacoes` (`fornecimentoId`, `fornecedorId`, `pedidoId`, `quantidade`, `valorUnitario` e `valorTotal`, com o rateio já calculado). Fora de `ENCERRADO`, a lista vem vazia. O `valorTotal` é o valor a cobrar. Veja o [exemplo](../contratos/negociacao/negociacao_fechada.exemplo.json).
- **Liberação:** ao receber `negociacao_fechada`, o Pedidos cancela todo pedido temporário da negociação que não estiver em `alocacoes`.
- **Vários pedidos por negociação** (leilão conjunto): `UNIQUE(negociacao_id, fornecimento_id)` no lugar de `UNIQUE(negociacao_id)`.
- **Destino da entrega:** `demandaId`, destino e data desejada não vêm no fechamento. O Pedidos resolve o destino por outro caminho.

## Pendências com outros grupos

| Grupo | Pendência |
|---|---|
| Infra | Criar `pedido_temporario_solicitado` e `fornecimento_atualizado`; trocar o produtor de `leilao_iniciado` para `negociacao-service`; registrar os consumidores no `CONTRACTS.md` |
| Fornecimento | Registrar o Negociação como consumidor dos três eventos de oferta |
| Banco de Dados | DDL do schema `negociacao` (tabelas na seção 6 da ESP), `NUMERIC(18,4)` para dinheiro e quantidade decimal |
| Autenticação | Divergência no formato de erro (`codigo` × `code`), a mesma do INT-PED-08 |
| Professor | Confirmar que o lance é o valor total do lote e a regra do centavo do rateio |
| QA | Revisar a ESP no gate G1 |
