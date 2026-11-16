#!/bin/bash
# Build a patched xe.ko (patches 0004 power_limit_unclamped + 0005 debugfs oc/) for the RUNNING Ubuntu kernel, from the
# matching Ubuntu source package (no root needed). Output: ~/opt/xe-oc/<kernelrelease>/xe.ko
# Optional: XEOC_EXTRA="a.patch b.patch" applies further patches after 0004 + 0005, e.g. Intel's xe fan-control series
# 168027 (https://patchwork.freedesktop.org/series/168027/) for tools/b70-fan-smooth. Use XEOC_DIR=~/opt/xe-oc-fan for
# such a build so an installed module's staging dir is untouched.
#   xe-oc-build.sh [kernelrelease]        (default: uname -r)
# Needs the matching linux-headers package installed. Rerun after every kernel update; install the module only if
# its vermagic matches (this script checks).
set -euo pipefail
KR=${1:-$(uname -r)}; O=${XEOC_DIR:-$HOME/opt/xe-oc}; P=${XEOC_PATCHES:-$(cd "$(dirname "$0")/../patches" && pwd)}
VER=$(dpkg-query -W -f='${Version}' "linux-image-$KR" 2>/dev/null || dpkg-query -W -f='${Version}' "linux-image-unsigned-$KR")
UP=${VER%%-*}                                   # 7.0.0
mkdir -p "$O/src-dl" "$O/$KR"; cd "$O/src-dl"
DSC=linux_$VER.dsc; BASE=https://archive.ubuntu.com/ubuntu/pool/main/l/linux
[ -s "$DSC" ] || curl -sfL -o "$DSC" "$BASE/$DSC"
for f in "linux_$UP.orig.tar.gz" "linux_$VER.diff.gz"; do [ -s "$f" ] || curl -sfL -o "$f" "$BASE/$f"; done
grep -A2 '^Checksums-Sha256' "$DSC" | tail -2 | awk '{print $1"  "$3}' | sha256sum -c
S=$O/$KR/linux; rm -rf "$S"; mkdir -p "$S"
tar xzf "linux_$UP.orig.tar.gz" -C "$S" --strip-components=1
zcat "linux_$VER.diff.gz" > "$O/$KR/ubuntu.diff"; patch -d "$S" -p1 -s -i "$O/$KR/ubuntu.diff"
PKGS=("$P"/0004-*.patch "$P"/0005-*.patch)
# shellcheck disable=SC2206
[ -n "${XEOC_EXTRA:-}" ] && PKGS+=(${XEOC_EXTRA})
for p in "${PKGS[@]}"; do patch -d "$S" -p1 -s -i "$p"; done
# No kconfig run (needs flex/bison, not installed; no root): seed the tree with the installed headers package's
# generated config, generated headers and prebuilt kbuild host tools / objtool, i.e. exactly the running kernel's.
H=/usr/src/linux-headers-$KR
cp "$H/.config" "$S/.config"; cp "$H/Module.symvers" "$S/Module.symvers"
cp -a "$H/include/config" "$S/include/"; cp -a "$H/include/generated" "$S/include/"
cp -a "$H/arch/x86/include/generated" "$S/arch/x86/include/"
( cd "$H/scripts" && find -L . -type f -perm -u+x ) | while read -r f; do mkdir -p "$S/scripts/$(dirname "$f")"; cp -L "$H/scripts/$f" "$S/scripts/$f"; done
cp -L "$H/scripts/module.lds" "$S/scripts/" 2>/dev/null || true
mkdir -p "$S/tools/objtool"; cp -L "$H/tools/objtool/objtool" "$S/tools/objtool/" 2>/dev/null || true
cmp -s "/boot/config-$KR" "$S/.config" || echo "note: headers .config differs from /boot/config-$KR" >&2
cd "$S"
K="KERNELRELEASE=$KR"
nice -n 19 make -s -j"${XEOC_JOBS:-$(nproc)}" M=drivers/gpu/drm/xe modules "$K"
cp drivers/gpu/drm/xe/xe.ko "$O/$KR/xe.ko"; strip --strip-debug "$O/$KR/xe.ko"   # like the distro modules
modinfo -F vermagic "$O/$KR/xe.ko" | tee "$O/$KR/vermagic.txt"
modinfo -F vermagic "/lib/modules/$KR/kernel/drivers/gpu/drm/xe/xe.ko.zst" > "$O/$KR/vermagic.stock.txt"
cmp -s "$O/$KR/vermagic.txt" "$O/$KR/vermagic.stock.txt" && echo "vermagic matches $KR" || { echo "VERMAGIC MISMATCH" >&2; exit 1; }
modinfo -F parm "$O/$KR/xe.ko" | grep -q power_limit_unclamped && echo "has power_limit_unclamped"
[ -n "${XEOC_EXTRA:-}" ] && { strings "$O/$KR/xe.ko" | grep -c pwm1_auto_point10_pwm >/dev/null && echo "has fan control (pwm1_*)"; }
sha256sum "$O/$KR/xe.ko" > "$O/$KR/xe.ko.sha256"
