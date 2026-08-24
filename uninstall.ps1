<#
.SYNOPSIS
    Uninstall flex-wavelog: stop the app, remove the autostart task, shortcut,
    and WebView2 profile.

.DESCRIPTION
    Removes everything install.ps1 created plus the runtime leftovers a user
    would never think to clean up:

      - the "Flex-Wavelog CAT Bridge" scheduled task (else it keeps starting
        the app at every logon)
      - the Start Menu shortcut
      - the .webview profile directory (holds a live Wavelog session cookie)
      - rotated log files

    config.json is KEPT by default because it contains your Wavelog API token -
    silently deleting a credential is worse than leaving a file. Pass -Purge to
    delete it too. The pywebview pip package is also left alone: other tools may
    use it, and removing shared packages behind the user's back is bad manners.
    Remove it yourself with:  python -m pip uninstall pywebview

.EXAMPLE
    .\uninstall.ps1
    .\uninstall.ps1 -Purge   # also delete config.json (contains your API token)
#>

param(
    [switch]$Purge
)

$ErrorActionPreference = 'Stop'

$Root     = $PSScriptRoot
$TaskName = 'Flex-Wavelog CAT Bridge'

function Say([string]$m) { Write-Host "  $m" }

Write-Host "`nflex-wavelog uninstaller`n"

# --- Stop the running app ----------------------------------------------------
# Match on command line, not process name - killing every pythonw.exe on the
# box would take down unrelated tools.
$appPy = (Join-Path $Root 'app.py')
$procs = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*$appPy*" -or $_.CommandLine -like '*flex_wavelog.py*' }
if ($procs) {
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Say "Stopped running app (pid $(($procs.ProcessId) -join ', '))"
} else {
    Say "App not running"
}

# --- Scheduled task ----------------------------------------------------------
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Say "Scheduled task '$TaskName' removed"
} else {
    Say "Scheduled task not present"
}

# --- Start Menu shortcut -----------------------------------------------------
$lnkPath = Join-Path ([Environment]::GetFolderPath('Programs')) 'Flex-Wavelog.lnk'
if (Test-Path $lnkPath) {
    Remove-Item $lnkPath -Force
    Say "Start Menu shortcut removed"
} else {
    Say "Start Menu shortcut not present"
}

# --- WebView2 profile (holds the Wavelog session cookie) ----------------------
$webview = Join-Path $Root '.webview'
if (Test-Path $webview) {
    Remove-Item $webview -Recurse -Force
    Say "WebView2 profile removed (Wavelog session cookie gone with it)"
} else {
    Say "WebView2 profile not present"
}

# --- Logs ---------------------------------------------------------------------
$logs = Get-ChildItem $Root -Filter 'flex_wavelog.log*' -ErrorAction SilentlyContinue
if ($logs) {
    $logs | Remove-Item -Force
    Say "Log files removed ($($logs.Count))"
}

# --- Config -------------------------------------------------------------------
$config = Join-Path $Root 'config.json'
if ($Purge) {
    if (Test-Path $config) {
        Remove-Item $config -Force
        Say "config.json deleted (-Purge)"
    }
} elseif (Test-Path $config) {
    Say "config.json KEPT - it contains your Wavelog API token."
    Say "  Delete it yourself or re-run with -Purge. Consider revoking the token"
    Say "  in Wavelog (API Keys page) if you are retiring this install."
}

Write-Host "`nDone. The repo directory itself is untouched - delete it whenever you like."
Write-Host "pywebview was left installed; remove with: python -m pip uninstall pywebview`n"
