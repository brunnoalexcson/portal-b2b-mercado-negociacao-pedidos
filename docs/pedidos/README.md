# Pedidos (`pedidos-service`)

Responsável: Daniel Junio. Porta `8107`, rotas `/api/v1/pedidos`, schema PostgreSQL `pedidos`, role `svc_pedidos`.

O serviço registra o pedido que bloqueia o estoque de uma oferta durante a negociação e acompanha esse pedido até a entrega. Quando um leilão ou uma negociação direta abre, nasce um **pedido temporário, sem comprador**, para cada oferta. Esse pedido reserva o estoque no Fornecimento. No fechamento, Pedidos efetiva os pedidos vendidos (`VENDA_CONCLUIDA`, com comprador e valor rateado) e cancela os demais. Depois da venda, o comprador informa o destino, a Logística cota e seleciona o frete, e a entrega encerra o pedido.

## Situação em 19/11/2026

| Item | Situação |
|---|---|
| Especificação (ESP-PED-001 v2.1) | No modelo **ERS-LLM v1.2 do QA**, como a ESP-MRC-001: 10 RFs, 39 critérios de aceitação em Gherkin, 29 regras, 11 RNFs. Conteúdo aprovado em 19/11 (v2.0, processo AED, 5 rodadas de revisão independente). Pronta para o gate G1. Pontos que dependem de outros grupos marcados com **⚠ Ressalva INT-PED-xx** (§13). |
| Contratos de eventos | Schemas JSON 2020-12 e exemplos validados para os 14 tópicos consumidos e publicados. Compatíveis com os contratos da Negociação (`926dfb8`), do Mercado (`809d4c4`) e do Fornecimento (`56da541`). |
| Código (`services/pedidos-service/`) | Primeiro fluxo (v1.1) implementado e testado. Falta adaptar ao pedido temporário da v2.0. |
| Alinhamento com Negociação e Mercado | **Divergência no gatilho do pedido temporário** (ver abaixo), a decidir no grupo. |

## Documentos

- [Especificação ESP-PED-001](requisitos.md): modelo ERS-LLM v1.2, com portas, regras, API, eventos, critérios de aceitação, RNFs, decisões e ressalvas por grupo (§13).
- [Contratos de eventos](contratos/): um `<topico>.schema.json` e um ou mais `<topico>[.variante].exemplo.json` por tópico.

## Como o serviço funciona

1. **Pedido temporário.** Para cada oferta que entra numa negociação, Pedidos cria um pedido `CRIADO` com `compradorId: null` e publica `pedido_criado`. Fornecimento reserva o estoque.
2. **Reserva.** A confirmação (`estoque_atualizado` `RESERVA`) muda `reservaSituacao` para `CONFIRMADA`. A recusa (`reserva_estoque_recusada`) cancela o pedido. Nos dois casos, Pedidos publica `pedido_atualizado`, que a Negociação usa para aceitar ou não a oferta no lote.
3. **Fechamento.** No `negociacao_fechada` `ENCERRADO`, os pedidos de `alocacoes` vão a `VENDA_CONCLUIDA` e o Fornecimento dá baixa. Os demais vão a `CANCELADO` e o Fornecimento libera. Em `DESERTO`, `RECUSADO` e `EXPIRADO`, todos são cancelados.
4. **Destino.** O comprador informa o destino (`PUT /api/v1/pedidos/{id}/destino`). O pedido vai a `AGUARDANDO_FRETE`, e a Logística começa a cotar.
5. **Frete e entrega.** `frete_selecionado` leva a `FRETE_SELECIONADO`, inclusive na troca de transportadora. `entrega_concluida` leva a `ENTREGUE`. `frete_indisponivel` não cancela, porque a baixa já ocorreu.

Fora do escopo: nota fiscal, pagamento, comissão, cancelamento manual e estorno (fora do Portal na v1); matching (Mercado); lances e rateio (Negociação); saldo de estoque (Fornecimento); escolha da transportadora e rastreio (Logística).

## Fronteira com os outros serviços

| Direção | Tópico | Com quem | Chave |
|---|---|---|---|
| Consome | `pedido_temporario_solicitado` (tópico novo) | Negociação | `negociacaoId` |
| Consome | `negociacao_fechada` | Negociação | `negociacaoId` |
| Consome | `produto_cadastrado`, `produto_atualizado` | Produtos | `produtoId` |
| Consome | `fornecimento_criado`, `estoque_atualizado` | Fornecimento | `fornecimentoId` |
| Consome | `reserva_estoque_recusada` (tópico novo) | Fornecimento | `pedidoId` |
| Consome | `solicitacao_frete_criada` | Logística | `solicitacaoFreteId` |
| Consome | `frete_selecionado`, `entrega_concluida`, `frete_indisponivel` | Logística | `pedidoId` |
| Publica | `pedido_criado` | Fornecimento, Logística | `pedidoId` |
| Publica | `pedido_atualizado` | Fornecimento, Logística, Negociação | `pedidoId` |
| Publica | `pedidos.dlt` | operação, QA | chave original |

A API usa o JWT RS256 do Identity-Service, validado localmente pela JWKS. Erros em `application/problem+json` com `codigo` e `correlationId`.

## Divergência a decidir no grupo: quem pede o pedido temporário

As especificações de Negociação e de Mercado propõem gatilhos diferentes para o pedido temporário:

| | ESP-NEG-001 (Caio, 17/11) | ESP-MRC-001 v0.3 (Brunno, 18/11) |
|---|---|---|
| Quem forma o leilão | Fornecedor cria na Negociação, por HTTP. Outros fornecedores entram até `dataInicio`. | Mercado abre uma sugestão. Os fornecedores aderem no Mercado, e o lote fecha em T1. |
| Evento para Pedidos | `pedido_temporario_solicitado`, um por oferta, quando a oferta entra | `leilao_iniciado` com `itens[]` em T1; na direta, `modo_negociacao_definido` com comprador e destino |
| Quando reserva | Antes dos lances. A oferta só entra no lote com a reserva confirmada. | Depois de T1, com o lote já fixado. A ESP-MRC-001 aceita que, nesse intervalo, outra negociação use o mesmo saldo e a reserva seja recusada (PR-01). |
| `negociacao_fechada` | `alocacoes` com `pedidoId` | Igual, com `demandaId` opcional |

**Posição de Pedidos:** a ESP-PED-001 v2.0 segue a ESP-NEG-001, porque nela o lote só contém ofertas com reserva confirmada. Isso evita vender uma oferta cujo estoque foi recusado. O `negociacao_fechada` das duas propostas já é compatível com o nosso schema. Se o grupo escolher o modelo do Mercado, Pedidos troca só a entrada da RN-06 e do §7.3 (ler `leilao_iniciado.itens[]` e `modo_negociacao_definido.ofertas[]`), e a ESP sobe para a v2.2. Com isso, na direta o comprador e o destino viriam desde a abertura.

## Pendências com outros grupos

| Grupo | Pendência |
|---|---|
| **Logística (crítico)** | Ignorar `pedido_criado` sem comprador, cotar no `AGUARDANDO_FRETE`, não concluir a venda na entrega, não cancelar em `frete_indisponivel` e reabrir a cotação (INT-PED-04 a 06). Sem isso, a Logística cotaria pedidos temporários. |
| Negociação e Mercado | Decidir o gatilho do pedido temporário (INT-PED-11, acima). Aceite de INT-PED-01. |
| Fornecimento | Resposta na issue #1 e deduplicação da baixa (INT-PED-02) |
| Infra | Tópicos novos, consumidores no `CONTRACTS.md` e rota no gateway (INT-PED-07) |
| Autenticação | `iss` e `aud` reais, `PLATFORM_ADMIN` e formato de erro (INT-PED-08) |
| Banco de Dados | DDL do schema `pedidos` e quantidade decimal (INT-PED-03) |
| UX/UI | Tela de destino do comprador (INT-PED-12) |
| Professor | Nota fiscal e cancelamento manual fora da v1 (INT-PED-10) |
| QA | Revisar a ESP no gate G1 (INT-PED-09) |
