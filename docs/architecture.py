"""
Draws docs/architecture.png, the architecture picture in the README.

    python -m pip install matplotlib
    python docs/architecture.py

Edit the boxes and arrows below and rerun it, rather than editing the image. Not part of
the server; nothing here is installed or run in the container.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Ellipse, FancyBboxPatch, Polygon  # noqa: E402

OUT = Path(__file__).with_suffix(".png")
INK, QUIET, EDGE, LINE = "#1D2B36", "#556570", "#7A868E", "#5F6B73"
BLUE, BLUE_FILL, BOX_FILL = "#2F6FB0", "#DCE8F5", "#F4F6F5"
W, H = 120, 66

installed = {f.name for f in font_manager.fontManager.ttflist}
plt.rcParams["font.family"] = next((f for f in ("Calibri", "Carlito") if f in installed), "DejaVu Sans")

fig = plt.figure(figsize=(W / 10, H / 10), dpi=200)
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, W)
ax.set_ylim(H, 0)
ax.axis("off")

STYLES = {
    "title": {"fontsize": 11.5, "color": INK, "fontweight": "bold"},
    "name": {"fontsize": 11.5, "color": INK},
    "note": {"fontsize": 10.5, "color": QUIET},
    "flag": {"fontsize": 10.5, "color": BLUE, "fontweight": "bold"},
}


def box(x0, y0, x1, y1, lines, fill=BOX_FILL, edge=EDGE, lw=1.1):
    ax.add_patch(
        FancyBboxPatch(
            (x0, y0),
            x1 - x0,
            y1 - y0,
            boxstyle="round,pad=0,rounding_size=1.2",
            facecolor=fill,
            edgecolor=edge,
            lw=lw,
            zorder=3,
        )
    )
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    start = cy - (len(lines) - 1) * 1.45
    for i, (text, style) in enumerate(lines):
        ax.text(cx, start + i * 2.9, text, ha="center", va="center", zorder=4, **STYLES[style])


def database(x0, y0, x1, y1, lines):
    cx, w = (x0 + x1) / 2, x1 - x0
    t = np.linspace(np.pi, 0, 60)
    body = [(x0, y0 + 1), (x0, y1 - 1), *zip(cx + (w / 2) * np.cos(t), y1 - 1 + np.sin(t), strict=True), (x1, y0 + 1)]
    ax.add_patch(Polygon(body, closed=True, facecolor=BOX_FILL, edgecolor=EDGE, lw=1.1, zorder=3))
    ax.add_patch(Ellipse((cx, y0 + 1), w, 2.0, facecolor=BOX_FILL, edgecolor=EDGE, lw=1.1, zorder=4))
    cy = (y0 + y1) / 2 + 0.8
    for i, (text, style) in enumerate(lines):
        ax.text(cx, cy - 1.45 + i * 2.9, text, ha="center", va="center", zorder=5, **STYLES[style])


def arrow(points, label=None, at=None, ha="center", size=10):
    xs, ys = zip(*points, strict=True)
    ax.plot(xs, ys, color=LINE, lw=1.3, zorder=2)
    ax.annotate(
        "",
        xy=points[-1],
        xytext=points[-2],
        zorder=5,
        arrowprops={
            "arrowstyle": "-|>,head_length=0.7,head_width=0.35",
            "color": LINE,
            "lw": 1.3,
            "shrinkA": 0,
            "shrinkB": 0,
        },
    )
    if label:
        ax.text(*at, label, ha=ha, va="center", fontsize=size, color=QUIET, zorder=4)


ax.text(
    2,
    2.8,
    "Arrows that cross the dashed line carry data to or from outside the county",
    fontsize=14,
    fontweight="bold",
    color=INK,
    va="center",
)

# The county network, and the outside services on either side of it
ax.add_patch(
    FancyBboxPatch(
        (27, 8),
        60,
        56,
        boxstyle="round,pad=0,rounding_size=1.5",
        facecolor="white",
        edgecolor=EDGE,
        lw=1.3,
        ls=(0, (5, 3)),
        zorder=0,
    )
)
ax.text(28.6, 10.6, "County network", fontsize=12, fontweight="bold", color=INK, va="center")
ax.text(3, 10.6, "Outside the county", fontsize=12, fontweight="bold", color=QUIET, va="center")
ax.text(93, 10.6, "Outside the county", fontsize=12, fontweight="bold", color=QUIET, va="center")

box(3, 15, 21, 23, [("Copilot Studio", "name"), ("staff agent", "note")])
box(3, 38, 21, 46, [("Power Automate", "name"), ("flows", "note")])
database(31, 14, 47, 23.5, [("ACES database", "name"), ("ai_api procedures", "note")])
box(
    52,
    30,
    70,
    44,
    [("A.C.E.S. server", "title"), ("read-only", "note"), ("with reviewed facts", "note")],
    fill=BLUE_FILL,
    edge=BLUE,
    lw=1.8,
)
box(31, 52, 47, 60, [("Staff browser", "name"), ("/ui", "note")])
box(63, 52, 85, 60, [("Assessor parcel map", "name"), ("assessormap.bernco.gov", "note")])
box(93, 12, 117, 20, [("Microsoft Entra ID", "name"), ("county sign-in", "note")])
box(
    93,
    28,
    117,
    38.5,
    [("CustomGPT", "name"), ("specialist agents", "note"), ("Under contract with the office", "flag")],
)
box(93, 44, 117, 52, [("Azure OpenAI or Anthropic", "name"), ("chat model", "note")])

# Callers coming in
arrow([(21, 19), (31, 19)], "ACES_SQL", (23.4, 17.4), size=9)
arrow([(12, 23), (12, 33), (52, 33)], "MCP, access key", (13, 31.4), ha="left")
arrow([(21, 42), (52, 42)], "REST, admin token", (28.5, 40.4), ha="left")
arrow([(47, 56), (56, 56), (56, 44)], "POST /chat, signed in", (55, 48.5), ha="right")

# The server's calls
arrow([(56, 30), (56, 18.75), (47, 18.75)], "ai_api procedures only", (55, 25.5), ha="right")
arrow([(66, 30), (66, 16), (93, 16)], "county sign-in", (79, 14.4))
arrow([(70, 33), (93, 33)], "specialist questions", (80, 31.4))
arrow([(70, 41), (89, 41), (89, 48), (93, 48)], "chat only", (79, 39.4))
arrow([(66, 44), (66, 52)], "parcel lookups", (67, 48.5), ha="left")

fig.savefig(OUT, facecolor="white")
print(f"wrote {OUT}")
