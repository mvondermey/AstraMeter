$ErrorActionPreference = 'Stop'

$pythonPath = 'C:\Users\mvond\AppData\Roaming\uv\python\cpython-3.14.7-windows-x86_64-none\python.exe'

# A denied Windows Defender prompt created explicit application block rules.
# Disable only rules attached to AstraMeter's uv-managed Python runtime.
Get-NetFirewallApplicationFilter |
    Where-Object { $_.Program -ieq $pythonPath } |
    ForEach-Object {
        $rule = $_ | Get-NetFirewallRule
        if ($rule.Action -eq 'Block' -and $rule.Enabled -eq 'True') {
            $rule | Disable-NetFirewallRule
        }
    }

foreach ($port in 1010, 2220) {
    $displayName = "AstraMeter Shelly UDP $port"
    if (-not (Get-NetFirewallRule -DisplayName $displayName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule `
            -DisplayName $displayName `
            -Direction Inbound `
            -Action Allow `
            -Protocol UDP `
            -LocalPort $port `
            -RemoteAddress '192.168.178.0/24' `
            -Profile Any | Out-Null
    }
}

Write-Host 'AstraMeter firewall rules configured successfully.'
