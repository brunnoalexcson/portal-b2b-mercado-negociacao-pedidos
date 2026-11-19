# Pedidos (`pedidos-service`)

Responsável: Daniel Junio. Porta `8107`, rotas `/api/v1/pedidos`, schema PostgreSQL `pedidos`, role `svc_pedidos`.

O serviço registra o pedido que bloqueia o estoque de uma oferta durante a negociação e acompanha esse pedido até a entrega. Quando um leilão ou uma negociação direta abre, nasce um **pedido temporário, sem comprador**, para cada oferta. Esse pedido reserva o estoque no Fornecimento. No fechamento, Pedidos efetiva os pedidos vendidos (`VENDA_CONCLUIDA`, com comprador e valor rateado) e cancela os demais. Depois da venda, o comprador informa o destino, a Logística cota e seleciona o frete, e a entrega encerra o pedido.

## Situação em 19/11/2026

| Item | Situação |
|---|---|
| Especificação (ESP-PED-001 v2.1) | No modelo **ERS-LLM v1.2 do QA**, como a ESP-MRC-001: 10 RFs, 39 critérios de aceitação em Gherkin, 29 regras, 11 RNFs. Conteúdo aprovado em 19/11 (v2.0, processo AED, 5 rodadas de revisão independente). Pronta para o gate G1. |
| Contratos de eventos | Schemas JSON 2020-12 e exemplos validados para os 14 tópicos consumidos e publicados. Compatíveis com os contratos da Negociação (`926dfb8`), do Mercado (`5c8e8a2`) e do Fornecimento (`56da541`). |
| Código (`services/pedidos-service/`) | Primeiro fluxo (v1.1) implementado e testado. Falta adaptar ao pedido temporário da v2.1. |
| Alinhamento com Negociação e Mercado | Fluxo combinado em 19/11 (ver abaixo), registrado na ESP-MRC-001 v0.5. |

## Documentos

- [Especificação ESP-PED-001](requisitos.md): modelo ERS-LLM v1.2, com portas, regras, API, eventos, critérios de aceitação, RNFs, e decisões (§13).
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

## Fluxo combinado no grupo: quem pede o pedido temporário

Combinado em 19/11 entre Mercado, Negociação e Pedidos (ESP-MRC-001 v0.5, DG-37 a DG-39 e DG-51):

```text
Mercado forma o lote ──leilao_iniciado / modo_negociacao_definido──► Negociação
Negociação ──pedido_temporario_solicitado (um por item)──► Pedidos ──pedido_criado──► Fornecimento reserva
Pedidos ──pedido_atualizado (reservaSituacao)──► Negociação
   item com reserva recusada sai do lote antes do primeiro lance
   os lances só começam com a reserva de todos os itens confirmada
Negociação ──negociacao_fechada (alocacoes, compradorId)──► Pedidos
```

Pedidos não consome eventos do Mercado. O pedido temporário nasce sem comprador, na direta e no leilão, e o destino vem depois, pela API de Pedidos. O `demandaId` que o Mercado acrescenta ao `negociacao_fechada` é aceito e ignorado.

## Pendências com outros grupos

| Grupo | Pendência |
|---|---|
| **Logística (crítico)** | Ignorar `pedido_criado` sem comprador, cotar no `AGUARDANDO_FRETE`, não concluir a venda na entrega, não cancelar em `frete_indisponivel` e reabrir a cotação. Sem isso, a Logística cotaria pedidos temporários. |
| Negociação | Validar os contratos de `pedido_temporario_solicitado`, `negociacao_fechada` e `pedido_atualizado` e republicar o fechamento corrigido em caso de `FECHAMENTO_INCOMPATIVEL`. |
| Fornecimento | Resposta na issue #1 e deduplicação da baixa |
| Infra | Tópicos novos, consumidores no `CONTRACTS.md` e rota no gateway |
| Autenticação | `iss` e `aud` reais, `PLATFORM_ADMIN` e formato de erro |
| Banco de Dados | DDL do schema `pedidos` e quantidade decimal |
| UX/UI | Tela de destino do comprador |
| Professor | Nota fiscal e cancelamento manual fora da v1 |
| QA | Revisar a ESP no gate G1 |
