# b70-fan-refresh: the loud fan after a load (IGCIT #1574)

**Symptom.** After a load, once the GT idles in `gt-c6`, the card's pcode temperature telemetry freezes at the last
loaded values. The firmware fan curve follows those stale readings, so the fan stays fast at idle. Measured: pkg / VRAM read a frozen 68 / 80 C while the card was really 39 / 42 C, and the fan held ~2,390 rpm. That
was identical with the firmware curve (`pwm1_enable 2`) and a user table, so the input is wrong, not the curve. Reading
hwmon does not wake the GT. Any tiny GPU request woke it: within ~30 s the readings fell to 39 / 42 C and the fan to ~1,095 rpm.

**Fix (userspace, no kernel patch).** Every `REFRESH_S` (10) seconds, if `tile0/gt0/gtidle/idle_status` is `gt-c6`
and the reported **package** temperature is >= `PKG_C` (50) C, hold debugfs `dri/<pci>/forcewake_all` open for
`HOLD_MS` (2) ms. The GT wakes briefly and pcode refreshes the readings. Once the reading is low it does nothing.

Install: `install -m755 b70-fan-refresh.sh /usr/local/sbin/ && install -m644 b70-fan-refresh.service /etc/systemd/system/ && systemctl enable --now b70-fan-refresh`.
Test first: `sudo b70-fan-refresh.sh --dry-run --once -v`. Knobs: `B70_PCI` (auto-detected), `REFRESH_S`, `HOLD_MS`,
`PKG_C`, `DRY_RUN=1`, `VERBOSE=1`.

**Caveats (measured):**
- Gate on the package temperature only. A GT forcewake refreshes pkg but **not** VRAM: VRAM stayed at a stale 72 C,
  and a VRAM gate kept waking every 10 s (17 wakes in 3 min, the fan swinging ~600 <-> 1,250 rpm).
- Don't wake unconditionally. A stale LOW reading on a cool card is harmless, while a poller that keeps waking the GT
  leaves it almost never in RC6 (idle power). The tool stops once the card reads cool.
- Needs root and a mounted debugfs. `forcewake_all` is a debug interface; its path can change between kernels.
- Reading temperatures while the GT is in gt-c6 shows the stale value: judge results by the fan speed, or after a
  refresh.
- Kernel alternative (not used here): github.com/hugs7/intel-xe-battlemage-hwmon-fix forcewakes before every hwmon
  temperature read (drm/xe #4560 / #7805 RFC patches). Same mechanism, but it wakes on every read.

Issue: https://github.com/IGCIT/Intel-GPU-Community-Issue-Tracker-IGCIT/issues/1574 (linked to drm/xe #9462).
License: MIT (`../../LICENSE`).
