# Direct Fronius to Marstek control on Windows

This controller bypasses Shelly emulation and the Marstek phase-diagnosis flow.
It is intended for a Fronius Smart Meter at the load position whose calculated
`P_Grid` value does not include the Marstek AC output.

The sign mapping is direct: positive Fronius `P_Grid` requests battery
discharge, while negative `P_Grid` requests charging. Commands use Marstek
`Passive` mode and expire after 45 seconds, so a stopped controller does not
leave a stale non-zero setpoint active.

The Venus E 3.0 local UDP service occasionally drops responses even when Wi-Fi
remains reachable. The controller therefore keeps at least 10 seconds between
API requests, validates `ES.GetMode` before every `ES.SetMode`, and makes up to
three attempts. It does not use UDP broadcast discovery during recovery.

The controller does not impose its own SOC, depth-of-discharge, backup, grid,
or protection limits. Those remain under the Marstek battery firmware and app
configuration. Reported SOC is validated and logged, but never changes the
Fronius-derived power target.

Run once without controlling the battery:

```powershell
.venv\Scripts\python.exe -m astrameter.fronius_marstek_direct --once --dry-run
```

Run continuously:

```powershell
.venv\Scripts\python.exe -m astrameter.fronius_marstek_direct
```

The battery is identified by its stable BLE MAC/device suffix. Its last known
address is read from `.marstek-direct-ip`; reserve that address for the battery
in the router so DHCP does not change it.
