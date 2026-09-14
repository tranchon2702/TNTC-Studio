$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Chua tim thay uv. Cai uv roi chay lai: https://docs.astral.sh/uv/" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath ".venv")) {
    Write-Host "Dang cai moi truong Python lan dau..." -ForegroundColor Cyan
    uv sync --frozen
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

if (-not (Test-Path -LiteralPath "config.toml")) {
    Copy-Item -LiteralPath "config.example.toml" -Destination "config.toml"
}

Write-Host "VietCreator Studio: http://127.0.0.1:8501" -ForegroundColor Green
Write-Host "Nhan Ctrl+C trong cua so nay de dung ung dung." -ForegroundColor DarkGray
uv run streamlit run webui/Main.py --server.address 127.0.0.1 --server.port 8501
exit $LASTEXITCODE
