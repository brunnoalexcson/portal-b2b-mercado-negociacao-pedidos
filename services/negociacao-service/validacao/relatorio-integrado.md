# Teste integrado local: Mercado, Negociação e Pedidos no kit — 28/11/2026

**Resultado: aprovado.** A negociação direta, a compra imediata e o leilão fecharam de ponta a ponta com os três serviços reais, no Compose do kit, sem nenhuma mensagem nas DLTs. O teste achou um defeito no Mercado e dois no kit local; nenhum na Negociação.

Base: repositório do grupo em `499a96a` (os três serviços) e kit de Infraestrutura em `4720748`.

## 1. Ambiente

- Compose do kit (`docker-compose.yml`) com os overrides `compose.mercado.yml`, `compose.negociacao.yml` e `compose.pedidos.yml` deste repositório: duas réplicas de cada serviço e a migração de cada um.
- PostgreSQL primário do kit, cluster Redpanda de 3 brokers e os 27 tópicos do `init_topics.sh`, mais os 7 que ainda não estão no kit: `pedido_temporario_solicitado`, `reserva_estoque_recusada`, `produto_atualizado`, `entrega_concluida`, `frete_indisponivel`, `fornecimento_atualizado` e `demanda_cancelada`.
- [`kit-integrado.yml`](kit-integrado.yml): emissor de tokens de teste (o mesmo da demo), portas no host e o contorno do achado 2.
- [`roteiro_integrado.py`](roteiro_integrado.py): faz o papel do Produtos, do Fornecimento e da Demanda (publica os eventos no formato dos contratos) e das empresas (chama as APIs com o token de cada uma).

## 2. Evidências

| Verificação | Resultado | Limite da evidência |
|---|---|---|
| Migrações das três roles no banco do kit | Aprovadas: Mercado e Negociação na `0002`, Pedidos na `0001`, com o `DROP` das tabelas de exemplo | Banco vazio, primeira subida |
| Prontidão das seis réplicas | Negociação e Pedidos: 200 nas duas réplicas. Mercado: 503 (achado 1) | A API e o consumo do Mercado funcionaram mesmo assim |
| Direta com propostas | Mercado abre → Negociação recebe → Pedidos cria o pedido temporário → reserva confirmada → contraproposta de 7.00 e aceite → `VENDA_CONCLUIDA` de 70.00 para o Comprador A; demanda A de 50 para 40 no Mercado | Reserva confirmada pelo roteiro, no lugar do Fornecimento |
| Aceite antes da reserva | `409 RESERVA_PENDENTE` | — |
| Compra imediata | Fecha sozinha quando a reserva confirma: `ENCERRADO` a 8.00 e `VENDA_CONCLUIDA` de 40.00 | — |
| Leilão | Aceites dos dois fornecedores no Mercado → `leilao_iniciado` → `AGUARDANDO_RESERVAS` com 2 pedidos temporários → reservas → `ABERTO` → 2 lances → `ENCERRADO` com o Comprador B (671.65) | `dataFim` adiantado no banco da Negociação (abaixo) |
| Rateio do lance vencedor (RN-11) | Pedidos com 444.40 (oferta X, `valorItem` 440.00) e 227.25 (oferta Y, 225.00): soma 671.65, igual ao lance | — |
| Fechamento no Mercado | Leilão e direta `CONCLUIDA`; saldo das demandas abatido | — |
| DLTs | `mercado.dlt`, `negociacao.dlt` e `pedidos.dlt` vazias depois de todas as rodadas | — |
| Contratos de entrada compartilhados | O Produtos e o Fornecimento reais publicam os campos que o Mercado e o Pedidos exigem (`tipoTransporte` e `cadastradoEm`; `preco`, `quantidadeDisponivel`, `origem` e `criadoEm`), conferido no código do repositório deles | Leitura de código, não execução |

Saída completa da rodada aprovada: [`saida-integrado.txt`](saida-integrado.txt).

**Prazo do leilão.** O Mercado só aceita leilão de 1 h ou mais (`duracaoLeilaoHoras` ≥ 1). Para não esperar, o `data_fim` do leilão foi adiantado no banco da Negociação; o agendador encerrou o leilão no ciclo seguinte e publicou o `negociacao_fechada`, que o Pedidos e o Mercado consumiram. O encerramento no horário natural está coberto pela demo da Negociação e pelo CA-57.

## 3. Achados

| # | Onde | Achado | Situação |
|---|---|---|---|
| 1 | Mercado | O `/health/ready` responde 503 desde o commit `5385dcf`: `VERSAO_MIGRACAO` continua `"0001"`, e o banco fica na `0002_duracao_leilao` | Issue [#29](https://github.com/brunnoalexcson/portal-b2b-mercado-negociacao-pedidos/issues/29) |
| 2 | Kit local | Na primeira subida, o `pg-primary` trava no `CREATE DATABASE` do init: `synchronous_standby_names` espera a réplica, que não conecta durante o init | Contornado no `kit-integrado.yml` (primário sem a exigência síncrona); não reportado |
| 3 | Kit local | A `pg-replica` nunca conecta: falta a entrada de replicação no `pg_hba.conf` do primário. Com a configuração síncrona, toda escrita ficaria travada | Réplica parada no teste; não reportado |

Os achados 2 e 3 valem só para o Compose local; o banco da AWS é outro.

**Cuidado ao simular o Fornecimento.** O Mercado consome o `estoque_atualizado` e troca o saldo da oferta pelo `quantidadeDisponivel` do evento. O evento de reserva precisa levar o saldo que sobrou, como o Fornecimento real faz; com zero, a oferta some da vitrine.

## 4. Fora do teste

- Gateway do kit: as chamadas foram direto às réplicas (Mercado e Pedidos na `app1`, Negociação na `app2`).
- Identity, Produtos, Fornecimento e Demanda reais: tokens do emissor de teste e eventos publicados pelo roteiro.
- Caminhos negativos entre serviços: reserva recusada e leilão `DESERTO`.

## 5. Como repetir

Na pasta do kit, com a imagem do emissor da demo construída (`docker compose -f compose.demo.yml build identity`) e um `.env` com as DSNs das roles (`MERCADO_DATABASE_URL`, `NEGOCIACAO_DATABASE_URL`, `DATABASE_URL` do Pedidos, as três de migração, `JWKS_URL=http://identity-teste:8000/.well-known/jwks.json` e os `*_PROJECT_DIR`):

```bash
docker compose --env-file .env -f docker-compose.yml -f <repo>/services/mercado-service/compose.mercado.yml -f <repo>/services/negociacao-service/compose.negociacao.yml -f <repo>/services/pedidos-service/compose.pedidos.yml -f <repo>/services/negociacao-service/validacao/kit-integrado.yml up -d pg-primary rp-0 rp-1 rp-2 identity-teste
```

Depois: criar os tópicos, rodar `mercado-migrate`, `negociacao-migrate` e `pedidos-migrate`, subir as seis réplicas e rodar o roteiro na rede `portal-net`:

```bash
docker run --rm --network portal-net -v <repo>/services/negociacao-service/validacao:/integ -e PYTHONPATH=/srv/negociacao -w /srv/negociacao negociacao-demo-popular python /integ/roteiro_integrado.py direta
```
