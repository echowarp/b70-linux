#!/usr/bin/env python3
"""b70-governor: follows the desktop power profile (power-profiles-daemon) and sets the Intel Arc Pro B70 power limit
and fan mode through the root helper b70-powerd (../b70-powerd). Python 3 standard library + busctl.

  power-saver -> SAVER_W (190)    balanced -> BALANCED_W (190)    performance -> PERF_W (300)
  Fan: "fan smooth" in power-saver (FAN_SMOOTH=1, default), in every profile (2), never (0); otherwise "fan stock".
  Without Intel's xe fan-control series 168027 b70-powerd ignores the fan command.
Manual override: <config dir>/override (one number, watts; written by `b70-power <W>`, removed by `b70-power auto`)
wins over the profile while it exists.
Safe fallback: if the profile cannot be read the governor uses "balanced"; if b70-powerd is unreachable nothing is
changed (the command is retried; the card keeps whatever limit it has). Targets are clamped to MIN_W..MAX_W.
Config, later files win, an environment variable of the same name wins over both:
  /etc/b70-governor.conf, then $XDG_CONFIG_HOME/b70-governor/conf (default ~/.config/b70-governor/conf)
  KEY=VALUE lines: SAVER_W BALANCED_W PERF_W FAN_SMOOTH MIN_W MAX_W POLL_S
Profile source: D-Bus ActiveProfile of power-profiles-daemon (polled every POLL_S). Every target is re-sent every
60 s (b70-powerd may have restarted). Logs transitions only.
  b70-governor.py [--once] [--dry-run]     (--dry-run: log what would be sent, send nothing)"""
import os, socket, subprocess, sys, time

CFG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "b70-governor")
SOCK = os.environ.get("B70_POWERD_SOCK", "/run/b70-powerd.sock")
DEFAULTS = {"SAVER_W": 190, "BALANCED_W": 190, "PERF_W": 300, "FAN_SMOOTH": 1, "MIN_W": 150, "MAX_W": 330, "POLL_S": 1.0}


def conf():
    c = dict(DEFAULTS)
    for path in ("/etc/b70-governor.conf", f"{CFG_DIR}/conf"):
        try:
            lines = open(path).read().splitlines()
        except OSError:
            continue
        for ln in lines:
            k, _, v = ln.strip().partition("=")
            if k in c:
                try:
                    c[k] = type(DEFAULTS[k])(float(v.strip().strip("\"'")))
                except ValueError:
                    pass
    for k in c:
        try:
            if os.environ.get(k):
                c[k] = type(DEFAULTS[k])(float(os.environ[k]))
        except ValueError:
            pass
    return c


def log(msg):
    print("b70-governor: " + msg, flush=True)


def profile():
    for svc, path in (("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles"),
                      ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles")):
        try:
            out = subprocess.run(["busctl", "--system", "get-property", svc, path, svc, "ActiveProfile"],
                                 capture_output=True, text=True, timeout=3).stdout.split()
            if len(out) == 2:
                return out[1].strip('"')
        except (OSError, subprocess.SubprocessError):
            pass
    return "balanced"


def override():
    try:
        w = int(float(open(f"{CFG_DIR}/override").read().split()[0]))
        return w if w > 0 else None
    except (OSError, ValueError, IndexError):
        return None


def target(c, prof):
    ov = override()
    w = ov if ov else c["SAVER_W"] if prof == "power-saver" else c["PERF_W"] if prof == "performance" else c["BALANCED_W"]
    return max(c["MIN_W"], min(c["MAX_W"], w))


def fan_mode(c, prof):
    if c["FAN_SMOOTH"] >= 2:
        return "smooth"
    return "smooth" if c["FAN_SMOOTH"] == 1 and prof == "power-saver" else "stock"


def send(cmd):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(SOCK)
        s.sendall(cmd.encode() + b"\n")
        r = s.recv(256).decode().strip()
        s.close()
        return r
    except OSError as e:
        return f"err powerd unreachable ({e.__class__.__name__})"


def main():
    once, dry = "--once" in sys.argv, "--dry-run" in sys.argv
    last, last_sent, lastf, lastf_sent = None, 0.0, None, 0.0
    while True:
        c = conf()
        prof = profile()
        f = fan_mode(c, prof)
        if f != lastf or time.time() - lastf_sent > 60:
            r = "dry-run" if dry else send(f"fan {f}")
            if f != lastf:
                log(f"profile {prof} -> fan {f} ({r})")
            lastf, lastf_sent = (f if not r.startswith("err") else None), time.time()
        w = target(c, prof)
        if w != last or time.time() - last_sent > 60:
            r = "dry-run" if dry else send(f"set {w}")
            if w != last:
                log(f"profile {prof} -> {w} W ({r})")
            last, last_sent = (w if not r.startswith("err") else None), time.time()
        if once:
            return
        time.sleep(c["POLL_S"])


if __name__ == "__main__":
    main()
