param(
    [string]$DatabasePath = "backend\data\smartenergy.db",
    [string]$BackupDir = "backend\data\backups"
)

if (-not (Test-Path $DatabasePath)) {
    Write-Error "Database not found: $DatabasePath"
    exit 1
}

New-Item -ItemType Directory -Force $BackupDir | Out-Null

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupName = "smartenergy_$timestamp.db"
$backupPath = Join-Path $BackupDir $backupName

Copy-Item $DatabasePath $backupPath

Write-Host "Runtime DB backup created:"
Write-Host $backupPath