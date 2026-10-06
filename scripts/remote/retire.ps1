# Move the pre-launch latent store out of the live filename.
# Launch day means AVI's memory starts empty; a store that already holds
# development data was never born, it was restored from a backup.
$a = "$env:USERPROFILE\avi\assistant"
if (Test-Path "$a\latents.db") {
  $dest = "$a\latents-dev-night1.db"
  if (Test-Path $dest) { Remove-Item $dest -Force }
  Move-Item "$a\latents.db" $dest
  Write-Output "retired latents.db -> latents-dev-night1.db"
} else {
  Write-Output "no live latents.db present; launch day already starts empty"
}
Get-ChildItem "$a\latents*.db" | ForEach-Object { "{0,12:N0}  {1}" -f $_.Length, $_.Name }
