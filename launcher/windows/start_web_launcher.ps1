# Start the AI Hub web launcher (Windows/PowerShell)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path "$ScriptDir/../.."
. "$ScriptDir/paths.ps1"
$Env:AIHUB_LOG_PATH = Get-AIHubLogPath
$Env:AIHUB_LOG_DIR = Get-AIHubLogRoot
$Env:AIHUB_CONFIG_DIR = Get-AIHubConfigRoot
$Env:CONFIG_FILE = Get-AIHubConfigFile
$Env:CONFIG_STATE_FILE = Get-AIHubStatePath
$HostValue = $Env:AIHUB_WEB_HOST
if (-not $HostValue) { $HostValue = "127.0.0.1" }
$PortValue = $Env:AIHUB_WEB_PORT
if (-not $PortValue) { $PortValue = 3939 }

$python = $Env:AIHUB_PYTHON
if (-not $python) {
    $venvPython = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
    $python = if (Test-Path $venvPython) { $venvPython } else { 'python' }
}
& $python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Error 'AI Hub requires Python 3.11 or newer (3.11 and 3.12 tested).'
    exit 1
}

Push-Location $ProjectRoot
$env:PYTHONPATH = "$ProjectRoot;$($env:PYTHONPATH)"
$env:AIHUB_WEB_HOST = $HostValue
$env:AIHUB_WEB_PORT = $PortValue
& $python -m modules.runtime.web_launcher serve --host $HostValue --port $PortValue
$code = $LASTEXITCODE
Pop-Location
exit $code
