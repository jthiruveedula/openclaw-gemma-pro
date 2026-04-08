<#
.SYNOPSIS
    OpenClaw Gemma Pro – Windows Task Scheduler setup for nightly memory indexer.

.DESCRIPTION
    Registers a Windows Scheduled Task that runs the memory indexer at 02:00 daily,
    providing the same automation parity as the Linux/macOS cron job in bootstrap.sh.

    Fixes issue #7: https://github.com/jthiruveedula/openclaw-gemma-pro/issues/7

.USAGE
    # Run as Administrator in PowerShell:
    .\scripts\setup_task_scheduler.ps1

    # Or with a custom venv path:
    .\scripts\setup_task_scheduler.ps1 -VenvPath C:\path\to\venv

.PARAMETER VenvPath
    Path to the Python virtual environment. Defaults to .venv in the repo root.

.PARAMETER TaskName
    Name for the scheduled task. Defaults to 'OpenClaw-MemoryIndexer'.

.PARAMETER Hour
    Hour (0-23) at which the task runs daily. Defaults to 2 (02:00 AM).
#>

[CmdletBinding()]
param(
    [string]$VenvPath = ".venv",
    [string]$TaskName = "OpenClaw-MemoryIndexer",
    [int]$Hour = 2
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
$RepoRoot = (Get-Item -LiteralPath $PSScriptRoot).Parent.FullName
$PythonExe = Join-Path $RepoRoot $VenvPath "Scripts" "python.exe"
$IndexerScript = Join-Path $RepoRoot "workers" "memory_indexer" "index_memory.py"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at '$PythonExe'. Run bootstrap first or set -VenvPath."
    exit 1
}

if (-not (Test-Path $IndexerScript)) {
    Write-Error "Memory indexer not found at '$IndexerScript'."
    exit 1
}

# ---------------------------------------------------------------------------
# Build the scheduled task
# ---------------------------------------------------------------------------
Write-Host "[OpenClaw] Registering Windows Scheduled Task: $TaskName" -ForegroundColor Cyan

$trigger = New-ScheduledTaskTrigger -Daily -At "$($Hour):00"

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$IndexerScript`"" `
    -WorkingDirectory $RepoRoot

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false

$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Highest

try {
    # Remove existing task if it exists
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[OpenClaw] Removed existing task '$TaskName'." -ForegroundColor Yellow
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Trigger $trigger `
        -Action $action `
        -Settings $settings `
        -Principal $principal `
        -Description "OpenClaw Gemma Pro nightly memory indexer (issue #7 fix)" | Out-Null

    Write-Host "[OpenClaw] Task '$TaskName' registered successfully!" -ForegroundColor Green
    Write-Host "[OpenClaw]   Runs daily at $($Hour):00 AM"
    Write-Host "[OpenClaw]   Python: $PythonExe"
    Write-Host "[OpenClaw]   Script: $IndexerScript"
    Write-Host ""
    Write-Host "To verify: Get-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Cyan
    Write-Host "To remove: Unregister-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Cyan
}
catch {
    Write-Error "Failed to register task: $_"
    Write-Host ""
    Write-Host "Tip: Try running this script as Administrator." -ForegroundColor Yellow
    exit 1
}
