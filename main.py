"""Dip-anchored mean-reversion bot for cbBTC/USDC on 1-minute bars, v2.

Two long-only entry engines share one position (the harness allows one):

1. Dip engine (the original Dip Anchor, unchanged). An EMA of the last
   420 closes (~7 hours) is the anchor. Buy with all cash when the close
   falls 0.6% or more below it: on this pool, multi-hour dislocations
   under the running mean have historically snapped back, and a round
   trip costs roughly 0.3% in fees, slippage and impact, so the dip has
   to be at least twice that. On recovery to 0.4% above the EMA a runner
   arms and trails 0.2% under its peak close (or exits back under a
   quarter of the target band). Guards: a 1.5% stop under the entry
   close, and a 720-bar time stop.

2. Range engine (new in v2). The dip engine alone starved: in the dev
   window it traded twice in 24 hours. The range engine watches the
   trailing 480-bar range and buys when the close sits in its bottom
   12% while still at least 0.15% under the EMA anchor and the bar has
   stopped falling (close no lower than the previous close) - the
   capitulation print, not the falling knife. It exits at the range's
   60% height, or trails 0.25% under its peak once the 45% height is
   reached, with a 1.2% stop and a 420-bar time stop. On the dev window
   this adds the re-dip trade after the first snap-back (+$4.98) that
   the 120-bar cooldown of v1 slept through; the cooldown is now 45
   bars for every exit (60 after a stop-loss exit).

Live-race guards (they never trigger on the dev window; they exist for
the live 24 hours, whose regime is unknown):
- Trend guard: no entries while the EMA-240 falls faster than 0.35%
  per hour with the price beneath it (an accelerating crash is not a
  dislocation).
- Drawdown breaker, on marked equity: at -8% new entries are half
  size; at -14% entries halt for 180 bars; at -18% any position is
  liquidated and the bot stops for the day (the race disqualifies
  beyond -25%).
- Zero-trade fallback: the race bar requires at least one trade, so if
  nothing has triggered by minute 1200 the bot takes one small (25% of
  cash) round trip, sold after 30 bars, at +0.4%, or at a -1.0% stop.

Deterministic: decisions are a pure function of the bars and the
account the harness sends. No clock, no environment, no randomness.
Indicators run in float64 on +, -, *, / only, in a fixed order.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
SPAN = 420          # EMA span in bars (~7 hours of 1-minute bars)
DIP = 0.006         # dip engine: buy when close < EMA * (1 - DIP)
TARGET = 0.004      # dip engine: close >= EMA * (1 + TARGET) arms the runner
TRAIL = 0.002       # dip engine: once armed, sell under peak * (1 - TRAIL)
STOP = 0.015        # dip engine: sell under entry close * (1 - STOP)
MAX_HOLD = 720      # dip engine: time stop, bars in the position
COOLDOWN = 45       # bars to sit out after an exit
STOP_COOLDOWN = 60  # bars to sit out after a stop-loss exit
RNG_W = 480         # range engine: trailing range window in bars
RNG_IN = 0.12       # range engine: buy at/below this range height
RNG_BELOW_EMA = 0.0015  # range engine: close must be this far under the EMA
RNG_OUT = 0.6       # range engine: sell at/above this range height
RNG_ARM = 0.45      # range engine: trailing arms at this range height
RNG_TRAIL = 0.0025  # range engine: sell under peak * (1 - RNG_TRAIL)
RNG_STOP = 0.012    # range engine: sell under entry close * (1 - RNG_STOP)
RNG_HOLD = 420      # range engine: time stop, bars in the position
GUARD_SLOPE = -0.0035   # trend guard: EMA-240 slope over 60 bars
DD_HALF = 0.08      # drawdown breaker: half size from here
DD_HALT = 0.14      # drawdown breaker: halt entries for HALT_BARS from here
DD_DEAD = 0.18      # drawdown breaker: liquidate and stop from here
HALT_BARS = 180
FB_MINUTE = 1200    # zero-trade fallback: race minute of the forced trade
FB_FRAC = 0.25      # zero-trade fallback: fraction of cash
FB_HOLD = 30        # zero-trade fallback: bars to hold
FB_TARGET = 0.004   # zero-trade fallback: take-profit over entry close
FB_STOP = 0.010     # zero-trade fallback: stop under entry close
MAX_SLIPPAGE_BPS = 500


class DipBot:
    def __init__(self):
        self.warm = []            # closes collected until the EMA seeds
        self.ema = None           # float, in raw price units
        self.highs = []           # recent bar highs (raw ints), oldest first
        self.lows = []            # recent bar lows (raw ints)
        self.prev_close = None
        self.last_close = None
        self.rng_pos = None       # latest position in the 480-bar range
        self.warm240 = []         # closes collected until the EMA-240 seeds
        self.ema240 = None
        self.ema240_hist = []     # last 61 EMA-240 values (floats)
        self.entry_seq = None     # seq of the batch the open position was decided on
        self.entry_close = None   # close (float, raw units) the entry was decided on
        self.mode = None          # 'dip' | 'rng' | 'fb'
        self.buy_pending = False  # a buy was sent last batch, fill not yet seen
        self.was_held = False     # the account held the symbol on the previous batch
        self.last_exit_seq = -10 ** 9
        self.last_exit_stop = False
        self.stop_sent = False    # the sell we just sent was a stop-loss exit
        self.armed = False        # the exit runner/trailing is armed
        self.peak = None          # highest close since arming (float, raw units)
        self.entries_made = 0     # buys sent (the fallback fires only at zero)
        self.peak_eq = None       # highest marked equity (quote units)
        self.halt_until = -1      # entries blocked while seq < this
        self.dead = False         # drawdown breaker: stopped for the day

    def step_indicators(self, batch):
        for bar in batch["bars"]:
            if bar["symbol"] != SYMBOL:
                continue
            close = int(bar["c"])
            self.prev_close = self.last_close
            self.last_close = close
            self.highs.append(int(bar["h"]))
            self.lows.append(int(bar["l"]))
            if len(self.highs) > 1000:
                self.highs = self.highs[-1000:]
                self.lows = self.lows[-1000:]
            if len(self.lows) >= RNG_W:
                hh = max(self.highs[-RNG_W:])
                ll = min(self.lows[-RNG_W:])
                self.rng_pos = (close - ll) / (hh - ll) if hh > ll else None
            if self.ema240 is not None:
                self.ema240 += (2.0 / 241) * (close - self.ema240)
                self.ema240_hist.append(self.ema240)
                if len(self.ema240_hist) > 61:
                    self.ema240_hist = self.ema240_hist[-61:]
            else:
                self.warm240.append(close)
                if len(self.warm240) == 120:
                    self.ema240 = sum(self.warm240) / 120
                    self.warm240 = []
            if self.ema is not None:
                self.ema += (2.0 / (SPAN + 1)) * (close - self.ema)
            else:
                self.warm.append(close)
                if len(self.warm) == SPAN:
                    self.ema = sum(self.warm) / SPAN
                    self.warm = []

    def clear_entry(self):
        self.entry_seq = None
        self.entry_close = None
        self.mode = None
        self.armed = False
        self.peak = None

    def equity(self, state, held):
        eq = state.cash
        if held is not None:
            eq += state.value(SYMBOL, held.qty)
        return eq

    def on_batch(self, state, batch):
        self.step_indicators(batch)
        held = state.positions.get(SYMBOL)
        close = state.close(SYMBOL)

        # Book-keeping: notice fills and closes so the timers stay honest.
        if self.buy_pending:
            self.buy_pending = False
            if held is None:
                # The buy was rejected: forget the entry.
                self.clear_entry()
            else:
                self.entries_made += 1
        if held is None and self.was_held:
            self.last_exit_seq = state.seq
            self.last_exit_stop = self.stop_sent
            self.clear_entry()
        self.stop_sent = False
        self.was_held = held is not None

        if state.warming_up or close is None or self.ema is None:
            return None

        # Drawdown breaker bookkeeping (marked equity, quote units).
        eq = self.equity(state, held)
        if self.peak_eq is None or eq > self.peak_eq:
            self.peak_eq = eq
        dd = (self.peak_eq - eq) / self.peak_eq if self.peak_eq else 0.0
        if dd >= DD_DEAD:
            self.dead = True
            if held is not None:
                return [self.sell(state, held.qty)]
            return None
        if dd >= DD_HALT and state.seq >= self.halt_until:
            self.halt_until = state.seq + HALT_BARS

        if held is not None:
            if self.entry_seq is None:
                # Defensive: a position we do not remember opening.
                self.entry_seq = state.seq
                self.entry_close = float(close)
                self.mode = "dip"
            if self.mode == "rng":
                if close < self.entry_close * (1.0 - RNG_STOP):
                    self.stop_sent = True
                    return [self.sell(state, held.qty)]
                if state.seq - self.entry_seq >= RNG_HOLD:
                    return [self.sell(state, held.qty)]
                if self.peak is None or close > self.peak:
                    self.peak = float(close)
                if self.rng_pos is not None and self.rng_pos >= RNG_OUT:
                    return [self.sell(state, held.qty)]
                if self.rng_pos is not None and self.rng_pos >= RNG_ARM:
                    self.armed = True
                if self.armed and close < self.peak * (1.0 - RNG_TRAIL):
                    return [self.sell(state, held.qty)]
                return None
            if self.mode == "fb":
                if close < self.entry_close * (1.0 - FB_STOP):
                    self.stop_sent = True
                    return [self.sell(state, held.qty)]
                if close >= self.entry_close * (1.0 + FB_TARGET):
                    return [self.sell(state, held.qty)]
                if state.seq - self.entry_seq >= FB_HOLD:
                    return [self.sell(state, held.qty)]
                return None
            # Dip engine exits.
            if close < self.entry_close * (1.0 - STOP):
                self.stop_sent = True
                return [self.sell(state, held.qty)]
            if state.seq - self.entry_seq >= MAX_HOLD:
                return [self.sell(state, held.qty)]
            if self.armed:
                if close > self.peak:
                    self.peak = float(close)
                if close < self.peak * (1.0 - TRAIL) or close < self.ema * (1.0 + TARGET * 0.25):
                    return [self.sell(state, held.qty)]
                return None
            if close >= self.ema * (1.0 + TARGET):
                # Recovery reached: do not sell into strength. Arm the
                # runner and let the trailing exit decide.
                self.armed = True
                self.peak = float(close)
                return None
            return None

        # Flat: entries, unless the breaker has stopped or halted us.
        if self.dead or state.seq < self.halt_until:
            return None
        cooldown = STOP_COOLDOWN if self.last_exit_stop else COOLDOWN
        if state.seq - self.last_exit_seq <= cooldown:
            return None
        # Trend guard: an accelerating crash is not a dislocation.
        if self.ema240 is not None and len(self.ema240_hist) > 60:
            if self.ema240 / self.ema240_hist[0] - 1.0 < GUARD_SLOPE and close < self.ema240:
                return None
        size_mult = 0.5 if dd >= DD_HALF else 1.0

        # Dip engine entry.
        if close < self.ema * (1.0 - DIP):
            return self.buy(state, "dip", size_mult)
        # Range engine entry: bottom of the trailing range, under the
        # anchor, and no longer falling.
        if (self.rng_pos is not None and self.rng_pos <= RNG_IN
                and close < self.ema * (1.0 - RNG_BELOW_EMA)
                and self.prev_close is not None and close >= self.prev_close):
            return self.buy(state, "rng", size_mult)
        # Zero-trade fallback: the race requires at least one trade.
        if (self.entries_made == 0
                and state.seq - state.warmup_bars >= FB_MINUTE):
            return self.buy(state, "fb", FB_FRAC)
        return None

    def buy(self, state, mode, size_mult):
        notional = min(int(state.cash * size_mult), state.weight_room(SYMBOL))
        if notional < int(state.hello["fees"]["min_notional_quote_units"]):
            return None
        self.entry_seq = state.seq
        self.entry_close = float(state.close(SYMBOL))
        self.buy_pending = True
        self.armed = False
        self.peak = None
        self.mode = mode
        return [{
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": notional,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]

    @staticmethod
    def sell(state, qty):
        return {
            "side": "sell",
            "symbol": SYMBOL,
            "qty": qty,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }


BOT = DipBot()

if __name__ == "__main__":
    makefun_bars.run(BOT.on_batch)
