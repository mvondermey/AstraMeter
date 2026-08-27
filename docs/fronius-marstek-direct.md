# Direct Fronius to Marstek control on Windows

This controller bypasses Shelly emulation and the Marstek phase-diagnosis flow.
It is intended for a Fronius Smart Meter at the load position whose calculated
`P_Grid` value does not include the Marstek AC output.

The sign mapping is direct: positive Fronius `P_Grid` requests battery
discharge, while negative `P_Grid` requests charging. Commands use Marstek
`Passive` mode and expire after 15 seconds, so a stopped controller does not
leave a stale non-zero setpoint active.

Run once without controlling the battery:

```powershell
.venv\Scripts\python.exe -m astrameter.fronius_marstek_direct --once --dry-run
```

Run continuously:

```powershell
.venv\Scripts\python.exe -m astrameter.fronius_marstek_direct
```

The battery is identified by its stable BLE MAC/device suffix. UDP discovery
updates `.marstek-direct-ip` automatically if DHCP assigns a new IP address.
