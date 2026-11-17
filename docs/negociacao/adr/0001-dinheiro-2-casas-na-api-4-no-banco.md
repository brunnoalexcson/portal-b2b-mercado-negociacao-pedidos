---
status: accepted
data: 2026-11-13
autor: Caio Almeida Oliveira
---

# Dinheiro com 2 casas na API e 4 casas no banco

O `negociacao-service` aceita e devolve valores em R$ com no máximo 2 casas decimais, mas grava em `NUMERIC(18,4)`. Decidimos assim para seguir o modelo de referência do grupo de Banco de Dados (DR-BD-002 usa `DECIMAL(18,4)` em `valor_reserva`, `preco_unitario` e `valor_total`) e para não perder centavos no rateio do leilão conjunto. Exemplo: um lance de R$ 1000,00 dividido entre três fornecedores com 1 unidade cada dá R$ 333,3333 por fornecedor. A alternativa era `NUMERIC(18,2)` em tudo, como no kit (`NUMERIC(12,2)`) e em Produtos/Pedidos, mais simples porém com arredondamento a cada rateio.

## Consequências

- O arredondamento para 2 casas acontece só na saída da API e nos eventos, sempre com `Decimal` (nunca `float`).
- Quem consome `negociacao_fechada` recebe 2 casas; a regra de arredondamento do rateio (qual fornecedor fica com o centavo que sobra) precisa entrar na ESP quando o leilão conjunto for implementado (D15).
