<#
.SYNOPSIS
    Install flex-wavelog: dependencies, config, autostart task, Start Menu entry.

.DESCRIPTION
    Deliberately a readable script rather than a packaged binary. A PyInstaller
    .exe would need code signing to avoid antivirus false positives, and an
    unsigned one gets flagged on reputation alone - the exact reason small ham
    utilities look untrustworthy. You can read this file before running it.

    Safe to re-run. Does not require administrator rights. Never overwrites an
    existing config.json.

.EXAMPLE
    .\install.ps1
#>

$ErrorActionPreference = 'Stop'

$Root      = $PSScriptRoot
$AppPy     = Join-Path $Root 'app.py'
$TaskName  = 'Flex-Wavelog CAT Bridge'
$Config    = Join-Path $Root 'config.json'
$Example   = Join-Path $Root 'config.example.json'
$MinPython = [version]'3.9'

function Say([string]$m) { Write-Host "  $m" }
function Fail([string]$m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

Write-Host "`nflex-wavelog installer`n"

# --- Python -----------------------------------------------------------------
$python = $null
foreach ($candidate in @('python', 'python3')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    # The WindowsApps stub resolves but is not a real interpreter.
    if ($found -and $found.Source -notlike '*WindowsApps*') { $python = $found.Source; break }
}
if (-not $python) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { $python = (& $launcher.Source -3 -c "import sys; print(sys.executable)") }
}
if (-not $python) { Fail "Python not found. Install Python 3.9+ and re-run." }

$verText = (& $python -c "import sys; print('%d.%d' % sys.version_info[:2])")
if ([version]$verText -lt $MinPython) { Fail "Python $verText found, need $MinPython or newer." }
Say "Python $verText at $python"

$pythonw = Join-Path (Split-Path $python -Parent) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { Fail "pythonw.exe not found beside python.exe. A windowed launcher is required." }
Say "Windowed launcher: pythonw.exe"

# --- Dependencies -----------------------------------------------------------
Say "Installing dependencies..."
& $python -m pip install --quiet --disable-pip-version-check -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail "pip install failed." }
& $python -c "import webview" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "pywebview did not import after install." }
Say "pywebview OK"

# --- Config -----------------------------------------------------------------
if (Test-Path $Config) {
    Say "config.json already exists - left untouched"
} else {
    Copy-Item $Example $Config
    Say "config.json created from the example - edit it, or use Preferences in the app"
}

# --- Autostart task ---------------------------------------------------------
# Runs only while you are logged on: making it run otherwise would mean storing
# your Windows password, and the app needs a desktop session anyway.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$AppPy`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:COMPUTERNAME\$env:USERNAME"
$trigger.Delay = 'PT30S'   # let the network settle before reaching for the radio
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'Publishes FlexRadio slice state to Wavelog /api/v2/radio' -Force | Out-Null
Say "Scheduled task '$TaskName' registered (starts at logon, 30s delay)"

# --- Start Menu shortcut ----------------------------------------------------
$programs = [Environment]::GetFolderPath('Programs')
$lnkPath = Join-Path $programs 'Flex-Wavelog.lnk'
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "`"$AppPy`""
$lnk.WorkingDirectory = $Root
$lnk.Description = 'Wavelog with the FlexRadio CAT bridge'
$icon = Join-Path $Root 'assets\flex-wavelog.ico'
if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
$lnk.Save()
Say "Start Menu shortcut created"

# --- Add/Remove Programs entry ------------------------------------------------
# Per-user (HKCU), so no admin rights - and anything that registers an autostart
# task ought to be discoverable in Settings > Apps for removal.
$version = '0.0.0'
$verLine = Select-String -Path (Join-Path $Root 'flex_wavelog.py') -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
if ($verLine) { $version = $verLine.Matches[0].Groups[1].Value }

$arp = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Flex-Wavelog'
New-Item -Path $arp -Force | Out-Null
$size = [int]((Get-ChildItem $Root -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1KB)
Set-ItemProperty -Path $arp -Name DisplayName -Value 'Flex-Wavelog'
Set-ItemProperty -Path $arp -Name DisplayVersion -Value $version
Set-ItemProperty -Path $arp -Name Publisher -Value 'flex-wavelog project'
Set-ItemProperty -Path $arp -Name InstallLocation -Value $Root
Set-ItemProperty -Path $arp -Name DisplayIcon -Value $(if (Test-Path $icon) { $icon } else { $pythonw })
Set-ItemProperty -Path $arp -Name UninstallString -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $Root 'uninstall.ps1')`""
Set-ItemProperty -Path $arp -Name EstimatedSize -Value $size -Type DWord
Set-ItemProperty -Path $arp -Name NoModify -Value 1 -Type DWord
Set-ItemProperty -Path $arp -Name NoRepair -Value 1 -Type DWord
Say "Registered in Add/Remove Programs (v$version)"

Write-Host "`nDone.`n"
Write-Host "Next steps:"
Write-Host "  1. Put your Wavelog URL and a v2 API token (wl2_...) with the radio:write"
Write-Host "     scope into config.json, or start the app and use Preferences."
Write-Host "  2. Start it now:  Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "  3. In Wavelog's QSO entry, pick the TX radio from the Radio dropdown."
Write-Host ""
Write-Host "To remove everything:  .\uninstall.ps1   (add -Purge to delete config.json too)"
Write-Host ""
