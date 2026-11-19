# Common helpers for PowerShell-based AI Hub actions
. "$PSScriptRoot/paths.ps1"

function Ensure-Path {
  param([string]$Path)
  $parent = Split-Path $Path -Parent
  if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
  if (-not (Test-Path $Path)) { New-Item -ItemType File -Path $Path -Force | Out-Null }
}

function Write-LogLine {
  param(
    [string]$Message,
    [string]$Level = "INFO",
    [string]$LogPath = $null
  )
  $path = if ($LogPath) { $LogPath } else { Get-AIHubLogPath }
  $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  $payload = @{
    ts = $stamp
    level = $Level
    message = $Message
  } | ConvertTo-Json -Compress
  Write-Host $payload
  Add-Content -Path $path -Value $payload
}

function Invoke-AIHubShellAction {
  param(
    [string]$ActionName,
    [string]$ScriptName,
    [string[]]$AdditionalArgs = @()
  )

  $projectRoot = Resolve-Path "$PSScriptRoot/../.."
  $scriptPath = Join-Path $projectRoot "modules/shell/$ScriptName"
  $logPath = Get-AIHubLogPath
  # Preserve caller-provided AIHUB_LOG_PATH overrides so log redirection is
  # consistent across shell and PowerShell entry points.
  $Env:AIHUB_LOG_PATH = $logPath
  $Env:AIHUB_LOG_DIR = Get-AIHubLogRoot
  $Env:AIHUB_CONFIG_DIR = Get-AIHubConfigRoot
  $Env:CONFIG_FILE = Get-AIHubConfigFile
  $Env:CONFIG_STATE_FILE = Get-AIHubStatePath
  Ensure-Path $logPath

  if (-not (Test-Path $scriptPath)) {
    Write-LogLine "Script not found: $scriptPath" "ERROR" -LogPath $logPath
    return 1
  }

  $bashPath = $Env:AIHUB_BASH
  if (-not $bashPath) {
    # Prefer Git Bash over the WindowsApps/WSL bash aliases.
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($git) {
      $candidate = Join-Path (Split-Path (Split-Path $git.Source -Parent) -Parent) 'bin/bash.exe'
      if (Test-Path $candidate) { $bashPath = $candidate }
    }
  }
  if (-not $bashPath) {
    $bash = Get-Command bash.exe -ErrorAction SilentlyContinue
    if ($bash -and $bash.Source -notmatch 'WindowsApps|Windows\\System32') { $bashPath = $bash.Source }
  }
  $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
  if (-not $bashPath -and -not $wsl) {
    Write-LogLine "Install Git Bash or a WSL distribution to run '$ActionName'." "ERROR" -LogPath $logPath
    return 1
  }

  Write-LogLine "Starting $ActionName via $ScriptName" "INFO" -LogPath $logPath

  Push-Location $projectRoot
  try {
    if ($bashPath) {
      foreach ($name in @('AIHUB_LOG_PATH', 'AIHUB_LOG_DIR', 'AIHUB_CONFIG_DIR', 'CONFIG_FILE', 'CONFIG_STATE_FILE', 'DOWNLOAD_STATUS_FILE', 'DOWNLOAD_LOG_FILE')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value) { [Environment]::SetEnvironmentVariable($name, $value.Replace('\', '/')) }
      }
      & $bashPath $scriptPath.Replace('\', '/') @AdditionalArgs 2>&1 | ForEach-Object {
        $msg = $_
        if ($msg -ne $null) { Write-LogLine $msg "STREAM" -LogPath $logPath }
      }
      $exitCode = $LASTEXITCODE
      if ($exitCode -ne 0) {
        Write-LogLine "$ActionName exited with code $exitCode" "ERROR" -LogPath $logPath
      } else {
        Write-LogLine "$ActionName completed" "INFO" -LogPath $logPath
      }
      return $exitCode
    }

    $wslScriptPath = & $wsl.Source wslpath -a $scriptPath 2>$null
    $wslLogPath = & $wsl.Source wslpath -a $logPath 2>$null
    $wslConfigRoot = & $wsl.Source wslpath -a (Get-AIHubConfigRoot) 2>$null
    $wslConfigFile = & $wsl.Source wslpath -a (Get-AIHubConfigFile) 2>$null
    $wslStateFile = & $wsl.Source wslpath -a (Get-AIHubStatePath) 2>$null

    if (-not $wslScriptPath) {
      Write-LogLine "WSL is installed but script path could not be translated for $scriptPath." "ERROR" -LogPath $logPath
      return 1
    }

    Write-LogLine "Bash not found; using WSL fallback for $ActionName." "WARN" -LogPath $logPath
    $linuxEnv = @("AIHUB_LOG_PATH=$wslLogPath", "AIHUB_CONFIG_DIR=$wslConfigRoot", "CONFIG_FILE=$wslConfigFile", "CONFIG_STATE_FILE=$wslStateFile")
    foreach ($name in @('HEADLESS', 'HEADLESS_UPDATE', 'CURATED_MODEL_NAMES', 'CURATED_LORA_NAMES')) {
      $value = [Environment]::GetEnvironmentVariable($name)
      if ($null -ne $value) { $linuxEnv += "$name=$value" }
    }
    foreach ($name in @('DOWNLOAD_STATUS_FILE', 'DOWNLOAD_LOG_FILE', 'AIHUB_LOG_DIR', 'AIHUB_OFFLINE_BUNDLE', 'DOWNLOAD_OFFLINE_BUNDLE')) {
      $value = [Environment]::GetEnvironmentVariable($name)
      if ($value) {
        $linuxPath = & $wsl.Source wslpath -a $value
        if ($LASTEXITCODE -ne 0) { throw "Unable to translate $name for WSL." }
        $linuxEnv += "$name=$linuxPath"
      }
    }
    & $wsl.Source --exec env @linuxEnv bash $wslScriptPath @AdditionalArgs 2>&1 | ForEach-Object {
      $msg = $_
      if ($msg -ne $null) { Write-LogLine $msg "STREAM" -LogPath $logPath }
    }
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
      Write-LogLine "$ActionName exited with code $exitCode (WSL fallback)" "ERROR" -LogPath $logPath
    } else {
      Write-LogLine "$ActionName completed (WSL fallback)" "INFO" -LogPath $logPath
    }
    return $exitCode
  } finally {
    Pop-Location
  }
}
