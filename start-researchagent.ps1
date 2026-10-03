$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv-v3\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先按 docs/context-memory.md 创建运行环境。' }
try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2
    if ($health.status -eq 'ok') { Write-Output 'ResearchAgent 已运行：http://127.0.0.1:8000/'; return }
} catch {}
$process = Start-Process -FilePath $pythonPath -ArgumentList '-B','server.py' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'data/server-v3.out.log') -RedirectStandardError (Join-Path $projectRoot 'data/server-v3.err.log')
$process.Id | Set-Content -LiteralPath (Join-Path $projectRoot 'data/server-v3.pid')
Write-Output 'ResearchAgent 正在启动：http://127.0.0.1:8000/。日志位于 data/server-v3.err.log。'
