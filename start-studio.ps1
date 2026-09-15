$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Khong tim thay uv. Vui long cai dat uv tai: https://docs.astral.sh/uv/" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath "config.toml")) {
    if (Test-Path -LiteralPath "config.example.toml") {
        Copy-Item -LiteralPath "config.example.toml" -Destination "config.toml"
    }
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  TNTC Studio - AI Video Studio" -ForegroundColor Green
Write-Host "  WebUI: http://127.0.0.1:8501" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Nhan Ctrl+C de dung ung dung." -ForegroundColor DarkGray

uv run streamlit run webui/Main.py --server.address 127.0.0.1 --server.port 8501
exit $LASTEXITCODE
