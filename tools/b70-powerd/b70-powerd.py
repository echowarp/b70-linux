#!/usr/bin/env python3
"""b70-powerd: root helper that sets the Arc Pro B70's power limit, with optional fan smoothing and the
stale-temperature refresh (IGCIT #1574).

Socket /run/b70-powerd.sock (stream, mode 0666): only peers with uid ALLOW_UID (SO_PEERCRED; default 1000) and root
are served. Protocol, one line per request:
  "set <watts>"  -> "ok <applied> <path>" | "err <reason>"
  "get"          -> "state ..."
  "fan smooth" | "fan stock" | "fan get"
- watts are clamped to [MIN_W, MAX_W] = [150, B70_POWERD_MAX_W (default 330, the ASRock B70 Creator's board max)].
- VRAM guard (B70_POWERD_VRAM_GUARD_C, default 92; 0 disables): hottest vram* hwmon sensor above the limit on 3
  consecutive 5 s samples while the target is > 300 W -> applied limit stepped down to 300 W until a target <= 300 W
  is set.
- With patch 0005 (/sys/kernel/debug/dri/<pci>/oc/power_limit, 1/8 W): every target goes to oc/power_limit = watts*8
  (pcode domain 0x30, min 1096 = 137 W, max 2640 = 330 W, default 2200 = 275 W), plus power1_cap = min(watts, 275).
  Measured: power1_cap below 275 W is accepted but NOT enforced (the card kept drawing ~270 W under 190 and 160 W
  caps); the OC domain is the limit pcode honours.
- Without it: hwmon power1_cap (uW) for <= 275 W (may not be enforced, see above); > 275 W is clamped to 275 W
  ("oc unavailable").
- Re-applies the last target when the card comes back (new hwmon dir or OC debugfs dir after a reset or reconnect)
  and when power1_cap drifts. Never writes while the card looks wedged (runtime_status != active, config space 0xffff).
- On stop: back to stock (OC 2200 if it was raised, power1_cap 275 W).
- Fan smoothing (needs Intel's xe fan-control series 168027: hwmon pwm1_enable + pwm1_auto_point*): "fan smooth"
  feeds the STOCK curve the package temperature averaged over FAN_TAU_S; the applied speed rises at most FAN_UP %/s
  and falls at most FAN_DOWN %/s. It is written as a user table that is flat at that speed below FAN_SAFE_C and keeps
  the stock speeds from FAN_SAFE_C up, so the firmware still ramps on its own if this daemon dies. Package above
  FAN_HOT_C: the speed jumps to the stock value at once. "fan stock" (and daemon stop, and a module without the
  series) = pwm1_enable 2. Same logic as tools/b70-fan-smooth.
  Env: B70_FAN_UP_PCT_S (0.5), B70_FAN_DOWN_PCT_S (0.05 = 3 %/min), B70_FAN_TAU_S (20), B70_FAN_SAFE_C (80),
  B70_FAN_HOT_C (90), B70_FAN_PERIOD_S (2).
- Telemetry refresh (IGCIT #1574): in deep idle (GT in C6) pcode's temperature telemetry can freeze at the last
  loaded values (measured: 68 / 80 C pkg / VRAM shown while the card was really 39 / 42 C) and the firmware fan keeps
  that speed (~2,400 rpm idle instead of ~1,100). Every TELEM_REFRESH_S (10) seconds, only while gt0 is in gt-c6 AND
  the reported pkg >= TELEM_PKG_C (50) (VRAM only if TELEM_VRAM_C > 0: a GT forcewake refreshes pkg but not VRAM),
  the daemon holds debugfs dri/<pci>/forcewake_all open for ~2 ms. Once a refresh shows the card cool, the GT is left
  asleep. Same logic as tools/b70-fan-refresh; don't run both.
  Env: B70_TELEM_REFRESH_S (10; 0 = off), B70_TELEM_HOLD_MS (2), B70_TELEM_PKG_C (50), B70_TELEM_VRAM_C (0).
Env: B70_PCI (default: the first xe device with an "xe" hwmon), B70_POWERD_SOCK, B70_POWERD_ALLOW_UID,
B70_SYSFS_ROOT / B70_DEBUGFS_ROOT (tests: mock trees).
Logs (journal / stderr): target changes and errors only."""
import glob, math, os, signal, socket, struct, sys, threading, time

SYS = os.environ.get("B70_SYSFS_ROOT", "/sys")


def _find_pci():
    for d in sorted(glob.glob(f"{SYS}/bus/pci/drivers/xe/0000:*")):
        for h in glob.glob(f"{d}/hwmon/hwmon*"):
            try:
                if open(f"{h}/name").read().strip() == "xe":
                    return os.path.basename(d)
            except OSError:
                pass
    return None


PCI = os.environ.get("B70_PCI") or _find_pci()
if not PCI:
    sys.exit("b70-powerd: no xe device with hwmon found (set B70_PCI)")
SOCK = os.environ.get("B70_POWERD_SOCK", "/run/b70-powerd.sock")
ALLOW_UID = int(os.environ.get("B70_POWERD_ALLOW_UID", "1000"))
DBG = os.environ.get("B70_DEBUGFS_ROOT", "/sys/kernel/debug")
MIN_W, STOCK_W, OC_STOCK = 150, 275, 2200
MAX_W = int(os.environ.get("B70_POWERD_MAX_W", "330"))          # upper clamp = board max 330 W (ASRock B70 Creator)
GUARD_C = float(os.environ.get("B70_POWERD_VRAM_GUARD_C", "92")) # VRAM guard on by default (0 disables)
GUARD_N, GUARD_W = 3, 300                                          # 3 hot samples in a row -> at most 300 W

FAN_UP = float(os.environ.get("B70_FAN_UP_PCT_S", "0.5"))
FAN_DOWN = float(os.environ.get("B70_FAN_DOWN_PCT_S", "0.05"))
FAN_TAU = float(os.environ.get("B70_FAN_TAU_S", "20"))
FAN_SAFE_C = float(os.environ.get("B70_FAN_SAFE_C", "80"))
FAN_HOT_C = float(os.environ.get("B70_FAN_HOT_C", "90"))
FAN_PERIOD = float(os.environ.get("B70_FAN_PERIOD_S", "2"))
TELEM_S = float(os.environ.get("B70_TELEM_REFRESH_S", "10"))
TELEM_HOLD = float(os.environ.get("B70_TELEM_HOLD_MS", "2")) / 1000
TELEM_PKG_C = float(os.environ.get("B70_TELEM_PKG_C", "50"))
TELEM_VRAM_C = float(os.environ.get("B70_TELEM_VRAM_C", "0"))   # 0 = ignore VRAM (a GT wake does not refresh it)
telem = {"refreshes": 0, "err": None}

fan = {"want": "stock", "active": False, "hw": None, "stock": None, "pct": None, "ema": None, "written": None,
       "err": None}

state = {"guard": False, "hot": 0, "target": None, "applied": None, "path": None, "oc_raised": False, "hwmon": None, "ocdir": None}
lock = threading.Lock()


def log(msg):
    print("b70-powerd: " + msg, file=sys.stderr, flush=True)


def dev():
    return f"{SYS}/bus/pci/devices/{PCI}"


def healthy():
    d = dev()
    try:
        if open(f"{d}/power/runtime_status").read().strip() != "active":
            return False
        with open(f"{d}/config", "rb") as f:
            vid = f.read(2)
        return vid != b"\xff\xff" and len(vid) == 2
    except OSError:
        return False


def hwmon_dir():
    for h in sorted(glob.glob(f"{dev()}/hwmon/hwmon*")):
        try:
            if open(f"{h}/name").read().strip() == "xe" and os.path.exists(f"{h}/power1_cap"):
                return h
        except OSError:
            pass
    return None


def oc_dir():
    d = f"{DBG}/dri/{PCI}/oc"
    return d if os.path.exists(f"{d}/power_limit") else None


def write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def vram_max_c(h):
    t = 0.0
    for lab in glob.glob(f"{h}/temp*_label"):
        try:
            if "vram" in open(lab).read():
                t = max(t, int(open(lab[:-6] + "_input").read()) / 1000)
        except (OSError, ValueError):
            pass
    return t


def apply(w):
    """Apply target w (already clamped). Returns (applied watts, path)."""
    if state["guard"]:
        w = min(w, GUARD_W)
    h, oc = hwmon_dir(), oc_dir()
    if not h:
        raise RuntimeError("no xe hwmon for the B70")
    if oc:
        write(f"{h}/power1_cap", min(w, STOCK_W) * 1_000_000)
        write(f"{oc}/power_limit", w * 8)
        state["oc_raised"] = w * 8 != OC_STOCK          # "away from stock": restored to 2200 on stop
        res = (w, "oc/power_limit")
    elif w <= STOCK_W:
        write(f"{h}/power1_cap", w * 1_000_000)
        res = (w, "power1_cap")
    else:
        write(f"{h}/power1_cap", STOCK_W * 1_000_000)
        res = (STOCK_W, "power1_cap (oc unavailable)")
    state.update(hwmon=h, ocdir=oc, applied=res[0], path=res[1])
    return res


def set_target(w):
    w = max(MIN_W, min(MAX_W, int(round(w))))
    with lock:
        if w <= GUARD_W and state["guard"]:
            state["guard"], state["hot"] = False, 0
            log("VRAM guard cleared (target <= %d W)" % GUARD_W)
        if not healthy():
            state["target"] = w
            return "err card not healthy (target kept, applied when it is back)"
        changed = state["target"] != w
        state["target"] = w
        try:
            a, p = apply(w)
        except Exception as e:  # noqa: BLE001
            log(f"apply {w} W failed: {e!r}")
            return f"err {e}"
    if changed:
        log(f"target {w} W -> {a} W via {p}")
    return f"ok {a} {p}"


def watchdog():
    """Re-apply after reconnects / drift. 5 s period, cheap sysfs reads."""
    while True:
        time.sleep(5)
        with lock:
            w = state["target"]
            if w is None or not healthy():
                continue
            h, oc = hwmon_dir(), oc_dir()
            drift = False
            try:
                if h:
                    want = (w if w <= STOCK_W else STOCK_W) * 1_000_000
                    drift = int(open(f"{h}/power1_cap").read()) != want
            except (OSError, ValueError):
                pass
            if GUARD_C > 0 and h and w > GUARD_W and not state["guard"]:
                t = vram_max_c(h)
                state["hot"] = state["hot"] + 1 if t > GUARD_C else 0
                if state["hot"] >= GUARD_N:
                    state["guard"] = True
                    try:
                        a, p = apply(w)
                        log(f"VRAM guard: {t:.0f} C > {GUARD_C:.0f} C x{GUARD_N}: {w} W -> {a} W until a target <= {GUARD_W} W")
                    except Exception as e:  # noqa: BLE001
                        log(f"VRAM guard apply failed: {e!r}")
                    continue
            if h != state["hwmon"] or oc != state["ocdir"] or drift:
                try:
                    a, p = apply(w)
                    log(f"re-applied {w} W -> {a} W via {p} (card back / drift)")
                except Exception as e:  # noqa: BLE001
                    log(f"re-apply failed: {e!r}")


def fan_table(h):
    """[(temp C, speed %)] of the current user table (after 2 -> 1 it is the stock table)."""
    pts, i = [], 1
    while os.path.exists(f"{h}/pwm1_auto_point{i}_temp"):
        t = int(open(f"{h}/pwm1_auto_point{i}_temp").read()) / 1000
        pts.append((t, int(open(f"{h}/pwm1_auto_point{i}_pwm").read()) * 100 / 255))
        i += 1
    return pts


def fan_interp(stock, t):
    if t <= stock[0][0]:
        return stock[0][1]
    for (t0, s0), (t1, s1) in zip(stock, stock[1:]):
        if t <= t1:
            return s0 + (s1 - s0) * (t - t0) / max(t1 - t0, 1e-6)
    return stock[-1][1]


def pkg_c(h):
    for lab in glob.glob(f"{h}/temp*_label"):
        try:
            if open(lab).read().strip() == "pkg":
                return int(open(lab[:-6] + "_input").read()) / 1000
        except (OSError, ValueError):
            pass
    return None


def fan_write(h, stock, pct):
    """User table: flat at pct below FAN_SAFE_C, stock (but >= pct) from there up; non-decreasing as the driver needs.
    The driver commits when the last point's temp and pwm are both written."""
    n = len(stock)
    sp = [max(pct, s) if t >= FAN_SAFE_C else pct for t, s in stock]
    for i in range(1, n):
        sp[i] = max(sp[i], sp[i - 1])
    for i in range(n - 1):
        write(f"{h}/pwm1_auto_point{i + 1}_pwm", round(sp[i] * 255 / 100))
    write(f"{h}/pwm1_auto_point{n}_temp", round(stock[-1][0] * 1000))
    write(f"{h}/pwm1_auto_point{n}_pwm", round(sp[-1] * 255 / 100))


def fan_stock(h):
    if h and os.path.exists(f"{h}/pwm1_enable"):
        write(f"{h}/pwm1_enable", 2)
    fan.update(active=False, hw=None, pct=None, ema=None, written=None)


def fan_step():
    h = hwmon_dir()
    if not h or not os.path.exists(f"{h}/pwm1_enable") or not healthy():
        fan.update(active=False, hw=None)
        return
    if fan["want"] != "smooth":
        if fan["active"]:
            fan_stock(h)
            log("fan: stock (firmware curve)")
        return
    if not fan["active"] or fan["hw"] != h:
        write(f"{h}/pwm1_enable", 2)                 # auto -> manual copies the stock table into the user table
        write(f"{h}/pwm1_enable", 1)
        stock = fan["stock"] if fan.get("stock_hw") == h else fan_table(h)   # first read per card instance
        if len(stock) < 2 or any(b[0] < a[0] or b[1] < a[1] for a, b in zip(stock, stock[1:])):
            write(f"{h}/pwm1_enable", 2)
            raise RuntimeError(f"unusable stock table {stock}")
        fan.update(active=True, hw=h, stock=stock, stock_hw=h, pct=None, ema=None, written=None)
        log("fan: smooth; stock table " + " ".join(f"{t:.0f}C:{s:.0f}%" for t, s in stock))
    t = pkg_c(h)
    if t is None:
        raise RuntimeError("no pkg temperature")
    stock = fan["stock"]
    fan["ema"] = t if fan["ema"] is None else fan["ema"] + (t - fan["ema"]) * (1 - math.exp(-FAN_PERIOD / FAN_TAU))
    target = fan_interp(stock, fan["ema"])
    cur = fan["pct"] if fan["pct"] is not None else fan_interp(stock, t)
    if t >= FAN_HOT_C:
        cur = max(cur, fan_interp(stock, t))
    elif target > cur:
        cur = min(target, cur + FAN_UP * FAN_PERIOD)
    else:
        cur = max(target, cur - FAN_DOWN * FAN_PERIOD)
    fan["pct"] = cur
    ip = max(0, min(100, round(cur)))
    if ip != fan["written"]:
        fan_write(h, stock, ip)
        fan["written"] = ip


def fan_loop():
    while True:
        time.sleep(FAN_PERIOD)
        with lock:
            try:
                fan_step()
                fan["err"] = None
            except Exception as e:  # noqa: BLE001
                if fan["err"] != repr(e):
                    log(f"fan: {e!r}; back to stock")
                fan["err"] = repr(e)
                try:
                    fan_stock(hwmon_dir())
                except Exception:  # noqa: BLE001
                    pass


def gt0_idle():
    try:
        return open(f"{dev()}/tile0/gt0/gtidle/idle_status").read().strip() == "gt-c6"
    except OSError:
        return False


def telem_loop():
    """Wake the GT for ~2 ms every TELEM_S while it sleeps, so pcode refreshes the temperatures the fan follows."""
    path = f"{DBG}/dri/{PCI}/forcewake_all"
    while True:
        time.sleep(TELEM_S)
        try:
            if not (healthy() and gt0_idle() and os.path.exists(path)):
                continue
            h = hwmon_dir()
            p, v = (pkg_c(h) if h else None), (vram_max_c(h) if h else 0.0)
            if not ((p is not None and p >= TELEM_PKG_C) or (TELEM_VRAM_C > 0 and v >= TELEM_VRAM_C)):
                continue                                  # reported cool: a stale low reading is harmless
            with open(path, "rb"):
                time.sleep(TELEM_HOLD)
            telem["refreshes"] += 1
            telem["err"] = None
        except Exception as e:  # noqa: BLE001
            if telem["err"] != repr(e):
                log(f"telemetry refresh: {e!r}")
            telem["err"] = repr(e)


def set_fan(mode):
    with lock:
        if fan["want"] != mode:
            log(f"fan mode {mode}")
        fan["want"] = mode
        h = hwmon_dir()
        avail = bool(h and os.path.exists(f"{h}/pwm1_enable"))
    return f"ok fan {mode}" + ("" if avail else " (no fan control in this xe module)")


def restore_stock(*_):
    with lock:
        try:
            if fan["active"] and healthy():
                fan_stock(hwmon_dir())
            h, oc = hwmon_dir(), oc_dir()
            if healthy():
                if oc and state["oc_raised"]:
                    write(f"{oc}/power_limit", OC_STOCK)
                if h:
                    write(f"{h}/power1_cap", STOCK_W * 1_000_000)
            log("stopped: back to stock 275 W")
        except Exception as e:  # noqa: BLE001
            log(f"restore failed: {e!r}")
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    os._exit(0)


def serve():
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(SOCK)
    os.chmod(SOCK, 0o666)          # anyone may connect; only ALLOW_UID (and root) are served
    s.listen(8)
    signal.signal(signal.SIGTERM, restore_stock)
    signal.signal(signal.SIGINT, restore_stock)
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=fan_loop, daemon=True).start()
    if TELEM_S > 0:
        threading.Thread(target=telem_loop, daemon=True).start()
    log(f"listening on {SOCK} for uid {ALLOW_UID}; B70 {PCI}; oc {'available' if oc_dir() else 'not available'}")
    while True:
        c, _ = s.accept()
        with c:
            c.settimeout(2)
            try:
                pid, uid, gid = struct.unpack("3i", c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid not in (ALLOW_UID, 0):
                    c.sendall(b"err not allowed\n")
                    continue
                line = c.recv(64).decode("ascii", "replace").strip().split()
                if len(line) == 2 and line[0] == "set":
                    reply = set_target(float(line[1]))
                elif len(line) == 2 and line[0] == "fan" and line[1] in ("smooth", "stock"):
                    reply = set_fan(line[1])
                elif line == ["fan", "get"]:
                    with lock:
                        reply = "fan want=%s active=%s pct=%s ema=%s err=%s telem_refreshes=%d telem_err=%s" % (
                            fan["want"], fan["active"], fan["written"],
                            None if fan["ema"] is None else round(fan["ema"], 1), fan["err"],
                            telem["refreshes"], telem["err"])
                elif line == ["get"]:
                    reply = "state target=%s applied=%s path=%s oc=%s healthy=%s guard=%s max=%s" % (
                        state["target"], state["applied"], state["path"], "yes" if oc_dir() else "no", healthy(),
                        state["guard"], MAX_W)
                else:
                    reply = "err usage: set <watts> | get | fan smooth|stock|get"
                c.sendall((reply + "\n").encode())
            except Exception as e:  # noqa: BLE001
                try:
                    c.sendall(f"err {e}\n".encode())
                except OSError:
                    pass


if __name__ == "__main__":
    serve()
