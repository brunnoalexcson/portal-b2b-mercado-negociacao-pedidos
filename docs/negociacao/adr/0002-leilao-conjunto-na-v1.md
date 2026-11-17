---
status: accepted
data: 2026-11-13
autor: Caio Almeida Oliveira
---

# Leilão conjunto já na primeira versão

O professor permitiu juntar vários fornecedores do mesmo produto num único leilão, com o valor rateado pelo volume de cada um, mas recomendou fazer "a versão básica primeiro". Mesmo assim, o leilão conjunto entra na v1 do `negociacao-service`, por decisão do Caio. Assim o modelo já nasce com várias ofertas por leilão, em vez de ser refeito depois. A alternativa era começar com um fornecedor por leilão e tratar o conjunto como segunda iteração.

## Consequências

- Um leilão tem uma ou mais ofertas (`fornecimentoId`), cada uma com a sua quantidade e o seu preço; o lote é a soma das quantidades.
- Cada oferta gera o seu pedido temporário na abertura, e o fechamento efetiva um pedido por oferta, com o valor rateado. Isso muda o contrato com Pedidos, que hoje aceita um único pedido por `negociacaoId` (D7).
- A ESP precisa fixar o rateio: a proporção do volume, o arredondamento (o centavo que sobra) e o lance inicial como soma ponderada.
