# What persistent stores exist on the MSI, and how big.
foreach ($dir in @("$env:USERPROFILE\avi\assistant", "$env:USERPROFILE\avi", "$env:USERPROFILE\avi-remote")) {
  Get-ChildItem "$dir\*.db","$dir\*.sqlite3","$dir\*.csv" -ErrorAction SilentlyContinue |
    ForEach-Object { "{0,12:N0}  {1}" -f $_.Length, $_.FullName.Replace($env:USERPROFILE, "~") }
}
