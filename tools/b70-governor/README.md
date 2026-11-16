# b70-governor

Makes the Arc Pro B70's power limit and fan mode follow the desktop power plan (power-profiles-daemon:
power-saver / balanced / performance), with a boost to the performance level while any client asks for it
(interactive / priority traffic; see `../../docs/priority-boost.md`). It is a small user service that reads the active profile over D-Bus
(`busctl`, polled every second) and tells `../b70-powerd` what to apply. It needs `b70-powerd` running as root and
the xe OC patches (0004 + 0005, see `../../patches/`) for limits above the stock 275 W.

## Defaults

| Profile | Power limit | Fan |
|---|---|---|
| power-saver | `SAVER_W` = 190 W | `smooth` (needs Intel's xe fan-control series 168027; ignored by b70-powerd without it) |
| balanced | `BALANCED_W` = 190 W | `stock` |
| performance | `PERF_W` = 300 W | `stock` |

Why 190 W: it is the most tokens per joule on card power (`../../docs/efficiency.md`) and the card is near silent
running continuously at 190 W (owner's observation, no rpm measured at steady state); 300 W keeps 97-98 % of the
330 W speed. Noise is the third tradeoff next to speed and efficiency.

Boost: while any trigger is fresh the target is max(profile watts, `BOOST_W`) and the fan `stock`.
`BOOST_W` = 0 (default) means `PERF_W`. `BOOST_MAX_S` (600) caps any trigger's life after its last write;
`BOOST_POLL_S` (0.2) is how often the trigger directory is checked.

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
BOOST_W=330
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

## Boost triggers (interactive traffic)

Any client (an LLM router or middleware, a desktop app, a script) can request the boost level for a time:

```sh
b70-power boost [seconds] [id]   # default 30 s, id cli-$USER; re-run to extend
b70-power release [id]
```

A trigger is a file `/run/b70-boost/<id>` (`B70_BOOST_DIR` to change it) containing a unix expiry time, written
atomically (write `.<id>.tmp`, rename). One file per client id, so concurrent requesters don't clobber each other;
the governor boosts while **any** is fresh. Safe defaults:
- a trigger is fresh until min(its expiry, its mtime + `BOOST_MAX_S`), so stale or far-future triggers lapse;
- unreadable or malformed contents, non-regular files (FIFOs are never opened blocking), symlinks and dot files
  count as no boost; a missing directory means no boost;
- the manual override wins over a boost.

Create the directory at boot with the tmpfiles.d entry (sticky and world-writable like `/tmp`; edit it to a group
to limit who may boost):

```sh
sudo install -m644 b70-boost.tmpfiles /etc/tmpfiles.d/b70-boost.conf && sudo systemd-tmpfiles --create
```

`b70_boost_middleware.py` is a standard-library ASGI middleware (and importable helper for routers) that classifies
requests as interactive or background (explicit tag > agent name > origin host > default; rules in
`/etc/b70-boost-rules.json`, example `b70-boost-rules.example.json`) and holds one trigger, refreshed every 5 s with a
15 s TTL, while interactive requests are in flight, with a 10 s hold after the last one. With vLLM:
`PYTHONPATH=<this dir> vllm serve ... --middleware b70_boost_middleware.BoostMiddleware`. See
`../../docs/priority-boost.md` for the mechanism and what is measured.

## Manual override

`../b70-powerd/b70-power 250` writes `~/.config/b70-governor/override` and the governor holds 250 W regardless of
profile until `b70-power auto` removes it.

## Install

```sh
install -m755 b70-governor.py ~/.local/bin/
install -Dm644 b70-governor.service ~/.config/systemd/user/b70-governor.service
systemctl --user daemon-reload && systemctl --user enable --now b70-governor
```

Dry run (logs what would be sent, sends nothing): `b70-governor.py --dry-run --once`; with
`B70_BOOST_DIR=<a test dir>` and `b70-power boost 10 test` it shows the boost.
Python 3 standard library and `busctl` only.

License: MIT (`../../LICENSE`).
