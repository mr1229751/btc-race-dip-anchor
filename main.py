"""Dip-anchored mean-reversion bot for cbBTC/USDC on 1-minute bars.

Strategy (long only, one position, low turnover):

- Track an EMA of the last SPAN closes (about 7 hours). The EMA is seeded
  with the simple mean of its first SPAN closes, warm-up bars included, so
  the anchor is ready as early as the data allows.
- Buy with (nearly) all cash when the latest close falls DIP (0.6%) or more
  below the EMA: on this pool, multi-hour dips below the running mean have
  historically been liquidity dislocations that recover, and a single
  round trip costs roughly 0.3% in fees, slippage and impact, so the dip
  has to be at least twice that to be worth taking.
- Do not sell into the recovery: when the close reaches TARGET (0.4%)
  above the EMA the runner arms, and the position is sold when the close
  then falls TRAIL (0.2%) below its highest close since arming, or back
  under a quarter of the target band. Two guards override everything:
  sell when the close falls STOP (1.5%) below the close the entry was
  decided on (the dip was a real downtrend, not a dislocation), or after
  MAX_HOLD bars in the position without a recovery (dead money goes back
  to cash).
- After an exit, wait COOLDOWN bars before re-entering, so one dislocation
  is traded once, not churned.

Deterministic: the decisions are a pure function of the bars and the
account the harness sends. No clock, no environment, no randomness.
Indicators run in float64 on +, -, *, / only, in a fixed order.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
SPAN = 420          # EMA span in bars (~7 hours of 1-minute bars)
DIP = 0.006         # buy when close < EMA * (1 - DIP)
TARGET = 0.004      # recovery level: close >= EMA * (1 + TARGET) arms the runner
TRAIL = 0.002       # once armed, sell when close < peak close * (1 - TRAIL)
STOP = 0.015        # sell when close < entry close * (1 - STOP)
MAX_HOLD = 720      # sell after this many bars in the position
COOLDOWN = 120      # bars to sit out after an exit
MAX_SLIPPAGE_BPS = 500


class DipBot:
    def __init__(self):
        self.warm = []            # closes collected until the EMA seeds
        self.ema = None           # float, in raw price units
        self.entry_seq = None     # seq of the batch the open position was decided on
        self.entry_close = None   # close (float, raw units) the entry was decided on
        self.buy_pending = False  # a buy was sent last batch, fill not yet seen
        self.was_held = False     # the account held the symbol on the previous batch
        self.last_exit_seq = -10 ** 9
        self.armed = False        # the recovery target was reached: the runner is trailing
        self.peak = None          # highest close since the runner armed (float, raw units)

    def step_indicators(self, batch):
        for bar in batch["bars"]:
            if bar["symbol"] != SYMBOL:
                continue
            close = int(bar["c"])
            if self.ema is not None:
                self.ema += (2.0 / (SPAN + 1)) * (close - self.ema)
            else:
                self.warm.append(close)
                if len(self.warm) == SPAN:
                    self.ema = sum(self.warm) / SPAN
                    self.warm = []

    def on_batch(self, state, batch):
        self.step_indicators(batch)
        held = state.positions.get(SYMBOL)
        close = state.close(SYMBOL)

        # Book-keeping: notice fills and closes so the timers stay honest.
        if self.buy_pending:
            self.buy_pending = False
            if held is None:
                # The buy was rejected: forget the entry.
                self.entry_seq = None
                self.entry_close = None
                self.armed = False
                self.peak = None
        if held is None and self.was_held:
            self.last_exit_seq = state.seq
            self.entry_seq = None
            self.entry_close = None
            self.armed = False
            self.peak = None
        self.was_held = held is not None

        if state.warming_up or close is None or self.ema is None:
            return None

        if held is not None:
            if self.entry_seq is None:
                # Defensive: a position we do not remember opening.
                self.entry_seq = state.seq
                self.entry_close = float(close)
            if close < self.entry_close * (1.0 - STOP):
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

        # Flat: look for the next dip.
        if state.seq - self.last_exit_seq <= COOLDOWN:
            return None
        if close < self.ema * (1.0 - DIP):
            notional = min(state.cash, state.weight_room(SYMBOL))
            if notional < int(state.hello["fees"]["min_notional_quote_units"]):
                return None
            self.entry_seq = state.seq
            self.entry_close = float(close)
            self.buy_pending = True
            self.armed = False
            self.peak = None
            return [{
                "side": "buy",
                "symbol": SYMBOL,
                "notional_quote_units": notional,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return None

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
