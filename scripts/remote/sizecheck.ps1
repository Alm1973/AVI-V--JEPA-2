# Compare every deployed file against the size the Mac sent.
#
# Verification has to be independent of the transport's own exit status. A
# previous deploy reported success per file while silently truncating at the
# Windows command-line limit, and the only reason anyone noticed was two
# survey runs spent debugging code that was never there.
param([string]$Manifest = "$env:USERPROFILE\avi-remote\manifest.tsv")
$home_ = $env:USERPROFILE
$bad = 0; $ok = 0; $missing = 0
foreach ($line in Get-Content -LiteralPath $Manifest) {
    if (-not $line.Trim()) { continue }
    $parts = $line -split "`t"
    if ($parts.Count -lt 2) { continue }
    $rel = $parts[0]; $want = [int]$parts[1]
    $full = Join-Path $home_ ($rel -replace '/', '\')
    if (-not (Test-Path -LiteralPath $full)) {
        Write-Output "  MISSING  $rel"; $missing++; continue
    }
    $got = (Get-Item -LiteralPath $full).Length
    if ($got -ne $want) {
        Write-Output "  MISMATCH $rel  sent $want, on disk $got"; $bad++
    } else { $ok++ }
}
Write-Output "$ok match, $bad wrong size, $missing missing"
if ($bad -gt 0 -or $missing -gt 0) { exit 1 }
