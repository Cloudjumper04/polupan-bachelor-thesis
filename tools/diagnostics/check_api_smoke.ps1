$BaseUrl = "http://localhost:6001"

$today = (Get-Date).ToString("yyyy-MM-dd")

$endpoints = @(
  "/api/health",
  "/api/solar/dashboard",
  "/api/solar/current-buffer?seconds=75",
  "/api/solar/weather-current",
  "/api/solar/history/bounds",
  "/api/grid/current",
  "/api/grid/outages?date=$today",
  "/api/grid/history?start=2026-01-01T00:00:00&end=2026-01-14T23:59:59"
)

Write-Host "SmartEnergy API smoke check" -ForegroundColor Magenta
Write-Host "Base URL: $BaseUrl"

foreach ($ep in $endpoints) {
    $url = "$BaseUrl$ep"
    Write-Host "`nGET $url" -ForegroundColor Cyan

    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 20
        Write-Host "Status: $($r.StatusCode)" -ForegroundColor Green

        try {
            $json = $r.Content | ConvertFrom-Json
            if ($json.status) {
                Write-Host "Response status: $($json.status)"
            }
            if ($json.points) {
                Write-Host "Points: $(@($json.points).Count)"
            }
            if ($json.current) {
                Write-Host "Current timestamp: $($json.current.timestamp_local)"
            }
        } catch {
            # Not JSON or not important.
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

        Write-Host "Status: $status" -ForegroundColor Red
        Write-Host $_.Exception.Message
        if ($body) {
            Write-Host "Body: $body"
        }
    }
}

Write-Host "`nDone." -ForegroundColor Magenta
