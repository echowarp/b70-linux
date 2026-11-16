# Monitoring an Arc Pro B70 without keeping it awake

Measured on one ASRock Arc Pro B70 Creator in a server that serves an LLM. Card power is from the xe hwmon
`energy1_input` counter; RC6 residency is from `idle_residency_ms`.

## Which readers keep the GT out of RC6

Each phase ran for 181 s with the card drained of work; counters were read only at the phase ends.

| Reader running | RC6 gt0 | RC6 gt1 | Card W |
|---|---|---|---|
| nothing | 99.9 % | 100.0 % | 46 |
| `xpu-smi dump` at 1 Hz + DRM fdinfo engine-cycle reads | **0.0 %** | **0.0 %** | **57** |
| hwmon reads (energy, power, temps) every 15 s | 99.9 % | 100.0 % | 46 |
| an exporter that reads the inference engine's HTTP `/metrics` | 98.5 % | 100.0 % | 46 |
| periodic small inference requests (health probes) | 68 % | 100 % | 136 |

- **Keeps the GT awake:** `xpu-smi dump` at 1 Hz together with fdinfo cycle reads. On our card that costs
  ~11 W at idle (about 0.27 kWh/day). With it running, `tile0/gt*/gtidle/idle_status` stays at `gt-c0`
  and `idle_residency_ms` never moves.
- **Safe:** hwmon energy at a slow cadence (derive power from the cumulative counter), `idle_residency_ms`,
  PCI `runtime_*_time`, and the serving engine's own `/metrics` endpoint, which never touches the GPU.
- Health probes that send real requests wake the GPU by design; count them in your idle budget.

## Gated telemetry

Keep the rich telemetry, but only while there is work:

1. Use a GPU-free busy signal: the engine's `/metrics` (requests running or waiting).
2. Start `xpu-smi` and the fdinfo reads only while that shows work, and for ~30 s after the last request.
3. Leave the columns empty while idle; keep hwmon energy and `idle_residency_ms` running all the time.

Overnight this took the card from a flat 56-57 W to the ~46 W RC6 floor.

## Idle power numbers

- Idle (RC6, nothing polling): **~46 W** card power, with PCIe ASPM disabled on the whole path (our
  platform's firmware does not hand ASPM to the OS).
- With the 1 Hz `xpu-smi` poller: ~57 W. Typical serving decode: 265-324 W at a 300-330 W limit.
- For reference, a Windows review measured a single B70 idling at 26 W, and users report Arc cards going
  from 30-40 W to 5-7 W once ASPM L1 is enabled. **ASPM is the larger lever** on platforms that allow it.

## ASPM warning

Do **not** boot with `pcie_aspm.policy=powersupersave` (L1.1/L1.2 substates): the B70 then fails to come
out of D3cold at boot (xe probe fails with -71). A PCI quirk disabling the L1 substates on the card's
upstream switch port has been proposed. `pcie_aspm.policy=powersave` (L0s + L1) works. If other devices
share the machine (for example a GPU on an external link), check that they tolerate ASPM before forcing it
globally with `pcie_aspm=force`.

## The fan latch after load (IGCIT #1574)

After a load ends, the reported package and VRAM temperatures can freeze at their last high values while
the GT sits in RC6. Firmware fan control follows the stale readings, so the fan stays at ~2,400 rpm on an
idle card. Intel has reproduced it; it is linked to drm/xe #9462 (package latched in G2). Any small request
or a forcewake refreshes the readings.

**Userspace workaround:** every 10 s, if gt0 `idle_status` is `gt-c6` and the reported package temperature
is at least 50 °C, hold the debugfs `dri/<pci>/forcewake_all` file open for ~2 ms (opening it takes a
forcewake reference, closing it drops it). This refreshes the **package** reading only; a forcewake does not
refresh the VRAM sensor, but the package reading is what drags the fan along. Measured: package/VRAM
68/80 °C → 39/42 °C and the fan 2,390 → 1,095 rpm within 30 s. The cost is a 2 ms wake every 10 s, and only
while the latch condition holds.

## Links

- IGCIT issue: https://github.com/IGCIT/Intel-GPU-Community-Issue-Tracker-IGCIT/issues/1574
- drm/xe #9462: https://gitlab.freedesktop.org/drm/xe/kernel/-/issues/9462
- Intel xe fan-control series (Karthik Poosa): https://patchwork.freedesktop.org/series/168027/
- Battlemage hwmon fix: https://github.com/hugs7/intel-xe-battlemage-hwmon-fix
- Pollers keeping a B70 awake (llama.cpp): https://github.com/ggml-org/llama.cpp/issues/24946
- Intel on ASPM and Arc idle power: https://www.intel.com/content/www/us/en/support/articles/000092564/graphics.html
