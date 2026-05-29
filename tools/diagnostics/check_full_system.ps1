$ErrorActionPreference = "Continue"

$BaseUrl = "http://localhost:6001"
$FrontendUrl = "http://localhost:5173"
$ProjectRoot = Get-Location
$DbPath = Join-Path $ProjectRoot "backend\data\smartenergy.db"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

$PassCount = 0
$WarnCount = 0
$FailCount = 0

function Add-Pass {
    param([string]$Message)
    $script:PassCount++
    Write-Host "[PASS] $Message" -ForegroundColor Green
}

function Add-Warn {
    param([string]$Message)
    $script:WarnCount++
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Add-Fail {
    param([string]$Message)
    $script:FailCount++
    Write-Host "[FAIL] $Message" -ForegroundColor Red
}

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Invoke-ApiCheck {
    param(
        [string]$Name,
        [string]$Path
    )

    $url = "$BaseUrl$Path"
    Write-Info "GET $url"

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 20

        if ($response.StatusCode -eq 200) {
            Add-Pass "$Name returned 200"
        } else {
            Add-Fail "$Name returned $($response.StatusCode)"
        }

        try {
            return ($response.Content | ConvertFrom-Json)
        } catch {
            return $null
        }
    }
    catch {
        $status = "NO_RESPONSE"
        $body = ""

        if ($_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode

            try {
                $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $body = $reader.ReadToEnd()
            } catch {}
        }

        Add-Fail "$Name failed: HTTP $status"
        Write-Host $_.Exception.Message

        if ($body) {
            Write-Host $body
        }

        return $null
    }
}

Write-Host ""
Write-Host "=== SmartEnergy Full System Check ===" -ForegroundColor Magenta
Write-Host "Project root: $ProjectRoot"
Write-Host "Backend:      $BaseUrl"
Write-Host "Frontend:     $FrontendUrl"

Write-Host ""
Write-Host "=== Docker Compose Status ===" -ForegroundColor Magenta

try {
    docker compose ps
} catch {
    Add-Warn "docker compose ps failed: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "=== Port Ownership ===" -ForegroundColor Magenta

foreach ($port in @(6001, 5173)) {
    $lines = netstat -ano | findstr ":$port"

    if (-not $lines) {
        Add-Fail "Port $port is not listening"
        continue
    }

    Write-Info ("Port " + $port + " listeners:")
    Write-Host $lines

    $listeningPids = @()

    foreach ($line in $lines) {
        $parts = ($line -split "\s+") | Where-Object { $_ -ne "" }

        if ($parts.Count -ge 5) {
            $localAddress = $parts[1]
            $state = $parts[3]
            $pidValue = $parts[4]

            if ($localAddress -match ":$port$" -and $state -eq "LISTENING") {
                $listeningPids += $pidValue
            }
        }
    }

    $listeningPids = $listeningPids | Sort-Object -Unique

    foreach ($pidValue in $listeningPids) {
        try {
            $process = Get-Process -Id $pidValue -ErrorAction Stop
            Write-Info ("Port " + $port + " owner PID=" + $pidValue + " Name=" + $process.ProcessName)
        } catch {
            Add-Warn "Could not resolve PID $pidValue for port $port"
        }
    }

    Add-Pass "Port $port is listening"
}

Write-Host ""
Write-Host "=== Frontend Check ===" -ForegroundColor Magenta

try {
    $frontendResponse = Invoke-WebRequest -UseBasicParsing -Uri $FrontendUrl -TimeoutSec 20

    if ($frontendResponse.StatusCode -eq 200) {
        Add-Pass "Frontend returned 200"
    } else {
        Add-Fail "Frontend returned $($frontendResponse.StatusCode)"
    }
} catch {
    Add-Fail "Frontend failed: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "=== Backend API Smoke Check ===" -ForegroundColor Magenta

$today = (Get-Date).ToString("yyyy-MM-dd")
$winterStart = "2026-01-01T00:00:00"
$winterEnd = "2026-01-14T23:59:59"

$solarDashboard = Invoke-ApiCheck "solar dashboard" "/api/solar/dashboard"
$solarBuffer = Invoke-ApiCheck "solar current buffer" "/api/solar/current-buffer?seconds=75"
$solarWeather = Invoke-ApiCheck "solar weather current" "/api/solar/weather-current"
$solarBounds = Invoke-ApiCheck "solar history bounds" "/api/solar/history/bounds"
$gridCurrent = Invoke-ApiCheck "grid current" "/api/grid/current"
$gridOutages = Invoke-ApiCheck "grid outages today" "/api/grid/outages?date=$today"
$gridHistory = Invoke-ApiCheck "grid winter history" "/api/grid/history?start=$winterStart&end=$winterEnd"

Write-Host ""
Write-Host "=== API Payload Validation ===" -ForegroundColor Magenta

if ($solarDashboard -and $solarDashboard.current -and $solarDashboard.current.timestamp_local) {
    Add-Pass "Solar dashboard timestamp exists: $($solarDashboard.current.timestamp_local)"
} else {
    Add-Fail "Solar dashboard missing current timestamp"
}

if ($gridCurrent -and $gridCurrent.status -eq "ok" -and $gridCurrent.current -and $gridCurrent.current.timestamp_utc) {
    Add-Pass "Grid current status ok: $($gridCurrent.current.timestamp_local)"

    try {
        $gridTime = [DateTimeOffset]::Parse($gridCurrent.current.timestamp_utc)
        $ageMinutes = ([DateTimeOffset]::UtcNow - $gridTime).TotalMinutes
        $ageRounded = [math]::Round($ageMinutes, 1)

        if ($ageMinutes -ge -15 -and $ageMinutes -le 180) {
            Add-Pass "Grid current is fresh enough: age $ageRounded min"
        } else {
            Add-Warn "Grid current may be stale/future: age $ageRounded min"
        }
    } catch {
        Add-Warn "Could not parse grid current timestamp"
    }
} else {
    Add-Fail "Grid current missing status/current timestamp"
}

if ($gridHistory -and $gridHistory.points) {
    $historyCount = @($gridHistory.points).Count

    if ($historyCount -eq 672) {
        Add-Pass "Grid winter history has expected 672 points"
    } else {
        Add-Warn "Grid winter history has $historyCount points; expected 672"
    }
} else {
    Add-Fail "Grid winter history returned no points"
}

Write-Host ""
Write-Host "=== Grid Future Buffer Check ===" -ForegroundColor Magenta

$futureStart = (Get-Date).AddDays(1).ToString("yyyy-MM-dd") + "T00:00:00"
$futureEnd = (Get-Date).AddDays(7).ToString("yyyy-MM-dd") + "T23:59:59"
$futureGrid = Invoke-ApiCheck "grid future buffer" "/api/grid/history?start=$futureStart&end=$futureEnd"

if ($futureGrid -and $futureGrid.points) {
    $futureCount = @($futureGrid.points).Count

    if ($futureCount -gt 0) {
        Add-Pass "Grid future buffer exists: $futureCount points"
    } else {
        Add-Fail "Grid future buffer has zero points"
    }
} else {
    Add-Fail "Grid future buffer missing points"
}

Write-Host ""
Write-Host "=== SQLite Integrity Check ===" -ForegroundColor Magenta

if (-not (Test-Path $DbPath)) {
    Add-Fail "DB file not found: $DbPath"
} elseif (-not (Test-Path $PythonExe)) {
    Add-Fail "Python venv not found: $PythonExe"
} else {
    $TempDbCheck = Join-Path ([System.IO.Path]::GetTempPath()) "smartenergy_db_integrity_check_$PID.py"

    $DbCheckPython = @"
from pathlib import Path
import sqlite3
import sys

db = Path(r"$DbPath")

print(f"DB exists: {db.exists()}")
print(f"DB path: {db}")
print(f"DB size MB: {db.stat().st_size / 1024 / 1024:.2f}")

for suffix in ["-wal", "-shm", "-journal"]:
    p = Path(str(db) + suffix)
    print(f"{p.name}: exists={p.exists()} size={p.stat().st_size if p.exists() else 0}")

try:
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA busy_timeout = 30000;")

    journal_mode = con.execute("PRAGMA journal_mode;").fetchone()[0]
    integrity = con.execute("PRAGMA integrity_check;").fetchone()[0]

    print("journal_mode:", journal_mode)
    print("integrity_check:", integrity)

    tables = [row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]

    wanted = [
        "weatherforecast",
        "simulatedsolarproduction",
        "interpolatedsolarproduction",
        "gridavailabilitypoint",
        "griddamageevent",
    ]

    print("table_counts:")
    for table in wanted:
        if table in tables:
            count = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            print(f"{table}: {count}")
        else:
            print(f"{table}: MISSING")

    if "gridavailabilitypoint" in tables:
        grid_range = con.execute(
            'SELECT MIN(timestamp_utc), MAX(timestamp_utc), COUNT(*) FROM gridavailabilitypoint'
        ).fetchone()
        print("grid_range:", grid_range)

    con.close()
    sys.exit(0 if integrity == "ok" else 2)
except Exception as exc:
    print("DB_CHECK_ERROR:", repr(exc))
    sys.exit(1)
"@

    $DbCheckPython | Set-Content $TempDbCheck -Encoding UTF8

    & $PythonExe $TempDbCheck
    $dbExitCode = $LASTEXITCODE

    Remove-Item $TempDbCheck -ErrorAction SilentlyContinue

    if ($dbExitCode -eq 0) {
        Add-Pass "SQLite integrity_check ok"
    } else {
        Add-Fail "SQLite integrity check failed or DB is locked"
    }
}

Write-Host ""
Write-Host "=== Scheduler / Automation Risk Check ===" -ForegroundColor Magenta

try {
    $runningContainers = docker ps --format "{{.Names}}"
    $schedulerContainers = $runningContainers | Where-Object {
        $_ -match "scheduler|weather|solar-data|worker"
    }

    if ($schedulerContainers) {
        Add-Warn "Scheduler-like Docker container is running: $($schedulerContainers -join ', ')"
    } else {
        Add-Pass "No scheduler-like Docker container detected"
    }
} catch {
    Add-Warn "Could not inspect Docker containers"
}

try {
    $localSchedulerProcesses = Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -match "solar_data_scheduler|generate_grid_availability|update_weather_cache"
        } |
        Select-Object ProcessId, Name, CommandLine

    if ($localSchedulerProcesses) {
        Add-Warn "Local scheduler/generator process detected:"
        $localSchedulerProcesses | Format-List
    } else {
        Add-Pass "No local scheduler/generator Python process detected"
    }
} catch {
    Add-Warn "Could not inspect local Python processes"
}

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Magenta
Write-Host "PASS: $PassCount" -ForegroundColor Green
Write-Host "WARN: $WarnCount" -ForegroundColor Yellow
Write-Host "FAIL: $FailCount" -ForegroundColor Red

if ($FailCount -eq 0) {
    Write-Host ""
    Write-Host "SYSTEM CHECK PASSED. Demo launch is acceptable." -ForegroundColor Green
    exit 0
} else {
    Write-Host ""
    Write-Host "SYSTEM CHECK FAILED. Fix failures before demo." -ForegroundColor Red
    exit 1
}
