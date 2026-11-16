#!/usr/bin/env python3
"""b70-fan-smooth.py: calmer fan for the Intel Arc Pro B70 (xe driver) - OPTIONAL, separate from b70-fan-refresh.sh.

Needs Intel's xe fan-control series 168027 ("Add fan control support", Karthik Poosa,
https://patchwork.freedesktop.org/series/168027/): hwmon pwm1_enable and pwm1_auto_point1..N_{temp,pwm}. Root.

The firmware follows the instantaneous package temperature, so the fan surges on every short load. This tool keeps the
stock curve but feeds it the package temperature averaged over TAU_S, and changes the speed slowly (UP_PCT_S up,
DOWN_PCT_S down). It writes a user table that is flat at the current speed below SAFE_C and the stock curve (or higher)
from SAFE_C up, so the firmware still ramps by itself if this process dies. At pkg >= HOT_C it jumps to the stock value at
once. On exit (SIGTERM / SIGINT / any error) it sets pwm1_enable 2 = the firmware curve.

Env: B70_PCI (auto), PERIOD_S (2), TAU_S (20), UP_PCT_S (0.5), DOWN_PCT_S (0.05), SAFE_C (80), HOT_C (90).
Flags: --dry-run (print the speed it would set; never writes), --stock (set pwm1_enable 2 and exit).
Python 3 standard library only.
"""
import glob, math, os, signal, sys, time

E = os.environ.get
PERIOD, TAU = float(E("PERIOD_S", "2")), float(E("TAU_S", "20"))
UP, DOWN = float(E("UP_PCT_S", "0.5")), float(E("DOWN_PCT_S", "0.05"))
SAFE_C, HOT_C = float(E("SAFE_C", "80")), float(E("HOT_C", "90"))
DRY = "--dry-run" in sys.argv


def log(m):
    print("b70-fan-smooth: " + m, flush=True)


def hwmon():
    pcis = [E("B70_PCI")] if E("B70_PCI") else [os.path.basename(d) for d in sorted(glob.glob("/sys/bus/pci/drivers/xe/0000:*"))]
    for p in pcis:
        for h in glob.glob(f"/sys/bus/pci/devices/{p}/hwmon/hwmon*"):
            try:
                if open(f"{h}/name").read().strip() == "xe":
                    return p, h
            except OSError:
                pass
    return None, None


def rd(p):
    return int(open(p).read())


def wr(p, v):
    if DRY:
        return
    with open(p, "w") as f:
        f.write(str(int(v)))


def pkg_c(h):
    for lab in glob.glob(f"{h}/temp*_label"):
        if open(lab).read().strip() == "pkg":
            return rd(lab[:-6] + "_input") / 1000
    raise RuntimeError("no pkg temperature channel")


def table(h):
    pts, i = [], 1
    while os.path.exists(f"{h}/pwm1_auto_point{i}_temp"):
        pts.append((rd(f"{h}/pwm1_auto_point{i}_temp") / 1000, rd(f"{h}/pwm1_auto_point{i}_pwm") * 100 / 255))
        i += 1
    return pts


def interp(stock, t):
    if t <= stock[0][0]:
        return stock[0][1]
    for (t0, s0), (t1, s1) in zip(stock, stock[1:]):
        if t <= t1:
            return s0 + (s1 - s0) * (t - t0) / max(t1 - t0, 1e-6)
    return stock[-1][1]


def write_table(h, stock, pct):
    """Flat at pct below SAFE_C, stock (but >= pct) from SAFE_C up; non-decreasing. The driver commits when the last
    point's temp and pwm are written."""
    n = len(stock)
    sp = [max(pct, s) if t >= SAFE_C else pct for t, s in stock]
    for i in range(1, n):
        sp[i] = max(sp[i], sp[i - 1])
    for i in range(n - 1):
        wr(f"{h}/pwm1_auto_point{i + 1}_pwm", round(sp[i] * 255 / 100))
    wr(f"{h}/pwm1_auto_point{n}_temp", round(stock[-1][0] * 1000))
    wr(f"{h}/pwm1_auto_point{n}_pwm", round(sp[-1] * 255 / 100))


def main():
    pci, h = hwmon()
    if not h:
        sys.exit("b70-fan-smooth: no xe device with hwmon found")
    if not os.path.exists(f"{h}/pwm1_enable") or not os.path.exists(f"{h}/pwm1_auto_point1_temp"):
        sys.exit("b70-fan-smooth: no pwm1_enable / pwm1_auto_point* (needs Intel's xe fan-control series 168027)")
    if not DRY and os.geteuid() != 0:
        sys.exit("b70-fan-smooth: needs root (or --dry-run)")
    if "--stock" in sys.argv:
        wr(f"{h}/pwm1_enable", 2); log("firmware curve (pwm1_enable 2)"); return

    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    try:
        if rd(f"{h}/pwm1_enable") != 2:
            wr(f"{h}/pwm1_enable", 2)             # back to auto first: the next 2 -> 1 copies the STOCK table
        wr(f"{h}/pwm1_enable", 1)
        stock = table(h)
        if len(stock) < 2 or any(b[0] < a[0] or b[1] < a[1] for a, b in zip(stock, stock[1:])):
            raise RuntimeError(f"unusable stock table {stock}")
        log(f"device {pci}: stock curve " + " ".join(f"{t:.0f}C:{s:.0f}%" for t, s in stock) + (" (dry run)" if DRY else ""))
        ema = cur = written = None
        while True:
            t = pkg_c(h)
            ema = t if ema is None else ema + (t - ema) * (1 - math.exp(-PERIOD / TAU))
            target = interp(stock, ema)
            cur = interp(stock, t) if cur is None else cur
            if t >= HOT_C:
                cur = max(cur, interp(stock, t))
            elif target > cur:
                cur = min(target, cur + UP * PERIOD)
            else:
                cur = max(target, cur - DOWN * PERIOD)
            ip = max(0, min(100, round(cur)))
            if ip != written:
                write_table(h, stock, ip); written = ip
                if DRY:
                    log(f"pkg {t:.0f} C (avg {ema:.1f}): would set {ip} % below {SAFE_C:.0f} C")
            time.sleep(PERIOD)
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:  # noqa: BLE001
        log(f"error {e!r}")
    finally:
        try:
            wr(f"{h}/pwm1_enable", 2)
            log("exit: firmware curve (pwm1_enable 2)")
        except OSError as e:
            log(f"could not restore pwm1_enable 2: {e!r}")


if __name__ == "__main__":
    main()
