<#
Simulador interativo do pedidos-service, para a apresentação.

Cada opção do menu é um acontecimento de outro serviço (Negociação, Fornecimento, Logística) ou do
comprador. O simulador publica o evento de teste no Kafka, no formato combinado (PROF-05), e mostra
na hora o que o Pedidos fez: os pedidos da negociação e a auditoria.

Uso (cmd ou PowerShell, na pasta do serviço):
  simular                         menu interativo
  simular -Passos "1,3,T,5,8,1"   responde o menu automaticamente (ensaio)
#>
param([string]$Passos = "")

. "$PSScriptRoot\demo\funcoes.ps1"

$fila = New-Object System.Collections.Queue
foreach ($p in ($Passos -split ",")) { if ($p.Trim()) { $fila.Enqueue($p.Trim()) } }

function Perguntar($texto, $padrao = "") {
    if ($fila.Count -gt 0) {
        $r = $fila.Dequeue(); Write-Host "   $texto $r" -ForegroundColor DarkGray
        if ($r -eq "-") { return $padrao }  # "-" no -Passos = aceitar o padrão
        return $r
    }
    $r = Read-Host ("   " + $texto.TrimEnd(":"))  # o Read-Host já põe os dois-pontos
    if (-not $r) { return $padrao }
    return $r.Trim()
}

# ------------------------------------------------------------------ estado da sessão

$S = @{ Neg = $null; Tipo = $null; Ids = @{}; Sol = @{}; Rodada = @{}; Tentativa = @{}; Entrega = @{}; Transp = @{} }

function Oferta($n) { return $OFERTAS[[int]$n - 1] }

function EscolherPedidos($texto, [switch]$Todas) {
    if (-not $S.Neg) { Aviso "abra uma negociação primeiro (opção 1 ou 2)"; return @() }
    $disponiveis = @($S.Ids.Keys | Sort-Object)
    $sufixo = $(if ($Todas) { " ou T = todas" } else { "" })
    $r = Perguntar "$texto [$($disponiveis -join '/')$sufixo]:" ($disponiveis[0])
    if ($Todas -and $r -match "^[Tt]") { return $disponiveis }
    if ($S.Ids.ContainsKey([int]$r)) { return @([int]$r) }
    Aviso "oferta $r não tem pedido nesta negociação"
    return @()
}

function Retrato {
    $p = @(PedidosDaNegociacao)
    $auditoria = Sql "SELECT coalesce(max(id),0) FROM pedidos.audit_log"
    return (($p | ForEach-Object { "$($_.pedidoId):$($_.versao):$($_.atualizadoEm)" }) -join ",") + "|$auditoria"
}

function Mostrar($antes) {
    # Espera o Pedidos processar (até 5 s) e mostra os pedidos e a auditoria da negociação.
    if ($antes) {
        $fim = (Get-Date).AddSeconds(5)
        while ((Get-Date) -lt $fim -and (Retrato) -eq $antes) { Start-Sleep -Milliseconds 400 }
        Start-Sleep -Milliseconds 300
    }
    if (-not $S.Neg) { return }
    $pedidos = @(PedidosDaNegociacao)
    if ($pedidos.Count -eq 0) { Write-Host "   (nenhum pedido nesta negociação)" -ForegroundColor DarkGray }
    else { Tabela $pedidos }
    Write-Host "   Auditoria recente:" -ForegroundColor DarkGray
    Auditoria $S.Neg 4
}

# ------------------------------------------------------------------ ações

function AbrirNegociacao($tipo, $quais) {
    $S.Neg = [guid]::NewGuid().ToString(); $S.Tipo = $tipo
    foreach ($chave in @("Ids", "Sol", "Rodada", "Tentativa", "Entrega", "Transp")) { $S[$chave] = @{} }
    $script:NEG = $S.Neg
    Info "negociação $($S.Neg.Substring(0,8)) ($tipo) com as ofertas $($quais -join ', ')"
    foreach ($n in $quais) {
        $o = Oferta $n
        Publicar "pedido_temporario_solicitado" "negociacao-service" $S.Neg ([ordered]@{
                negociacaoId = $S.Neg; tipo = $tipo; fornecimentoId = $o.Id; fornecedorId = $o.Fornecedor
                produtoId = $PROD; quantidade = $o.Qtd; precoUnitario = $o.Preco }) "Negociação"
    }
    $pedidos = Esperar "pedidos temporários" { $p = @(PedidosDaNegociacao); if ($p.Count -eq $quais.Count) { $p } } 20
    foreach ($p in $pedidos) {
        for ($i = 1; $i -le 3; $i++) { if ((Oferta $i).Id -eq $p.fornecimentoId) { $S.Ids[$i] = $p.pedidoId } }
    }
}

function Reservar($recusar) {
    foreach ($n in (EscolherPedidos "Qual oferta?" -Todas)) {
        $o = Oferta $n
        if ($recusar) {
            Publicar "reserva_estoque_recusada" "fornecimentos-service" $S.Ids[$n] ([ordered]@{
                    pedidoId = $S.Ids[$n]; fornecimentoId = $o.Id; quantidadeSolicitada = $o.Qtd
                    quantidadeDisponivel = 2.0; motivo = "ESTOQUE_INSUFICIENTE"; recusadoEm = (Agora) }) "Fornecimento (recusa)"
        } else {
            Publicar "estoque_atualizado" "fornecimentos-service" $o.Id ([ordered]@{
                    fornecimentoId = $o.Id; produtoId = $PROD; fornecedorId = $o.Fornecedor; operacao = "RESERVA"
                    quantidadeMovimentada = $o.Qtd; quantidadeDisponivel = 100.0; quantidadeReservada = $o.Qtd
                    pedidoId = $S.Ids[$n]; atualizadoEm = (Agora) }) "Fornecimento (reserva)"
        }
    }
}

function Fechar($status, [switch]$Incompativel) {
    if (-not $S.Neg) { Aviso "abra uma negociação primeiro"; return }
    $alocacoes = @()
    $comprador = $null
    if ($status -eq "ENCERRADO") {
        $comprador = $COMP
        $vencedoras = $(if ($S.Tipo -eq "VENDA_DIRETA") { @(1) } else { @(1, 2) })
        foreach ($n in $vencedoras) {
            if (-not $S.Ids.ContainsKey($n)) { continue }
            $o = Oferta $n
            $qtd = $(if ($Incompativel -and $n -eq 1) { $o.Qtd - 1 } else { $o.Qtd })
            $alocacoes += [ordered]@{ fornecimentoId = $o.Id; fornecedorId = $o.Fornecedor; pedidoId = $S.Ids[$n]
                quantidade = $qtd; valorUnitario = 130.0; valorTotal = $o.Qtd * 130.0 }
        }
        Info "vencedor: comprador COMP, R$ 130,00/t, alocações: $(($alocacoes | ForEach-Object { (Oferta (1 + [array]::IndexOf(@($OFERTAS.Id), $_.fornecimentoId))).Nome }) -join ', ')"
        if ($Incompativel) { Aviso "a alocação de F1 vai com quantidade errada: o fechamento inteiro deve ir para a DLT" }
    }
    Publicar "negociacao_fechada" "negociacao-service" $S.Neg ([ordered]@{
            negociacaoId = $S.Neg; tipo = $S.Tipo; status = $status; compradorId = $comprador; produtoId = $PROD
            demandaId = $null; alocacoes = $alocacoes }) "Negociação ($status)"
}

function Destino {
    foreach ($n in (EscolherPedidos "Pedido de qual oferta?")) {
        $cidade = Perguntar "Cidade [Anápolis]:" "Anápolis"
        $uf = Perguntar "UF [GO]:" "GO"
        $data = Perguntar "Data desejada AAAA-MM-DD [vazio = sem data]:" ""
        $corpo = [ordered]@{ cidade = $cidade; uf = $uf }
        if ($data) { $corpo.dataEntregaDesejada = $data }
        $json = $corpo | ConvertTo-Json -Compress
        Info "PUT /api/v1/pedidos/$($S.Ids[$n].Substring(0,8))…/destino $json  (token do comprador)"
        $r = Tentar PUT "/api/v1/pedidos/$($S.Ids[$n])/destino" $script:COMPRADOR $json
        $corpoResp = $r.Corpo | ConvertFrom-Json
        if ($r.Status -eq 200) { Ok "$($r.Status): status $($corpoResp.status), versão $($corpoResp.versao)" }
        else { Aviso "$($r.Status) $($corpoResp.codigo): $($corpoResp.detail)" }
    }
}

function SelecionarFrete($trocar) {
    foreach ($n in (EscolherPedidos "Pedido de qual oferta?")) {
        $pid8 = $S.Ids[$n].Substring(0, 8)
        if (-not $S.Sol.ContainsKey($n)) {
            $S.Rodada[$n] = 1 + [int]$S.Rodada[$n]
            $S.Sol[$n] = "sol-$pid8-$($S.Rodada[$n])"
            $S.Tentativa[$n] = 0
            Publicar "solicitacao_frete_criada" "logistica-service" $S.Sol[$n] ([ordered]@{
                    solicitacaoFreteId = $S.Sol[$n]; pedidoId = $S.Ids[$n]; status = "ABERTA" }) "Logística (abre cotação)"
        } elseif (-not $trocar -and $S.Tentativa[$n] -gt 0) {
            Aviso "este pedido já tem frete; use a opção de troca de transportadora"
            return
        }
        $S.Tentativa[$n] = 1 + [int]$S.Tentativa[$n]
        $t = $S.Tentativa[$n]
        $S.Transp[$n] = $(if ($t % 2 -eq 1) { "tra-go-8" } else { "tra-go-9" })
        $S.Entrega[$n] = "ent-$pid8-$($S.Rodada[$n])-$t"
        Publicar "frete_selecionado" "logistica-service" $S.Ids[$n] ([ordered]@{
                pedidoId = $S.Ids[$n]; solicitacaoFreteId = $S.Sol[$n]; entregaId = $S.Entrega[$n]
                cotacaoId = "cot-$pid8-$t"; transportadoraId = $S.Transp[$n]; tentativa = $t
                preco = (140.0 + 10 * $t); prazoDias = 2; status = "FRETE_SELECIONADO" }) "Logística (tentativa $t, $($S.Transp[$n]))"
    }
}

function FreteIndisponivel {
    foreach ($n in (EscolherPedidos "Pedido de qual oferta?")) {
        if (-not $S.Sol.ContainsKey($n)) {
            $S.Rodada[$n] = 1 + [int]$S.Rodada[$n]
            $S.Sol[$n] = "sol-$($S.Ids[$n].Substring(0,8))-$($S.Rodada[$n])"
            Publicar "solicitacao_frete_criada" "logistica-service" $S.Sol[$n] ([ordered]@{
                    solicitacaoFreteId = $S.Sol[$n]; pedidoId = $S.Ids[$n]; status = "ABERTA" }) "Logística (abre cotação)"
        }
        Publicar "frete_indisponivel" "logistica-service" $S.Ids[$n] ([ordered]@{
                pedidoId = $S.Ids[$n]; solicitacaoFreteId = $S.Sol[$n]; motivo = "SEM_COTACAO"; ocorridoEm = (Agora)
                detalhe = "Nenhuma transportadora respondeu" }) "Logística (sem cotação)"
        Info "a próxima seleção de frete abre uma nova cotação (nova solicitação)"
        $S.Sol.Remove($n)
    }
}

function Entregar {
    foreach ($n in (EscolherPedidos "Pedido de qual oferta?")) {
        $entrega = $(if ($S.Entrega[$n]) { $S.Entrega[$n] } else { "ent-sem-frete" })
        $transp = $(if ($S.Transp[$n]) { $S.Transp[$n] } else { "tra-go-8" })
        Publicar "entrega_concluida" "logistica-service" $S.Ids[$n] ([ordered]@{
                pedidoId = $S.Ids[$n]; entregaId = $entrega; transportadoraId = $transp; entregueEm = (Agora) }) "Logística (entrega $entrega)"
    }
}

function EventoInvalido {
    $chave = "teste-dlt-" + [guid]::NewGuid().ToString().Substring(0, 8)
    Publicar "frete_selecionado" "logistica-service" $chave ([ordered]@{ pedidoId = $chave }) "Teste (evento sem campos obrigatórios)"
}

function ConsultarComo {
    if (-not $S.Neg) { Aviso "abra uma negociação primeiro"; return }
    $perfis = [ordered]@{
        "Comprador COMP"         = $script:COMPRADOR
        "Fornecedor F1"          = $script:FORNECEDOR
        "Outra empresa (BUYER)"  = $script:OUTRO
        "Suporte (PLATFORM_ADMIN)" = $script:SUPORTE
    }
    $perfis.GetEnumerator() | ForEach-Object {
        $r = Api GET "/api/v1/pedidos?negociacaoId=$($S.Neg)" $_.Value
        [pscustomobject]@{ Perfil = $_.Key; "Pedidos visíveis" = $r.total
            Status = (($r.items | ForEach-Object { $_.status }) -join ", ") }
    } | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
    $r = Tentar GET "/api/v1/pedidos" $script:TRANSPORTADORA $null
    Ok "Transportadora (CARRIER) ......... $($r.Status) $(($r.Corpo | ConvertFrom-Json).codigo)"
    $r = Tentar GET "/api/v1/pedidos" $null $null
    Ok "Sem token ........................ $($r.Status) $(($r.Corpo | ConvertFrom-Json).codigo)"
}

function Historico {
    foreach ($n in (EscolherPedidos "Pedido de qual oferta?")) {
        Info "GET /api/v1/pedidos/$($S.Ids[$n].Substring(0,8))…/historico"
        (Api GET "/api/v1/pedidos/$($S.Ids[$n])/historico?size=50" $script:SUPORTE).items |
            Select-Object versao, statusAnterior, status, reservaSituacao, origem, motivo |
            Format-Table -AutoSize | Out-String -Width 200 | Write-Host
    }
}

function Replica($ligar) {
    if ($ligar) {
        Info "docker compose start app1"
        & docker @COMPOSE start app1 2>$null | Out-Null
        Esperar "app1 de volta" { (& docker @COMPOSE ps app1 --format "{{.State}}" 2>$null) -eq "running" } 30 | Out-Null
        Start-Sleep -Seconds 3
    } else {
        Info "docker compose stop app1  (a réplica 1 cai)"
        & docker @COMPOSE stop app1 2>$null | Out-Null
    }
    & docker @COMPOSE ps app1 app2 --format "table {{.Service}}\t{{.State}}" 2>$null | Out-String | Write-Host
    $respostas = 1..6 | ForEach-Object { (Tentar GET "/api/v1/pedidos?size=1" $script:SUPORTE $null).Status }
    Ok "6 consultas pelo gateway: $($respostas -join ', ')"
    if (-not $ligar) {
        Info "o Nginx manda tudo para a app2 e o Kafka passa as partições da app1 para ela"
        Info "continue o fluxo (reserva, fechamento, frete): a app2 processa sozinha"
    }
}

# ------------------------------------------------------------------ início

Write-Host ""
Write-Host "  Simulador do Pedidos: preparando o ambiente..." -ForegroundColor Cyan
IniciarStack $false
$script:SUPORTE = Token "usr-admin" $null "BUYER" -Admin
$script:COMPRADOR = Token "usr-comp" $COMP "BUYER"
$script:OUTRO = Token "usr-outra-b" $OUTRA "BUYER"
$script:FORNECEDOR = Token "usr-f1" $OFERTAS[0].Fornecedor "SUPPLIER"
$script:TRANSPORTADORA = Token "usr-carrier" $OUTRA "CARRIER"
Publicar "produto_cadastrado" "produtos-service" $PROD ([ordered]@{
        produtoId = $PROD; nome = "Milho em grão"; unidade = "TON"; tipoTransporte = "GRANEL"; pesoUnitarioKg = $null }) "Produtos"
foreach ($o in $OFERTAS) {
    Publicar "fornecimento_criado" "fornecimentos-service" $o.Id ([ordered]@{
            fornecimentoId = $o.Id; fornecedorId = $o.Fornecedor; produtoId = $PROD; unidade = "TON"
            origem = [ordered]@{ cidade = $o.Cidade; uf = "GO" } }) "Fornecimento"
}
Ok "pronto: produto Milho em grão e ofertas F1 (5 t, R$ 120), F2 (15 t, R$ 125) e F3 (10 t, R$ 118)"
Info "mensagens: http://localhost:18091  |  API: $API/docs"
if (Test-Path "compose.portal.yml") { Info "telas: $API/portal/" }

$MENU = @"

  NEGOCIAÇÃO (o que a Negociação publicaria)
    1  Abrir leilão conjunto (F1, F2, F3)    -> pedidos temporários, sem comprador
    2  Abrir venda direta (só F1)
    5  Encerrar com vencedor                  -> VENDA_CONCLUIDA / F3 fora do lote
    6  Leilão deserto (sem lances)            -> todos CANCELADO
    7  Fechamento incompatível                -> rejeitado inteiro, vai para a DLT
  FORNECIMENTO
    3  Confirmar reserva                      -> reserva CONFIRMADA
    4  Recusar reserva (sem estoque)          -> pedido CANCELADO
  COMPRADOR (API)
    8  Informar o destino (PUT /destino)      -> AGUARDANDO_FRETE
  LOGÍSTICA
    9  Selecionar frete                       -> FRETE_SELECIONADO
   10  Trocar de transportadora               -> nova versão, mesmo status
   11  Frete indisponível                     -> não cancela; abre nova cotação
   12  Entrega concluída                      -> ENTREGUE
  CONSULTAS E FALHAS
   13  Ver pedidos da negociação         14  Histórico de um pedido
   15  Quem vê o quê (por perfil)        16  Auditoria da negociação
   17  Enviar evento inválido (DLT)      18  Ver últimas mensagens da DLT
  RESILIÊNCIA (duas réplicas atrás do gateway)
   19  Derrubar a réplica app1           20  Religar a app1
    0  Sair
"@

while ($true) {
    $titulo = $(if ($S.Neg) { "negociação $($S.Neg.Substring(0,8)) ($($S.Tipo))" } else { "nenhuma negociação aberta" })
    Write-Host ""
    Write-Host ("-" * 78) -ForegroundColor DarkCyan
    Write-Host "  Simulador do Pedidos — $titulo" -ForegroundColor Cyan
    Write-Host ("-" * 78) -ForegroundColor DarkCyan
    Write-Host $MENU -ForegroundColor Gray
    $opcao = Perguntar "Opção:"
    Write-Host ""
    $antes = $(if ($S.Neg) { Retrato } else { $null })
    switch ($opcao) {
        "1" { AbrirNegociacao "LEILAO" @(1, 2, 3); Mostrar $null }
        "2" { AbrirNegociacao "VENDA_DIRETA" @(1); Mostrar $null }
        "3" { Reservar $false; Mostrar $antes }
        "4" { Reservar $true; Mostrar $antes }
        "5" { Fechar "ENCERRADO"; Mostrar $antes }
        "6" { Fechar "DESERTO"; Mostrar $antes }
        "7" { Fechar "ENCERRADO" -Incompativel; Mostrar $antes; Write-Host "   pedidos.dlt:" -ForegroundColor DarkGray; UltimasDlt 1 }
        "8" { Destino; Mostrar $null }
        "9" { SelecionarFrete $false; Mostrar $antes }
        "10" { SelecionarFrete $true; Mostrar $antes }
        "11" { FreteIndisponivel; Mostrar $antes }
        "12" { Entregar; Mostrar $antes }
        "13" { Mostrar $null }
        "14" { Historico }
        "15" { ConsultarComo }
        "16" { if ($S.Neg) { Auditoria $S.Neg 20 } else { Aviso "abra uma negociação primeiro" } }
        "17" { EventoInvalido; Start-Sleep -Seconds 2; UltimasDlt 1 }
        "18" { UltimasDlt 5 }
        "19" { Replica $false }
        "20" { Replica $true }
        "0" { Write-Host "   até mais" -ForegroundColor Gray; exit 0 }
        default { Aviso "opção inválida" }
    }
    if ($fila.Count -eq 0 -and $Passos) { exit 0 }
}
