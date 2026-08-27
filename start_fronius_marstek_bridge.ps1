$ErrorActionPreference = 'Stop'

Set-Location -LiteralPath $PSScriptRoot

# During Marstek's phase diagnosis the Venus temporarily stops answering its
# local status API. Allow raw Fronius readings only for this bounded startup
# window so the diagnostic pulse can be detected. Normal fail-safe behavior
# takes over automatically afterwards.
$graceUntil = [DateTimeOffset]::UtcNow.AddSeconds(90).ToUnixTimeSeconds()
Set-Content `
    -LiteralPath (Join-Path $PSScriptRoot '.fronius-marstek-diagnostic-until') `
    -Value $graceUntil
Set-Content `
    -LiteralPath (Join-Path $PSScriptRoot '.fronius-marstek-last-power') `
    -Value '0'

& (Join-Path $PSScriptRoot '.venv\Scripts\astrameter.exe') --loglevel info
exit $LASTEXITCODE
