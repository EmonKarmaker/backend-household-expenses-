#Requires -Version 7.0
<#
.SYNOPSIS
    Daily database backup for the Household Expense Tracker.

.DESCRIPTION
    Reads ALEMBIC_DATABASE_URL from .env, runs pg_dump, compresses the SQL
    to ./backups/household_db_YYYYMMDD_HHMMSS.sql.gz, keeps the last 14
    backup files, and logs success/failure to ./backups/backup.log.

    Exit code 0  → success (Task Scheduler "Last Run Result: 0x0")
    Exit code 1  → failure (Task Scheduler reports the task as failed)

.NOTES
    Run from the repo root:
        .\scripts\backup_db.ps1

    pg_dump must be on PATH.  The database password is placed in PGPASSWORD
    for the duration of the pg_dump call and cleared in the finally block —
    it is never written to stdout, stderr, or the log file.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

$RepoRoot  = Split-Path $PSScriptRoot -Parent
$BackupDir = Join-Path $RepoRoot 'backups'
$LogFile   = Join-Path $BackupDir 'backup.log'
$EnvFile   = Join-Path $RepoRoot '.env.backup'
$KeepCount = 14

# ---------------------------------------------------------------------------
# Logging helper  (never receives or logs the password)
# ---------------------------------------------------------------------------

function Write-Log {
    param(
        [string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level = 'INFO'
    )
    $stamp = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    $line  = "[$stamp] [$Level] $Message"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    if ($Level -eq 'ERROR') { Write-Host $line -ForegroundColor Red }
    else                    { Write-Host $line }
}

# ---------------------------------------------------------------------------
# Bootstrap: ensure backup directory exists before we try to log anything
# ---------------------------------------------------------------------------

if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
}

# ---------------------------------------------------------------------------
# Parse .env — extract ALEMBIC_DATABASE_URL only
# ---------------------------------------------------------------------------

if (-not (Test-Path $EnvFile)) {
    # Cannot call Write-Log yet if backup dir wasn't created above, but it was.
    Write-Log ".env not found at $EnvFile — run from the repo root." 'ERROR'
    exit 1
}

$alembicUrl = $null
Get-Content $EnvFile | ForEach-Object {
    # Match KEY=VALUE lines only; ignore comments and bare values
    if ($_ -match '^ALEMBIC_DATABASE_URL=(.+)$') {
        $alembicUrl = $Matches[1].Trim()
    }
}

if (-not $alembicUrl) {
    Write-Log 'ALEMBIC_DATABASE_URL not found in .env.' 'ERROR'
    exit 1
}

# ---------------------------------------------------------------------------
# Parse connection URL
# Format: postgresql://user:password@host[:port]/dbname[?params]
# ---------------------------------------------------------------------------

if ($alembicUrl -notmatch '^postgresql://([^:@]+):([^@]+)@([^/:]+)(?::(\d+))?/([^?#\s]+)') {
    Write-Log 'Cannot parse ALEMBIC_DATABASE_URL — unexpected format.' 'ERROR'
    exit 1
}

$dbUser = $Matches[1]
$dbPass = $Matches[2]   # stored only in this variable; never logged
$dbHost = $Matches[3]
$dbPort = if ($Matches[4]) { $Matches[4] } else { '5432' }
$dbName = $Matches[5]

# Extract sslmode from query string if present
$sslMode = $null
if ($alembicUrl -match '[?&]sslmode=([^&#\s]+)') {
    $sslMode = $Matches[1]
}

Write-Log "Target: ${dbUser}@${dbHost}:${dbPort}/${dbName}$(if ($sslMode) { " [sslmode=$sslMode]" })"
# ---------------------------------------------------------------------------
# Verify pg_dump is available
# ---------------------------------------------------------------------------

if (-not (Get-Command pg_dump -ErrorAction SilentlyContinue)) {
    Write-Log (
        'pg_dump not found on PATH. ' +
        'Install PostgreSQL client tools and add their bin/ directory to PATH.' +
        ' On Windows: C:\Program Files\PostgreSQL\<version>\bin'
    ) 'ERROR'
    exit 1
}

$pgVer = (& pg_dump --version 2>&1) | Select-Object -First 1
Write-Log "pg_dump: $pgVer"

# ---------------------------------------------------------------------------
# Run backup
# ---------------------------------------------------------------------------

$timestamp = (Get-Date -Format 'yyyyMMdd_HHmmss')
$outFile   = Join-Path $BackupDir "household_db_${timestamp}.sql.gz"
$tempSql   = [System.IO.Path]::GetTempFileName()
$exitCode  = 0
$script:backupCompleted = $false

Write-Log "Starting backup → $(Split-Path $outFile -Leaf)"

try {
    # Place password in env for pg_dump — cleared in finally regardless of outcome
    $env:PGPASSWORD = $dbPass
    if ($sslMode) { $env:PGSSLMODE = $sslMode }

    # Dump plain SQL to a temp file
    $pgArgs = @(
        '-h', $dbHost,
        '-p', $dbPort,
        '-U', $dbUser,
        '-d', $dbName,
        '--no-password',
        '-f', $tempSql
    )
    & pg_dump @pgArgs
    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump exited with code $LASTEXITCODE"
    }

    # Compress to .sql.gz using .NET GZipStream (always available on Windows 10+)
    Add-Type -AssemblyName System.IO.Compression
    $inputStream  = [System.IO.File]::OpenRead($tempSql)
    $outputStream = [System.IO.File]::Create($outFile)
    $gzipStream   = [System.IO.Compression.GZipStream]::new(
        $outputStream,
        [System.IO.Compression.CompressionLevel]::Optimal
    )
    try {
        $inputStream.CopyTo($gzipStream)
    } finally {
        $gzipStream.Close()
        $outputStream.Close()
        $inputStream.Close()
    }

    $sizeMB = [math]::Round((Get-Item $outFile).Length / 1MB, 3)
    Write-Log "Backup complete — ${sizeMB} MB compressed"
    $script:backupCompleted = $true

    # Prune oldest backups beyond $KeepCount
    # @(...) forces an array even when there's only one match (single FileInfo has no .Count)
    $allBackups = @(
        Get-ChildItem -Path $BackupDir -Filter 'household_db_*.sql.gz' |
        Sort-Object LastWriteTime -Descending
    )
    if ($allBackups.Count -gt $KeepCount) {
        $toDelete = $allBackups | Select-Object -Skip $KeepCount
        foreach ($f in $toDelete) {
            Remove-Item $f.FullName -Force
            Write-Log "Pruned: $($f.Name)"
        }
    }
    $kept = [math]::Min($allBackups.Count, $KeepCount)
    Write-Log "Retention: $kept / $KeepCount backups kept"
} catch {
    Write-Log "Backup FAILED: $_" 'ERROR'
    $exitCode = 1
    # Only delete the output if it's likely corrupt (i.e. we never got to "Backup complete")
    # The retention prune happens AFTER the file is fully written, so don't delete then.
    if ((Test-Path $outFile) -and (-not $script:backupCompleted)) {
        Remove-Item $outFile -Force
        Write-Log "Removed incomplete output file" 'WARN'
    }

} finally {
    # Always scrub credentials from environment
    $env:PGPASSWORD = $null
    $env:PGSSLMODE  = $null
    if (Test-Path $tempSql) { Remove-Item $tempSql -Force }
}

exit $exitCode
