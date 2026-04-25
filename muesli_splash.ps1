param(
    [Parameter(Mandatory = $true)]
    [string]$Root,
    [Parameter(Mandatory = $true)]
    [string]$Token
)

$ErrorActionPreference = "SilentlyContinue"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$statusPath = Join-Path $Root (".launch_status_" + $Token + ".json")
$tracePath = Join-Path $Root (".launch_trace_" + $Token + ".log")
$iconPath = Join-Path $Root "assets\muesli-icon.png"
$started = Get-Date
$currentProgress = 5

function Add-LaunchTrace {
    param(
        [string]$Event,
        [string]$Detail = ""
    )
    try {
        $line = @(
            (Get-Date).ToString("o"),
            $Event,
            ($Detail -replace "`r|`n", " ").Trim()
        ) -join "`t"
        Add-Content -Path $tracePath -Value $line -Encoding UTF8
    } catch {
    }
}

Add-LaunchTrace -Event "splash_process_started" -Detail "PowerShell splash process booted."

$form = New-Object System.Windows.Forms.Form
$form.Text = "Launching Muesli"
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.ShowInTaskbar = $false
$form.TopMost = $true
$form.BackColor = [System.Drawing.ColorTranslator]::FromHtml("#f3f4f6")
$form.ClientSize = New-Object System.Drawing.Size(430, 210)
$form.DoubleBuffered = $true

$picture = New-Object System.Windows.Forms.PictureBox
$picture.Size = New-Object System.Drawing.Size(72, 72)
$picture.Location = New-Object System.Drawing.Point(28, 28)
$picture.SizeMode = "Zoom"
if (Test-Path $iconPath) {
    $picture.Image = [System.Drawing.Image]::FromFile($iconPath)
}
$form.Controls.Add($picture)

$title = New-Object System.Windows.Forms.Label
$title.Text = "Muesli"
$title.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 22)
$title.ForeColor = [System.Drawing.ColorTranslator]::FromHtml("#111827")
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(118, 34)
$form.Controls.Add($title)

$subtitle = New-Object System.Windows.Forms.Label
$subtitle.Text = "Starting Muesli..."
$subtitle.Font = New-Object System.Drawing.Font("Segoe UI", 10)
$subtitle.ForeColor = [System.Drawing.ColorTranslator]::FromHtml("#6b7280")
$subtitle.AutoSize = $true
$subtitle.Location = New-Object System.Drawing.Point(122, 78)
$form.Controls.Add($subtitle)

$detail = New-Object System.Windows.Forms.Label
$detail.Text = "Launching the Python process and splash screen."
$detail.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$detail.ForeColor = [System.Drawing.ColorTranslator]::FromHtml("#6b7280")
$detail.AutoSize = $false
$detail.Size = New-Object System.Drawing.Size(278, 36)
$detail.Location = New-Object System.Drawing.Point(122, 102)
$form.Controls.Add($detail)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Style = "Continuous"
$progress.Minimum = 0
$progress.Maximum = 100
$progress.Value = $currentProgress
$progress.Location = New-Object System.Drawing.Point(30, 148)
$progress.Size = New-Object System.Drawing.Size(370, 18)
$form.Controls.Add($progress)

$hint = New-Object System.Windows.Forms.Label
$hint.Text = "5% complete"
$hint.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$hint.ForeColor = [System.Drawing.ColorTranslator]::FromHtml("#6b7280")
$hint.AutoSize = $true
$hint.Location = New-Object System.Drawing.Point(30, 180)
$form.Controls.Add($hint)

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 150
$timer.Add_Tick({
    if (-not (Test-Path $statusPath)) {
        $timer.Stop()
        $form.Close()
        return
    }

    try {
        $payload = Get-Content -Path $statusPath -Raw | ConvertFrom-Json
        if ($payload.stage) {
            $subtitle.Text = [string]$payload.stage
        }
        if ($payload.detail) {
            $detail.Text = [string]$payload.detail
        }
        if ($null -ne $payload.progress) {
            $parsed = 0
            if ([int]::TryParse([string]$payload.progress, [ref]$parsed)) {
                $currentProgress = [Math]::Max(0, [Math]::Min(100, $parsed))
                $progress.Value = $currentProgress
                $hint.Text = "$currentProgress% complete"
            }
        }
        if ($payload.close -eq $true) {
            $progress.Value = 100
            $hint.Text = "100% complete"
            $timer.Stop()
            $form.Close()
            return
        }
    } catch {
    }

    if (((Get-Date) - $started).TotalSeconds -gt 90) {
        $timer.Stop()
        $form.Close()
    }
})

$form.Add_Shown({
    Add-LaunchTrace -Event "splash_window_shown" -Detail "Splash window reached the shown event."
    $timer.Start()
})

[void]$form.ShowDialog()

if ($picture.Image) {
    $picture.Image.Dispose()
}
