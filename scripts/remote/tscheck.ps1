$ts = "C:\Program Files\Tailscale\tailscale.exe"
if (-not (Test-Path $ts)) { $ts = "tailscale" }
& $ts status --json 2>&1 | Select-String -Pattern '"DNSName"|"Self"|MagicDNS' | Select-Object -First 3
Write-Output "--- cert capability ---"
& $ts cert --help 2>&1 | Select-Object -First 4
Write-Output "--- serve status ---"
& $ts serve status 2>&1 | Select-Object -First 6
