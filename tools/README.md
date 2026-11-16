# tools

Userspace helpers for the B70. **Not published yet**: each directory holds a placeholder README until the
code is reviewed and scrubbed.

| Dir | What | Needs |
|---|---|---|
| `b70-fan-refresh/` | Standalone script + systemd unit for the IGCIT #1574 workaround (periodic `forcewake_all` refresh while the GT is idle and the package reading is stale-high; see `../docs/monitoring.md`) | debugfs, root |
| `b70-fan-smooth/` | Optional fan-curve smoothing | Intel's fan-control series https://patchwork.freedesktop.org/series/168027/ |
| `b70-powerd/` | Power/fan daemon (`b70-powerd.py`, `b70-power` CLI, `xe-oc-build.sh`) | patches 0004 + 0005 |

License: TODO, to be chosen (the GPL-2.0 in `../patches/` covers only the kernel patches).
