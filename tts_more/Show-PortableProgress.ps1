[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OperationRoot,
    [string]$Url = "",
    [switch]$Console,
    [switch]$RequestCancel
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$OperationRoot = [IO.Path]::GetFullPath($OperationRoot)
$operationPath = Join-Path $OperationRoot "operation.json"
$eventsPath = Join-Path $OperationRoot "events.jsonl"
$cancelPath = Join-Path $OperationRoot "cancel.requested"

function Request-PortableCancellation {
    $stream = [IO.File]::Open($cancelPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
    $stream.Dispose()
}

function Test-PortableCancellationAvailable {
    param([string]$Phase)
    if ($Phase -in @("starting", "ready", "stopped", "repairable", "blocked")) { return $false }
    return $Phase -in @("not_initialized", "checking", "downloading", "installing", "validating")
}

function Read-PortableOperation {
    if (!(Test-Path -LiteralPath $operationPath -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $operationPath -Raw | ConvertFrom-Json } catch { return $null }
}

function Read-PortableEventDelta {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ref]$Offset,
        [Parameter(Mandatory = $true)][ref]$Carry
    )
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { return @() }
    [IO.FileStream]$stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    $reader = $null
    try {
        if ([int64]$Offset.Value -lt 0 -or [int64]$Offset.Value -gt $stream.Length) {
            $Offset.Value = [int64]0
            $Carry.Value = ""
        }
        [void]$stream.Seek([int64]$Offset.Value, [IO.SeekOrigin]::Begin)
        $reader = New-Object IO.StreamReader($stream, (New-Object Text.UTF8Encoding($false, $true)), $true, 4096, $true)
        $chunk = $reader.ReadToEnd()
        $Offset.Value = [int64]$stream.Position
    } finally {
        if ($reader) { $reader.Dispose() }
        $stream.Dispose()
    }
    $text = [string]$Carry.Value + [string]$chunk
    $lastNewline = $text.LastIndexOf("`n")
    if ($lastNewline -lt 0) {
        $Carry.Value = $text
        return @()
    }
    $complete = $text.Substring(0, $lastNewline + 1)
    $Carry.Value = $text.Substring($lastNewline + 1)
    $events = @()
    foreach ($line in $complete.Split("`n")) {
        $record = $line.TrimEnd("`r")
        if ([string]::IsNullOrWhiteSpace($record)) { continue }
        try { $events += ($record | ConvertFrom-Json) } catch { break }
    }
    return @($events)
}

$script:eventOffset = [int64]0
$script:eventCarry = ""

function Show-ConsoleProgress {
    Write-Host "Portable progress console: $OperationRoot"
    Write-Host "Press Ctrl+C to leave the display; create cancel.requested to cancel safely."
    $lastSequence = 0
    while ($true) {
        foreach ($event in @(Read-PortableEventDelta -Path $eventsPath -Offset ([ref]$script:eventOffset) -Carry ([ref]$script:eventCarry))) {
            if ([int]$event.seq -le $lastSequence) { continue }
            $lastSequence = [int]$event.seq
            $percent = if ($null -ne $event.PSObject.Properties["percent"]) { " [$($event.percent)%]" } else { "" }
            Write-Host "$($event.phase)$percent $($event.message)"
        }
        $operation = Read-PortableOperation
        if ($operation -and $null -ne $operation.exit_code) {
            if ([int]$operation.exit_code -eq 0 -and $Url) { Write-Host "Ready: $Url" }
            return [int]$operation.exit_code
        }
        Start-Sleep -Milliseconds 250
    }
}

if ($MyInvocation.InvocationName -eq ".") { return }

if ($RequestCancel) {
    Request-PortableCancellation
    Write-Host "Cancellation requested: $cancelPath"
    exit 0
}

if ($Console) { exit (Show-ConsoleProgress) }

try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing

    $form = New-Object Windows.Forms.Form
    $form.Text = "TTS More portable package progress"
    $form.StartPosition = "CenterScreen"
    $form.Size = New-Object Drawing.Size(620, 310)
    $form.MinimumSize = New-Object Drawing.Size(520, 260)

    $phaseLabel = New-Object Windows.Forms.Label
    $phaseLabel.AutoSize = $false
    $phaseLabel.Location = New-Object Drawing.Point(20, 18)
    $phaseLabel.Size = New-Object Drawing.Size(560, 48)
    $phaseLabel.Text = "Preparing startup..."
    $form.Controls.Add($phaseLabel)

    $progress = New-Object Windows.Forms.ProgressBar
    $progress.Location = New-Object Drawing.Point(20, 72)
    $progress.Size = New-Object Drawing.Size(560, 24)
    $progress.Style = "Marquee"
    $form.Controls.Add($progress)

    $details = New-Object Windows.Forms.TextBox
    $details.Location = New-Object Drawing.Point(20, 108)
    $details.Size = New-Object Drawing.Size(560, 90)
    $details.Multiline = $true
    $details.ReadOnly = $true
    $details.ScrollBars = "Vertical"
    $form.Controls.Add($details)

    $minimize = New-Object Windows.Forms.Button
    $minimize.Text = "Minimize"
    $minimize.Location = New-Object Drawing.Point(20, 214)
    $minimize.Add_Click({ $form.WindowState = "Minimized" })
    $form.Controls.Add($minimize)

    $background = New-Object Windows.Forms.Button
    $background.Text = "Run in background"
    $background.Location = New-Object Drawing.Point(125, 214)
    $background.Add_Click({ $form.WindowState = "Minimized" })
    $form.Controls.Add($background)

    $cancel = New-Object Windows.Forms.Button
    $cancel.Text = "Cancel"
    $cancel.Location = New-Object Drawing.Point(230, 214)
    $cancel.Add_Click({
        Request-PortableCancellation
        $cancel.Enabled = $false
        $phaseLabel.Text = "Safe cancellation requested; preserving downloaded data..."
    })
    $form.Controls.Add($cancel)

    $close = New-Object Windows.Forms.Button
    $close.Text = "Close"
    $close.Location = New-Object Drawing.Point(480, 214)
    $close.Enabled = $false
    $close.Add_Click({ $form.Close() })
    $form.Controls.Add($close)

    $lastSequence = 0
    $timer = New-Object Windows.Forms.Timer
    $timer.Interval = 250
    $timer.Add_Tick({
        foreach ($event in @(Read-PortableEventDelta -Path $eventsPath -Offset ([ref]$script:eventOffset) -Carry ([ref]$script:eventCarry))) {
            if ([int]$event.seq -le $lastSequence) { continue }
            $script:lastSequence = [int]$event.seq
            $phaseLabel.Text = [string]$event.message
            $cancel.Enabled = Test-PortableCancellationAvailable -Phase ([string]$event.phase)
            $details.AppendText("$($event.phase): $($event.message)`r`n")
            if ($null -ne $event.PSObject.Properties["percent"]) {
                $progress.Style = "Continuous"
                $progress.Value = [Math]::Max(0, [Math]::Min(100, [int]$event.percent))
            }
        }
        $operation = Read-PortableOperation
        if ($operation -and $null -ne $operation.exit_code) {
            $timer.Stop()
            $cancel.Enabled = $false
            $close.Enabled = $true
            if ([int]$operation.exit_code -eq 0) {
                $phaseLabel.Text = if ($Url) { "Service ready: $Url" } else { "Service ready" }
                $progress.Style = "Continuous"
                $progress.Value = 100
            } else {
                $phaseLabel.Text = "Startup did not complete. Check the error details and retry."
            }
        }
    })
    $timer.Start()
    [void]$form.ShowDialog()
    exit 0
} catch {
    Write-Warning "WinForms progress UI is unavailable; falling back to the console: $($_.Exception.Message)"
    exit (Show-ConsoleProgress)
}
