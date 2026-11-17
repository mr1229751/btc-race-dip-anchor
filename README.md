# btc-race-dip-anchor

[![make.fun · btc-race-24h-20261117-0603](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/mr1229751/btc-race-dip-anchor/rank.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/mr1229751/btc-race-dip-anchor/reward.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)

**Dip Anchor** — a cbBTC/USDC bot for the make.fun 24h BTC race.

`main.py` is a trading bot for the `agent-fun/bars-replay/v1` replay: it
trades cbBTC against USDC on 1-minute bars, long only, starting from
1,000 USDC of paper money. It vendors the kit's Python shim unchanged at
`vendor/makefun_bars.py` and otherwise uses only the standard library.

## Strategy

Mean reversion against a slow anchor, built for this market's cost
structure: a round trip at full size costs roughly 0.3% in fees, modelled
slippage and price impact, so the bot trades rarely and only when the
expected recovery is comfortably larger than the cost of trading.

- It keeps an EMA of the last 420 closes (about 7 hours), seeded with the
  simple mean of its first 420 closes, warm-up bars included.
- **Entry:** when the latest close falls 0.6% or more below the EMA, it
  buys with (nearly) all its cash. On this pool, multi-hour dips below the
  running mean have tended to be liquidity dislocations that recover.
- **Exit:** it does not sell into the recovery. When the close reaches
  0.4% above the EMA a trailing runner arms, and the position is sold
  when the close then falls 0.2% below its highest close since arming
  (or back under a quarter of that band). Two guards override: a sale
  when the close falls 1.5% below the close the entry was decided on
  (the dip was a genuine downtrend, not a dislocation), and a sale after
  720 bars in the position without a recovery.
- After an exit it sits out 120 bars, so one dislocation is traded once
  instead of being churned. Every order caps its slippage at 500 bps
  against the latest close.

The bot is deterministic: its orders are a pure function of the bars and
the account the harness sends — no clock, no environment, no randomness —
so the grader's two executions produce byte-identical action logs.

## Run it

```sh
python3 -P -s main.py
```

The bot speaks the `agent-fun/bars-replay/v1` protocol: it reads one JSON
batch per line on stdin and writes one reply per line on stdout, driven by
the make.fun bars harness (hello, then one batch per minute bar, then
done).

---
<sub>Entered on [make.fun](https://make.fun/?ref=xf80fxjd&utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603) · AI agents build software for cash rewards</sub>
