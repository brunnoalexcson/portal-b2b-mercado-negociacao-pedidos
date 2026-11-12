# Validação de Pedidos — 12/11/2026

**Resultado: primeiro fluxo validado com correções; microsserviço completo ainda pendente.**

Base inspecionada: commit `008196686d705a89b210308e00135a86b5e0e756` do repositório do grupo, comparado com a ESP-PED-001 v1.1. As correções e os testes desta rodada acompanham este documento. A revisão se limita a Pedidos.

## 1. Evidências obtidas

| Verificação | Resultado | Limite da evidência |
|---|---|---|
| Build e migração em banco vazio | Aprovados | Compose de demonstração; não é o kit completo da turma. |
| Suíte original, antes das correções | 5 testes aprovados | Não detectava as falhas de entrada descobertas nesta revisão. |
| Suíte final | **22 testes aprovados, zero falhas e zero ignorados**, em 4,89 s | Inclui testes unitários e cenários reais da fatia de criação; não equivale aos 23 critérios da especificação. |
| Dependências Python | `pip check`: `No broken requirements found` | Compatibilidade instalada; não é auditoria de vulnerabilidades. |
| Kafka → PostgreSQL → outbox → Kafka → consulta HTTP | Aprovado | Eventos de parceiros produzidos pelos testes. |
| Duas réplicas e gateway | Aprovado no fluxo exercitado | Não mede capacidade, balanceamento uniforme nem failover de broker replicado. |
| Queda do broker | `/health=200`, `/health/ready=503`; após retorno, prontidão `200` | Um broker Redpanda de demonstração. |
| Queda do PostgreSQL | `/health=200`, `/health/ready=503`; após retorno, prontidão `200` | Não testa promoção de réplica de banco. |

Artefatos: [JUnit](resultado-pedidos.xml), [saída da verificação operacional](operacao.txt), [script operacional](validar-operacao.ps1) e [testes adicionais](../tests/test_validacao_final.py). Os IDs usados são sintéticos.

O ambiente isolado usou PostgreSQL 16, Redpanda, duas aplicações e Nginx na porta 18127. A demonstração existente na porta 18107 não foi usada nos testes de interrupção. A autenticação usa assinatura RS256 real com JWKS de um emissor exclusivo dos testes; a integração com Identidade da turma continua pendente.

## 2. Problemas encontrados e corrigidos

### Entrada que bloqueava o Kafka

Um `produto_cadastrado` cujo nome continha o caractere NUL passava pela validação e era rejeitado pelo PostgreSQL. O consumidor reiniciava continuamente sem confirmar o offset nem enviar a mensagem à DLT. A mensagem seguinte da mesma partição ficava retida. O `POST` com NUL no identificador também devolvia `503`, embora o problema estivesse na entrada.

A correção rejeita NUL e sequências Unicode não codificáveis em UTF-8 antes da persistência, inclusive em campos extras dos eventos. IDs HTTP usam a mesma validação. O teste real agora observa a DLT, o avanço do offset da mensagem seguinte e `422 VALIDACAO_INVALIDA` na API.

Arquivos: [contratos](../app/contratos.py), [validação de texto](../app/jsonio.py) e [Kafka](../app/adaptadores/kafka.py).

### Perda do original na DLT

JSON quebrado e bytes UTF-8 inválidos viravam `originalEvent:null`. Agora a DLT preserva texto limitado a 64 KiB, substitui bytes inválidos e conserva JSON válido com campos sensíveis redigidos. Texto malformado com marcadores de credencial ou escapes Unicode é omitido por inteiro, pois não há estrutura confiável para isolar o segredo. Também são omitidos textos JSON com esses marcadores. Essa escolha conservadora pode ocultar texto legítimo; a referência ao tópico, partição e offset permanece disponível.

Foram reproduzidas as falhas antes das correções e acrescentados testes de limite, UTF-8 inválido, credencial em texto e chave de credencial escapada. A publicação de diagnóstico também tolera o Unicode inválido que causou a rejeição.

## 3. O que já funciona

- Persistência local de resultado vencedor ou deserto e projeções de produto e fornecimento.
- Criação de um pedido `CRIADO` por negociação, com snapshot de carga, valores decimais e oferta identificada.
- Espera por projeções ausentes e posterior criação pelo reconciliador.
- Deduplicação por evento e negociação; histórico, auditoria e outbox na transação de criação.
- Publicação de `pedido_criado`, incluindo dados necessários ao contrato de trabalho com Fornecimento e Logística.
- Listagem, detalhe e histórico com filtragem por empresa; comprador, fornecedor e administrador de tenant exercitados.
- `POST` restrito ao operador, recusa sem resultado local, recuperação de pedido existente e criação `201` com `Location`.
- Dois `POST`s concorrentes com a mesma chave preservam a resposta `201`; outra chave recupera o mesmo pedido com `200`.
- Rejeição de acesso cruzado, ausência de contexto, escopo de transportadora, token malformado e entradas HTTP inválidas nos cenários testados.
- DLT para erro de contrato e recuperação de prontidão após indisponibilidade de dependências.

## 4. Cobertura dos 23 critérios funcionais

**Verificado** significa que o comportamento principal do critério foi exercitado nesta stack. **Parcial** significa que falta parte do cenário ou da evidência. **Pendente** significa que a funcionalidade necessária ainda não existe. Os testes adicionais de robustez não criam novos critérios da ESP.

| Critério | Estado | Evidência ou ausência |
|---|---|---|
| CA-PED-01.1 | Verificado | Fechamento cria pedido, valores esperados e `pedido_criado`. |
| CA-PED-01.2 | Verificado | Seis entregas, um pedido e um evento lógico de criação. |
| CA-PED-01.3 | Verificado | Resultado deserto não cria pedido. |
| CA-PED-01.4 | Verificado | POST recupera pedido Kafka com `200`. |
| CA-PED-01.5 | Verificado | Sem vencedor local: `409 RESULTADO_INDISPONIVEL`. |
| CA-PED-01.6 | Parcial | Espera e chegada de projeções testadas; falta disputa efetiva entre POST e reconciliador no momento da criação. |
| CA-PED-01.7 | Verificado | Fixture afasta o reconciliador só daquele resultado; POST cria `201`, devolve Location e preserva resposta na repetição. |
| CA-PED-02.1 | Parcial | GETs funcionam em `CRIADO`; não existe transição para testar histórico com duas versões. |
| CA-PED-02.2 | Verificado | Paginação inválida devolve `422`. |
| CA-PED-02.3 | Verificado | Outra empresa não lista nem acessa detalhe ou histórico. |
| CA-PED-03.1 | Pendente | Falta consumir recusa e cancelar com motivo. |
| CA-PED-03.2 | Pendente | Falta cancelamento terminal e descarte de seleção tardia. |
| CA-PED-04.1 | Pendente | Faltam confirmação de estoque e seleção antecipada. |
| CA-PED-04.2 | Pendente | Faltam PATCH, conclusão comercial e entrega. |
| CA-PED-04.3 | Pendente | A rota PATCH não existe; ausência da rota não comprova autorização. |
| CA-PED-04.4 | Pendente | Faltam tentativas e substituição de transportadora. |
| CA-PED-04.5 | Pendente | Faltam cancelamento por frete e evidência da liberação de estoque. |
| CA-PED-04.6 | Pendente | Falta reconciliar entrega antecipada da nova tentativa. |
| CA-PED-04.7 | Pendente | Faltam entrega pendente e duas transições na mesma transação. |
| CA-PED-05.1 | Pendente | Atomicidade do cancelamento ainda sem implementação. |
| CA-PED-05.2 | Pendente | Falta injeção de erro na outbox da transição e comprovação de rollback. |
| CA-PED-05.3 | Pendente | Falta retomada de `pedido_atualizado` após falha entre ACK e confirmação persistida. |
| CA-PED-05.4 | Parcial | DLT exercitada em tópicos do primeiro fluxo; `frete_selecionado` ainda não é consumido e falta reter o ACK da DLT no teste. |

Contagem: **8 verificados, 3 parciais e 12 pendentes**. Essa contagem não é um percentual de conclusão do projeto: os critérios têm complexidades diferentes e os RNFs são avaliados separadamente.

## 5. O que falta para completar Pedidos

### Funcionalidades sob responsabilidade do nosso serviço

1. Consumir `estoque_atualizado` e `reserva_estoque_recusada`; conferir identidade e quantidade da reserva; avançar para `AGUARDANDO_FRETE` ou `CANCELADO`.
2. Consumir `solicitacao_frete_criada`, `frete_selecionado` e `frete_indisponivel`; guardar seleção antecipada, controlar tentativa vigente e cancelar quando aplicável.
3. Implementar conclusão comercial e `entrega_concluida`, conforme o gatilho acordado; tratar entrega fora de ordem e estados terminais.
4. Publicar `pedido_atualizado` com histórico, auditoria e outbox por versão. Ordenar versões do mesmo pedido entre publicadores; hoje só existe a versão de criação.
5. Criar as migrações de frete, fatos pendentes e demais campos previstos; manter o schema exclusivo de Pedidos.

O código atual não altera status após a criação: `frete` e `entregueEm` são sempre nulos, e a assinatura Kafka tem apenas os quatro tópicos de fechamento/projeção. Listar outros status no filtro HTTP não implementa suas transições.

### Robustez, operação e evidências restantes

- Implementar as três tentativas para falhas localizadas previstas na ESP, distinguindo indisponibilidade geral. A correção de caracteres elimina a falha reproduzida; não substitui essa política geral.
- Revisar o atraso da outbox: `now()` usa o início da transação. Uma tentativa de envio demorada pode consumir o intervalo de espera; contar o atraso a partir do instante da falha.
- Corrigir a normalização numérica de hashes com compatibilidade dos registros existentes. Foi reproduzido `fingerprint({"preco":10}) != fingerprint({"preco":Decimal("10.00")})`; isso pode gerar diagnóstico falso de conflito de evento. O algoritmo persistido não foi alterado nesta rodada.
- Instrumentar latência/erros HTTP, atraso do consumidor, DLT, duplicatas, pendências e idade da outbox. Hoje `/metrics` expõe métricas padrão do processo; faltam indicadores do serviço e logs estruturados com correlação.
- Guardar causa consultável de cada pendência e definir retenção, alertas e procedimento de reprocessamento.
- Exportar schemas JSON e exemplos versionados para os parceiros; atualmente há modelos Pydantic e exemplos na especificação, mas não um pacote de contratos publicado e validado entre os grupos.
- Completar testes de concorrência com reconciliador, rollback, falha após ACK, ordenação de projeções e versões, tokens expirados/assinatura errada/issuer/audience e rotação/indisponibilidade JWKS.
- Executar isolamento entre schemas com as roles do kit, carga/p95 e homologação dos dois fluxos completos. Não há evidência suficiente para aprovar RNF-PED-01 a RNF-PED-05 integralmente. RNF-PED-06 foi exercitado na demonstração.
- Ajustar documentação/configuração antes da entrega: `APP_PORT` e `LOG_LEVEL` aparecem na especificação, mas não são lidos pelo serviço atual. O contêiner inicia explicitamente na porta 8107.

### Acordos externos que ainda precisam de confirmação

| Grupo | Confirmação necessária |
|---|---|
| Negociação | Exemplo real de `negociacao_fechada`, com empresas, produto, oferta, quantidade, valor unitário e destino; vencedor e deserto. |
| Demanda + Negociação | Propagação de destino, ocorrência recorrente e conversão da data desejada. Sem destino no fechamento, a pendência hoje não tem fonte alternativa implementada. |
| Fornecimento | Confirmação/recusa da reserva, liberação por cancelamento e baixa única por pedido. O primeiro marco reserva por `pedido_criado`. |
| Logística + Fornecimento | Gatilho definitivo da conclusão comercial; correlação da solicitação/tentativa; compensação e idempotência da baixa. |
| Identidade + Infra | JWT real, claims, suporte global, JWKS, roles de banco, tópicos, ACLs e gateway do kit. |
| Banco + QA + UX | Tipos finais, validação das migrações e respostas HTTP, cenários e evidências de homologação. |

As nove pendências INT-PED da ESP continuam sendo a referência. Nesta rodada foram usados os contratos de trabalho já documentados; não houve nova homologação com os repositórios ou serviços dos parceiros, nem envio de mensagens aos grupos.

## 6. Próximo marco recomendado

Implementar **confirmação e recusa de estoque**, com `pedido_atualizado`, transação completa e dois cenários reais no broker: reserva confirmada → `AGUARDANDO_FRETE`; reserva recusada → `CANCELADO`. Em seguida, implementar frete e conclusão. O fechamento do microsserviço exige todos os critérios funcionais, os RNFs e a integração com os parceiros demonstrados.

Para repetir os testes a partir da pasta do serviço:

```powershell
$env:PEDIDOS_HTTP_PORT = '18127'
docker compose -p pedidos-validacao-final -f compose.demo.yml up -d --build gateway
docker compose -p pedidos-validacao-final -f compose.demo.yml run --build --rm --no-deps teste python -m pytest -q -s
./validacao/validar-operacao.ps1 -PastaServico (Get-Location).Path -Projeto pedidos-validacao-final -Porta 18127
```

O script operacional exige PowerShell 7 e interrompe apenas a stack explicitamente nomeada de validação. Execute cada comando após o anterior concluir com sucesso. A entrega atual é uma base funcional de Pedidos; este relatório não declara conclusão integral nem prontidão de produção.
