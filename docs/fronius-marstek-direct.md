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

At or below the configured minimum SOC (12% by default), positive discharge
requests are held at 0 W and checked once per minute. Negative charging
requests remain allowed. Use `--min-soc` and `--reserve-interval` to match a
different reserve configuration.

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
