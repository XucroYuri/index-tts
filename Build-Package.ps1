$ErrorActionPreference = "Stop"
try {
    & "$PSScriptRoot\tts_more\Build-Package.ps1" @args
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
exit 0
