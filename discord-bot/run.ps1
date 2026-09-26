# ReCoder Discord Bot - Windows 실행 스크립트 (run.sh 와 같은 동작)
#
# 사용법 (PowerShell, discord-bot 폴더에서):
#   powershell -ExecutionPolicy Bypass -File .\run.ps1          # 봇 실행
#   powershell -ExecutionPolicy Bypass -File .\run.ps1 test     # 단위 테스트
#
# 사전 조건: Python 3.10 이상, .env 에 DISCORD_BOT_TOKEN
param([string]$Command = "run")
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venv = Join-Path $PSScriptRoot ".venv"
$onWindows = ($env:OS -eq "Windows_NT")
$python = if ($onWindows) { Join-Path $venv "Scripts\python.exe" } else { Join-Path $venv "bin/python" }

if (-not (Test-Path $python)) {
    Write-Host "가상환경 생성 중..."
    if ($onWindows -and (Get-Command py -ErrorAction SilentlyContinue)) { & py -3 -m venv $venv }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv $venv }
    else { & python3 -m venv $venv }
    if ($LASTEXITCODE -ne 0) { Write-Error "Python 3.10 이상을 설치한 뒤 다시 실행하세요."; exit 1 }
}

$stamp = Join-Path $venv ".deps_installed"
$needsInstall = -not (Test-Path $stamp)
if (-not $needsInstall) { $needsInstall = (Get-Item "requirements.txt").LastWriteTime -gt (Get-Item -Force $stamp).LastWriteTime }
if ($needsInstall) {
    Write-Host "의존성 설치 중..."
    & $python -m pip install -q --upgrade pip
    & $python -m pip install -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    New-Item -ItemType File -Path $stamp -Force | Out-Null
}

if ($Command -eq "test") {
    & $python -m pip install -q -r requirements-dev.txt
    & $python -m pytest tests -q
    exit $LASTEXITCODE
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env 를 만들었습니다. 메모장으로 열어 DISCORD_BOT_TOKEN 을 채운 뒤 다시 실행하세요."
    exit 1
}
if (-not (Select-String -Path ".env" -Pattern '^DISCORD_BOT_TOKEN=.+' -Quiet)) {
    Write-Host ".env 의 DISCORD_BOT_TOKEN 이 비어 있습니다. Developer Portal -> Bot -> Reset Token 값을 넣으세요."
    exit 1
}

Write-Host "ReCoder Discord Bot 시작... (중지: Ctrl+C)"
& $python bot.py
exit $LASTEXITCODE
