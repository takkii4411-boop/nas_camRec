#requires -Version 5.1
param()
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$IsTermux = $false
if ($PSVersionTable.Platform -eq "Unix" -and (Test-Path "/system/bin/termux-setup-storage")) { $IsTermux = $true }
elseif ($PSVersionTable.Platform -eq "Unix" -and (Test-Path "$env:HOME/storage/shared")) { $IsTermux = $true }
elseif ($PSVersionTable.OS -match "Android") { $IsTermux = $true }
Write-Host "CameraNAS starting on: $(if($IsTermux){'Termux'}else{'Windows'})" -ForegroundColor Cyan
cd $ScriptDir

# Load .env
$EnvFile = "$ScriptDir/camera_recorder/system/config/.env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match "^([^=]+)=(.*)$" -and $_ -notmatch "^#") {
            $key = $matches[1].Trim()
            $val = $matches[2].Trim()
            [Environment]::SetEnvironmentVariable($key, $val, "Process")
        }
    }
    Write-Host "[.env loaded]" -ForegroundColor Green
}

# Determine root
$RootPath = if ($IsTermux) { "$env:HOME/storage/shared/DCIM/CameraNAS" } else { "C:\CameraNAS\DCIM\CameraNAS" }
Write-Host "Root: $RootPath" -ForegroundColor Cyan
Write-Host "Google Photos: Auto-backup from DCIM/" -ForegroundColor Cyan

python app.py