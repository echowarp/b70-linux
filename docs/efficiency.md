# Tokens per joule vs power limit (one B70, LLM serving)

How much speed, energy per token and fan noise each power limit costs on an Intel Arc Pro B70 serving an LLM. These
are measured numbers from **one board** (ASRock Arc Pro B70 Creator), one model (**Qwen3.8-27B, EXL3 4.0 bpw**) and
our own engine (vLLM XPU with custom ESIMD kernels for EXL3). Other boards, models, engines and batch sizes will
land elsewhere; the shape of the curve is the useful part. The power limit was set with patches 0004 + 0005 (see the
root README, "Setting the power limit").

## How it was measured

- One sweep, 2026-10-20, limits 160 / 190 / 220 / 250 / 275 / 300 / 330 W, run as a down pass (330 → 160 W) and an
  up pass (160 → 330 W); the tables are the mean of both passes, which agree within ~2 %.
- Three phases per limit: **prefill** of a ~41k-token prompt, **decode with 1 stream** (~485 tokens) and **decode
  with 4 streams** (~2,470 tokens). Before each phase the card waited for idle and was warmed up; the production
  server was idle (no other requests) during the sweep.
- **Card power** = delta of the xe hwmon `energy1_input` counter over the phase, divided by the phase time. It covers
  the card only. GT clock, temperatures and fan speed were sampled every 0.5 s from hwmon / sysfs.
- **Wall power** (second table) is an estimate: card power + CPU package and DRAM measured with RAPL under load at
  330 W (idle 7.7 W, decode 25.5-25.8 W, prefill 29.0 W: the engine's host thread) + an **estimated 25 W** for the
  rest of the board (chipset, fans, boot disk, NIC; plausible range 15-35 W), divided by **0.90** for the PSU. Other
  GPUs in the same machine are excluded.

## Card power only

| Limit | Prefill tok/s (% of 330 W) | Prefill tok/J | Decode 1 stream tok/s | tok/J | Decode 4 streams tok/s | tok/J |
|---|---|---|---|---|---|---|
| 160 W | 1,745 (64 %) | 10.85 | 60 (68 %) | 0.372 | 158 (65 %) | 0.990 |
| **190 W** | 2,065 (76 %) | **10.89** | 71 (80 %) | **0.372** | 190 (79 %) | **1.003** |
| 220 W | 2,294 (84 %) | 10.42 | 78 (89 %) | 0.355 | 212 (88 %) | 0.962 |
| 250 W | 2,416 (89 %) | 9.74 | 83 (94 %) | 0.333 | 222 (92 %) | 0.893 |
| 275 W (stock) | 2,565 (94 %) | 9.36 | 85 (96 %) | 0.310 | 229 (95 %) | 0.838 |
| 300 W | 2,645 (97 %) | 8.84 | 87 (98 %) | 0.292 | 235 (97 %) | 0.789 |
| 330 W (board max) | 2,724 (100 %) | 8.31 | 88 (100 %) | 0.274 | 242 (100 %) | 0.744 |

- **Most efficient on card power: 190 W** in all three phases, +31-36 % tok/J vs 330 W at 76-80 % of the speed.
  160 W is no better (the clock drops further than the power).
- Above the 275 W stock limit each watt buys little: **300 → 330 W gives +2-3 % speed** for 10 % more power.
- Decode is mostly memory-bound, so it loses less speed than prefill at low limits.

## Estimated wall power (excluding other GPUs; rest of board 25 W, PSU 90 %)

| Limit | Wall W (prefill / dec 1 / dec 4) | Prefill tok/J (vs 330 W) | Decode 1 stream tok/J | Decode 4 streams tok/J |
|---|---|---|---|---|
| 160 W | 239 / 234 / 234 | 7.30 (+14 %) | 0.256 (+20 %) | 0.675 (+17 %) |
| **190 W** | 272 / 269 / 268 | **7.60 (+19 %)** | **0.264 (+24 %)** | **0.711 (+23 %)** |
| 220 W | 304 / 302 / 301 | 7.54 (+18 %) | 0.259 (+22 %) | 0.705 (+22 %) |
| 250 W | 336 / 334 / 334 | 7.20 (+12 %) | 0.249 (+17 %) | 0.666 (+15 %) |
| 275 W | 364 / 359 / 360 | 7.04 (+10 %) | 0.235 (+11 %) | 0.634 (+10 %) |
| 300 W | 392 / 387 / 386 | 6.75 (+5 %) | 0.224 (+6 %) | 0.608 (+5 %) |
| 330 W | 426 / 416 / 418 | 6.40 | 0.213 | 0.579 |

The fixed ~55-60 W at the wall flattens the curve. 190 W stays the most efficient for any rest-of-board value from
15 to 35 W, but **220 W is within 0.5-1 % of it at 8-11 % more speed**, so 220 W is the practical efficiency point at
the wall (+18-22 % tok/J vs 330 W at 84-89 % of the speed).

## Noise and temperature

Noise is a third tradeoff next to speed and tok/J: on a desk, a card running continuously matters more for how loud it
is than for the last few percent of speed.

Fan speed, mean over each 4-stream decode phase of the sweep (both passes). These phases last only 10-16 s and the fan
lags, so they show the trend, not steady-state levels:

| Limit | 160 W | 190 W | 220 W | 250 W | 275 W | 300 W | 330 W |
|---|---|---|---|---|---|---|---|
| Fan rpm (down / up pass) | 1,497 / 1,581 | 1,958 / 1,872 | 2,259 / 2,327 | 2,654 / 2,478 | 2,888 / 2,863 | 3,300 / 3,329 | 3,319 / 3,320 |

10-minute soaks under continuous 4-stream decode (steady state):

| Limit | Mean card power | GT clock | VRAM | Package | Fan |
|---|---|---|---|---|---|
| 330 W | 328 W | 2,737 MHz | 86 °C (flat from minute 0; warm from the sweep) | 74 °C | 3,485 rpm |
| 300 W | 298 W | 2,628 MHz | 84 °C | 74 °C | 3,105 rpm |

A 30-minute watch after we moved production to 330 W (82 % of samples under load) peaked at VRAM 86 °C.

We have no 190 W soak. In daily use the card is **near silent running continuously at 190 W** (the owner's
observation, not a measured rpm or sound level). That, as much as the tok/J result, is why the governor's defaults
put power-saver and balanced at 190 W.

## What we do with it

- **Background / batch work: 190-220 W** (190 W best on card power and quiet; 220 W best at the wall).
- **Interactive work: full power** (300-330 W) while someone is waiting for tokens.
- `tools/b70-governor/` sets these levels: profile watts (190 W by default) for efficiency, and a boost to the
  performance watts while any client asks for it. How an LLM server decides when to ask: [priority-boost.md](priority-boost.md).
