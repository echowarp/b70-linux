"""b70_boost_middleware: classify LLM requests as interactive or background and hold a b70-governor boost trigger
while interactive requests are in flight. Python 3 standard library only. MIT (see ../../LICENSE).

Two ways to use it:
  - ASGI middleware in front of any ASGI server (vLLM: put this file on PYTHONPATH and start with
    `--middleware b70_boost_middleware.BoostMiddleware`).
  - From your own router or proxy: `cls = classify(headers, body, client_ip)` and `with BOOST.hold(cls): ...`
    around the upstream call.

Classification, first decision wins (precedence: explicit tag > agent > host > default):
  1. explicit tag: header X-Priority (looked up in rules "priority"), else the JSON body's OpenAI `service_tier`
     ("priority" -> interactive, "flex" -> background; "auto" / "default" decide nothing), else vLLM's integer
     `priority` (< 0 -> interactive, > 0 -> background, 0 decides nothing; lower is more urgent in vLLM)
  2. agent: X-Agent-Name, else X-Agent, else the body's `model` (a per-client model alias), matched against the
     rules "agents" globs in file order
  3. host: the client address (X-Forwarded-For first hop only when the peer is in "trusted_proxies", default
     loopback) against the rules "hosts" (CIDR, or a host name resolved when the rules file is (re)loaded)
  4. rules "default" (default "interactive": without rules everything is boosted, as on a single-user desktop)

Rules: JSON file $B70_BOOST_RULES (default /etc/b70-boost-rules.json), re-read when its mtime changes; a missing or
broken file means no rules. Example: b70-boost-rules.example.json.
Trigger: $B70_BOOST_DIR/<B70_BOOST_ID> (default /run/b70-boost/mw-<pid>) holds a unix expiry time, refreshed every
TTL/3 s with TTL = B70_BOOST_TTL_S (15) while any interactive request is in flight; when the last one ends the expiry
is set to now + B70_BOOST_HOLD_S (10) so back-to-back requests don't flap the power limit. If this process dies the
trigger lapses within TTL. Background requests touch nothing (the card stays at the profile's efficiency watts).
Nothing about a request is logged or stored; only an in-flight count is kept."""
import fnmatch, ipaddress, json, os, socket, threading, time

RULES = os.environ.get("B70_BOOST_RULES", "/etc/b70-boost-rules.json")
DIR = os.environ.get("B70_BOOST_DIR", "/run/b70-boost")
ID = os.environ.get("B70_BOOST_ID", f"mw-{os.getpid()}")
TTL = float(os.environ.get("B70_BOOST_TTL_S", "15"))
HOLD = float(os.environ.get("B70_BOOST_HOLD_S", "10"))
MAX_BODY = 16 << 20                       # larger bodies are passed through unparsed
INTERACTIVE, BACKGROUND = "interactive", "background"
_rules = {"mtime": None, "r": {}}


def _net(spec):
    try:
        return [ipaddress.ip_network(spec, strict=False)]
    except ValueError:
        try:
            return [ipaddress.ip_network(a[4][0]) for a in socket.getaddrinfo(spec, None)]
        except OSError:
            return []


def rules():
    try:
        m = os.stat(RULES).st_mtime
    except OSError:
        return {}
    if m != _rules["mtime"]:
        try:
            r = json.load(open(RULES))
            r["_hosts"] = [(n, cls) for spec, cls in r.get("hosts", {}).items() for n in _net(spec)]
            r["_proxies"] = [n for s in r.get("trusted_proxies", ["127.0.0.0/8", "::1/128"]) for n in _net(s)]
        except (OSError, ValueError, AttributeError):
            r = {}
        _rules.update(mtime=m, r=r)
    return _rules["r"]


def _in(ip, nets):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in nets)


def classify(headers, body, client_ip):
    """headers: dict with lowercase names; body: parsed JSON dict or None. Returns "interactive" or "background"."""
    r = rules()
    ok = (INTERACTIVE, BACKGROUND)
    tag = headers.get("x-priority", "").strip().lower()
    if tag and r.get("priority", {}).get(tag) in ok:
        return r["priority"][tag]
    body = body if isinstance(body, dict) else {}
    tier = {"priority": INTERACTIVE, "flex": BACKGROUND}.get(str(body.get("service_tier", "")).lower())
    if tier:
        return tier
    p = body.get("priority")
    if isinstance(p, int) and not isinstance(p, bool) and p:
        return INTERACTIVE if p < 0 else BACKGROUND
    agent = headers.get("x-agent-name") or headers.get("x-agent") or str(body.get("model", ""))
    for glob, cls in r.get("agents", {}).items():
        if agent and fnmatch.fnmatchcase(agent, glob) and cls in ok:
            return cls
    ip = client_ip or ""
    if _in(ip, r.get("_proxies", [])) and headers.get("x-forwarded-for"):
        ip = headers["x-forwarded-for"].split(",")[0].strip()
    for net, cls in r.get("_hosts", []):
        if cls in ok and _in(ip, [net]):
            return cls
    return r.get("default") if r.get("default") in ok else INTERACTIVE


class Boost:
    """In-flight counter that keeps one trigger file fresh while the count is > 0."""
    def __init__(self, path=os.path.join(DIR, ID)):
        self.path, self.n, self.lock = path, 0, threading.Lock()
        self.wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _write(self, until):
        tmp = os.path.join(os.path.dirname(self.path), "." + os.path.basename(self.path) + ".tmp")
        try:
            with open(tmp, "w") as f:
                f.write(f"{until:.0f}\n")
            os.replace(tmp, self.path)
        except OSError:
            pass                                    # no trigger dir: no boost, requests are never affected

    def _loop(self):
        while True:
            self.wake.wait(TTL / 3)
            self.wake.clear()
            with self.lock:
                if self.n > 0:
                    self._write(time.time() + TTL)

    def hold(self, cls):
        return _Hold(self) if cls == INTERACTIVE else _NoHold()


class _Hold:
    def __init__(self, b):
        self.b = b

    def __enter__(self):
        with self.b.lock:
            self.b.n += 1
            if self.b.n == 1:
                self.b._write(time.time() + TTL)

    def __exit__(self, *_):
        with self.b.lock:
            self.b.n -= 1
            if self.b.n == 0:
                self.b._write(time.time() + HOLD)


class _NoHold:
    def __enter__(self):
        pass

    def __exit__(self, *_):
        pass


BOOST = None


class BoostMiddleware:
    """ASGI middleware: classifies each HTTP request, holds the boost while interactive ones run (incl. streaming)."""
    def __init__(self, app):
        global BOOST
        self.app = app
        BOOST = BOOST or Boost()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        client = (scope.get("client") or ("", 0))[0]
        body, msgs = None, []
        if scope.get("method") == "POST" and "json" in headers.get("content-type", ""):
            size, more = 0, True
            while more:                              # buffer the body to read service_tier / priority / model
                m = await receive()
                msgs.append(m)
                size += len(m.get("body", b""))
                more = m.get("type") == "http.request" and m.get("more_body", False)
            if size <= MAX_BODY:
                try:
                    body = json.loads(b"".join(m.get("body", b"") for m in msgs))
                except ValueError:
                    body = None

        async def replay():
            return msgs.pop(0) if msgs else await receive()

        with BOOST.hold(classify(headers, body, client)):
            await self.app(scope, replay if msgs else receive, send)
