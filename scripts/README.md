# Scripts

## backup_db.ps1 — Database backup

Dumps the production Postgres database to a gzip-compressed SQL file and keeps
the last 14 backups locally.

### Prerequisites

- PowerShell 7+ (`pwsh`)
- `pg_dump` on PATH — install [PostgreSQL client tools](https://www.postgresql.org/download/windows/)
  and add `C:\Program Files\PostgreSQL\<version>\bin` to your PATH
- A valid `.env` in the repo root with `ALEMBIC_DATABASE_URL` set

### Running manually

```powershell
# From the repo root
.\scripts\backup_db.ps1
```

Output goes to `./backups/household_db_YYYYMMDD_HHMMSS.sql.gz`.
The log is at `./backups/backup.log`.

### Scheduling via Windows Task Scheduler (daily at 3 AM)

1. Open **Task Scheduler** (`taskschd.msc`)
2. Click **Create Task** (not "Basic Task")
3. **General** tab:
   - Name: `Household DB Backup`
   - Check **Run whether user is logged on or not**
   - Check **Run with highest privileges**
4. **Triggers** tab → New:
   - Begin the task: **On a schedule**
   - Daily, at **3:00 AM**
5. **Actions** tab → New:
   - Program: `pwsh.exe`
   - Arguments: `-NonInteractive -File "E:\Live_Projects\household-app-scaffold\backend\scripts\backup_db.ps1"`
   - Start in: `E:\Live_Projects\household-app-scaffold\backend`
6. **Conditions** tab: uncheck "Start only if the computer is on AC power" if on a laptop
7. **Settings** tab: check **If the task fails, restart every 1 hour**, up to **3 times**
8. Click OK and enter your Windows credentials when prompted

To verify the task ran: check **Last Run Result** in Task Scheduler — it should show `0x0`.
Any non-zero result means the backup failed; check `./backups/backup.log` for details.

### Restoring from a backup

```powershell
# Decompress
$backup = "backups\household_db_20260512_030000.sql.gz"
$sqlFile = $backup -replace '\.gz$', ''

Add-Type -AssemblyName System.IO.Compression
$inStream  = [System.IO.File]::OpenRead($backup)
$outStream = [System.IO.File]::Create($sqlFile)
$gz = [System.IO.Compression.GZipStream]::new($inStream, [System.IO.Compression.CompressionMode]::Decompress)
$gz.CopyTo($outStream)
$gz.Close(); $outStream.Close(); $inStream.Close()

# Restore (will prompt for password, or set $env:PGPASSWORD first)
psql -h <host> -U <user> -d <dbname> -f $sqlFile
```

> **Warning:** restoring overwrites existing data. Always restore to a test
> database first to verify the backup is intact before restoring to production.
