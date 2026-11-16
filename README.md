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
| `tools/` | Userspace helpers (placeholders for now, see `tools/*/README.md`) |

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

## Third-party work (links, not included)

- Intel's xe fan-control series (Karthik Poosa): https://patchwork.freedesktop.org/series/168027/
- Battlemage hwmon fix: https://github.com/hugs7/intel-xe-battlemage-hwmon-fix
- Fan/power latch after load: https://github.com/IGCIT/Intel-GPU-Community-Issue-Tracker-IGCIT/issues/1574
  and drm/xe issue #9462 (https://gitlab.freedesktop.org/drm/xe/kernel/-/issues/9462)

## License

- `patches/`: GPL-2.0 (`patches/LICENSE`), like the kernel they modify. Patch 0003 keeps its original
  author.
- `docs/` and `tools/`: **TODO, license to be chosen.**
