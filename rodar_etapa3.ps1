# Auto-restart da etapa 3: relança enquanto for interrompida (SIGINT espúrio do
# terminal/energia). Para quando a etapa concluir (exit 0). A etapa é resumível e
# crash-safe (fsync por linha), então relançar nunca perde nem reprocessa trabalho.
# Uso:  .\rodar_etapa3.ps1   (a partir da raiz do repositório)
$ErrorActionPreference = "Continue"
$tentativa = 0
while ($true) {
    $tentativa++
    Write-Host "=== etapa 3 — tentativa $tentativa ===" -ForegroundColor Cyan
    uv run python -m pesquisa_precos.etapas.e3_classificar --provedor openrouter --concurrency 8
    if ($LASTEXITCODE -eq 0) {
        Write-Host "=== etapa 3 concluída (exit 0). ===" -ForegroundColor Green
        break
    }
    Write-Host "=== interrompida (exit $LASTEXITCODE) — relançando em 3s... ===" -ForegroundColor Yellow
    Start-Sleep -Seconds 3
}
