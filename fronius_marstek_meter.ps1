param(
    [string]$FroniusHost = 'pv.fritz.box',
    [string]$MarstekHost = 'wlan0.fritz.box',
    [int]$MarstekPort = 30000
)

$ErrorActionPreference = 'Stop'
$statePath = Join-Path $PSScriptRoot '.fronius-marstek-last-power'

function Get-MarstekOnGridPower {
    foreach ($attempt in 1..2) {
        $udp = [System.Net.Sockets.UdpClient]::new()
        try {
            $udp.Client.ReceiveTimeout = 700
            $request = '{"id":1,"method":"ES.GetMode","params":{"id":0}}'
            $bytes = [Text.Encoding]::UTF8.GetBytes($request)
            [void]$udp.Send($bytes, $bytes.Length, $MarstekHost, $MarstekPort)
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
            # Retry once after a short local-API timeout.
        }
        finally {
            $udp.Dispose()
        }
    }
    throw 'No ES.GetMode response from Marstek'
}

$fronius = Invoke-RestMethod `
    -Uri "http://$FroniusHost/solar_api/v1/GetPowerFlowRealtimeData.fcgi" `
    -TimeoutSec 2
if ($fronius.Head.Status.Code -ne 0) {
    throw "Fronius API status $($fronius.Head.Status.Code)"
}
$froniusGrid = [double]$fronius.Body.Data.Site.P_Grid

try {
    $marstekPower = Get-MarstekOnGridPower
    Set-Content -LiteralPath $statePath -Value (
        $marstekPower.ToString([Globalization.CultureInfo]::InvariantCulture)
    )
}
catch {
    # Decay a last-known output toward zero on API loss. This avoids feeding
    # the uncorrected load-position reading indefinitely while also tolerating
    # an occasional missed UDP reply.
    $marstekPower = 0.0
    if (Test-Path -LiteralPath $statePath) {
        [void][double]::TryParse(
            (Get-Content -Raw -LiteralPath $statePath).Trim(),
            [Globalization.NumberStyles]::Float,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$marstekPower
        )
    }
    $marstekPower *= 0.5
    Set-Content -LiteralPath $statePath -Value (
        $marstekPower.ToString([Globalization.CultureInfo]::InvariantCulture)
    )
}

# Positive Fronius P_Grid is import. Positive Marstek ongrid_power is battery
# discharge, which reduces that import; negative battery power is charging.
$actualGrid = $froniusGrid - $marstekPower
$actualGrid.ToString([Globalization.CultureInfo]::InvariantCulture)
