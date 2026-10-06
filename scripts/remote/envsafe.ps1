# .env must be ignored by the MSI's own repo too. CLAUDE.md: avi\ is a flat
# folder AND a git repo, so a key pushed there is one `git add -A` from being
# committed on a machine whose history nobody is watching.
Set-Location "$env:USERPROFILE\avi"
$gi = ".gitignore"
if (-not (Test-Path $gi)) { New-Item -ItemType File $gi | Out-Null }
$lines = Get-Content $gi -ErrorAction SilentlyContinue
foreach ($pat in @(".env", ".env.local", "latents*.db")) {
  if ($lines -notcontains $pat) { Add-Content $gi $pat; Write-Output "added $pat to avi\.gitignore" }
}
& git check-ignore -v .env 2>&1
& git status --short .env 2>&1
