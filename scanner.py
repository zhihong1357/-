"""
VCP 自动扫描器 —— USDT 永续合约

用法（在本文件夹打开命令行）：
  python scanner.py                 扫描一次
  python scanner.py --loop          一直运行，每隔 N 分钟扫描一次（N 在 config.yaml 设置）
  python scanner.py --symbol SOLUSDT   单独分析一个币，列出每条规则是否通过并画图
  python scanner.py --demo          不联网，用模拟数据演示一张 VCP 图
  python scanner.py --test-alert    测试 Telegram/Discord 提醒是否设置成功
  python scanner.py --find-chat-id  查询你的 Telegram chat_id
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from vcp.chart import plot_vcp
from vcp.data import ExchangeError, drop_unclosed, get_exchange
from vcp.detector import DEFAULTS, detect_vcp
from vcp.notify import Notifier, find_chat_id
from vcp.trade import TRADE_DEFAULTS, VALIDATION_DEFAULTS, make_plan, validate_symbol

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
STATE = ROOT / "state.json"

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台中文
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # 环境变量优先（GitHub Actions 用 Secrets 传入）
    tg = cfg.setdefault("telegram", {}) or {}
    cfg["telegram"] = tg
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        tg["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN")
    if os.getenv("TELEGRAM_CHAT_ID"):
        # Secrets 里的 chat_id 和 config.yaml 里的 chat_id 合并，都会收到通知
        ids = [str(tg.get("chat_id") or ""), os.getenv("TELEGRAM_CHAT_ID")]
        merged = []
        for x in ",".join(ids).replace("，", ",").split(","):
            if x.strip() and x.strip() not in merged:
                merged.append(x.strip())
        tg["chat_id"] = ",".join(merged)
    if os.getenv("DISCORD_WEBHOOK_URL"):
        cfg.setdefault("discord", {})["webhook_url"] = os.getenv("DISCORD_WEBHOOK_URL")
    if os.getenv("VCP_EXCHANGE"):
        cfg["exchange"] = os.getenv("VCP_EXCHANGE")
    cfg["vcp"] = {**DEFAULTS, **(cfg.get("vcp") or {})}
    cfg["trade"] = {**TRADE_DEFAULTS, **(cfg.get("trade") or {})}
    cfg["validation"] = {**VALIDATION_DEFAULTS, **(cfg.get("validation") or {})}
    return cfg


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(s: dict) -> None:
    # 只保留 30 天内的记录
    cutoff = time.time() - 30 * 86400
    s = {k: v for k, v in s.items() if v > cutoff}
    STATE.write_text(json.dumps(s, indent=1), encoding="utf-8")


def fmt_price(p: float) -> str:
    return f"{p:.6g}"


def trend_ok(r) -> bool:
    tt = [v for k, v in r.checks.items() if k.startswith("趋势:")]
    return bool(tt) and all(tt)


def vs_pivot(r) -> float:
    """现价相对 pivot 的百分比：负数=还在 pivot 下方，正数=已突破"""
    return (r.last_close / r.pivot - 1) * 100


def alert_text(r, ex_name: str, interval: str, plan=None, cfg=None) -> str:
    head = {"BREAKOUT": "🚀 VCP 带量突破", "SETUP": "👀 VCP 形态成立 · 接近突破点"}[r.status]
    lines = [
        f"{head}",
        f"{r.symbol}  ({ex_name} 永续 · {interval})",
        f"收盘价: {fmt_price(r.last_close)}",
        f"Pivot 突破价: {fmt_price(r.pivot)}  (现价相对 pivot {vs_pivot(r):+.2f}%)",
        f"收缩: {r.depths_text()}  (共 {len(r.contractions)} 次)",
        f"底部长度: {r.base_bars} 根K线",
    ]
    if r.breakout_volume_ratio:
        lines.append(f"突破量: {r.breakout_volume_ratio}x 均量")
    lines.append(f"评分: {r.score}")
    if plan is not None:
        lines += ["", *plan_lines(plan, cfg, r.status)]
    lines.append("⚠️ 规则化计算结果，非投资建议")
    return "\n".join(lines)


def plan_lines(p, cfg: dict, status: str) -> list[str]:
    t = cfg["trade"]
    entry_how = ("突破K线已收盘确认，按收盘价进场" if status == "BREAKOUT" else
                 f"在 pivot 上方 {t['entry_buffer']*100:.1f}% 挂突破单(buy stop)；"
                 f"日线收盘不能站上 pivot 或量 < {cfg['vcp']['breakout_volume_ratio']}x 均量则撤单/离场")
    out = [
        "📐 交易计划（做多）",
        f"进场: {fmt_price(p.entry)}",
        f"止损: {fmt_price(p.stop)}  (-{p.risk_pct*100:.2f}%，末段收缩低点下方)",
        f"止盈1: {fmt_price(p.tp1)}  (+{t['tp1_r']:g}R，+{p.risk_pct*t['tp1_r']*100:.2f}%) 平 {t['tp1_close_pct']*100:.0f}%",
        f"止盈2: {fmt_price(p.tp2)}  (+{t['tp2_r']:g}R，+{p.risk_pct*t['tp2_r']*100:.2f}%) 全平",
        f"盈亏比: 1 : {p.rr:g}",
        f"风险仓位(账户 {t['account_usdt']:g}U，每笔风险 {t['risk_per_trade_pct']*100:g}%):",
        f"  名义仓位 {p.notional:,.0f}U ≈ {p.qty:.6g} 个，{p.leverage:.0f}x 杠杆，保证金 {p.margin:,.0f}U",
        f"  打到止损亏 {p.risk_usdt:,.1f}U",
        f"进场逻辑: {entry_how}",
        f"出场逻辑: 跌破止损全部离场；到止盈1平 {t['tp1_close_pct']*100:.0f}% 并把止损移到进场价(保本)；"
        f"到止盈2全平；持仓满 {t['max_hold_bars']} 根K线仍未结束按收盘离场",
    ]
    if p.warn:
        out.append(f"⚠️ {p.warn}")
    return out


def analyse(ex, symbol: str, cfg: dict):
    df = ex.klines(symbol, cfg["interval"], cfg["kline_limit"])
    df = drop_unclosed(df, cfg["interval"])
    r = detect_vcp(df, symbol, cfg["vcp"])
    trades = []
    if cfg["validation"].get("enabled", True):
        trades = validate_symbol(df, symbol, cfg["vcp"], cfg["trade"], cfg["validation"])
    return df, r, trades


def scan_once(cfg: dict, notifier: Notifier) -> pd.DataFrame:
    t0 = time.time()
    ex, vols = get_exchange(cfg.get("exchange", "auto"))
    excl = {s.upper() for s in cfg.get("exclude") or []}
    syms = [s for s, v in sorted(vols.items(), key=lambda kv: -kv[1])
            if v >= cfg.get("min_quote_volume_24h", 0) and s.upper() not in excl
            and s.replace("-", "").replace("_", "").upper().replace("SWAP", "") not in excl]
    if cfg.get("max_symbols"):
        syms = syms[: cfg["max_symbols"]]
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] 交易所: {ex.name}  周期: {cfg['interval']}  "
          f"扫描 {len(syms)} 个 USDT 永续合约 ...")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    run_dir = OUT / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    results, frames, trades = [], {}, []
    workers = 3 if ex.name == "okx" else 6
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(analyse, ex, s, cfg): s for s in syms}
        for i, fut in enumerate(as_completed(futs), 1):
            s = futs[fut]
            try:
                df, r, tr = fut.result()
                results.append(r)
                if r.valid or tr:
                    frames[s] = df
                trades += tr
            except Exception as e:
                print(f"  {s}: 获取失败 {e}")
            if i % 50 == 0:
                print(f"  已完成 {i}/{len(syms)}")

    rows = [r.to_row() for r in results]
    table = pd.DataFrame(rows)
    if table.empty:
        print("没有拿到任何数据")
        return table
    table = table.sort_values(["valid", "score"], ascending=False)
    table.to_csv(run_dir / "all_results.csv", index=False, encoding="utf-8-sig")

    hits = [r for r in results if r.status in ("BREAKOUT", "SETUP")]
    hits.sort(key=lambda r: (r.status != "BREAKOUT", -r.score))
    forming = [r for r in results if r.valid and r.status == "NONE"]

    state = load_state()
    alert_on = set(cfg.get("alert_on") or ["BREAKOUT", "SETUP"])
    print()
    print("=" * 70)
    if not hits:
        print("本轮没有符合 VCP（接近突破/突破）的合约。")
    for r in hits:
        print(f"[{r.status:8}] {r.symbol:16} 收盘 {fmt_price(r.last_close):>10}  pivot {fmt_price(r.pivot):>10}"
              f"  相对pivot {vs_pivot(r):+6.2f}%  收缩 {r.depths_text()}  评分 {r.score}")
        plan = make_plan(r, cfg["trade"])
        img = None
        if cfg.get("charts", "all") != "none":
            img = str(run_dir / f"{r.symbol}_{r.status}.png")
            plot_vcp(frames[r.symbol], r, img, title=f"{r.symbol} ({ex.name} perp, {cfg['interval']})", plan=plan)
        key = f"{ex.name}|{r.symbol}|{cfg['interval']}|{r.status}|{fmt_price(r.pivot)}"
        if r.status in alert_on and key not in state:
            if notifier.enabled:
                notifier.send(alert_text(r, ex.name, cfg["interval"], plan, cfg), img)
            state[key] = time.time()
    if forming:
        print(f"\n另有 {len(forming)} 个币形态成立但离 pivot 还远（观察名单）: "
              + ", ".join(r.symbol for r in sorted(forming, key=lambda r: -r.score)[:20]))
    print("=" * 70)
    save_state(state)

    # 每轮扫描结束发一条简短汇总，让你知道机器人在正常工作
    if notifier.enabled and cfg.get("send_summary", True):
        watch = ", ".join(r.symbol for r in sorted(forming, key=lambda r: -r.score)[:10])
        lines = [f"📊 VCP 扫描完成 ({ex.name} · {cfg['interval']})",
                 f"交易所共 {len(vols)} 个 USDT 永续，成交额达标并扫描 {len(results)} 个",
                 f"通过趋势模板: {sum(trend_ok(r) for r in results)} 个",
                 f"VCP 形态完整成立: {sum(r.valid for r in results)} 个",
                 f"突破 BREAKOUT: {sum(r.status == 'BREAKOUT' for r in hits)} 个",
                 f"接近突破 SETUP: {sum(r.status == 'SETUP' for r in hits)} 个"]
        if hits:
            lines.append("信号: " + ", ".join(r.symbol for r in hits[:15]))
        else:
            lines.append("本轮没有符合 VCP 的合约")
        if watch:
            lines.append(f"观察名单(形态成立、离 pivot 还远): {watch}")
        if cfg["validation"].get("enabled", True):
            lines += ["", *validation_stats(trades, cfg)]
        notifier.send("\n".join(lines))

    # 最近 N 个经历史验证成功的 VCP 案例（带完整交易计划和结果）
    if cfg["validation"].get("enabled", True):
        send_cases(trades, frames, cfg, ex.name, run_dir, notifier, state)
        save_state(state)
        pd.DataFrame([{k: v for k, v in t.items() if k not in ("plan", "result")} for t in trades]) \
            .to_csv(run_dir / "validation_trades.csv", index=False, encoding="utf-8-sig")

    pd.DataFrame([r.to_row() for r in hits]).to_csv(run_dir / "signals.csv", index=False, encoding="utf-8-sig")
    table.to_csv(OUT / "latest_all_results.csv", index=False, encoding="utf-8-sig")
    print(f"结果已保存到: {run_dir}   用时 {time.time() - t0:.0f} 秒\n")
    return table


ICON = {"止盈2 全部达成": "✅", "止盈1后保本离场": "✅", "持仓中": "⏳", "止损": "❌", "到期按收盘离场": "⌛"}


def _fmt_date(x) -> str:
    return pd.Timestamp(x).strftime("%Y-%m-%d")


def validation_stats(trades: list, cfg: dict) -> list[str]:
    closed = [t for t in trades if not t["open"]]
    n_open = len(trades) - len(closed)
    look = cfg["validation"]["lookback_bars"]
    if not trades:
        return [f"🧪 历史验证(最近 {look} 根K线): 这段时间没有出现过 VCP 突破"]
    wins = [t for t in closed if t["tp1_hit"]]
    out = [f"🧪 历史验证(最近 {look} 根K线，同一套规则回放):",
           f"出现突破 {len(trades)} 次：已结束 {len(closed)} 次，持仓中 {n_open} 次"]
    if closed:
        avg_r = sum(t["r"] for t in closed) / len(closed)
        out.append(f"已结束的交易到达止盈1(2R): {len(wins)}/{len(closed)} = {len(wins)/len(closed)*100:.0f}%")
        out.append(f"已结束交易平均每笔: {avg_r:+.2f}R（不含手续费/滑点）")
    else:
        out.append("还没有已结束的交易，暂时无法计算成功率")
    for t in sorted(trades, key=lambda t: t["time"], reverse=True)[:10]:
        out.append(f"{ICON.get(t['outcome'], '•')} {t['symbol']} {_fmt_date(t['time'])} {t['outcome']} {t['r']:+.2f}R")
    out.append("（每笔的图和完整交易计划会单独发送，同一笔只发一次，结果变化时再发）")
    return out


def send_cases(trades, frames, cfg, ex_name, run_dir, notifier, state):
    """先发最近的成功案例，不足 show_cases 个时用最近的其他历史突破（持仓中/止损）补足。"""
    k = cfg["validation"]["show_cases"]
    recent = sorted(trades, key=lambda t: t["time"], reverse=True)
    wins = [t for t in recent if t["tp1_hit"]]
    cases = wins[:k] + [t for t in recent if not t["tp1_hit"]][: max(0, k - len(wins))]
    if not cases:
        print("历史验证：这段时间没有出现过 VCP 突破")
        return
    print(f"历史 VCP 突破案例 {len(cases)} 个:")
    for t in cases:
        p, r = t["plan"], t["result"]
        df = frames[t["symbol"]]
        d0, d1 = _fmt_date(t["time"]), _fmt_date(df["time"].iloc[t["exit_idx"]])
        bars = t["exit_idx"] - t["idx"]
        print(f"  {t['symbol']} {d0} {t['outcome']} {t['r']:+.2f}R 最大涨幅 {t['max_gain_pct']}%")
        key = f"case|{ex_name}|{t['symbol']}|{d0}|{t['outcome']}"
        if key in state or not notifier.enabled:
            continue
        if t["open"]:
            head = "⏳ 历史 VCP 突破 · 仍在持仓中（按规则未触发离场）"
            res = [f"现价: {fmt_price(df['close'].iloc[-1])}   浮动结果: {t['r']:+.2f}R   已持有 {bars} 根K线",
                   f"期间最大涨幅: {t['max_gain_pct']}%   止盈1 {'已到达，止损已移到进场价' if t['tp1_hit'] else '未到达'}"]
        else:
            head = {True: "✅ 已验证成功的 VCP 案例", False: "❌ 历史 VCP 突破 · 失败案例"}[t["tp1_hit"]]
            res = [f"结果: {t['outcome']}  {t['r']:+.2f}R   离场日 {d1}（持有 {bars} 根K线）",
                   f"期间最大涨幅: {t['max_gain_pct']}%"]
        text = "\n".join([
            head,
            f"{t['symbol']}  ({ex_name} 永续 · {cfg['interval']})",
            f"突破日: {d0}   收缩: {t['depths']}",
            f"Pivot: {fmt_price(t['pivot'])}",
            *res,
            "",
            *plan_lines(p, cfg, "BREAKOUT"),
            "⚠️ 历史案例用于理解规则，不代表未来结果",
        ])
        img = None
        if cfg.get("charts", "all") != "none":
            img = str(run_dir / f"CASE_{t['symbol']}_{d0}.png")
            end = min(len(df), t["exit_idx"] + 15)
            plot_vcp(df.iloc[:end], r, img, title=f"CASE {t['symbol']} {d0} ({ex_name}, {cfg['interval']})",
                     plan=p, entry_idx=t["idx"], exit_idx=None if t["open"] else t["exit_idx"])
        notifier.send(text, img)
        state[key] = time.time()


def single(cfg: dict, symbol: str):
    ex, vols = get_exchange(cfg.get("exchange", "auto"))
    if symbol not in vols:
        # 允许输入 BTCUSDT 自动转换成 OKX / Gate 的写法
        alt = {"okx": symbol.replace("USDT", "-USDT-SWAP"), "gate": symbol.replace("USDT", "_USDT")}.get(ex.name)
        if alt in vols:
            symbol = alt
    df, r, trades = analyse(ex, symbol, cfg)
    print(f"\n{symbol} ({ex.name}, {cfg['interval']})  状态: {r.status}   完全符合VCP: {'是' if r.valid else '否'}")
    print(f"收缩: {r.depths_text() or '-'}   pivot: {fmt_price(r.pivot) if r.pivot else '-'}   底部 {r.base_bars} 根")
    for k, v in r.checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if r.reasons:
        print("不符合原因: " + "; ".join(r.reasons))
    OUT.mkdir(exist_ok=True)
    img = str(OUT / f"{symbol}_analysis.png")
    plot_vcp(df, r, img, title=f"{symbol} ({ex.name} perp, {cfg['interval']})")
    print(f"图表: {img}")


def demo(cfg: dict):
    from tests.synthetic import make_vcp
    df = make_vcp()
    r = detect_vcp(df, "DEMOUSDT", cfg["vcp"])
    OUT.mkdir(exist_ok=True)
    img = str(OUT / "DEMO_vcp.png")
    plot_vcp(df, r, img, title="DEMO (synthetic data)")
    print(f"模拟数据 → 状态 {r.status}，收缩 {r.depths_text()}，pivot {fmt_price(r.pivot)}")
    for k, v in r.checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    print(f"图表已保存: {img}")


def main():
    ap = argparse.ArgumentParser(description="Minervini VCP 扫描器（USDT 永续）")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--loop", action="store_true", help="持续运行")
    ap.add_argument("--symbol", help="只分析一个币，如 BTCUSDT")
    ap.add_argument("--exchange", help="覆盖 config 里的交易所")
    ap.add_argument("--interval", help="覆盖 config 里的周期，如 4h")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--find-chat-id", action="store_true")
    a = ap.parse_args()

    cfg = load_config(a.config)
    if a.exchange:
        cfg["exchange"] = a.exchange
    if a.interval:
        cfg["interval"] = a.interval
    notifier = Notifier(cfg)

    if a.find_chat_id:
        tok = cfg["telegram"].get("bot_token")
        if not tok:
            print("请先在 config.yaml 里填好 telegram.bot_token")
        else:
            find_chat_id(tok)
        return
    if a.test_alert:
        if not notifier.enabled:
            print("还没有在 config.yaml 里填写 Telegram 或 Discord 信息")
            return
        notifier.send("✅ VCP 扫描器提醒测试成功！之后符合形态时会在这里通知你。")
        print("已发送，去 Telegram/Discord 看看有没有收到。")
        return
    if a.demo:
        demo(cfg)
        return
    if a.symbol:
        single(cfg, a.symbol.upper())
        return

    while True:
        try:
            scan_once(cfg, notifier)
        except ExchangeError as e:
            print(f"连接交易所失败：{e}")
            notifier.send(f"⚠️ VCP 扫描失败：连接交易所失败\n{str(e)[:500]}")
        except Exception as e:
            print(f"本轮出错：{e}")
            notifier.send(f"⚠️ VCP 扫描出错：{str(e)[:500]}")
        if not a.loop:
            break
        mins = cfg.get("scan_every_minutes", 60)
        print(f"{mins} 分钟后再次扫描（按 Ctrl+C 停止）...")
        time.sleep(mins * 60)


if __name__ == "__main__":
    main()
