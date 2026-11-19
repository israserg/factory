# Stop the deployer MCP server. Does NOT stop mynews.
# Note: do not use "taskkill /T" here. mynews is a child of the deployer by parent-pid, so a tree kill would take it down too.
# Instead kill exactly the processes whose command line runs server.py from this repo (the venv launcher + the real python).
$root = Split-Path -Parent $PSScriptRoot
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'server\.py' -and $_.CommandLine -match [regex]::Escape($root) }
if (-not $procs) { Write-Host "Deployer not running"; exit 0 }
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item "C:\mywork\runtime\deployer.pid" -Force -ErrorAction SilentlyContinue
Write-Host "Deployer stopped (pids: $($procs.ProcessId -join ', '))"
