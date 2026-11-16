# b70-powerd

Root daemon that sets the B70 power limit through patch 0005's `oc/power_limit` (falls back to hwmon
`power1_cap` <= 275 W without it), re-applies it after a card reset or reconnect, steps down to 300 W if VRAM stays
above 92 C, and restores stock (275 W) on stop. It also contains the same fan smoothing as `../b70-fan-smooth`
(off until `fan smooth`; needs Intel's series 168027) and the same IGCIT #1574 telemetry refresh as
`../b70-fan-refresh` (on by default, `B70_TELEM_REFRESH_S=0` turns it off). Run either this daemon or the standalone
tools, not both. All knobs are in the docstring at the top of `b70-powerd.py`.

Clients talk to a Unix socket (`/run/b70-powerd.sock`; only `B70_POWERD_ALLOW_UID` and root are served), one line
per request:

```sh
printf 'set 330\n' | sudo socat - UNIX-CONNECT:/run/b70-powerd.sock   # -> ok 330 oc/power_limit
printf 'get\n'     | socat - UNIX-CONNECT:/run/b70-powerd.sock
printf 'fan smooth\n' | socat - UNIX-CONNECT:/run/b70-powerd.sock
```

Run it as root from a systemd unit (`ExecStart=/usr/bin/python3 -I /usr/local/sbin/b70-powerd.py`, `Restart=on-failure`).
Python 3 standard library only.

License: MIT (`../../LICENSE`).
