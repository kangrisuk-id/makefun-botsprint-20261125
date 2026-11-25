"""Build the README chart images from simulator output (build-time only).

The bot itself never touches PIL -- the grader's Python image is stdlib
only. These charts are documentation assets, generated locally from the
equity curve the simulator writes:

  python3 tools/sim.py --kit <kit dir> --curve-out assets/equity-full.csv
  python3 tools/make_charts.py assets/equity-full.csv

Outputs assets/cover.png, assets/equity-full.png and assets/slices.png.
The slice numbers below are the four consecutive 6-hour runs of the final
configuration, as reported in the README's test table; regenerate them with
--from-slot/--to-slot over the dev window's quarters.
"""

import csv
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "..", "assets")

BG = (13, 17, 23)
PANEL = (22, 27, 34)
GRID = (48, 54, 61)
TEXT = (230, 237, 243)
MUTED = (139, 148, 158)
GOLD = (227, 179, 65)
GREEN = (63, 185, 80)
RED = (248, 81, 73)

SLICES = [  # (label, net PnL SOL, distinct coins, max drawdown %)
    ("slice 1", -0.047, 18, 0.88),
    ("slice 2", -0.002, 20, 0.55),
    ("slice 3", +0.100, 28, 0.99),
    ("slice 4", -0.076, 25, 1.22),
]


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for base in ("/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/"):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def load_curve(path):
    with open(path) as fh:
        rows = list(csv.reader(fh))
    return [float(r[1]) for r in rows[1:]]


def draw_curve(draw, pts, box, color, width=3):
    x0, y0, x1, y1 = box
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    xy = [(x0 + (x1 - x0) * i / (len(pts) - 1),
           y1 - (y1 - y0) * (v - lo) / span) for i, v in enumerate(pts)]
    draw.line(xy, fill=color, width=width)
    return lo, hi


def cover(curve):
    img = Image.new("RGB", (1200, 480), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1200, 10], fill=GOLD)
    d.text((60, 52), "TWO-TRACK PUMP.FUN BOT", font=font(54, True), fill=TEXT)
    d.text((60, 122), "Late-stage strength on one track, a fast wide net on the other.",
           font=font(25), fill=MUTED)
    stats = [("sim net PnL (24 h dev tape)", "+0.059 SOL"),
             ("round trips / distinct coins", "97 / 87"),
             ("max drawdown", "1.5%"),
             ("capital / guardrail pace", "10 SOL / 20+ coins per 6 h")]
    y = 180
    for label, value in stats:
        d.text((60, y), label, font=font(21), fill=MUTED)
        d.text((560, y), value, font=font(21, True), fill=GOLD)
        y += 36
    draw_curve(d, curve, (60, 350, 1140, 440), GREEN, 3)
    d.text((60, 448), "equity on the dev tape, simulator run (see README: estimates, not the grader)",
           font=font(16), fill=MUTED)
    img.save(os.path.join(ASSETS, "cover.png"))


def equity_chart(curve):
    img = Image.new("RGB", (1200, 560), BG)
    d = ImageDraw.Draw(img)
    d.text((60, 30), "Equity curve -- full 24 h dev window, simulator",
           font=font(28, True), fill=TEXT)
    box = (90, 90, 1140, 470)
    lo, hi = min(curve), max(curve)
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        y = box[3] - (box[3] - box[1]) * i / 4
        d.line([box[0], y, box[2], y], fill=GRID, width=1)
        d.text((8, y - 10), f"{v:.2f}", font=font(16), fill=MUTED)
    draw_curve(d, curve, box, GREEN, 2)
    d.line([box[0], box[3] - (box[3] - box[1]) * (10.0 - lo) / ((hi - lo) or 1),
            box[2], box[3] - (box[3] - box[1]) * (10.0 - lo) / ((hi - lo) or 1)],
           fill=GOLD, width=1)
    d.text((90, 492), "gold line: the 10 SOL start. Equity marks every position at its close-out value,",
           font=font(17), fill=MUTED)
    d.text((90, 518), "after curve and transaction fees -- the same accounting the harness uses.",
           font=font(17), fill=MUTED)
    img.save(os.path.join(ASSETS, "equity-full.png"))


def slices_chart():
    img = Image.new("RGB", (1200, 640), BG)
    d = ImageDraw.Draw(img)
    d.text((60, 30), "Four consecutive 6-hour slices of the dev window",
           font=font(28, True), fill=TEXT)
    d.text((60, 68), "the final window is 6 hours, so this is the shape of one real run",
           font=font(19), fill=MUTED)
    base_y = 360
    d.line([70, base_y, 1140, base_y], fill=GRID, width=2)
    scale = 1400.0  # px per SOL
    for i, (label, pnl, mints, dd) in enumerate(SLICES):
        cx = 170 + i * 270
        h = pnl * scale
        color = GREEN if pnl >= 0 else RED
        top, bottom = min(base_y, base_y - h), max(base_y, base_y - h)
        d.rectangle([cx - 70, top, cx + 70, bottom], fill=color)
        y = bottom + 14
        d.text((cx - 62, y), label, font=font(18), fill=TEXT)
        d.text((cx - 62, y + 28), f"{pnl:+.3f} SOL", font=font(19, True), fill=color)
        d.text((cx - 62, y + 58), f"{mints} coins", font=font(17), fill=MUTED)
        d.text((cx - 62, y + 84), f"DD {dd:.2f}%", font=font(17), fill=MUTED)
    d.text((70, 130), "Guardrails per slice: at least 20 distinct coins and drawdown under 50%.",
           font=font(18), fill=MUTED)
    d.text((70, 156), "Three slices clear the coin count; the first reaches 18. Results hover around",
           font=font(18), fill=MUTED)
    d.text((70, 182), "breakeven per slice -- the honest summary is in the README's test table.",
           font=font(18), fill=MUTED)
    img.save(os.path.join(ASSETS, "slices.png"))


if __name__ == "__main__":
    curve = load_curve(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(ASSETS, "equity-full.csv"))
    os.makedirs(ASSETS, exist_ok=True)
    cover(curve)
    equity_chart(curve)
    slices_chart()
    print("wrote cover.png, equity-full.png, slices.png to", ASSETS)
