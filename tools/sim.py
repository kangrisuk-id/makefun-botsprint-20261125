"""Offline replay simulator for this bot, used only for development testing.

A faithful Python port of the harness market model (sim/engine.ts and
sim/curve.ts of the kit's harness source): one batch per slot carrying
universe events, orders decided on slot s resolving at s + latency against
that mint's last recorded trade in the resolving slot, the bot's own fills
shifting the reserves it meets (the offset rule), exact-in buys and exact
token sells with the curve's own fee maths, the harness's rejection order,
completion closes and the end-of-window flatten, and a seeded failed
transaction draw at the competition's failed_tx_bps. It drives the real
main.py through the kit SDK's BotState, exactly as the grader's shim does,
and reports net PnL, round trips, drawdown and guardrail compliance.

It is not the grader: the official grade-local needs Docker, which this
machine does not have, so every number it prints is labelled a simulator
estimate wherever the bot's results are reported.

Usage:
  python3 tools/sim.py --kit <kit dir> [--from-slot S] [--to-slot S]
                       [--fail-seed N] [--set KEY=VALUE ...]
                       [--curve-out equity.csv] [--json]
"""

import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import random
import sys

BPS = 10000
LAMPORTS = 1_000_000_000
TX_FEE = 5000 + 100000            # hello fees: base + priority
FAILED_TX_BPS = 200               # competition.yaml
LATENCY = 2
CAPITAL = 10_000_000_000
MIN_BUY = 10_000_000              # guardrail round-trip floor, 0.01 SOL


def ceil_div(a, b):
    return -(-a // b)


def fee_of(amount, bps):
    return ceil_div(amount * bps, BPS)


def sell_quote_out(t, vq, vt):
    return (t * vq) // (vt + t)


def buy_tokens_out(q, vq, vt):
    return 0 if q <= 1 else ((q - 1) * vt) // (vq + q - 1)


def buy_quote_in(t, vq, vt):
    return (t * vq) // (vt - t) + 1


def quote_buy_exact_in(spend, st, pb, cb):
    net0 = (spend * BPS) // (BPS + pb + cb)
    fee = fee_of(net0, pb)
    cfee = fee_of(net0, cb)
    over = net0 + fee + cfee - spend if net0 + fee + cfee > spend else 0
    q = net0 - over
    t = buy_tokens_out(q, st["vq"], st["vt"])
    if t > st["rt"]:
        tc = st["rt"]
        if tc == 0:
            return {"q": 0, "t": 0, "fee": 0, "cfee": 0, "paid": 0}
        q = buy_quote_in(tc, st["vq"], st["vt"])
        fee = fee_of(q, pb)
        cfee = fee_of(q, cb)
        return {"q": q, "t": tc, "fee": fee, "cfee": cfee, "paid": q + fee + cfee}
    return {"q": q, "t": t, "fee": fee, "cfee": cfee, "paid": q + fee + cfee}


def quote_sell(t, st, pb, cb):
    q = sell_quote_out(t, st["vq"], st["vt"])
    fee = fee_of(q, pb)
    cfee = fee_of(q, cb)
    return {"q": q, "fee": fee, "cfee": cfee, "proceeds": q - fee - cfee}


def offset_reserves(rec, off):
    num, den = 1, 1
    if off["tokens"] > rec["rt"]:
        num, den = rec["rt"], off["tokens"]
    if off["quote"] < 0 and rec["rq"] * den < num * -off["quote"]:
        num, den = rec["rq"], -off["quote"]

    def scale(x):
        if num == den:
            return x
        return (x * num) // den if x >= 0 else -((-x * num) // den)

    q, t = scale(off["quote"]), scale(off["tokens"])
    return {"vq": rec["vq"] + q, "vt": rec["vt"] - t,
            "rq": rec["rq"] + q, "rt": rec["rt"] - t}


def load_events(kit):
    man = json.load(open(os.path.join(kit, "dataset", "manifest.json")))
    sample = man["universe"]["sample"]
    mod, res = sample["modulus"], sample["residue"]
    excl = {x["mint"] for x in man.get("excluded_mints") or []}
    members, done, events = set(), set(), []
    files = sorted(f["path"] for f in man["files"] if f["path"].endswith(".gz")) \
        if "files" in man else ["dataset/events/part-00000.ndjson.gz",
                                "dataset/events/part-00001.ndjson.gz",
                                "dataset/events/part-00002.ndjson.gz"]
    for rel in files:
        path = os.path.join(kit, rel)
        if not os.path.exists(path):
            path = os.path.join(kit, "dataset", rel)
        with gzip.open(path, "rt") as fh:
            for line in fh:
                e = json.loads(line)
                mint = e["mint"]
                if e["event"] == "CreateEvent" and mint not in members:
                    h = int(hashlib.sha256(mint.encode()).hexdigest(), 16)
                    if (e["quote_mint"] == "11111111111111111111111111111111"
                            and not e["is_mayhem_mode"]
                            and not e["is_cashback_enabled"]
                            and mint not in excl and h % mod == res):
                        members.add(mint)
                if mint in members and mint not in done:
                    events.append(e)
                    if e["event"] == "CompleteEvent":
                        done.add(mint)
    events.sort(key=lambda e: (e["slot"], e["mint"], e["chain_seq"]))
    return events, man["window"]["end_slot"]


class Sim:
    def __init__(self, bot, fail_seed):
        self.bot = bot
        self.rng = random.Random(fail_seed)
        self.cash = CAPITAL
        self.positions = {}       # mint -> {"tokens": int, "cost": int}
        self.recorded = {}        # mint -> reserves + complete + last bps
        self.offset = {}          # mint -> {"quote": int, "tokens": int}
        self.pending = []         # orders waiting for their resolve slot
        self.report = []          # fills for the next batch
        self.reply_buy_sum = {}   # (seq, mint) -> quote that passed size check
        self.trips = []           # closed round trips
        self.open_trip = {}       # mint -> {"bought": int, "cf": int, "opened": int}
        self.equity = [CAPITAL]
        self.fills = 0
        self.close_n = 0
        self.reject_reasons = {}

    # -- bookkeeping -------------------------------------------------------
    def account(self):
        return {"cash_lamports": str(self.cash),
                "positions": [{"mint": m, "token_amount": str(p["tokens"]),
                               "cost_lamports": str(p["cost"])}
                              for m, p in self.positions.items()]}

    def shifted(self, mint):
        return offset_reserves(self.recorded[mint],
                               self.offset.get(mint, {"quote": 0, "tokens": 0}))

    def bps_of(self, mint):
        rec = self.recorded[mint]
        if rec["bps"] is None:
            return 95, 30, True
        return rec["bps"][0], rec["bps"][1], False

    def trip_flow(self, mint, delta):
        trip = self.open_trip.get(mint)
        if trip is not None:
            trip["cf"] += delta

    def close_position(self, mint, slot, kind):
        """A harness close (completion or flatten): sell everything held."""
        pos = self.positions.pop(mint, None)
        if pos is None:
            return
        st = self.shifted(mint)
        pb, cb, fb = self.bps_of(mint)
        out = quote_sell(pos["tokens"], st, pb, cb)
        self.close_n += 1
        cid = f"_{kind}_{self.close_n}"
        if out["proceeds"] <= TX_FEE:
            self.report.append(self.fill(cid, mint, "sell", "filled", None, slot,
                                         slot, 0, 0, 0, 0, fb, kind))
            self.trip_flow(mint, 0)
        else:
            self.cash += out["proceeds"] - TX_FEE
            off = self.offset.setdefault(mint, {"quote": 0, "tokens": 0})
            off["quote"] -= out["q"]
            off["tokens"] -= pos["tokens"]
            self.report.append(self.fill(cid, mint, "sell", "filled", None, slot,
                                         slot, out["proceeds"], pos["tokens"],
                                         out["fee"] + out["cfee"], TX_FEE, fb, kind))
            self.trip_flow(mint, out["proceeds"] - TX_FEE)
        trip = self.open_trip.pop(mint, None)
        if trip is not None:
            self.trips.append({"mint": mint, "opened": trip["opened"],
                               "closed": slot, "bought": trip["bought"],
                               "pnl": trip["cf"], "exit": kind})

    def fill(self, cid, mint, side, status, reason, decided, slot, quote,
             tokens, fees, tx, fb, kind="order"):
        return {"client_id": cid, "mint": mint, "side": side, "status": status,
                "reason": reason, "decided_slot": decided, "slot": slot,
                "quote_lamports": str(quote), "token_amount": str(tokens),
                "fee_lamports": str(fees), "tx_fee_lamports": str(tx),
                "fee_fallback": fb, "_kind": kind}

    # -- order resolution ----------------------------------------------------
    def resolve(self, od, slot):
        mint = od["mint"]
        rec = self.recorded.get(mint)
        order = od["order"]
        side = order["side"]
        cid = order["client_id"]
        decided = od["decided_slot"]

        def reject(reason):
            self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1
            self.report.append(self.fill(cid, mint, side, "rejected", reason,
                                         decided, slot, 0, 0, 0, 0, False))

        if rec is None or rec["complete"]:
            return reject("token_complete")
        pos = self.positions.get(mint)
        if side == "sell" and (pos is None or int(order["token_amount"]) > pos["tokens"]):
            return reject("insufficient_tokens")
        if side == "buy" and pos is None and len(self.positions) >= 5:
            return reject("max_open_positions")
        if side == "buy":
            key = (od["seq"], mint)
            cap = od["decision"]["rq"] * 100 // BPS
            used = self.reply_buy_sum.get(key, 0)
            if used + int(order["quote_lamports"]) > cap:
                return reject("max_order_size")
            self.reply_buy_sum[key] = used + int(order["quote_lamports"])
        st = self.shifted(mint)
        pb, cb, fb = self.bps_of(mint)
        if side == "buy":
            got = quote_buy_exact_in(int(order["quote_lamports"]), st, pb, cb)
            if got["t"] == 0:
                return reject("zero_amount")
            want = quote_buy_exact_in(int(order["quote_lamports"]),
                                      od["decision"], pb, cb)
            if got["t"] * BPS < want["t"] * (BPS - int(order["max_slippage_bps"])):
                return reject("max_slippage")
            if self.cash < int(order["quote_lamports"]) + TX_FEE:
                return reject("insufficient_cash")
        else:
            got = quote_sell(int(order["token_amount"]), st, pb, cb)
            if got["proceeds"] <= 0:
                return reject("zero_amount")
            if got["q"] > st["rq"]:
                return reject("max_order_size")
            want = quote_sell(int(order["token_amount"]), od["decision"], pb, cb)
            if got["q"] * BPS < want["q"] * (BPS - int(order["max_slippage_bps"])):
                return reject("max_slippage")
            if self.cash < TX_FEE:
                return reject("insufficient_cash")
        if self.rng.random() * BPS < FAILED_TX_BPS:
            self.cash -= TX_FEE
            self.trip_flow(mint, -TX_FEE)
            self.report.append(self.fill(cid, mint, side, "failed", None,
                                         decided, slot, 0, 0, 0, TX_FEE, fb))
            return
        self.fills += 1
        off = self.offset.setdefault(mint, {"quote": 0, "tokens": 0})
        if side == "buy":
            self.cash -= got["paid"] + TX_FEE
            p = self.positions.setdefault(mint, {"tokens": 0, "cost": 0})
            p["tokens"] += got["t"]
            p["cost"] += got["paid"]
            off["quote"] += got["q"]
            off["tokens"] += got["t"]
            trip = self.open_trip.setdefault(
                mint, {"bought": 0, "cf": 0, "opened": slot})
            trip["bought"] += got["paid"]
            trip["cf"] -= got["paid"] + TX_FEE
            self.report.append(self.fill(cid, mint, side, "filled", None,
                                         decided, slot, got["paid"], got["t"],
                                         got["fee"] + got["cfee"], TX_FEE, fb))
        else:
            amount = int(order["token_amount"])
            self.cash += got["proceeds"] - TX_FEE
            pos["tokens"] -= amount
            pos["cost"] = 0 if pos["tokens"] == 0 else pos["cost"]
            off["quote"] -= got["q"]
            off["tokens"] -= amount
            self.trip_flow(mint, got["proceeds"] - TX_FEE)
            self.report.append(self.fill(cid, mint, side, "filled", None,
                                         decided, slot, got["proceeds"],
                                         amount, got["fee"] + got["cfee"],
                                         TX_FEE, fb))
            if pos["tokens"] == 0:
                del self.positions[mint]
                trip = self.open_trip.pop(mint, None)
                if trip is not None:
                    self.trips.append({"mint": mint, "opened": trip["opened"],
                                       "closed": slot, "bought": trip["bought"],
                                       "pnl": trip["cf"], "exit": "sell"})

    # -- main loop -----------------------------------------------------------
    def run(self, events, end_slot, bot_state, on_batch):
        by_slot = {}
        for e in events:
            by_slot.setdefault(e["slot"], []).append(e)
        first = min(by_slot) if by_slot else end_slot
        seq = 0
        completed_now = []
        for slot in range(first, end_slot + 1):
            todays = by_slot.get(slot)
            if todays:
                for e in todays:
                    mint = e["mint"]
                    rec = self.recorded.get(mint)
                    if e["event"] == "CreateEvent":
                        self.recorded[mint] = {
                            "vq": int(e["virtual_quote_reserves"]),
                            "vt": int(e["virtual_token_reserves"]),
                            "rq": 0, "rt": int(e["real_token_reserves"]),
                            "complete": False, "bps": None}
                    elif e["event"] == "TradeEvent":
                        if rec is None:
                            rec = self.recorded[mint] = {
                                "vq": 0, "vt": 0, "rq": 0, "rt": 0,
                                "complete": False, "bps": None}
                        rec["vq"] = int(e["virtual_quote_reserves"])
                        rec["vt"] = int(e["virtual_token_reserves"])
                        rec["rq"] = int(e["real_quote_reserves"])
                        rec["rt"] = int(e["real_token_reserves"])
                        pb, cb = int(e["fee_basis_points"]), int(e["creator_fee_basis_points"])
                        rec["bps"] = (pb, cb) if pb else None
                    elif e["event"] == "CompleteEvent" and rec is not None:
                        rec["complete"] = True
                        completed_now.append(mint)
                batch = {"type": "batch", "seq": seq, "slot": slot,
                         "events": todays, "marks": [],
                         "fills": [{k: v for k, v in f.items() if k != "_kind"}
                                   for f in self.report],
                         "account": self.account()}
                self.report = []
                bot_state.apply_batch(batch)
                orders = bot_state.to_orders(on_batch(bot_state, batch))
                bot_state.record_orders(orders)
                for o in orders:
                    mint = o["mint"]
                    self.pending.append({
                        "order": o, "seq": seq, "decided_slot": slot,
                        "resolve": slot + LATENCY, "mint": mint,
                        "decision": self.shifted(mint)})
                seq += 1
                for mint in completed_now:
                    self.close_position(mint, slot, "complete")
                completed_now = []
            due = [od for od in self.pending if od["resolve"] == slot]
            self.pending = [od for od in self.pending if od["resolve"] != slot]
            for od in due:
                self.resolve(od, slot)
            if todays:
                eq = self.cash
                for mint, p in self.positions.items():
                    pb, cb, _ = self.bps_of(mint)
                    out = quote_sell(p["tokens"], self.shifted(mint), pb, cb)
                    eq += max(0, out["proceeds"] - TX_FEE)
                self.equity.append(eq)
        for mint in list(self.positions):
            self.close_position(mint, end_slot, "flatten")
        self.equity.append(self.cash)
        return seq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True)
    ap.add_argument("--bot-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".."))
    ap.add_argument("--from-slot", type=int)
    ap.add_argument("--to-slot", type=int)
    ap.add_argument("--fail-seed", type=int, default=1)
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--curve-out")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    bot_dir = os.path.abspath(args.bot_dir)
    sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "entry_main", os.path.join(bot_dir, "main.py"))
    bot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bot)
    for kv in args.set:
        k, v = kv.split("=", 1)
        setattr(bot, k, int(v))

    import makefun_bot
    hello = {"type": "hello", "protocol": "agent-fun/replay/v2", "seed": "0",
             "universe": {"program": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                          "quote": "SOL", "mayhem": "excluded",
                          "exclude_cashback": True,
                          "sample": {"hash": "sha256", "modulus": 16, "residue": 15}},
             "capital": {"lamports": str(CAPITAL)},
             "caps": {"max_open_positions": 5, "max_order_bps_of_real_reserve": 100,
                      "max_orders_per_batch": 16, "shorts": False, "leverage": False},
             "latency_slots": LATENCY,
             "fees": {"protocol_bps_fallback": "95", "creator_bps_fallback": "30",
                      "base_fee_lamports": "5000", "priority_fee_lamports": "100000",
                      "failed_tx_bps": FAILED_TX_BPS}}
    events, end_slot = load_events(args.kit)
    if args.from_slot is not None:
        events = [e for e in events if e["slot"] >= args.from_slot]
    if args.to_slot is not None:
        end_slot = args.to_slot
        events = [e for e in events if e["slot"] <= args.to_slot]
    sim = Sim(bot, args.fail_seed)
    state = makefun_bot.BotState(hello)
    batches = sim.run(events, end_slot, state, bot.on_batch)

    counted = [t for t in sim.trips if t["bought"] >= MIN_BUY]
    counted_mints = {t["mint"] for t in counted}
    peak, max_dd = CAPITAL, 0.0
    for eq in sim.equity:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    gains = sorted((t["pnl"] for t in sim.trips), reverse=True)
    result = {
        "batches": batches,
        "net_pnl_sol": (sim.cash - CAPITAL) / LAMPORTS,
        "fills": sim.fills,
        "round_trips": len(sim.trips),
        "counted_round_trips": len(counted),
        "counted_mints": len(counted_mints),
        "max_drawdown": round(max_dd, 4),
        "guardrails_ok": len(counted_mints) >= 20 and max_dd <= 0.5,
        "reject_reasons": sim.reject_reasons,
        "top3_share_of_positive": None,
        "trip_pnls_sol": [round(t["pnl"] / LAMPORTS, 4) for t in sim.trips],
        "trips": [{"opened": t["opened"], "held": t["closed"] - t["opened"],
                   "bought_sol": round(t["bought"] / LAMPORTS, 4),
                   "pnl_sol": round(t["pnl"] / LAMPORTS, 4), "exit": t["exit"]}
                  for t in sorted(sim.trips, key=lambda t: t["pnl"])[:8]],
        "best": [{"held": t["closed"] - t["opened"],
                  "pnl_sol": round(t["pnl"] / LAMPORTS, 4), "exit": t["exit"]}
                 for t in sorted(sim.trips, key=lambda t: -t["pnl"])[:8]],
        "exit_kinds": {},
    }
    pos_sum = sum(g for g in gains if g > 0)
    if pos_sum > 0:
        result["top3_share_of_positive"] = round(sum(gains[:3]) / pos_sum, 3)
    for t in sim.trips:
        k = t["exit"]
        e = result["exit_kinds"].setdefault(k, {"n": 0, "pnl": 0.0})
        e["n"] += 1
        e["pnl"] = round(e["pnl"] + t["pnl"] / LAMPORTS, 4)
    if args.curve_out:
        with open(args.curve_out, "w") as fh:
            fh.write("point,equity_sol\n")
            for i, eq in enumerate(sim.equity):
                fh.write(f"{i},{eq / LAMPORTS:.6f}\n")
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        for k, v in result.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()
