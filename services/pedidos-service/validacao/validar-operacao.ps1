param(
    [Parameter(Mandatory=$true)][string]$PastaServico,
    [Parameter(Mandatory=$true)][string]$Projeto,
    [Parameter(Mandatory=$true)][int]$Porta
)
$ErrorActionPreference = 'Stop'
if ($Projeto -ne 'pedidos-validacao-final') { throw 'Use somente a stack descartável pedidos-validacao-final.' }
$env:PEDIDOS_HTTP_PORT = [string]$Porta
$compose = Join-Path (Resolve-Path -LiteralPath $PastaServico) 'compose.demo.yml'
$base = "http://127.0.0.1:$Porta"
function Obter-Status([string]$Caminho) {
    return [int](Invoke-WebRequest -Uri "$base$Caminho" -SkipHttpErrorCheck -TimeoutSec 5).StatusCode
}
function Esperar-Prontidao([int]$Esperado) {
    $limite = [DateTime]::UtcNow.AddSeconds(45)
    do {
        if ((Obter-Status '/health/ready') -eq $Esperado) { return }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $limite)
    throw "Prontidão não atingiu HTTP $Esperado."
}
Esperar-Prontidao 200
foreach ($dependencia in @('broker','db')) {
    try {
        docker compose -p $Projeto -f $compose stop $dependencia
        if ($LASTEXITCODE -ne 0) { throw "Falha ao parar $dependencia" }
        Esperar-Prontidao 503
        $vivo = Obter-Status '/health'
        if ($vivo -ne 200) { throw "Liveness retornou $vivo" }
        Write-Output "$dependencia indisponivel: health=200; ready=503"
    } finally {
        docker compose -p $Projeto -f $compose start $dependencia
        if ($LASTEXITCODE -ne 0) { throw "Falha ao recuperar $dependencia" }
    }
    Esperar-Prontidao 200
    Write-Output "$dependencia restabelecido: ready=200"
}
