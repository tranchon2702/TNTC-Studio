$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Chưa tìm thấy uv. Hãy cài uv rồi chạy lại: https://docs.astral.sh/uv/" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath ".venv")) {
    Write-Host "Đang chuẩn bị môi trường Python..." -ForegroundColor Cyan
    uv sync --frozen
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

if (-not (Test-Path -LiteralPath "config.toml")) {
    Copy-Item -LiteralPath "config.example.toml" -Destination "config.toml"
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  🎬 TNTC Studio - AI Video Studio" -ForegroundColor Green
Write-Host "  🌐 Giao diện: http://127.0.0.1:8501" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Nhấn Ctrl+C trong cửa sổ này để dừng ứng dụng." -ForegroundColor DarkGray

uv run streamlit run webui/Main.py --server.address 127.0.0.1 --server.port 8501
exit $LASTEXITCODE
