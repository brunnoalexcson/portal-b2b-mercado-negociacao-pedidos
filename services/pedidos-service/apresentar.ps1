<#
Roteiro de apresentação do pedidos-service (ESP-PED-001 v2.2).

Sobe a stack de demonstração e percorre o ciclo de vida do pedido, uma etapa por vez. Como os serviços
parceiros ainda não estão prontos, o roteiro publica no Kafka os eventos de teste que a Negociação, o
Fornecimento e a Logística publicariam (PROF-05), e mostra pela API o que o Pedidos fez com cada um.

Uso (cmd ou PowerShell, na pasta do serviço):
  apresentar              com pausa entre as etapas
  apresentar -SemPausa    direto, do início ao fim
  apresentar -Reiniciar   apaga os dados da demonstração antes de começar
#>
param([switch]$SemPausa, [switch]$Reiniciar)


. "$PSScriptRoot\demo\funcoes.ps1"

# ------------------------------------------------------------------ 0. ambiente

Etapa 0 "Ambiente" @(
    "Docker Compose com PostgreSQL 16, Redpanda (Kafka), duas réplicas do pedidos-service,",
    "gateway Nginx na porta 18107 e um emissor de JWT RS256 que faz o papel do Identity-Service."
)
IniciarStack $Reiniciar
& docker @COMPOSE ps --format "table {{.Service}}\t{{.State}}" 2>$null | Out-String | Write-Host
Ok "GET /health/ready -> ready (banco e Kafka disponíveis, nas duas réplicas atrás do gateway)"

$script:SUPORTE = Token "usr-admin" $null "BUYER" -Admin
$COMPRADOR = Token "usr-comp" $COMP "BUYER"
$OUTRO = Token "usr-outra-b" $OUTRA "BUYER"
$FORNECEDOR = Token "usr-f1" $OFERTAS[0].Fornecedor "SUPPLIER"
$script:NEG = [guid]::NewGuid().ToString()
Ok "tokens emitidos: comprador COMP, outro comprador, fornecedor F1 e suporte (PLATFORM_ADMIN)"
Pausa

# ------------------------------------------------------------------ 1. projeções

Etapa 1 "Produto e ofertas (cópia local)" @(
    "Produtos e Fornecimento publicam o produto e as ofertas. O Pedidos guarda uma cópia local,",
    "usada para montar o snapshot de carga do pedido (nome, unidade, transporte, peso, origem)."
)
Publicar "produto_cadastrado" "produtos-service" $PROD ([ordered]@{
        produtoId = $PROD; nome = "Milho em grão"; unidade = "TON"; tipoTransporte = "GRANEL"; pesoUnitarioKg = $null }) "Produtos"
foreach ($o in $OFERTAS) {
    Publicar "fornecimento_criado" "fornecimentos-service" $o.Id ([ordered]@{
            fornecimentoId = $o.Id; fornecedorId = $o.Fornecedor; produtoId = $PROD; unidade = "TON"
            origem = [ordered]@{ cidade = $o.Cidade; uf = "GO" } }) "Fornecimento"
}
Ok "produto Milho em grão (TON) e 3 ofertas em Goiás"
Pausa

# ------------------------------------------------------------------ 2. pedido temporário

Etapa 2 "Leilão aberto: pedido temporário, sem comprador" @(
    "A Negociação abre um leilão conjunto com as 3 ofertas e pede um pedido temporário por oferta.",
    "O Pedidos cria cada pedido em CRIADO, sem comprador, e publica pedido_criado:",
    "é esse evento que faz o Fornecimento reservar o estoque (regra do professor)."
)
Info "negociacaoId $script:NEG"
foreach ($o in $OFERTAS) {
    Publicar "pedido_temporario_solicitado" "negociacao-service" $script:NEG ([ordered]@{
            negociacaoId = $script:NEG; tipo = "LEILAO"; fornecimentoId = $o.Id; fornecedorId = $o.Fornecedor
            produtoId = $PROD; quantidade = $o.Qtd; precoUnitario = $o.Preco }) "Negociação"
}
$pedidos = Esperar "3 pedidos temporários" { $p = PedidosDaNegociacao; if ($p.Count -eq 3) { $p } }
Tabela $pedidos
$IDS = @{}
foreach ($item in $pedidos) { $IDS[$item.fornecimentoId] = $item.pedidoId }
$P1 = $IDS[$OFERTAS[0].Id]; $P2 = $IDS[$OFERTAS[1].Id]; $P3 = $IDS[$OFERTAS[2].Id]
$visto = Api GET "/api/v1/pedidos?negociacaoId=$script:NEG" $COMPRADOR
Ok "o comprador ainda não vê nenhum pedido temporário (total = $($visto.total))"
Pausa

# ------------------------------------------------------------------ 3. reserva

Etapa 3 "Fornecimento confirma as reservas" @(
    "Para cada pedido_criado, o Fornecimento reserva a quantidade e publica estoque_atualizado RESERVA.",
    "O Pedidos marca a reserva como CONFIRMADA e avisa a Negociação (pedido_atualizado):",
    "os lances só começam com todas as reservas confirmadas."
)
foreach ($o in $OFERTAS) {
    Publicar "estoque_atualizado" "fornecimentos-service" $o.Id ([ordered]@{
            fornecimentoId = $o.Id; produtoId = $PROD; fornecedorId = $o.Fornecedor; operacao = "RESERVA"
            quantidadeMovimentada = $o.Qtd; quantidadeDisponivel = 100.0; quantidadeReservada = $o.Qtd
            pedidoId = $IDS[$o.Id]; atualizadoEm = (Agora) }) "Fornecimento"
}
$pedidos = Esperar "3 reservas confirmadas" {
    $p = PedidosDaNegociacao; if (@($p | Where-Object reservaSituacao -eq "CONFIRMADA").Count -eq 3) { $p } }
Tabela $pedidos
Pausa

# ------------------------------------------------------------------ 4. fechamento

Etapa 4 "Leilão encerrado: venda concluída" @(
    "O comprador COMP vence com R$ 130,00 por tonelada para F1 e F2 (lote de 20 t).",
    "A Negociação publica negociacao_fechada com as alocações já rateadas.",
    "F1 e F2 vão a VENDA_CONCLUIDA (o Fornecimento dá baixa); F3 ficou fora do lote e é cancelado",
    "(o Fornecimento libera o estoque). A venda conclui no fechamento, não na entrega."
)
Publicar "negociacao_fechada" "negociacao-service" $script:NEG ([ordered]@{
        negociacaoId = $script:NEG; tipo = "LEILAO"; status = "ENCERRADO"; compradorId = $COMP; produtoId = $PROD
        demandaId = $null
        alocacoes = @(
            [ordered]@{ fornecimentoId = $OFERTAS[0].Id; fornecedorId = $OFERTAS[0].Fornecedor; pedidoId = $P1
                quantidade = 5.0; valorUnitario = 130.0; valorTotal = 650.0 },
            [ordered]@{ fornecimentoId = $OFERTAS[1].Id; fornecedorId = $OFERTAS[1].Fornecedor; pedidoId = $P2
                quantidade = 15.0; valorUnitario = 130.0; valorTotal = 1950.0 }) }) "Negociação"
$pedidos = Esperar "fechamento aplicado" {
    $p = PedidosDaNegociacao; if (@($p | Where-Object status -ne "CRIADO").Count -eq 3) { $p } }
Tabela $pedidos
$visto = Api GET "/api/v1/pedidos?negociacaoId=$script:NEG" $COMPRADOR
Ok "agora o comprador vê os seus $($visto.total) pedidos; o F3 cancelado continua invisível para ele"
Pausa

# ------------------------------------------------------------------ 5. destino

Etapa 5 "Comprador informa o destino (PUT /api/v1/pedidos/{id}/destino)" @(
    "A única escrita da API. O comprador informa para onde entregar o pedido F1.",
    "O pedido vai a AGUARDANDO_FRETE e o pedido_atualizado leva o snapshot de carga à Logística."
)
$corpo = '{"cidade":"Anápolis","uf":"GO"}'
Info "PUT /api/v1/pedidos/$($P1.Substring(0,8))…/destino  $corpo  (token do comprador)"
$r = Api PUT "/api/v1/pedidos/$P1/destino" $COMPRADOR $corpo
Ok "200: status $($r.status), destino $($r.destino.cidade)/$($r.destino.uf), versão $($r.versao)"
$r = Tentar PUT "/api/v1/pedidos/$P1/destino" $COMPRADOR $corpo
Ok "repetir o mesmo PUT é seguro: $($r.Status), sem nova versão"
Pausa

# ------------------------------------------------------------------ 6. frete

Etapa 6 "Logística cota e seleciona o frete" @(
    "A Logística abre a solicitação de frete e publica frete_selecionado.",
    "O pedido vai a FRETE_SELECIONADO, com transportadora, preço e prazo."
)
$sol = "sol-" + $P1.Substring(0, 8)
Publicar "solicitacao_frete_criada" "logistica-service" $sol ([ordered]@{
        solicitacaoFreteId = $sol; pedidoId = $P1; status = "ABERTA" }) "Logística"
Publicar "frete_selecionado" "logistica-service" $P1 ([ordered]@{
        pedidoId = $P1; solicitacaoFreteId = $sol; entregaId = "ent-go-31"; cotacaoId = "cot-go-18"
        transportadoraId = "tra-go-8"; tentativa = 1; preco = 150.0; prazoDias = 2; status = "FRETE_SELECIONADO" }) "Logística"
$d = Esperar "frete selecionado" { $x = Api GET "/api/v1/pedidos/$P1" $COMPRADOR; if ($x.status -eq "FRETE_SELECIONADO") { $x } }
Ok "status $($d.status): transportadora $($d.frete.transportadoraId), R$ $($d.frete.preco), $($d.frete.prazoDias) dias"
Pausa

# ------------------------------------------------------------------ 7. entrega

Etapa 7 "Entrega concluída" @(
    "A Logística publica entrega_concluida. O pedido chega ao estado final ENTREGUE.",
    "O histórico guarda cada versão, com a origem da mudança."
)
Publicar "entrega_concluida" "logistica-service" $P1 ([ordered]@{
        pedidoId = $P1; entregaId = "ent-go-31"; transportadoraId = "tra-go-8"; entregueEm = (Agora) }) "Logística"
Esperar "entrega" { (Api GET "/api/v1/pedidos/$P1" $COMPRADOR).status -eq "ENTREGUE" } | Out-Null
Info "GET /api/v1/pedidos/$($P1.Substring(0,8))…/historico"
(Api GET "/api/v1/pedidos/$P1/historico" $COMPRADOR).items |
    Select-Object versao, statusAnterior, status, reservaSituacao, origem |
    Format-Table -AutoSize | Out-String -Width 200 | Write-Host
Pausa

# ------------------------------------------------------------------ 8. segurança e falhas

Etapa 8 "Segurança e mensagens inválidas" @(
    "Erros no formato RFC 7807 com codigo e correlationId; JWT validado pela chave pública (JWKS).",
    "Mensagem inválida no Kafka não trava o consumo: vai para a DLT pedidos.dlt."
)
$r = Tentar GET "/api/v1/pedidos" $null $null
Ok "sem token ............................ $($r.Status) $(($r.Corpo | ConvertFrom-Json).codigo)"
$r = Tentar GET "/api/v1/pedidos/$P1" $OUTRO $null
Ok "outro comprador abre o pedido ......... $($r.Status) $(($r.Corpo | ConvertFrom-Json).codigo)"
$r = Tentar PUT "/api/v1/pedidos/$P2/destino" $FORNECEDOR $corpo
Ok "fornecedor tenta informar o destino ... $($r.Status) $(($r.Corpo | ConvertFrom-Json).codigo)"
$r = Tentar PUT "/api/v1/pedidos/$P2/destino" $COMPRADOR '{"cidade":"Anápolis","uf":"go"}'
Ok "UF inválida ........................... $($r.Status) $(($r.Corpo | ConvertFrom-Json).codigo)"

$chave = "teste-dlt-" + [guid]::NewGuid().ToString().Substring(0, 8)
Publicar "frete_selecionado" "logistica-service" $chave ([ordered]@{ pedidoId = $chave }) "Teste (evento sem campos obrigatórios)"
$linha = Esperar "mensagem na DLT" {
    & docker @COMPOSE exec -T broker rpk topic consume pedidos.dlt -o :end -f "%k|%v\n" 2>$null |
        Where-Object { $_ -like "$chave|*" } | Select-Object -First 1 }
$dlt = ($linha -split "\|", 2)[1] | ConvertFrom-Json
Ok "pedidos.dlt recebeu: error $($dlt.payload.error), failedAtAttempt $($dlt.payload.failedAtAttempt)"
Pausa

# ------------------------------------------------------------------ fim

Etapa 9 "Pronto" @(
    "Swagger da API ........... $API/docs  (Authorize com um token de $EMISSOR/tokens)",
    "Tokens prontos ........... $EMISSOR/tokens  (comprador, fornecedor, outro comprador, suporte, expirado...)",
    "Métricas Prometheus ...... $API/metrics",
    "Mensagens no Kafka ....... http://localhost:18091  (Redpanda Console)",
    "Banco PostgreSQL ......... localhost:15433, banco portal_b2b, usuário demo_admin",
    "Logs de uma réplica ...... docker compose -f compose.demo.yml logs -f app1",
    "Testes automatizados ..... docker compose -f compose.demo.yml run --build --rm teste",
    "Parar a stack ............ docker compose -f compose.demo.yml stop"
)
