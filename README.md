# b70-linux

Linux kernel patches and notes for running an **Intel Arc Pro B70** (Battlemage, `xe` driver) as a
compute card: power-limit overclocking within the board's own envelope, an xe GuC race fix, and what we
learned about idle power, monitoring and the fan/temperature latch.

Everything here was developed and measured on one ASRock Arc Pro B70 Creator serving an LLM around the
clock. Treat the numbers as one board's results, not a specification.

> **Warning: risk and warranty.** These patches let you raise the card's power limit through the
> firmware's own overclocking interface (the one the Windows control panel uses). The firmware still
> clamps every request to the board's advertised maximum (+20 % on our card: 275 W default, 330 W max),
> but running above the default draws more power, runs hotter, may shorten the card's life and may void
> your warranty. The GDDR rate patch (0006) **hung our GPU** and is experimental. You use all of this at
> your own risk. Watch the VRAM temperature after any change.

## Contents

| Path | What |
|---|---|
| `patches/0003-...` | drm/xe/guc: fix the `wake_up` race in `handle_sched_done` (backport of a reviewed upstream posting, not merged yet when we applied it) |
| `patches/0004-...` | drm/xe/hwmon: opt-in module param `xe.power_limit_unclamped=1` skips xe's extra clamp to the boot-default power limit (pcode still clamps to the card maximum) |
| `patches/0005-...` | drm/xe: debugfs for the Battlemage pcode overclocking domains (`dri/<pci>/oc/{domains,power_limit,write,exit}`) |
| `patches/0006-...` | **EXPERIMENTAL**: module param `xe.oc_mem_mbps` sets the GDDR rate at probe. Unstable on our board (GT0 forcewake hang right after any rate above stock); we run with it disabled |
| `docs/monitoring.md` | Which telemetry readers keep the GT out of RC6, idle-power numbers, ASPM warning, the stale-temperature fan latch and a userspace workaround |
| `tools/b70-fan-refresh/` | Script + systemd unit: userspace workaround for the stale-temperature fan latch (IGCIT #1574), no kernel patch |
| `tools/b70-fan-smooth/` | Optional calmer fan curve (script + unit); needs Intel's xe fan-control series 168027 |
| `tools/b70-powerd/` | Root daemon: power limit via `oc/power_limit`, VRAM guard, re-apply after reset, plus both fan functions |
| `tools/b70-governor/` | User service: follows the desktop power plan (power-saver / balanced / performance) and sets the power limit and fan mode through b70-powerd; `b70-power` (in `tools/b70-powerd/`) is the manual-override CLI |
| `tools/xe-oc-build.sh` | Builds a patched `xe.ko` (0004 + 0005) for the running Ubuntu kernel from Ubuntu's source package |

### Base kernel

- Built and run on **vanilla Linux 7.1.13**, patches applied in order 0003, 0004, 0005, 0006. 0005 builds
  on 0004, and 0006 on 0005.
- A second B70 box applied **0004 + 0005 on Ubuntu's 7.0.0-38** kernel.
- On kernels **older than 7.2** we also carry two upstream fixes that are not in this repo; take them from
  upstream (`git cherry-pick`):
  - `818bebeb63dd` "drm/xe: Don't hand out the flat CCS storage as usable VRAM"
  - `4d33314decfe` "drm/xe/guc: Add support for NO_RESPONSE_BUSY in CTB" (needed before any GuC firmware
    upgrade)

## Build

You only need a new `xe.ko`; the rest of the kernel can stay as it is, as long as the module is built
against the exact tree and config of the running kernel (same version and vermagic).

```sh
# 1. Source tree matching your running kernel (vanilla example)
wget https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.1.13.tar.xz
tar xf linux-7.1.13.tar.xz && cd linux-7.1.13
git init -q && git add -A && git commit -qm "linux 7.1.13 vanilla"   # optional, makes git am work

# 2. Patches (add 0006 only if you want to experiment with the memory rate)
git am ../b70-linux/patches/0003-*.patch ../b70-linux/patches/0004-*.patch ../b70-linux/patches/0005-*.patch

# 3a. Full kernel as .deb packages (reversible with dpkg), starting from your distro config
cp /boot/config-$(uname -r) .config
scripts/config --set-str LOCALVERSION "-b70" --disable LOCALVERSION_AUTO \
               --set-str SYSTEM_TRUSTED_KEYS "" --set-str SYSTEM_REVOCATION_KEYS ""
make olddefconfig
nice -n 19 make -j"$(nproc)" bindeb-pkg

# 3b. Or only the xe module, for a tree that already matches the running kernel
make olddefconfig && make modules_prepare
make -j"$(nproc)" M=drivers/gpu/drm/xe modules
```

Install the module so it wins over the stock one, then reboot (xe is usually in the initramfs):

```sh
sudo install -D -m 644 drivers/gpu/drm/xe/xe.ko /lib/modules/$(uname -r)/updates/xe.ko
echo "options xe power_limit_unclamped=1" | sudo tee /etc/modprobe.d/xe-oc.conf
sudo depmod -a && sudo update-initramfs -u   # or: sudo dracut -f
```

Undo: remove both files, `depmod -a`, rebuild the initramfs, reboot. When you install a whole new kernel,
keep your known-good kernel as the default and try the new one with a one-shot `grub-reboot` first,
especially on a headless machine. Secure Boot needs the module signed with an enrolled key.

## Setting the power limit

Units are **1/8 W** (firmware domain 0x30). On our board: min 1096, default 2200 (275 W), max 2640
(330 W). Check yours first:

```sh
D=/sys/kernel/debug/dri/0000:03:00.0        # your B70's PCI address
cat $D/oc/domains                            # enumerated domains with min / max / default
echo 2640 | sudo tee $D/oc/power_limit       # 330 W; takes effect immediately
echo 2200 | sudo tee $D/oc/power_limit       # back to the 275 W default
```

- The setter replays the Windows driver's sequence, including its proportional rescaling of the related
  domains; every step should log status 0 in `dmesg`.
- `hwmon` `power1_cap` **still reads 275 W** afterwards; it is a different register. Confirm the change
  with the card power from `energy1_input` under load instead.
- The setting does not survive a cold boot; set it again from a boot-time unit or your launcher.
- `oc/write` takes `"<domain> <value>"` for raw writes. **Do not write the GDDR rate domain (0x17) live**:
  on our card that hung the GT until a power cycle.

## Measured effect (one board, LLM serving)

- 275 → 300 W: card power ~282 → ~308 W, GT clock 2,633-2,650 → 2,750-2,800 MHz, VRAM 80 → 82 °C,
  single-stream decode +3.9 %.
- 300 → 330 W (A/B under real serving load): **prefill +3-7 %, decode +2.8 % (1 stream) / +1.9 %
  (4 streams)**, VRAM maximum 84 °C at both settings.
- Decode on this card is mostly memory-bound, so the gain from power is modest; prefill gains more.

## Fan: why it stays loud, and how to get it lower

**The fan curve only updates when the GT is awake.** The firmware fan follows the temperatures pcode reports. Once
the GT drops into `gt-c6` after a load, those readings stop updating: hwmon reads do not wake the GT, so the package
and VRAM temperatures stay frozen at their last loaded values (seen: 68 / 80 °C reported while the card was really
39 / 42 °C) and the fan holds ~2,390 rpm on an idle card. It is the input, not the curve: the same happens in firmware
fan mode (`pwm1_enable 2`) and with a user table. A tiny GPU request refreshes all readings (39 / 42 °C and
~1,095 rpm within ~30 s); a bare forcewake refreshes the package reading but not VRAM. See IGCIT #1574 / drm/xe #9462.

**Getting it lower (what worked, one ASRock B70 Creator):**
- `tools/b70-fan-refresh/` polls on purpose, but only when it helps: every 10 s, if gt0 is in `gt-c6` and the
  reported package temperature is >= 50 °C, it holds `debugfs dri/<pci>/forcewake_all` for ~2 ms. In our one measurement of the refresh alone (after a 40 s load, no requests) it brought the package reading from 67 to
  48 °C while RC6 stayed at 100 %; VRAM stayed at a stale 72 °C. The idle fan speed with this pkg-only gate was not
  recorded, and we don't know which sensor the firmware fan follows, In daily use on that card it reliably brings the fan back down after a run, where without it the fan often stays at
  high speed (observed, not yet measured in rpm).
  It stops waking the GT once the card reads cool, so the GPU still reaches RC6 (a poller that wakes it unconditionally costs ~11 W at idle; see
  `docs/monitoring.md`).
- Gate on the **package** temperature only. A forcewake refreshes the package sensor but not VRAM; gating on VRAM kept
  waking the GT every 10 s (17 wakes in 3 min) and made the fan swing ~600 ↔ 1,250 rpm.
- `tools/b70-fan-smooth/` (needs Intel's fan-control series 168027) lowers and steadies it further: the stock curve
  fed a 20 s average of the package temperature, ramping up ≤ 0.5 %/s and down ≤ 0.05 %/s. It writes a user table
  flat below 80 °C and stock from 80 °C, so the firmware still ramps if the tool dies, and jumps to stock at ≥ 90 °C.
- `tools/b70-powerd/` combines both with the power limit; run either it or the two fan tools, not both.

## Third-party work (links, not included)

- Intel's xe fan-control series (Karthik Poosa): https://patchwork.freedesktop.org/series/168027/
- Battlemage hwmon fix: https://github.com/hugs7/intel-xe-battlemage-hwmon-fix
- Fan/power latch after load: https://github.com/IGCIT/Intel-GPU-Community-Issue-Tracker-IGCIT/issues/1574
  and drm/xe issue #9462 (https://gitlab.freedesktop.org/drm/xe/kernel/-/issues/9462)

## Attribution

- `patches/0003-drm-xe-guc-Fix-wake_up-race-in-handle_sched_done.patch` is **third-party**: author Jakub
  Legowski (reviewed by Matthew Brost), from the v3 posting on the stable list
  (https://ratatoskr.run/stable/2026/09/17545816). GPL-2.0, carried unmodified with its original header; we only
  applied it to 7.1.13. Not yet merged upstream when we took it.
- Patches 0004-0006, `docs/` and `tools/` are ours (Evan Boldt).

## License

- `patches/`: GPL-2.0 (`patches/LICENSE`), like the kernel they modify.
- Everything else (`docs/`, `tools/`, this README): MIT (`LICENSE`).
