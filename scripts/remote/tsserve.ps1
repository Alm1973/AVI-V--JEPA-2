# Put the dashboard behind Tailscale's HTTPS on the MagicDNS name.
#
# WHY: an Artifact page is served from claude.ai over HTTPS, and browsers
# refuse to load an http:// image into an https:// page. With a real cert the
# stream loads DIRECTLY from the MSI to the browser, so frames never pass
# through claude.ai and the privacy rule is intact.
#
# `serve` is TAILNET-ONLY. It is not `funnel`, which would publish to the open
# internet; the audience is exactly the devices that can already reach the
# HTTP dashboard today.
param([int]$Port = 8000, [switch]$Off)
$ts = "C:\Program Files\Tailscale\tailscale.exe"
if (-not (Test-Path $ts)) { $ts = "tailscale" }
if ($Off) { & $ts serve reset 2>&1; Write-Output "serve reset"; exit }

# The dashboard binds only the Tailscale IP -- it refuses 0.0.0.0 on purpose --
# so proxy to that address rather than to localhost.
$ip = (& $ts ip -4 2>&1 | Select-Object -First 1).Trim()
Write-Output "tailscale ip: $ip"
& $ts serve --bg --https=443 "http://${ip}:${Port}" 2>&1
Write-Output "--- serve status ---"
& $ts serve status 2>&1
