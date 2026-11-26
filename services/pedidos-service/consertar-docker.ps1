<#
Conserta o Docker Desktop quando ele fecha ao iniciar com "An unexpected error occurred" por causa de um
socket antigo que o Windows não deixa apagar (dockerInference, engine.sock). Acontece depois de reiniciar
o computador. Fecha o Docker, tira do caminho as pastas com sockets quebrados (o Docker recria) e abre de novo.

Uso (cmd ou PowerShell):  consertar-docker
#>
$ErrorActionPreference = "Continue"
$utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8

Write-Host "Fechando o Docker Desktop..." -ForegroundColor Cyan
Get-Process | Where-Object { $_.ProcessName -in @("Docker Desktop", "com.docker.backend", "com.docker.build", "docker") } |
    Stop-Process -Force -Confirm:$false -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

$marca = Get-Date -Format "yyyyMMdd-HHmmss"
foreach ($pasta in @("$env:LOCALAPPDATA\Docker\run", "$env:LOCALAPPDATA\docker-secrets-engine")) {
    if (Test-Path $pasta) {
        try {
            Rename-Item $pasta "$(Split-Path $pasta -Leaf).antigo-$marca" -ErrorAction Stop
            Write-Host "  pasta movida: $pasta" -ForegroundColor Gray
        } catch {
            Write-Host "  não consegui mover $pasta : $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
}
# As pastas antigas não servem para nada; apaga as que o Windows deixar.
Get-ChildItem $env:LOCALAPPDATA -Directory -Recurse -Depth 1 -Filter "*.antigo-*" -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -like "*Docker*" -or $_.FullName -like "*docker-secrets-engine*" } |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host "Abrindo o Docker Desktop e esperando o motor (até 3 min)..." -ForegroundColor Cyan
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
$fim = (Get-Date).AddMinutes(3)
while ((Get-Date) -lt $fim) {
    Start-Sleep -Seconds 5
    $job = Start-Job { docker version --format "{{.Server.Version}}" 2>$null }
    if (Wait-Job $job -Timeout 8) { $versao = Receive-Job $job } else { $versao = $null }
    Remove-Job $job -Force
    if ($versao) { Write-Host "Docker pronto (motor $versao)." -ForegroundColor Green; exit 0 }
}
Write-Host "O motor não subiu. Veja o erro em $env:LOCALAPPDATA\Docker\log\host\com.docker.backend.exe.log (procure 'GET /error')." -ForegroundColor Red
exit 1
