[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

Add-Type -AssemblyName System.Windows.Forms
$dialog = [System.Windows.Forms.FolderBrowserDialog]::new()
$dialog.Description = "选择旧版 TTS More、GPT-SoVITS、IndexTTS 或 CosyVoice 便携包文件夹"
$dialog.ShowNewFolderButton = $false
try {
    $result = $dialog.ShowDialog()
    if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Out.WriteLine((@{ selected_path = [string]$dialog.SelectedPath } | ConvertTo-Json -Compress))
    } else {
        [Console]::Out.WriteLine('{"cancelled":true}')
    }
} finally {
    $dialog.Dispose()
}
