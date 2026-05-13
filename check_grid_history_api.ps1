$BaseUrl = "http://localhost:6001"

function Invoke-GridHistoryCase {
    param(
        [string]$Name,
        [string]$Start,
        [string]$End,
        [int[]]$ExpectedStatus = @(200)
    )

    Write-Host "`n=== $Name ===" -ForegroundColor Cyan

    $query = @()
    if ($Start -ne $null) { $query += "start=$([uri]::EscapeDataString($Start))" }
    if ($End -ne $null) { $query += "end=$([uri]::EscapeDataString($End))" }

    $url = "$BaseUrl/api/grid/history"
    if ($query.Count -gt 0) {
        $url = "$url`?$($query -join '&')"
    }

    Write-Host "GET $url"

    try {
        $response = Invoke-WebRequest -Uri $url -Method GET -TimeoutSec 30
        $status = [int]$response.StatusCode
        $json = $response.Content | ConvertFrom-Json

        $points = @()
        if ($json.points) { $points = @($json.points) }

        Write-Host "Status: $status"
        Write-Host "Points: $($points.Count)"

        if ($points.Count -gt 0) {
            Write-Host "First:  $($points[0].timestamp_local)"
            Write-Host "Last:   $($points[$points.Count - 1].timestamp_local)"
        }

        if ($ExpectedStatus -notcontains $status) {
            Write-Host "UNEXPECTED STATUS. Expected: $($ExpectedStatus -join ', ')" -ForegroundColor Red
        } else {
            Write-Host "OK" -ForegroundColor Green
        }
    }
    catch {
        $status = $null
        $body = ""

        if ($_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode
            $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
            $body = $reader.ReadToEnd()
        }

        Write-Host "Status: $status"
        Write-Host "Body: $body"

        if ($ExpectedStatus -notcontains $status) {
            Write-Host "UNEXPECTED ERROR STATUS. Expected: $($ExpectedStatus -join ', ')" -ForegroundColor Red
        } else {
            Write-Host "EXPECTED ERROR" -ForegroundColor Yellow
        }
    }
}

Write-Host "Checking /api/grid/history date-range edge cases..." -ForegroundColor Magenta

# 1. Normal full single day.
Invoke-GridHistoryCase `
    -Name "single full day" `
    -Start "2026-01-14T00:00:00" `
    -End "2026-01-14T23:59:59" `
    -ExpectedStatus @(200)

# 2. Normal multi-day winter peak range.
Invoke-GridHistoryCase `
    -Name "winter peak range" `
    -Start "2026-01-01T00:00:00" `
    -End "2026-01-18T23:59:59" `
    -ExpectedStatus @(200)

# 3. Exact frontend example.
Invoke-GridHistoryCase `
    -Name "frontend date selector example 2026-01-01 to 2026-01-14" `
    -Start "2026-01-01T00:00:00" `
    -End "2026-01-14T23:59:59" `
    -ExpectedStatus @(200)

# 4. Start and end same moment.
Invoke-GridHistoryCase `
    -Name "same timestamp start=end" `
    -Start "2026-01-14T12:00:00" `
    -End "2026-01-14T12:00:00" `
    -ExpectedStatus @(200,400,422)

# 5. End before start. Should ideally be rejected, but this test observes actual behavior.
Invoke-GridHistoryCase `
    -Name "end before start" `
    -Start "2026-01-14T23:59:59" `
    -End "2026-01-01T00:00:00" `
    -ExpectedStatus @(400,422,200)

# 6. Missing start.
Invoke-GridHistoryCase `
    -Name "missing start" `
    -Start $null `
    -End "2026-01-14T23:59:59" `
    -ExpectedStatus @(400,422)

# 7. Missing end.
Invoke-GridHistoryCase `
    -Name "missing end" `
    -Start "2026-01-01T00:00:00" `
    -End $null `
    -ExpectedStatus @(400,422)

# 8. Missing both.
Invoke-GridHistoryCase `
    -Name "missing both" `
    -Start $null `
    -End $null `
    -ExpectedStatus @(400,422)

# 9. Invalid start format.
Invoke-GridHistoryCase `
    -Name "invalid start format" `
    -Start "not-a-date" `
    -End "2026-01-14T23:59:59" `
    -ExpectedStatus @(400,422)

# 10. Invalid end format.
Invoke-GridHistoryCase `
    -Name "invalid end format" `
    -Start "2026-01-01T00:00:00" `
    -End "not-a-date" `
    -ExpectedStatus @(400,422)

# 11. Date-only values. Frontend should not send these to API, but API behavior is worth checking.
Invoke-GridHistoryCase `
    -Name "date-only values direct API" `
    -Start "2026-01-01" `
    -End "2026-01-14" `
    -ExpectedStatus @(200,400,422)

# 12. Explicit Kyiv timezone offsets.
Invoke-GridHistoryCase `
    -Name "explicit Kyiv offset winter" `
    -Start "2026-01-01T00:00:00+02:00" `
    -End "2026-01-14T23:59:59+02:00" `
    -ExpectedStatus @(200)

# 13. Explicit UTC timestamps.
Invoke-GridHistoryCase `
    -Name "explicit UTC timestamps" `
    -Start "2026-01-01T00:00:00Z" `
    -End "2026-01-14T23:59:59Z" `
    -ExpectedStatus @(200)

# 14. DST transition day in Kyiv, if data exists.
Invoke-GridHistoryCase `
    -Name "Kyiv DST transition day" `
    -Start "2026-03-29T00:00:00+02:00" `
    -End "2026-03-29T23:59:59+03:00" `
    -ExpectedStatus @(200)

# 15. Before generated data start. Should return empty or partial, not crash.
Invoke-GridHistoryCase `
    -Name "before generated range" `
    -Start "2025-10-01T00:00:00" `
    -End "2025-10-05T23:59:59" `
    -ExpectedStatus @(200)

# 16. Crosses generated start date. Should return partial data if available.
Invoke-GridHistoryCase `
    -Name "cross generated start date" `
    -Start "2025-10-01T00:00:00" `
    -End "2025-10-10T23:59:59" `
    -ExpectedStatus @(200)

# 17. Beyond future buffer. Should return empty/partial, not crash.
Invoke-GridHistoryCase `
    -Name "beyond generated future buffer" `
    -Start "2026-06-01T00:00:00" `
    -End "2026-06-07T23:59:59" `
    -ExpectedStatus @(200)

# 18. Very large range. Should not crash.
Invoke-GridHistoryCase `
    -Name "large available range" `
    -Start "2025-10-06T00:00:00" `
    -End "2026-05-20T23:59:59" `
    -ExpectedStatus @(200)

Write-Host "`nDone." -ForegroundColor Magenta
