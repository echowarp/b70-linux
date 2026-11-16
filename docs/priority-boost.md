# Priority boost: full power for interactive traffic, tokens per joule for the rest

An LLM server rarely needs full power all the time. Someone waiting for a streamed answer wants latency; a nightly
indexer or a batch of agent sub-tasks wants the most tokens per joule. This page describes a general trigger
mechanism for that split: a small classifier in front of the engine decides per request whether it is
**interactive** or **background**, and while any interactive request is in flight it asks the power governor for the
boost level. Nothing in it is specific to one engine, client or GPU vendor; the tools in this repo implement it for
the B70.

What is measured and what is design is marked; see [Status](#status-measured-vs-design).

## The two classes

| | Interactive / priority | Background / low priority |
|---|---|---|
| Goal | latency: time to first token, smooth streaming | tokens per joule |
| Power limit | boost level (`BOOST_W`, default the performance watts, 300 W) | the power profile's watts (190 W by default) |
| Scheduling (engine side, if it supports it) | latency-oriented: prefill split into chunks interleaved with running streams, priority queueing | throughput-oriented: larger batches (more parallel streams share each weight read), full prefill chunks, no interleaving |
| Who | chat UIs, IDE assistants, anything a person is watching | indexers, batch jobs, evaluation runs, sub-agents nobody watches |

On our board, the measured cost of the two power levels ([efficiency.md](efficiency.md), card power): 190 W gives
1.003 tok/J at 190 tok/s (4-stream decode); 330 W gives 0.744 tok/J at 242 tok/s. Background work at 190 W gets
~35 % more tokens per joule and keeps the card near silent (observed, not measured); interactive work at full
power gets 24-32 % more speed (decode 1 stream to prefill).

## The trigger (b70-governor)

Any client can request the boost: a router, a middleware, a desktop app or a shell script. There is no daemon API
to learn and no privileges beyond writing one file.

- A trigger is a file `/run/b70-boost/<client id>` that holds a unix expiry time. One file per client, so
  concurrent requesters never clobber each other. `b70-power boost [seconds] [id]` writes one;
  `b70-power release [id]` removes it.
- The governor boosts while **any** trigger is fresh: the target becomes max(profile watts, `BOOST_W`). Otherwise
  it uses the profile's watts. A manual override (`b70-power <W>`) still wins over both.
- Safe defaults: a trigger expires at its expiry time and in any case `BOOST_MAX_S` (600 s) after it was last
  written, so a crashed client's boost lapses by itself. Unreadable, malformed, non-regular files and symlinks count
  as no boost. A missing trigger directory means no boost.
- Long-running clients hold a short TTL and refresh it (the middleware below: 15 s, refreshed every 5 s), so a
  dead process stops boosting within seconds. On release they leave a short hold (10 s) instead of deleting, so
  back-to-back requests do not flap the power limit.
- The directory is created by a tmpfiles.d entry (sticky, world-writable like `/tmp`; restrict it to a group if
  only some users may boost). Details: [tools/b70-governor/README.md](../tools/b70-governor/README.md).

```sh
b70-power boost 120                    # this shell: interactive level for 2 minutes
b70-power release
echo $(( $(date +%s) + 30 )) > /run/b70-boost/my-app   # the same from any program
```

## Classifying requests

The classifier sits in front of the engine (in a router, a reverse proxy, or as middleware inside the engine's HTTP
server). It maps request attributes to a class, first decision wins:

1. **Explicit priority tag** (always wins): an `X-Priority` header (mapped through the rules, e.g. `high` /
   `normal` → interactive, `low` / `background` → background); the OpenAI `service_tier` field (`"priority"` →
   interactive, `"flex"` → background; `"auto"` / `"default"` decide nothing); vLLM's integer `priority`
   (lower is more urgent: < 0 → interactive, > 0 → background, 0 decides nothing).
2. **Agent name**: `X-Agent-Name`, else `X-Agent`, else the request's `model` (a per-client model alias in a proxy
   such as llama-swap works as an agent name), matched against name globs.
3. **Origin host**: the client address (from `X-Forwarded-For` only when the direct peer is a trusted proxy)
   matched against subnets or host names.
4. **Default**: interactive unless configured otherwise. Without any rules everything is boosted, which on a
   single-user desktop is what you want; a shared server usually sets `"default": "background"` or lists its batch
   clients.

Example rules file (all names and addresses are made up; the addresses are documentation ranges):

```json
{
  "default": "interactive",
  "priority": {"high": "interactive", "normal": "interactive", "low": "background", "background": "background"},
  "agents": {
    "chat": "interactive",
    "ide-assistant": "interactive",
    "batch-*": "background",
    "indexer": "background"
  },
  "hosts": {
    "192.0.2.0/24": "interactive",
    "198.51.100.0/24": "background",
    "nightly-runner.example.lan": "background"
  },
  "trusted_proxies": ["127.0.0.0/8", "::1/128"]
}
```

With these rules: `indexer` sending `X-Priority: high` is interactive (tag beats agent); `chat` sending
`service_tier: "flex"` is background; an unnamed client from 198.51.100.7 is background; an unnamed client from
anywhere else is interactive.

## Hooking an LLM server

The rule is: **hold a boost trigger while at least one interactive request is in flight** (from arrival until the
last streamed token), refresh it while it lasts, and release it (with a short hold) when none are left. Background
requests touch nothing.

- **Middleware (provided):** [tools/b70-governor/b70_boost_middleware.py](../tools/b70-governor/b70_boost_middleware.py)
  is a standard-library ASGI middleware that implements the classification above (rules file
  `/etc/b70-boost-rules.json`, re-read on change) and the trigger handling. With vLLM: put it on `PYTHONPATH` and
  start the server with `--middleware b70_boost_middleware.BoostMiddleware`. It counts in-flight requests only and
  never logs or stores request content.
- **Router or proxy:** call `classify(headers, body, client_ip)` from the same module and wrap the upstream call in
  `with BOOST.hold(cls): ...`, or write the trigger file yourself. A router that already assigns priority tiers
  (for example from `X-Priority` or per-agent policy) just maps its top tiers to interactive.
- **Several GPUs:** one trigger directory per card's governor. A router that sends interactive work to one GPU and
  background work to another only boosts the card that runs the interactive request.
- **Engine scheduling:** the power level is only half of it. Engines with a priority scheduler (vLLM
  `--scheduling-policy priority` plus a per-request `priority`) can also run interactive requests first; batch size
  and prefill chunking per class depend on the engine and are not part of this repo.

## Status: measured vs design

- **Measured** (one board, one model, our engine): tok/s, tok/J, clocks, temperatures and fan speeds per power limit,
  [efficiency.md](efficiency.md).
- **Tested:** the governor's trigger handling and the middleware's classification and trigger writes, as dry runs
  and unit checks without a GPU.
- **Not measured yet:** how long the power-limit change takes to show in the GT clock after the governor writes it
  (the governor checks triggers every 0.2 s; b70-powerd applies the limit through the firmware mailbox), and so how
  much of the first interactive request's prefill still runs at the lower limit. Until that is measured, treat the
  switching latency as unknown. If it proves slow, boost a little earlier (e.g. when a client connects) or hold the
  boost longer.
- **Not measured yet:** end-to-end energy savings on real mixed traffic. The per-limit numbers say what each level
  costs; the saving depends on how much of your traffic is background.
