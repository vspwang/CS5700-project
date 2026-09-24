"""Generate the report figures from the CSVs written by bench.py.

    python experiments/plot.py

Reads whatever exists in results/ and skips the rest, so you can plot after
each experiment instead of waiting until all of them are done.
"""

import csv
import statistics
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"

# Categorical slots, fixed order, validated for colour-vision deficiency.
C1, C2 = "#2a78d6", "#eb6834"
INK, MUTED, GRID = "#1a1a19", "#52514e", "#c3c2b7"


def load(label):
    path = RESULTS / f"{label}.csv"
    if not path.exists():
        return None
    with path.open() as fh:
        return [float(r["rtt_ms"]) for r in csv.DictReader(fh) if r["ok"] == "1"]


def style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, length=3)
    ax.yaxis.grid(True, color=GRID, linewidth=0.6, alpha=0.6)
    ax.set_axisbelow(True)


def bars(ax, labels, values, color, title, ylabel, fmt="{:.2f}"):
    xs = range(len(values))
    ax.bar(xs, values, width=0.55, color=color, zorder=3)
    for x, v in zip(xs, values):
        ax.annotate(fmt.format(v), (x, v), textcoords="offset points",
                    xytext=(0, 4), ha="center", fontsize=9, color=INK)
    ax.set_xticks(list(xs), labels, fontsize=9)
    ax.set_title(title, fontsize=11, color=INK, loc="left")
    ax.set_ylabel(ylabel, fontsize=9, color=MUTED)
    ax.set_ylim(0, max(values) * 1.25)
    style(ax)


def fig_conditions():
    lat0, lat1 = load("baseline-latency"), load("latency-100ms")
    los0, los1 = load("baseline-loss"), load("loss-5pct")
    panels = [p for p in ((lat0, lat1, "Experiment 1: 100 ms induced latency",
                           ["baseline", "netem delay 100ms"], "{:.2f}"),
                          (los0, los1, "Experiment 2: 5% packet loss",
                           ["baseline", "netem loss 5%"], "{:.2f}"))
              if p[0] and p[1]]
    if not panels:
        return
    fig, axes = plt.subplots(1, len(panels), figsize=(5 * len(panels), 3.6))
    axes = [axes] if len(panels) == 1 else list(axes)
    for ax, (a, b, title, names, fmt) in zip(axes, panels):
        bars(ax, names, [statistics.fmean(a), statistics.fmean(b)],
             [C1, C2], title, "mean response time (ms)", fmt)
    fig.suptitle("Polly: mean Representative Operation response time",
                 fontsize=12, color=INK, x=0.01, ha="left")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    save(fig, "fig1_conditions.png")


def fig_loss_cdf():
    a, b = load("baseline-loss"), load("loss-5pct")
    if not (a and b):
        return
    fig, ax = plt.subplots(figsize=(6, 3.8))
    for data, color, name in ((a, C1, "baseline"), (b, C2, "5% configured loss")):
        xs = sorted(data)
        ys = [(i + 1) / len(xs) for i in range(len(xs))]
        ax.step(xs, ys, where="post", color=color, linewidth=2, label=name, zorder=3)
    ax.set_xscale("log")
    ax.set_xlabel("response time (ms, log scale)", fontsize=9, color=MUTED)
    ax.set_ylabel("fraction of operations", fontsize=9, color=MUTED)
    ax.set_title("Packet loss moves the tail, not the median",
                 fontsize=11, color=INK, loc="left")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK, loc="lower right")
    style(ax)
    fig.tight_layout()
    save(fig, "fig2_loss_cdf.png")


def fig_concurrency():
    series = [(n, load(f"c{n}")) for n in (1, 3, 5)]
    series = [(n, d) for n, d in series if d]
    if len(series) < 2:
        return
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    names = [f"{n} client{'s' if n > 1 else ''}" for n, _ in series]
    bars(axes[0], names, [statistics.fmean(d) for _, d in series], C1,
         "Mean response time", "ms")
    bars(axes[1], names,
         [sorted(d)[min(len(d) - 1, int(0.95 * len(d)))] for _, d in series], C2,
         "95th percentile response time", "ms")
    fig.suptitle("Experiment 3: 20 Representative Operations per client",
                 fontsize=12, color=INK, x=0.01, ha="left")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    save(fig, "fig3_concurrency.png")


def save(fig, name):
    out = RESULTS / name
    fig.savefig(out, dpi=150, facecolor="#fcfcfb")
    plt.close(fig)
    print(f"wrote results/{name}")


if __name__ == "__main__":
    if not RESULTS.exists():
        sys.exit("no results/ directory -- run bench.py first")
    fig_conditions()
    fig_loss_cdf()
    fig_concurrency()
