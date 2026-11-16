# tools

Userspace helpers for the B70. None of them need a kernel patch except where noted.

| Path | What | Needs |
|---|---|---|
| `b70-fan-refresh/` | Script + systemd unit for the IGCIT #1574 workaround: a ~2 ms `forcewake_all` hold every 10 s while gt0 is in `gt-c6` and the reported package temperature is >= 50 C (see `../docs/monitoring.md`) | root, debugfs; bash + coreutils |
| `b70-fan-smooth/` | Optional calmer fan curve (stock curve fed a 20 s average, slow up / very slow down; firmware still ramps above 80 C) | Intel's xe fan-control series https://patchwork.freedesktop.org/series/168027/ (not included), root |
| `b70-powerd/` | Root daemon: power limit via `oc/power_limit`, VRAM guard, re-apply after reset, plus the two fan functions above | patch 0005 (for > 275 W), root |
| `xe-oc-build.sh` | Builds a patched `xe.ko` (0004 + 0005, optional extra patches) for the running Ubuntu kernel from Ubuntu's source package, and checks its vermagic | matching linux-headers package |

License: MIT (`../LICENSE`); the GPL-2.0 in `../patches/` covers only the kernel patches.
