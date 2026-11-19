# Start the run console in the background (hidden): http://127.0.0.1:8800  Logs: C:\mywork\runtime\console.log
$root = Split-Path -Parent $PSScriptRoot
$runtime = "C:\mywork\runtime"
New-Item -ItemType Directory -Force $runtime | Out-Null
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'console\\app\.py' -and $_.CommandLine -match [regex]::Escape($root) }
if ($existing) { Write-Host "Console already running"; exit 0 }
$p = Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList "console\app.py" `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput "$runtime\console.log" -RedirectStandardError "$runtime\console.err.log"
Write-Host "Console started (pid $($p.Id)): http://127.0.0.1:8800"
