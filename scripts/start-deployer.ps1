# Start the deployer MCP server in the background (hidden). Logs: C:\mywork\runtime\deployer.log
$root = Split-Path -Parent $PSScriptRoot
$runtime = "C:\mywork\runtime"
New-Item -ItemType Directory -Force $runtime | Out-Null
$pidFile = Join-Path $runtime "deployer.pid"
if (Test-Path $pidFile) {
    $old = Get-Process -Id ([int](Get-Content $pidFile)) -ErrorAction SilentlyContinue
    if ($old) { Write-Host "Deployer already running (pid $($old.Id))"; exit 0 }
}
$p = Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList "server.py" `
    -WorkingDirectory "$root\mcp-deployer" -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput "$runtime\deployer.log" -RedirectStandardError "$runtime\deployer.err.log"
$p.Id | Set-Content $pidFile
Write-Host "Deployer started (pid $($p.Id)) on http://127.0.0.1:8765/mcp"
