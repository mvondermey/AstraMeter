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
It keeps one serialized UDP socket bound to the configured OpenAPI port (both
local source and device destination port, normally `30000`) for the lifetime of
the controller. This matches the Venus E protocol's reply path and avoids the
ephemeral source ports used by an unbound UDP socket.

The same UDP OpenAPI is documented for a Venus connected through Wi-Fi or its
wired Ethernet port. Ethernet may receive a different MAC address and DHCP IP
than Wi-Fi. After changing interfaces, use a one-time documented
`Marstek.GetDevice` discovery, update the cached address, and validate the
device identity with a read call before resuming control. Runtime recovery
continues to avoid broadcast discovery.

`--api-request-gap` can override the conservative 10-second default after a
stable wired-Ethernet path has been verified. Because each control cycle sends
one `ES.GetMode` followed by one `ES.SetMode`, a 2.5-second request gap produces
an approximately 5-second setpoint refresh. Keep the default on Wi-Fi.

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

On Windows systems with Smart App Control or WDAC enabled, use an officially
signed Python installation for unattended tasks. Some standalone `uv` Python
runtimes may be rejected with Code Integrity events 3033/3077 even though an
already-running process continues to work. Do not disable the security policy;
install the signed python.org build and point Task Scheduler at it instead:

```powershell
winget install --id Python.Python.3.14 --exact --scope user
& "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe" `
  src\astrameter\fronius_marstek_direct.py
```

Using the script path directly avoids needing another virtual environment; the
direct controller and opportunity report use only the Python standard library.

The battery is identified by its stable BLE MAC/device suffix. Its last known
address is read from `.marstek-direct-ip`; reserve that address for the battery
in the router so DHCP does not change it.

## Second-battery opportunity report

The controller log retains the raw, unclamped `P_Grid` value as well as the
bounded battery target. A separate read-only report can therefore estimate how
long available PV surplus exceeded one battery's power limit and how much
additional energy another battery could theoretically have accepted:

```powershell
.venv\Scripts\python.exe -m astrameter.fronius_marstek_opportunity `
  --log-file fronius-marstek-direct.log `
  --output second-battery-opportunity.csv `
  --date yesterday `
  --power-limit 2500
```

The report reads the active log plus numeric rotated backups and atomically
upserts one CSV row per day. Gaps longer than 10 seconds are capped rather than
being counted as measured time. In addition to surplus above the power limit,
the report separately estimates surplus observed at 99% or higher SOC. These
figures are theoretical opportunities, not guaranteed usable battery energy;
conversion losses, battery limits, load changes, and the meter topology still
apply. Use multiple representative days before making a capacity decision.
