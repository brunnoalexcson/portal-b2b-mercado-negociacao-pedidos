# Funções comuns aos roteiros de demonstração (apresentar.ps1 e simular.ps1). Carregar com ponto:
#   . "$PSScriptRoot\demo\funcoes.ps1"
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot)
$utf8 = New-Object System.Text.UTF8Encoding $false
$OutputEncoding = $utf8
[Console]::OutputEncoding = $utf8

$API = "http://localhost:18107"
$EMISSOR = "http://localhost:18100"
$COMPOSE = @("compose", "-f", "compose.demo.yml")
# compose.portal.yml (telas) só existe na máquina de quem desenvolve; não é publicado.
if (Test-Path "compose.portal.yml") { $COMPOSE += @("-f", "compose.portal.yml") }
$PROD = "35000000-0000-4000-8000-000000000001"
$COMP = "b2000000-0000-4000-8000-000000000002"
$OUTRA = "c3000000-0000-4000-8000-000000000003"
$OFERTAS = @(
    @{ Nome = "F1 Rio Verde"; Id = "34000000-0000-4000-8000-000000000001"; Fornecedor = "a1000000-0000-4000-8000-000000000001"; Cidade = "Rio Verde"; Qtd = 5.0; Preco = 120.0 },
    @{ Nome = "F2 Jataí"; Id = "34000000-0000-4000-8000-000000000002"; Fornecedor = "a1000000-0000-4000-8000-000000000002"; Cidade = "Jataí"; Qtd = 15.0; Preco = 125.0 },
    @{ Nome = "F3 Mineiros"; Id = "34000000-0000-4000-8000-000000000003"; Fornecedor = "a1000000-0000-4000-8000-000000000003"; Cidade = "Mineiros"; Qtd = 10.0; Preco = 118.0 }
)

# ------------------------------------------------------------------ apresentação

function Etapa($numero, $titulo, $explicacao) {
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor DarkCyan
    Write-Host " $numero. $titulo" -ForegroundColor Cyan
    Write-Host ("=" * 78) -ForegroundColor DarkCyan
    foreach ($linha in $explicacao) { Write-Host "   $linha" -ForegroundColor Gray }
    Write-Host ""
}

function Info($texto) { Write-Host "   > $texto" -ForegroundColor Yellow }
function Ok($texto) { Write-Host "   OK  $texto" -ForegroundColor Green }

function Pausa {
    if (-not $SemPausa) { Write-Host ""; Read-Host "   [Enter] para a próxima etapa" | Out-Null }
}

function Falhar($texto) {
    Write-Host ""
    Write-Host "   ERRO: $texto" -ForegroundColor Red
    exit 1
}

# ------------------------------------------------------------------ infraestrutura

function Agora { (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'") }

function Publicar($topico, $fonte, $chave, $payload, $quem) {
    $evento = [ordered]@{
        eventId = [guid]::NewGuid().ToString(); eventType = $topico; eventVersion = "1.0"; timestamp = (Agora)
        source = $fonte; correlationId = "apresentacao"; aggregateId = $chave; payload = $payload
    }
    $json = $evento | ConvertTo-Json -Depth 10 -Compress
    $json | & docker @COMPOSE exec -T broker rpk topic produce $topico -k $chave -z none 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Falhar "não consegui publicar em $topico" }
    Write-Host "   $quem publicou " -NoNewline -ForegroundColor DarkGray
    Write-Host $topico -ForegroundColor White
}

function Token($usuario, $tenant, $escopo, [switch]$Admin) {
    $url = "$EMISSOR/token?usuario=$usuario&escopo=$escopo"
    if ($tenant) { $url += "&tenant=$tenant" }
    if ($Admin) { $url += "&admin=true" }
    $resposta = Invoke-RestMethod -Method Post -Uri $url
    return @{ Authorization = "Bearer " + $resposta.access_token }
}

function Api($metodo, $caminho, $cabecalhos, $corpo) {
    $req = @{ Method = $metodo; Uri = "$API$caminho"; Headers = $cabecalhos }
    if ($corpo) {
        $req.Body = $utf8.GetBytes($corpo)
        $req.ContentType = "application/json; charset=utf-8"
    }
    return Invoke-RestMethod @req
}

function Tentar($metodo, $caminho, $cabecalhos, $corpo) {
    # Devolve status HTTP e corpo, inclusive em erro.
    $req = @{ Method = $metodo; Uri = "$API$caminho"; UseBasicParsing = $true }
    if ($cabecalhos) { $req.Headers = $cabecalhos }
    if ($corpo) { $req.Body = $utf8.GetBytes($corpo); $req.ContentType = "application/json; charset=utf-8" }
    try {
        $r = Invoke-WebRequest @req
        return @{ Status = [int]$r.StatusCode; Corpo = $r.Content }
    } catch {
        $status = [int]$_.Exception.Response.StatusCode
        return @{ Status = $status; Corpo = $_.ErrorDetails.Message }
    }
}

function Esperar($descricao, [scriptblock]$condicao, $segundos = 30) {
    $fim = (Get-Date).AddSeconds($segundos)
    while ((Get-Date) -lt $fim) {
        try { $valor = & $condicao; if ($valor) { return $valor } } catch { }
        Start-Sleep -Milliseconds 500
    }
    Falhar "tempo esgotado esperando: $descricao"
}

function PedidosDaNegociacao {
    $lista = Api GET "/api/v1/pedidos?negociacaoId=$script:NEG&size=10" $script:SUPORTE
    $detalhes = @()
    foreach ($item in $lista.items) { $detalhes += Api GET "/api/v1/pedidos/$($item.pedidoId)" $script:SUPORTE }
    return $detalhes
}

function Tabela($pedidos) {
    $pedidos | Sort-Object fornecimentoId | ForEach-Object {
        [pscustomobject]@{
            Oferta     = ($OFERTAS | Where-Object Id -eq $_.fornecimentoId).Nome
            Pedido     = $_.pedidoId.Substring(0, 8)
            Status     = $_.status
            Reserva    = $_.reservaSituacao
            Comprador  = $(if ($_.compradorId) { "COMP" } else { "(nenhum)" })
            "Valor R$" = $_.valorTotal
            Motivo     = $_.motivoCancelamento
            Versao     = $_.versao
        }
    } | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
}

function Aviso($texto) { Write-Host "   !!  $texto" -ForegroundColor Magenta }

function IniciarStack([bool]$reiniciar) {
    & docker info --format "{{.ServerVersion}}" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Falhar "o Docker não está rodando. Abra o Docker Desktop, espere 'Engine running' e rode de novo." }
    if ($reiniciar) {
        Info "apagando os dados da demonstração anterior"
        & docker @COMPOSE down -v --remove-orphans 2>$null | Out-Null
    }
    Info "subindo a stack (a primeira vez demora alguns minutos)"
    & docker @COMPOSE up -d --build gateway console 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Falhar "docker compose up falhou. Rode 'docker compose -f compose.demo.yml up -d --build gateway' para ver o erro." }
    Esperar "prontidão do serviço" { (Invoke-RestMethod "$API/health/ready" -TimeoutSec 3).status -eq "ready" } 120 | Out-Null
}

function Sql($consulta) {
    # Consulta o banco da demonstração (somente leitura), uma linha por registro, campos separados por |.
    return & docker @COMPOSE exec -T db psql -U demo_admin -d portal_b2b -At -F "|" -c $consulta 2>$null
}

function Auditoria($negociacao, $quantos = 5) {
    $linhas = Sql ("SELECT to_char(instante AT TIME ZONE 'America/Sao_Paulo','HH24:MI:SS'), acao, " +
        "coalesce(left(pedido_id,8),'-'), detalhe->>'topico' FROM pedidos.audit_log " +
        "WHERE negociacao_id = '$negociacao' ORDER BY id DESC LIMIT $quantos")
    if (-not $linhas) { Write-Host "   (sem auditoria nesta negociação)" -ForegroundColor DarkGray; return }
    $linhas | ForEach-Object {
        $c = $_ -split "\|"
        [pscustomobject]@{ Hora = $c[0]; Acao = $c[1]; Pedido = $c[2]; Origem = $c[3] }
    } | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
}

function UltimasDlt($quantas = 3) {
    $linhas = & docker @COMPOSE exec -T broker rpk topic consume pedidos.dlt -o :end -f "%v\n" 2>$null
    $itens = @($linhas | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
    if (-not $itens) { Write-Host "   (pedidos.dlt vazia)" -ForegroundColor DarkGray; return }
    $itens | Sort-Object timestamp | Select-Object -Last $quantas | ForEach-Object {
        $original = $_.payload.originalEvent
        [pscustomobject]@{
            Quando  = $_.timestamp
            Erro    = $_.payload.error
            Topico  = $(if ($original -and $original.eventType) { $original.eventType } else { "?" })
            Chave   = $_.aggregateId
            Detalhe = $_.payload.stackTrace
        }
    } | Format-Table -AutoSize -Wrap | Out-String -Width 220 | Write-Host
}
