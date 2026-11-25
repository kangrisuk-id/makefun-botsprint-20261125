"""Two-track pump.fun replay bot (agent-fun/replay/v2).

Most pump.fun coins die young and quiet, so this bot only buys curves that
already have a real crowd behind them, and it runs two tracks over one book.

Track A is the strict one: a coin deep into its bonding curve's life (22 to
84 SOL of real quote reserves against the 85 SOL a curve completes at),
hours old, with many distinct buyers rather than one wallet washing the
tape. Track B is the wide net: younger, shallower coins (6 to 40 SOL) that
already trade broadly, taken with small stakes and sold fast -- it is what
carries the count of distinct coins the competition's guardrails ask for.

Both tracks demand the same tape: over the last few thousand slots buyers
must be putting in more SOL than sellers take out, and the price must be
calm -- no vertical spike in the last few hundred slots, no sag far under
its own recent level. Every position is sold whole on its track's exits:
take profit, stop loss, a trailing drop from the best value seen, or a time
stop. Nothing here reads the clock, the environment or an unseeded random,
so two runs of the same tape produce the same orders byte for byte.

The wire protocol is the kit's Python SDK, vendored under vendor/ exactly as
the kit instructs; everything in this file is the strategy itself.
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402

SOL = makefun_bot.LAMPORTS_PER_SOL

# --- When a coin becomes interesting --------------------------------------
A_RQ_MIN = 22 * SOL            # track A: real quote reserves at entry
A_RQ_MAX = 84 * SOL            # curves complete at 85 SOL
A_MIN_BUYERS = 80              # distinct wallets that have ever bought it
A_MIN_TRADERS = 110            # distinct wallets that have ever traded it
A_MIN_AGE_SLOTS = 20000        # slots since its CreateEvent (~2.2 hours)
A_MAX_ENTRIES = 2

B_RQ_MIN = 6 * SOL             # track B: earlier, but already alive
B_RQ_MAX = 40 * SOL
B_MIN_BUYERS = 50
B_MIN_TRADERS = 70
B_MAX_ENTRIES = 1
B_MAX_OPEN = 2                 # track B never holds more than this many

# --- What the recent tape must look like (both tracks) --------------------
FLOW_SPAN_SLOTS = 3000         # window the demand check reads
FLOW_MIN_TRADES = 25           # trades inside that window
FLOW_MIN_BUY_SHARE = 58        # percent of the window's SOL flow that is buys

# --- Spike guard (both tracks) --------------------------------------------
SPIKE_SHORT_SPAN = 300         # slots
SPIKE_SHORT_MAX_PCT = 10       # fastest rise we will still chase
SPIKE_LONG_SPAN = 1500         # slots
SPIKE_LONG_MAX_PCT = 15
DIP_LONG_MAX_PCT = 8           # price may sag at most this far under its
                               # level one long span ago

# --- Sizing ---------------------------------------------------------------
A_STAKE_LAMPORTS = 30 * SOL // 100   # 0.30 SOL on a track A entry
B_STAKE_LAMPORTS = 5 * SOL // 100    # 0.05 SOL on a track B entry
MIN_CAP_LAMPORTS = 2 * SOL // 100    # skip coins whose size cap is dust
CASH_FLOOR_LAMPORTS = SOL // 2       # never commit the last 0.50 SOL
BUY_SLIPPAGE_BPS = 1200
SELL_SLIPPAGE_BPS = 2500

# --- Exits, per track: take profit, stop loss, trailing-stop arm and drop,
#     and the time stop. Values are the position's current value against
#     what its buys paid, in thousandths.
A_EXITS = (1280, 850, 1150, 150, 30000)
B_EXITS = (1200, 880, 1100, 80, 6000)

# --- Housekeeping ---------------------------------------------------------
REENTRY_COOLDOWN_SLOTS = 20000     # track A re-entries wait this long
CURVE_FEE_BPS = 125            # protocol 95 + creator 30, for value maths


class CoinNotes:
    """What we remember about one coin: who traded it and how it flows."""

    __slots__ = ("buyers", "traders", "flow", "flow_buy", "flow_sell",
                 "samples", "last_sample_slot", "born")

    def __init__(self):
        self.buyers = set()
        self.traders = set()
        self.flow = deque()          # (slot, is_buy, quote_lamports)
        self.flow_buy = 0
        self.flow_sell = 0
        self.samples = deque()       # (slot, virtual_quote, virtual_token)
        self.last_sample_slot = -1
        self.born = None             # slot of its CreateEvent, when seen


class Position:
    """One open position: what its buys paid and the tokens they brought."""

    __slots__ = ("paid", "tokens", "opened_slot", "peak_milli", "exit_sent",
                 "track")

    def __init__(self, paid, tokens, opened_slot, track):
        self.paid = paid
        self.tokens = tokens
        self.opened_slot = opened_slot
        self.peak_milli = 1000
        self.exit_sent = False
        self.track = track


notes = {}          # mint -> CoinNotes, in first-seen order
positions = {}      # mint -> Position
buying = {}         # mint -> track, for buys of ours still in flight
history = {}        # mint -> [track A entries, track B entries, last exit]


def price_milli(vq_now, vt_now, vq_then, vt_then):
    """Price now as thousandths of the price then, integer maths only."""
    return (vq_now * vt_then * 1000) // (vt_now * vq_then)


def sell_out(curve, tokens):
    """Quote a sale of `tokens` would bring in before curve fees."""
    return (tokens * curve.virtual_quote_reserves) // (
        curve.virtual_token_reserves + tokens)


def net_value_milli(curve, pos):
    """What closing now would pay (after curve fees) vs what entry paid."""
    gross = sell_out(curve, pos.tokens)
    net = gross - (gross * CURVE_FEE_BPS + 9999) // 10000
    return (net * 1000) // pos.paid


def note_events(batch):
    """Fold this batch's events into the per-coin notes."""
    slot = batch["slot"]
    for e in batch["events"]:
        mint = e["mint"]
        coin = notes.get(mint)
        if coin is None:
            coin = notes[mint] = CoinNotes()
        if e["event"] == "CreateEvent":
            coin.born = slot
        elif e["event"] == "TradeEvent":
            coin.traders.add(e["user"])
            quote = int(e["quote_amount"])
            if e["is_buy"]:
                coin.buyers.add(e["user"])
                coin.flow_buy += quote
            else:
                coin.flow_sell += quote
            coin.flow.append((slot, bool(e["is_buy"]), quote))
            while coin.flow and coin.flow[0][0] <= slot - FLOW_SPAN_SLOTS:
                _, was_buy, old = coin.flow.popleft()
                if was_buy:
                    coin.flow_buy -= old
                else:
                    coin.flow_sell -= old
            if slot - coin.last_sample_slot >= 100:
                coin.last_sample_slot = slot
                coin.samples.append((slot, int(e["virtual_quote_reserves"]),
                                     int(e["virtual_token_reserves"])))
                while len(coin.samples) > 40:
                    coin.samples.popleft()
        elif e["event"] == "CompleteEvent":
            notes.pop(mint, None)


def sample_at(coin, slot, span):
    """The oldest sample at least `span` slots before `slot`, if we have one."""
    target = slot - span
    found = None
    for s in coin.samples:
        if s[0] <= target:
            found = s
        else:
            break
    return found


def learn_fills(batch):
    """Fold our own fills into the position book."""
    slot = batch["slot"]
    for f in batch["fills"]:
        mint = f["mint"]
        if f["status"] != "filled":
            if f["side"] == "buy":
                buying.pop(mint, None)
            elif mint in positions:
                positions[mint].exit_sent = False
            continue
        if f["side"] == "buy":
            track = buying.pop(mint, "B")
            pos = positions.get(mint)
            if pos is None:
                positions[mint] = Position(int(f["quote_lamports"]),
                                           int(f["token_amount"]), slot, track)
            else:
                pos.paid += int(f["quote_lamports"])
                pos.tokens += int(f["token_amount"])
        else:
            pos = positions.pop(mint, None)
            if pos is not None:
                rec = history.setdefault(mint, [0, 0, 0])
                if pos.track == "A":
                    rec[0] += 1
                    rec[2] = slot     # only A exits start the A cooldown
                else:
                    rec[1] += 1


def calm_enough(coin, curve, slot):
    """The spike guard: no vertical run-up, no sagging collapse."""
    now_vq, now_vt = curve.virtual_quote_reserves, curve.virtual_token_reserves
    short = sample_at(coin, slot, SPIKE_SHORT_SPAN)
    if short is not None:
        if price_milli(now_vq, now_vt, short[1], short[2]) > 1000 + SPIKE_SHORT_MAX_PCT * 10:
            return False
    long = sample_at(coin, slot, SPIKE_LONG_SPAN)
    if long is not None:
        level = price_milli(now_vq, now_vt, long[1], long[2])
        if level > 1000 + SPIKE_LONG_MAX_PCT * 10:
            return False
        if level < 1000 - DIP_LONG_MAX_PCT * 10:
            return False
    return True


def tape_agrees(coin):
    """Recent flow: enough trades, and buyers putting in the larger share."""
    total = coin.flow_buy + coin.flow_sell
    if len(coin.flow) < FLOW_MIN_TRADES or total <= 0:
        return False
    return coin.flow_buy * 100 >= total * FLOW_MIN_BUY_SHARE


def want_entry(state, mint, slot):
    """Which track takes this coin now, with its stake, or None."""
    if mint in positions or mint in buying:
        return None
    curve = state.curves.get(mint)
    if curve is None or curve.complete:
        return None
    coin = notes.get(mint)
    if coin is None or not tape_agrees(coin) or not calm_enough(coin, curve, slot):
        return None
    rq = curve.real_quote_reserves
    a_taken, b_taken, last_exit = history.get(mint, (0, 0, -10 ** 18))
    if (A_RQ_MIN <= rq <= A_RQ_MAX and a_taken < A_MAX_ENTRIES
            and slot - last_exit >= REENTRY_COOLDOWN_SLOTS
            and coin.born is not None and slot - coin.born >= A_MIN_AGE_SLOTS
            and len(coin.buyers) >= A_MIN_BUYERS
            and len(coin.traders) >= A_MIN_TRADERS):
        return "A", A_STAKE_LAMPORTS
    if (B_RQ_MIN <= rq <= B_RQ_MAX and b_taken < B_MAX_ENTRIES
            and len(coin.buyers) >= B_MIN_BUYERS
            and len(coin.traders) >= B_MIN_TRADERS):
        open_b = sum(1 for p in positions.values() if p.track == "B")
        open_b += sum(1 for t in buying.values() if t == "B")
        if open_b < B_MAX_OPEN:
            return "B", B_STAKE_LAMPORTS
    return None


def exit_reason(state, mint, slot):
    """Why a held position should be sold now, or None to keep holding."""
    pos = positions[mint]
    curve = state.curves.get(mint)
    if curve is None or curve.complete or pos.exit_sent:
        return None
    take_profit, stop_loss, trail_arm, trail_drop, max_hold = (
        A_EXITS if pos.track == "A" else B_EXITS)
    value = net_value_milli(curve, pos)
    if value > pos.peak_milli:
        pos.peak_milli = value
    if value >= take_profit:
        return "take profit"
    if value <= stop_loss:
        return "stop loss"
    if pos.peak_milli >= trail_arm and value <= pos.peak_milli - trail_drop:
        return "trailing stop"
    if slot - pos.opened_slot >= max_hold:
        return "time stop"
    return None


def on_batch(state, batch):
    slot = batch["slot"]
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()
    learn_fills(batch)
    note_events(batch)

    orders = []
    free = state.free_cash_lamports()

    # Exits first: a sale frees a position slot a new entry may take.
    for mint in list(positions):
        if exit_reason(state, mint, slot) is None:
            continue
        pos = positions[mint]
        orders.append({"side": "sell", "mint": mint,
                       "token_amount": pos.tokens,
                       "max_slippage_bps": SELL_SLIPPAGE_BPS})
        pos.exit_sent = True
        if len(orders) >= caps["max_orders_per_batch"]:
            return orders

    # Entries, in the order this batch's events first name the coins.
    slots = caps["max_open_positions"] - state.committed_position_count()
    seen = set()
    for e in batch["events"]:
        if len(orders) >= caps["max_orders_per_batch"] or slots <= 0:
            break
        mint = e["mint"]
        if mint in seen or e["event"] != "TradeEvent":
            continue
        seen.add(mint)
        pick = want_entry(state, mint, slot)
        if pick is None:
            continue
        track, stake = pick
        amount = min(state.max_buy_lamports(mint), stake)
        if amount < MIN_CAP_LAMPORTS or free < amount + tx + CASH_FLOOR_LAMPORTS:
            continue
        orders.append({"side": "buy", "mint": mint,
                       "quote_lamports": amount,
                       "max_slippage_bps": BUY_SLIPPAGE_BPS})
        buying[mint] = track
        free -= amount + tx
        slots -= 1
    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
