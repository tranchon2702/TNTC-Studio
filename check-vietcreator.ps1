$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

Set-Location -LiteralPath $PSScriptRoot
$blockingIssues = 0

function Show-CommandStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string[]]$VersionArguments,
        [Parameter(Mandatory = $true)][bool]$Required
    )

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        $level = if ($Required) { "THIEU" } else { "TUY CHON" }
        $color = if ($Required) { "Red" } else { "Yellow" }
        Write-Host "[$level] $Name chua duoc cai hoac chua co trong PATH." -ForegroundColor $color
        if ($Required) {
            $script:blockingIssues += 1
        }
        return
    }

    $summary = & $command.Source @VersionArguments 2>$null | Select-Object -First 1
    Write-Host "[OK] $Name - $summary" -ForegroundColor Green
}

Write-Host "Kiem tra moi truong VietCreator Studio" -ForegroundColor Cyan
Show-CommandStatus -Name "uv" -VersionArguments @("--version") -Required $true
Show-CommandStatus -Name "ffmpeg" -VersionArguments @("-version") -Required $true
Show-CommandStatus -Name "ollama" -VersionArguments @("--version") -Required $false

if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpu = nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>$null | Select-Object -First 1
    Write-Host "[OK] GPU NVIDIA - $gpu" -ForegroundColor Green
} else {
    Write-Host "[TUY CHON] Khong tim thay NVIDIA GPU; phan dung van co the chay bang CPU." -ForegroundColor Yellow
}

if (Test-Path -LiteralPath ".venv\Scripts\python.exe") {
    $pythonVersion = & ".venv\Scripts\python.exe" --version 2>&1
    Write-Host "[OK] Moi truong du an - $pythonVersion" -ForegroundColor Green
} else {
    Write-Host "[CHUA CAI] Chay start-vietcreator.bat de tao moi truong du an." -ForegroundColor Yellow
}

if (Test-Path -LiteralPath "config.toml") {
    Write-Host "[OK] Da co cau hinh ca nhan config.toml." -ForegroundColor Green
} else {
    Write-Host "[CHUA TAO] Lan chay dau se tao config.toml tu cau hinh mau." -ForegroundColor Yellow
}

if ($blockingIssues -gt 0) {
    Write-Host "Moi truong con $blockingIssues thanh phan bat buoc chua san sang." -ForegroundColor Red
    exit 1
}

Write-Host "Moi truong toi thieu da san sang. Ollama chi can khi muon AI tu viet kich ban." -ForegroundColor Green
exit 0
