# Stop the run console (not the pipeline subprocess it may have started, and not mynews/deployer).
$root = Split-Path -Parent $PSScriptRoot
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'console\\app\.py' -and $_.CommandLine -match [regex]::Escape($root) }
if (-not $procs) { Write-Host "Console not running"; exit 0 }
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "Console stopped (pids: $($procs.ProcessId -join ', '))"
