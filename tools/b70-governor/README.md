# b70-governor

Makes the Arc Pro B70's power limit and fan mode follow the desktop power plan (power-profiles-daemon:
power-saver / balanced / performance). It is a small user service that reads the active profile over D-Bus
(`busctl`, polled every second) and tells `../b70-powerd` what to apply. It needs `b70-powerd` running as root and
the xe OC patches (0004 + 0005, see `../../patches/`) for limits above the stock 275 W.

## Defaults

| Profile | Power limit | Fan |
|---|---|---|
| power-saver | `SAVER_W` = 190 W | `smooth` (needs Intel's xe fan-control series 168027; ignored by b70-powerd without it) |
| balanced | `BALANCED_W` = 190 W | `stock` |
| performance | `PERF_W` = 300 W | `stock` |

Targets are clamped to `MIN_W` (150) .. `MAX_W` (330, the board maximum; b70-powerd clamps too).
`FAN_SMOOTH`: 1 = smooth fan in power-saver only (default), 2 = in every profile, 0 = never.
`POLL_S` (1) is the profile polling interval.

## Configuration

`KEY=VALUE` lines in `/etc/b70-governor.conf`, then `~/.config/b70-governor/conf` (later wins); an environment
variable of the same name (e.g. `Environment=PERF_W=330` in the unit) wins over both. The files are re-read every
poll, so edits apply without a restart. Example:

```
SAVER_W=190
BALANCED_W=250
PERF_W=330
FAN_SMOOTH=2
```

## How it talks to b70-powerd

One line per request on the Unix socket `/run/b70-powerd.sock` (`B70_POWERD_SOCK` to change it; b70-powerd serves
only root and `B70_POWERD_ALLOW_UID`, default 1000, so run the governor as that user):
`set <watts>` and `fan smooth|stock`. Both are re-sent every 60 s in case b70-powerd restarted, and logged only
when they change.

Safe fallback: if the profile cannot be read it uses `balanced`; if b70-powerd is unreachable it changes nothing
and retries (the card keeps its current limit; b70-powerd itself restores stock on stop and has its own VRAM
temperature guard).

## Manual override

`../b70-powerd/b70-power 250` writes `~/.config/b70-governor/override` and the governor holds 250 W regardless of
profile until `b70-power auto` removes it.

## Install

```sh
install -m755 b70-governor.py ~/.local/bin/
install -Dm644 b70-governor.service ~/.config/systemd/user/b70-governor.service
systemctl --user daemon-reload && systemctl --user enable --now b70-governor
```

Dry run (logs what would be sent, sends nothing): `b70-governor.py --dry-run --once`.
Python 3 standard library and `busctl` only.

License: MIT (`../../LICENSE`).
