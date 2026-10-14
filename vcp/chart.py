"""把 VCP 形态画成图：K线 + 均线 + 每段收缩（标注回撤%）+ pivot 线 + 成交量。"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .detector import VCPResult  # noqa: E402

UP, DOWN = "#26a69a", "#ef5350"
SEG_COLORS = ["#ff9800", "#ab47bc", "#42a5f5", "#66bb6a", "#ec407a", "#8d6e63"]


def plot_vcp(df: pd.DataFrame, r: VCPResult, path: str, title: str = "", ma=(50, 150, 200)) -> str:
    df = df.reset_index(drop=True)
    start = max(0, (r.base_start_idx or len(df) - 120) - 60)
    d = df.iloc[start:].reset_index(drop=True)
    x = np.arange(len(d))
    off = start

    fig, (ax, axv) = plt.subplots(2, 1, figsize=(13, 7.5), sharex=True,
                                  gridspec_kw={"height_ratios": [3.2, 1]}, dpi=110)
    fig.patch.set_facecolor("white")

    # K线
    up = d["close"] >= d["open"]
    colors = np.where(up, UP, DOWN)
    ax.vlines(x, d["low"], d["high"], color=colors, linewidth=0.8)
    body_lo = np.minimum(d["open"], d["close"])
    body_h = np.maximum((d["close"] - d["open"]).abs(), (d["high"] - d["low"]) * 0.002)
    ax.bar(x, body_h, bottom=body_lo, color=colors, width=0.7, linewidth=0)

    # 均线
    for m, col in zip(ma, ["#1e88e5", "#fb8c00", "#8e24aa"]):
        s = df["close"].rolling(m).mean().iloc[start:].to_numpy()
        if np.isfinite(s).any():
            ax.plot(x, s, color=col, linewidth=1.1, label=f"MA{m}")

    # 底部区间
    if r.base_start_idx is not None:
        ax.axvspan(r.base_start_idx - off, len(d) - 1, color="#fff8e1", alpha=0.6, zorder=0)

    # 每段收缩
    for i, c in enumerate(r.contractions):
        col = SEG_COLORS[i % len(SEG_COLORS)]
        x0, x1 = c.peak_idx - off, c.trough_idx - off
        ax.plot([x0, x1], [c.peak, c.trough], color=col, linewidth=2.2)
        ax.scatter([x0], [c.peak], color=col, marker="v", s=50, zorder=5)
        ax.scatter([x1], [c.trough], color=col, marker="^", s=50, zorder=5)
        ax.annotate(f"T{i+1} -{c.depth*100:.1f}%", xy=(x1, c.trough),
                    xytext=(4, -14), textcoords="offset points", color=col, fontsize=10, fontweight="bold")
        ax.hlines([c.peak, c.trough], x0, max(x1, x0 + 1), colors=col, linestyles=":", linewidth=1)

    # pivot
    if r.pivot:
        x0 = (r.pivot_idx or 0) - off
        ax.hlines(r.pivot, x0, len(d) - 1 + 3, colors="#d32f2f", linestyles="--", linewidth=1.6)
        ax.text(len(d) + 3, r.pivot, f" Pivot {r.pivot:.6g}", color="#d32f2f", va="bottom", fontsize=10,
                fontweight="bold")

    st = {"BREAKOUT": "BREAKOUT", "SETUP": "SETUP (near pivot)"}.get(r.status, r.status)
    head = title or r.symbol
    ax.set_title(f"{head}   VCP: {st}   contractions: {r.depths_text()}   score {r.score}",
                 fontsize=11, loc="left")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.25)
    ax.set_xlim(-1, len(d) + 12)

    # 成交量
    axv.bar(x, d["volume"], color=colors, width=0.7, alpha=0.8)
    vma = df["volume"].rolling(50, min_periods=10).mean().iloc[start:].to_numpy()
    axv.plot(x, vma, color="#455a64", linewidth=1, label="Vol MA50")
    axv.legend(loc="upper left", fontsize=8)
    axv.grid(alpha=0.25)
    axv.set_ylabel("Volume (USDT)")

    ticks = np.linspace(0, len(d) - 1, 8).astype(int)
    axv.set_xticks(ticks)
    axv.set_xticklabels([d["time"].iloc[t].strftime("%Y-%m-%d") if "time" in d else str(t) for t in ticks],
                        fontsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path
