[CmdletBinding()]
param([ValidateSet("Auto", "CU128", "CU126", "CPU")][string]$Device = "Auto")

& (Join-Path $PSScriptRoot "Initialize.ps1") -Device $Device -Repair
exit $LASTEXITCODE
