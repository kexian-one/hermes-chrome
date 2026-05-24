param(
    [Parameter(Mandatory)][string]$Message,
    [string]$Title = "Claude Code",
    [ValidateSet("done", "question", "alert")][string]$Kind = "done"
)

$wavMap = @{
    "done"     = @{ file = "chimes.wav";  repeat = 1 }
    "question" = @{ file = "Alarm05.wav"; repeat = 2 }
    "alert"    = @{ file = "Alarm01.wav"; repeat = 3 }
}

$cfg = $wavMap[$Kind]
$wavPath = Join-Path $env:SystemRoot "Media\$($cfg.file)"

if (Test-Path $wavPath) {
    $player = New-Object Media.SoundPlayer $wavPath
    for ($i = 0; $i -lt $cfg.repeat; $i++) {
        $player.PlaySync()
    }
} else {
    for ($i = 0; $i -lt $cfg.repeat; $i++) {
        [Console]::Beep(1200, 400)
    }
}

$timeout = switch ($Kind) {
    "done"     { 5 }
    "question" { 60 }
    "alert"    { 60 }
    default    { 5 }
}

$icon = switch ($Kind) {
    "done"     { 64 }
    "question" { 32 }
    "alert"    { 48 }
    default    { 64 }
}

$wshell = New-Object -ComObject Wscript.Shell
[void]$wshell.Popup($Message, $timeout, $Title, $icon)
