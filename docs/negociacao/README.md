# Negociação e Leilão (`negociacao-service`)

Responsável: Caio Almeida Oliveira. Porta `8106`, rotas `/api/v1/negociacoes`, schema PostgreSQL `negociacao`, role `svc_negociacao`.

O serviço conduz as negociações que o Mercado define: leilões em que compradores disputam o lote de uma ou mais ofertas do mesmo produto, e negociações diretas de preço entre um comprador e um fornecedor. No fim, ele informa o resultado ao Pedidos, que efetiva ou cancela os pedidos temporários criados na abertura, e ao Mercado, que atualiza o saldo das demandas.

## Situação em 27/11/2026

| Item | Situação |
|---|---|
| Especificação (ESP-NEG-001 v1.5) | Rascunho alinhado com a ESP-MRC-001 v0.11 e a ESP-PED-001 v2.2; ainda não submetido ao gate G1 do QA |
| Contratos de eventos | Schemas JSON e exemplos para os 7 tópicos, validados também contra os schemas do Mercado e do Pedidos |
| Código (`services/negociacao-service/`) | Implementado em 26/11 (issue #14, partes 1 a 7): negociação direta com propostas, compra imediata e expiração; leilão com reservas, recálculo do lote, lances com sigilo e encerramento com rateio; idempotência HTTP e consumo idempotente com DLT. 188 testes automatizados e 6 de fluxo real na demonstração com duas réplicas, Redpanda e PostgreSQL. Swagger em `/docs` com resumo, exemplo e erros por código em cada rota, e botão Authorize; na demonstração, o emissor de tokens de teste fica em `localhost:18107`. Falta rodar no kit, que depende do tópico `pedido_temporario_solicitado` |
| Alinhamento com Mercado e Pedidos | Fechado em 19/11 (INT-NEG-01 a INT-NEG-03); INT-NEG-13 fechada com o Pedidos; IDs com até 64 caracteres no Mercado desde a v0.6 (INT-NEG-12, fechada em 22/11) |
| Tópicos novos na Infra | `pedido_temporario_solicitado` e os consumidores já pedidos nas issues [#3](https://github.com/Printf-PedroCardoso/portal-b2b-kit-infra/issues/3) (Pedidos) e [#6](https://github.com/Printf-PedroCardoso/portal-b2b-kit-infra/issues/6) (Mercado) do kit (INT-NEG-05) |

## Documentos

- [Especificação ESP-NEG-001](requisitos.md): regras, rotas, eventos, erros, critérios de aceitação e decisões (seção 13).
- [Contratos de eventos](contratos/): um `<topico>.schema.json` (JSON Schema 2020-12) e um `<topico>.exemplo.json` por tópico.
- Decisões de arquitetura: [ADR 0001: dinheiro com 2 casas na API e 4 no banco](adr/0001-dinheiro-2-casas-na-api-4-no-banco.md) e [ADR 0002: leilão conjunto na v1](adr/0002-leilao-conjunto-na-v1.md).

## Como o serviço funciona

**Leilão (`LEILAO_DIRETO`)**

1. O Mercado forma o lote pela sugestão de leilão e publica `leilao_iniciado`, com as ofertas, o preço inicial, o incremento mínimo, a participação de cada oferta no valor do lote, `dataInicio` e `dataFim`.
2. O serviço abre o leilão em `AGUARDANDO_RESERVAS` e pede um **pedido temporário** sem comprador para cada oferta, que reserva o estoque.
3. Quando nenhuma reserva está pendente, os lances são liberados (`ABERTO`). A oferta recusada sai do lote, e o preço inicial, o incremento e as participações são recalculados. Sem nenhuma reserva confirmada, o leilão termina `DESERTO` na hora.
4. Até `dataFim`, compradores dão lances. O lance é o **valor total** do lote: o primeiro precisa ser maior ou igual ao preço inicial, e cada lance seguinte, pelo menos o melhor lance mais o incremento mínimo. O comprador pode informar a sua demanda (`demandaId`).
5. Em `dataFim`, o leilão fecha sozinho:
   - com lance, fica `ENCERRADO`, e o valor é rateado na proporção exata do valor de cada oferta no lote (os centavos que sobram vão para a oferta de maior valor);
   - sem lance, ou com reserva ainda pendente, fica `DESERTO`.

**Negociação direta (`VENDA_DIRETA`)**

1. O comprador pede a negociação no Mercado, que publica `modo_negociacao_definido`. O serviço abre a negociação com a proposta do fornecedor pelo preço da oferta e pede o pedido temporário.
2. A vez começa com o comprador. As partes alternam contrapropostas de preço unitário, nunca acima do preço da oferta. A parte da vez pode aceitar (`ENCERRADO`, com a reserva confirmada), e qualquer uma pode recusar (`RECUSADO`).
3. Depois de 48 h sem resposta, a negociação vira `EXPIRADO`.
4. **Compra imediata:** se o comprador pediu a compra pelo preço anunciado (`compraImediata` no pedido ao Mercado), ninguém propõe, aceita ou recusa. A negociação fecha sozinha: `ENCERRADO` quando a reserva confirma, `RECUSADO` se ela for recusada, `EXPIRADO` depois de 48 h sem reserva.

Fora do escopo:
- comparar oferta × demanda, sugerir o leilão, formar o lote e validar a negociação direta (Mercado);
- o "leilão reverso", que é a vitrine do Mercado;
- criar ou cancelar pedidos (Pedidos);
- reservar estoque (Fornecimento);
- cancelar leilão, encerrar manualmente ou ter um administrador;
- republicar automaticamente um fechamento recusado pelo Pedidos (a recusa fica na `pedidos.dlt`, combinado com o Pedidos);
- SSE ou WebSocket.

## Fronteira com os outros serviços

| Direção | Tópico | Com quem | Chave |
|---|---|---|---|
| Consome | `leilao_iniciado` | Mercado | `negociacaoId` |
| Consome | `modo_negociacao_definido` (só `VENDA_DIRETA`) | Mercado | `negociacaoId` |
| Consome | `pedido_atualizado` | Pedidos | `pedidoId` |
| Publica | `pedido_temporario_solicitado` (tópico novo) | Pedidos | `negociacaoId` |
| Publica | `lance_realizado` | front-end, auditoria | `negociacaoId` |
| Publica | `negociacao_fechada` | Pedidos, Mercado | `negociacaoId` |
| Publica | `negociacao.dlt` | operação, QA | chave original |

Não consome eventos do Fornecimento, da Demanda nem do Identity. A autenticação usa o JWT RS256 do Identity-Service, validado localmente pelo JWKS.

## Pendências com outros grupos

| Grupo | Pendência |
|---|---|
| Infra | Atender as issues [#3](https://github.com/Printf-PedroCardoso/portal-b2b-kit-infra/issues/3) e [#6](https://github.com/Printf-PedroCardoso/portal-b2b-kit-infra/issues/6) do kit: criar `pedido_temporario_solicitado`, registrar o Negociação como consumidor de `pedido_atualizado` e o Mercado como consumidor de `negociacao_fechada` |
| Banco de Dados | DDL do schema `negociacao` (tabelas na seção 6 da ESP), `NUMERIC(18,4)` para dinheiro e quantidade decimal |
| QA | Revisar a ESP no gate G1 |
