#!/bin/bash
# b70-fan-refresh.sh: stop the Intel Arc Pro B70 (Battlemage, xe driver) from keeping a loud fan after a load.
#
# Problem (IGCIT #1574): once the GT drops into gt-c6 after a load, pcode's temperature telemetry freezes at the last
# loaded values; the firmware fan curve follows those stale readings and keeps the fan fast at idle.
# Fix: while the GT sleeps and the REPORTED package temperature is still high, wake it for ~2 ms (hold debugfs
# forcewake_all open) every REFRESH_S seconds so pcode refreshes the readings. Once the reading is low, do nothing:
# a stale LOW reading on a cool card is harmless, and repeated wakes would keep the GT out of RC6.
# Package temperature only: a GT wake refreshes pkg but not VRAM (gating on VRAM keeps waking forever).
#
# Needs root (debugfs) and a mounted debugfs. No kernel patch needed. Bash + coreutils only.
# Env / flags: B70_PCI (auto: the first xe device with an "xe" hwmon), REFRESH_S (10), HOLD_MS (2), PKG_C (50),
#              DRY_RUN=1 or --dry-run (log what it would do, never wake), --once (one check, then exit), VERBOSE=1.
set -u
REFRESH_S=${REFRESH_S:-10}; HOLD_MS=${HOLD_MS:-2}; PKG_C=${PKG_C:-50}; DRY_RUN=${DRY_RUN:-0}; ONCE=0; VERBOSE=${VERBOSE:-0}
for a in "$@"; do case "$a" in --dry-run) DRY_RUN=1;; --once) ONCE=1;; -v|--verbose) VERBOSE=1;;
  -h|--help) sed -n '2,15p' "$0"; exit 0;; *) echo "unknown argument: $a" >&2; exit 2;; esac; done
log() { echo "b70-fan-refresh: $*"; }

find_dev() {   # -> PCI address of the first xe device that has an "xe" hwmon
  local d
  for d in /sys/bus/pci/drivers/xe/0000:*; do
    [ -d "$d" ] || continue
    for h in "$d"/hwmon/hwmon*; do [ "$(cat "$h/name" 2>/dev/null)" = xe ] && { basename "$d"; return 0; }; done
  done
  return 1
}
PCI=${B70_PCI:-$(find_dev)} || { log "no xe device with hwmon found"; exit 1; }
DEV=/sys/bus/pci/devices/$PCI
hwmon() { local h; for h in "$DEV"/hwmon/hwmon*; do [ "$(cat "$h/name" 2>/dev/null)" = xe ] && { echo "$h"; return 0; }; done; return 1; }
pkg_mc() {   # reported package temperature, millidegrees C (the hwmon channel labelled "pkg")
  local h=$1 l
  for l in "$h"/temp*_label; do [ "$(cat "$l" 2>/dev/null)" = pkg ] && { cat "${l%_label}_input"; return 0; }; done
  return 1
}
IDLE=$DEV/tile0/gt0/gtidle/idle_status
FW=/sys/kernel/debug/dri/$PCI/forcewake_all
[ -r "$IDLE" ] || { log "no $IDLE (not an xe Battlemage device?)"; exit 1; }
if [ "$DRY_RUN" != 1 ]; then
  [ "$(id -u)" = 0 ] || { log "needs root (debugfs); use --dry-run to test without it"; exit 1; }
  [ -e "$FW" ] || { log "no $FW (mount debugfs: mount -t debugfs none /sys/kernel/debug)"; exit 1; }
fi
HOLD=$(awk -v m="$HOLD_MS" 'BEGIN { printf "%.3f", m / 1000 }')
log "device $PCI, every ${REFRESH_S}s: wake ${HOLD_MS} ms while gt0 is in gt-c6 and pkg >= ${PKG_C} C$([ "$DRY_RUN" = 1 ] && echo ' (dry run)')"
n=0
while :; do
  [ "$ONCE" = 1 ] || sleep "$REFRESH_S"
  h=$(hwmon) || { [ "$VERBOSE" = 1 ] && log "hwmon gone"; [ "$ONCE" = 1 ] && exit 1; continue; }
  st=$(cat "$IDLE" 2>/dev/null); t=$(pkg_mc "$h") || t=""
  if [ "$st" = gt-c6 ] && [ -n "$t" ] && [ "$t" -ge $(( PKG_C * 1000 )) ]; then
    if [ "$DRY_RUN" = 1 ]; then log "would wake: $st, pkg $((t / 1000)) C"
    elif sleep "$HOLD" 3<"$FW"; then n=$((n + 1)); [ "$VERBOSE" = 1 ] && log "woke ($n): pkg was $((t / 1000)) C"   # fd held for the sleep
    else log "could not open $FW"
    fi
  elif [ "$VERBOSE" = 1 ]; then log "no wake: $st, pkg ${t:+$((t / 1000)) C}"; fi
  [ "$ONCE" = 1 ] && exit 0
done
