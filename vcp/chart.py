"""
信号图（参照师父的图表样式）：深色背景 K线 + MA50/150/200 + 每个 Base 方框 + 每段收缩虚线框（B2C1 16.8%）
+ Entry / Stop / BE / Plan TP / Chart TP 价位线 + 左上角信息面板 + 成交额副图。
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

BG, PANEL, GRID, TEXT = "#0e1117", "#131722", "#232a36", "#c9d1d9"
UP, DOWN = "#26a69a", "#ef5350"
MA_COLORS = {50: "#f5a623", 150: "#2f80ed", 200: "#e0457b"}
BASE_COLORS = ["#4fc3f7", "#66bb6a", "#ffb74d", "#ba68c8", "#f06292"]
C_COLOR = "#e6c229"


def fp(p: float) -> str:
    """价格格式：保留 5 位有效数字。"""
    if p is None or not np.isfinite(p) or p <= 0:
        return "-"
    dec = max(2, 4 - int(np.floor(np.log10(p))))
    return f"{p:.{dec}f}"


def display_symbol(sym: str) -> str:
    """OKX 的 CTR-USDT-SWAP / Gate 的 CTR_USDT 统一显示成 CTRUSDT.P"""
    return sym.upper().replace("-SWAP", "").replace("-", "").replace("_", "") + ".P"


def fmoney(x: float) -> str:
    x = float(x or 0)
    if x >= 1e9:
        return f"${x / 1e9:.2f}B"
    if x >= 1e6:
        return f"${x / 1e6:.2f}M"
    if x >= 1e3:
        return f"${x / 1e3:.1f}K"
    return f"${x:.0f}"


def plot_signal(df: pd.DataFrame, sig, path: str, tf: str = "4h", title_extra: str = "",
                entry_idx: int | None = None, exit_idx: int | None = None, outcome: str = "") -> str:
    df = df.reset_index(drop=True)
    n = len(df)
    first_base = sig.bases[0].start if sig.bases else max(0, n - 200)
    start = max(0, min(first_base - 30, n - 120))
    d = df.iloc[start:].reset_index(drop=True)
    off = start
    x = np.arange(len(d))
    right = len(d) + 14

    fig, (ax, axv) = plt.subplots(2, 1, figsize=(16, 8.6), sharex=True,
                                  gridspec_kw={"height_ratios": [3.4, 1]}, dpi=100)
    fig.patch.set_facecolor(BG)
    for a in (ax, axv):
        a.set_facecolor(PANEL)
        a.tick_params(colors=TEXT, labelsize=8)
        for s in a.spines.values():
            s.set_color(GRID)
        a.grid(color=GRID, linewidth=0.6)
        a.yaxis.tick_right()

    # K线
    up = (d["close"] >= d["open"]).to_numpy()
    col = np.where(up, UP, DOWN)
    ax.vlines(x, d["low"], d["high"], color=col, linewidth=0.7)
    lo = np.minimum(d["open"], d["close"])
    hgt = np.maximum((d["close"] - d["open"]).abs(), (d["high"] - d["low"]) * 0.003)
    ax.bar(x, hgt, bottom=lo, color=col, width=0.65, linewidth=0)

    for k, c in MA_COLORS.items():
        m = df["close"].rolling(k).mean().iloc[start:].to_numpy()
        if np.isfinite(m).any():
            ax.plot(x, m, color=c, linewidth=1.1, label=f"MA{k}")

    # Base 方框 + 收缩虚线框
    for b in sig.bases:
        bc = BASE_COLORS[(b.no - 1) % len(BASE_COLORS)]
        x0, x1 = b.start - off, min(b.end, n) - off - 1
        if x1 < 0:
            continue
        ax.add_patch(Rectangle((x0, b.low), max(x1 - x0, 1), b.high - b.low, fill=True, facecolor=bc,
                               alpha=0.07, edgecolor=bc, linewidth=1.6))
        ax.add_patch(Rectangle((x0, b.low), max(x1 - x0, 1), b.high - b.low, fill=False, edgecolor=bc,
                               linewidth=1.6))
        if b.current:
            ax.hlines([b.high, b.low], 0, right, colors=bc, linewidth=0.8, alpha=0.6)
        ax.text(x0, b.high, f" B{b.no} {b.status}", color=bc, fontsize=8, va="bottom", fontweight="bold")
        for i, c in enumerate(b.cons):
            cx0, cx1 = c.peak_idx - off, c.trough_idx - off
            ax.add_patch(Rectangle((cx0, c.trough), max(cx1 - cx0, 1), c.peak - c.trough, fill=True,
                                   facecolor=C_COLOR, alpha=0.12, edgecolor=C_COLOR, linestyle="--", linewidth=1.1))
            ax.plot([cx0, cx1], [c.peak, c.trough], color=C_COLOR, linewidth=0.8, alpha=0.7)
            ax.text((cx0 + cx1) / 2, c.trough, f"B{b.no}C{i + 1} {c.depth * 100:.1f}%", fontsize=7, color="#111",
                    ha="center", va="top", bbox=dict(boxstyle="square,pad=0.15", fc=C_COLOR, ec="none"))

    # 价位线
    xs = (entry_idx - off) if entry_idx is not None else max(0, len(d) - 40)
    levels = [("Plan TP", sig.plan_tp, "#42a5f5", (0, (2, 3))), ("Chart TP", sig.chart_tp, "#66bb6a", "-"),
              ("BE", sig.be, "#9e9e9e", (0, (1, 3))), ("ENTRY", sig.entry, "#26c6da", (0, (3, 3))),
              ("STOP", sig.stop, "#ef5350", "-")]
    for name, price, c, ls in levels:
        if not price:
            continue
        ax.hlines(price, xs, right, colors=c, linewidth=1.0, linestyles=ls)
        ax.text(right, price, f" {name} {fp(price)} ", color="#0e1117", fontsize=8, va="center", ha="left",
                fontweight="bold", bbox=dict(boxstyle="square,pad=0.2", fc=c, ec="none"), clip_on=False)
    if entry_idx is not None and sig.entry:
        ax.scatter([entry_idx - off], [sig.entry], marker="^", s=120, color="#26c6da", zorder=6,
                   edgecolors="white", linewidths=0.6)
    if exit_idx is not None:
        ax.axvline(exit_idx - off, color="#9e9e9e", linestyle=":", linewidth=1)
        ax.text(exit_idx - off, ax.get_ylim()[0], f" EXIT {outcome}", color=TEXT, fontsize=8, va="bottom")

    # 信息面板
    b = sig.base
    hist = " -> ".join(x.label() for x in sig.bases) or "-"
    cdet = " | ".join(" ".join(f"B{x.no}C{i + 1}:{c.depth * 100:.1f}%" for i, c in enumerate(x.cons))
                      for x in sig.bases if x.cons)
    info = [
        f"{display_symbol(sig.symbol)} | {sig.stage.get('label', '')} | {tf} | LONG",
        f"Base {b.no if b else '-'} {b.status if b else ''} | {cdet}",
        f"Bases: {hist}",
        f"SCV: {sig.total:+.1f} {sig.rating}  (S{sig.s[0]:+.1f} C{sig.c[0]:+.1f} V{sig.v[0]:+.1f})",
        f"Last {tf} Vol: {fmoney(sig.vol.get('last'))}  ({sig.vol.get('ratio', 0):.2f}x MA50)",
        f"Est 24h Vol: {fmoney(sig.vol.get('est24h'))}",
    ]
    if sig.entry:
        rr = f" R:R {sig.plan_rr:.2f}" if sig.plan_rr else ""
        info.append(f"Ent:{fp(sig.entry)} SL:{fp(sig.stop)} TP:{fp(sig.plan_tp)}{rr}")
    ax.text(0.006, 0.985, "\n".join(info), transform=ax.transAxes, va="top", ha="left", fontsize=8.2,
            family="monospace", color="#8bd17c",
            bbox=dict(boxstyle="square,pad=0.4", fc="#0b0f14", ec="#3a7d44", alpha=0.92))

    ttl = f"{display_symbol(sig.symbol)}  {tf}  LONG" + (f"   {title_extra}" if title_extra else "")
    ax.set_title(ttl, color=TEXT, fontsize=12, fontweight="bold")
    leg = ax.legend(loc="lower left", fontsize=8, facecolor=PANEL, edgecolor=GRID)
    for t in leg.get_texts():
        t.set_color(TEXT)
    ax.set_xlim(-1, right + 12)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fp(v)))

    # 成交额
    axv.bar(x, d["volume"], color=col, width=0.65, alpha=0.85)
    vma = df["volume"].rolling(50, min_periods=10).mean().iloc[start:].to_numpy()
    axv.plot(x, vma, color="#f5a623", linewidth=1)
    axv.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmoney(v)))
    axv.set_ylabel("Vol ($)", color=TEXT, fontsize=8)
    axv.yaxis.set_label_position("right")
    ticks = np.linspace(0, len(d) - 1, 9).astype(int)
    axv.set_xticks(ticks)
    axv.set_xticklabels([d["time"].iloc[t].strftime("%m/%d %H:%M") for t in ticks], color=TEXT, fontsize=8)

    fig.tight_layout()
    fig.savefig(path, facecolor=BG)
    plt.close(fig)
    return path
