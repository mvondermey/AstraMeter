param(
    # FRITZ!Box DNS names keep working if DHCP later changes either address.
    [string]$FroniusHost = 'pv.fritz.box',
    [string]$MarstekHost = 'wlan0.fritz.box',
    [int]$MarstekPort = 30000
)

$ErrorActionPreference = 'Stop'
$statePath = Join-Path $PSScriptRoot '.fronius-marstek-last-power'
$diagnosticUntilPath = Join-Path $PSScriptRoot '.fronius-marstek-diagnostic-until'

function Get-FroniusGrid {
    param([string]$HostName)

    $fronius = Invoke-RestMethod `
        -Uri "http://$HostName/solar_api/v1/GetPowerFlowRealtimeData.fcgi" `
        -TimeoutSec 1
    if ($fronius.Head.Status.Code -ne 0) {
        throw "Fronius API status $($fronius.Head.Status.Code)"
    }
    return [double]$fronius.Body.Data.Site.P_Grid
}

function Get-MarstekStatus {
    param([string]$HostName, [int]$Port)

    # The Venus sometimes needs a second request after waking up. Keep both
    # attempts short so AstraMeter can still answer the battery once per second.
    foreach ($attempt in 1..2) {
        $udp = [System.Net.Sockets.UdpClient]::new()
        try {
            $udp.Client.ReceiveTimeout = 450
            $request = '{"id":1,"method":"ES.GetStatus","params":{"id":0}}'
            $bytes = [Text.Encoding]::UTF8.GetBytes($request)
            [void]$udp.Send($bytes, $bytes.Length, $HostName, $Port)
            $remote = [System.Net.IPEndPoint]::new(
                [System.Net.IPAddress]::Any,
                0
            )
            $payload = [Text.Encoding]::UTF8.GetString(
                $udp.Receive([ref]$remote)
            ) | ConvertFrom-Json
            if ($null -ne $payload.result.ongrid_power) {
                return [double]$payload.result.ongrid_power
            }
        }
        catch [System.Net.Sockets.SocketException] {
            # Retry once; the fail-safe below handles a second timeout.
        }
        finally {
            $udp.Dispose()
        }
    }

    throw 'No usable ES.GetStatus response from the Marstek battery'
}

$marstekPower = $null
try {
    $marstekPower = Get-MarstekStatus -HostName $MarstekHost -Port $MarstekPort
    Set-Content -LiteralPath $statePath -Value (
        $marstekPower.ToString([Globalization.CultureInfo]::InvariantCulture)
    )
}
catch {
    $diagnosticUntil = 0L
    if (Test-Path -LiteralPath $diagnosticUntilPath) {
        [void][long]::TryParse(
            (Get-Content -Raw -LiteralPath $diagnosticUntilPath).Trim(),
            [ref]$diagnosticUntil
        )
    }
    if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -le $diagnosticUntil) {
        try {
            $diagnosticGrid = Get-FroniusGrid -HostName $FroniusHost
            $diagnosticGrid.ToString(
                [Globalization.CultureInfo]::InvariantCulture
            )
            exit 0
        }
        catch {
            '0'
            exit 0
        }
    }

    # A raw Fronius load reading without the battery correction caused the
    # Venus to ramp to its 2500 W limit. On one missed local-API read, report
    # the opposite of the last known battery output once; this makes the
    # Venus' incremental controller return toward zero instead of running on.
    $lastPower = 0.0
    if (Test-Path -LiteralPath $statePath) {
        [void][double]::TryParse(
            (Get-Content -Raw -LiteralPath $statePath).Trim(),
            [Globalization.NumberStyles]::Float,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$lastPower
        )
    }
    Set-Content -LiteralPath $statePath -Value '0'
    (-$lastPower).ToString([Globalization.CultureInfo]::InvariantCulture)
    exit 0
}

try {
    $froniusGrid = Get-FroniusGrid -HostName $FroniusHost

    # With the Fronius meter at the load position, P_Grid excludes the Venus.
    # Venus ongrid_power is positive while discharging and negative while
    # charging, so subtract it to reconstruct the actual utility-grid flow.
    $correctedGrid = $froniusGrid - $marstekPower
    $correctedGrid.ToString([Globalization.CultureInfo]::InvariantCulture)
}
catch {
    # Fronius is unavailable but the battery is known: command the incremental
    # controller back toward zero output rather than holding an unsafe target.
    (-$marstekPower).ToString([Globalization.CultureInfo]::InvariantCulture)
}
