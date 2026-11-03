$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Force .local/tmp,.local/review | Out-Null
$env:TEMP = (Resolve-Path .local/tmp).Path
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = "$PWD/.local/cache"
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:DJANGO_SETTINGS_MODULE = 'config.settings'
$env:TEACH_STORAGE_BACKEND = 'local'
$env:TEACH_DATA_DIR = (Resolve-Path .local/review).Path
$env:TEACH_DB = Join-Path $env:TEACH_DATA_DIR 'school.sqlite3'
if (!(Test-Path .venv/Scripts/python.exe)) {
    python -m venv .venv
    if ($LASTEXITCODE) { throw '无法创建虚拟环境' }
}
& .venv/Scripts/python -m pip install -r requirements.txt
if ($LASTEXITCODE) { throw '依赖安装失败' }
& .venv/Scripts/python app/manage.py migrate --noinput
if ($LASTEXITCODE) { throw '迁移失败' }
& .venv/Scripts/python app/manage.py prepare_review
if ($LASTEXITCODE) { throw '验收数据准备失败' }
Write-Host '本机验收入口 http://127.0.0.1:8765/ 。按 Ctrl+C 停止。'
& .venv/Scripts/python app/manage.py runserver 127.0.0.1:8765 --noreload
