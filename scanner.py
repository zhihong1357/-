"""
VCP 自动扫描器 —— USDT 永续合约（日线判断阶段 + 4 小时找收缩和进场）

用法（在本文件夹打开命令行）：
  python scanner.py                    扫描一次
  python scanner.py --loop             一直运行，每隔 N 分钟扫描一次（N 在 config.yaml 设置）
  python scanner.py --symbol CTRUSDT   单独分析一个币，输出完整信号报告并画图
  python scanner.py --demo             不联网，用模拟数据演示信号报告和图
  python scanner.py --test-alert       测试 Telegram/Discord 提醒是否设置成功
  python scanner.py --find-chat-id     查询你的 Telegram chat_id
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

from vcp.chart import plot_signal
from vcp.data import ExchangeError, drop_unclosed, get_exchange
from vcp.engine import DEFAULTS, PROFILES, analyze, apply_rs, replay, rs_percentiles
from vcp.notify import Notifier, find_chat_id
from vcp.report import case_text, display_symbol, signal_text

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
STATE = ROOT / "state.json"
VALIDATION_DEFAULTS = {"enabled": True, "lookback_bars": 300, "show_cases": 3}

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
    # 策略参数 + 账户参数合并成一个字典给引擎
    cfg["strategy"] = {**DEFAULTS, **(cfg.get("strategy") or {}), **(cfg.get("account") or {})}
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


def fetch(ex, symbol: str, sc: dict):
    df = drop_unclosed(ex.klines(symbol, sc["exec_tf"], sc["exec_bars"]), sc["exec_tf"])
    d1 = drop_unclosed(ex.klines(symbol, sc["stage_tf"], sc["stage_bars"]), sc["stage_tf"])
    return df, d1


def analyse(ex, symbol: str, cfg: dict):
    sc = cfg["strategy"]
    df, d1 = fetch(ex, symbol, sc)
    sig = analyze(symbol, df, d1, sc)
    trades = []
    if cfg["validation"].get("enabled", True):
        trades = replay(symbol, df, d1, cfg["validation"]["lookback_bars"], sc)
    return df, sig, trades


def scan_once(cfg: dict, notifier: Notifier) -> pd.DataFrame:
    t0 = time.time()
    sc = cfg["strategy"]
    ex, vols = get_exchange(cfg.get("exchange", "auto"))
    excl = {s.upper() for s in cfg.get("exclude") or []}
    syms = [s for s, v in sorted(vols.items(), key=lambda kv: -kv[1])
            if v >= cfg.get("min_quote_volume_24h", 0) and s.upper() not in excl
            and s.replace("-", "").replace("_", "").upper().replace("SWAP", "") not in excl]
    if cfg.get("max_symbols"):
        syms = syms[: cfg["max_symbols"]]
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] 交易所: {ex.name}  阶段 {sc['stage_tf']} / 执行 {sc['exec_tf']}  "
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
                df, sig, tr = fut.result()
                results.append(sig)
                if sig.valid or tr:
                    frames[s] = df
                trades += tr
            except Exception as e:
                print(f"  {s}: 获取失败 {e}")
            if i % 50 == 0:
                print(f"  已完成 {i}/{len(syms)}")
    if not results:
        print("没有拿到任何数据")
        return pd.DataFrame()

    # RS 相对强度：在本次扫描的所有币里排名（Minervini 趋势模板第 8 条）
    pct = rs_percentiles({r.symbol: r.rs_raw for r in results})
    for r in results:
        if r.symbol in pct:
            apply_rs(r, pct[r.symbol], sc)

    table = pd.DataFrame([r.to_row() for r in results]).sort_values(["valid", "score"], ascending=False)
    table.to_csv(run_dir / "all_results.csv", index=False, encoding="utf-8-sig")

    hits = [r for r in results if r.status in ("BREAKOUT", "SETUP")]
    hits.sort(key=lambda r: (r.status != "BREAKOUT", -r.score, -r.total))
    watch = sorted([r for r in results if r.valid and r.status == "NONE"], key=lambda r: (-r.score, -r.total))

    state = load_state()
    alert_on = set(cfg.get("alert_on") or ["BREAKOUT", "SETUP"])
    new_alerts = 0
    print("\n" + "=" * 70)
    if not hits:
        print("本轮没有接近入场 / 突破的 VCP。")
    for r in hits:
        print(f"[{r.status:8}] {display_symbol(r.symbol):16} {r.rating:9} {r.score}/3  "
              f"B{r.base.no} {r.stage['label']}  Entry {r.entry:.6g}  Stop {r.stop:.6g}")
        img = None
        if cfg.get("charts", "all") != "none":
            img = str(run_dir / f"{display_symbol(r.symbol)}_{r.status}.png")
            plot_signal(frames[r.symbol], r, img, tf=sc["exec_tf"], title_extra=r.status)
        key = f"{ex.name}|{r.symbol}|B{r.base.no}|{r.status}|{r.entry:.6g}"
        if r.status in alert_on and key not in state:
            if notifier.enabled:
                notifier.send(signal_text(r, sc, ex.name), img)
            state[key] = time.time()
            new_alerts += 1
    if watch:
        print("\n观察名单（Base 已确认、离入场还远）: " + ", ".join(display_symbol(r.symbol) for r in watch[:20]))
    print("=" * 70)
    save_state(state)

    # 汇总最多每 summary_every_hours 小时发一次（本轮有新提醒时照常发），避免频繁运行时刷屏
    gap = float(cfg.get("summary_every_hours", 4)) * 3600 - 600
    due = time.time() - state.get("_last_summary", 0) >= gap or new_alerts > 0
    if notifier.enabled and cfg.get("send_summary", True) and due:
        state["_last_summary"] = time.time()
        save_state(state)
        s2 = sum(bool(r.stage.get("s2")) for r in results)
        conf = sum(bool(r.base and r.base.ok) for r in results)
        lines = [f"📊 VCP 扫描完成 ({ex.name} · 阶段 {sc['stage_tf']} / 执行 {sc['exec_tf']})",
                 f"交易所共 {len(vols)} 个 USDT 永续，成交额达标并扫描 {len(results)} 个",
                 f"日线 Stage 2: {s2} 个",
                 f"当前 Base 已确认(CONFIRMED): {conf} 个",
                 f"突破 BREAKOUT: {sum(r.status == 'BREAKOUT' for r in hits)} 个",
                 f"接近入场 SETUP: {sum(r.status == 'SETUP' for r in hits)} 个"]
        if hits:
            lines.append("信号: " + ", ".join(f"{display_symbol(r.symbol)}({r.score}/3)" for r in hits[:15]))
        if watch:
            lines.append("观察名单: " + ", ".join(display_symbol(r.symbol) for r in watch[:10]))
        if cfg["validation"].get("enabled", True):
            lines += ["", *validation_stats(trades, cfg)]
        notifier.send("\n".join(lines))

    if cfg["validation"].get("enabled", True):
        send_cases(trades, frames, cfg, ex.name, run_dir, notifier, state)
        save_state(state)
        pd.DataFrame([{k: v for k, v in t.items() if k != "sig"} for t in trades]) \
            .to_csv(run_dir / "validation_trades.csv", index=False, encoding="utf-8-sig")

    pd.DataFrame([r.to_row() for r in hits]).to_csv(run_dir / "signals.csv", index=False, encoding="utf-8-sig")
    table.to_csv(OUT / "latest_all_results.csv", index=False, encoding="utf-8-sig")
    print(f"结果已保存到: {run_dir}   用时 {time.time() - t0:.0f} 秒\n")
    return table


ICON = {"计划止盈达成": "✅", "部分止盈后离场": "✅", "跟踪止盈离场": "✅", "保本离场": "➖", "持仓中": "⏳",
        "止损": "❌", "到期离场": "⌛"}
EN = {"计划止盈达成": "TP HIT", "部分止盈后离场": "PARTIAL+EXIT", "跟踪止盈离场": "TRAIL EXIT", "保本离场": "BE EXIT",
      "持仓中": "OPEN", "止损": "STOPPED", "到期离场": "TIME EXIT"}


def _bucket_line(label: str, ts: list) -> str:
    closed = [t for t in ts if not t["open"]]
    if not closed:
        return f"{label}: {len(ts)} 笔（都还在持仓中）"
    win = sum(t["r"] > 0 for t in closed)
    avg = sum(t["r"] for t in closed) / len(closed)
    sl = sum(t["outcome"] == "止损" for t in closed)
    return (f"{label}: 已结束 {len(closed)} 笔 | 盈利 {win} | 止损 {sl} | 胜率 {win / len(closed) * 100:.0f}% | "
            f"平均 {avg:+.2f}R | 合计 {sum(t['r'] for t in closed):+.1f}R")


def validation_stats(trades: list, cfg: dict) -> list[str]:
    sc, look = cfg["strategy"], cfg["validation"]["lookback_bars"]
    span = f"最近 {look} 根 {sc['exec_tf']}"
    if not trades:
        return [f"🧪 历史回放({span}): 这段时间没有触发过入场"]
    taken = [t for t in trades if t["taken"]]
    skipped = [t for t in trades if not t["taken"]]
    out = [f"🧪 历史回放({span}，同一套规则，按 {PROFILES[sc['exit_profile']]['name']} 出场):",
           f"所有 VCP 突破 {len(trades)} 次；评级 ≥ {sc['min_rating']}（会提醒/会做）{len(taken)} 次，"
           f"低分被过滤 {len(skipped)} 次",
           _bucket_line("✅ 高分（照做）", taken),
           _bucket_line("🚫 低分（不做）", skipped),
           "按分数分组（检验扣分制是否有效：分数越高结果应越好）:"]
    for sc_ in (3, 2, 1, 0, -1, -2, -3):
        g = [t for t in trades if t["score"] == sc_]
        if g:
            out.append("  " + _bucket_line(f"Score {sc_}", g))
    out.append("出场方案对比（只算高分入场）:")
    for name, prof in PROFILES.items():
        res = [t["by_profile"][name] for t in taken if not t["by_profile"][name]["open"]]
        mark = "👉" if name == sc["exit_profile"] else "  "
        if not res:
            out.append(f"{mark}{prof['name']}: 暂无已结束交易")
            continue
        avg = sum(x["r"] for x in res) / len(res)
        win = sum(x["r"] > 0 for x in res)
        out.append(f"{mark}{prof['name']}: {len(res)} 笔，盈利 {win} 笔，平均 {avg:+.2f}R，合计 {sum(x['r'] for x in res):+.1f}R")
    for t in sorted(trades, key=lambda t: t["time"], reverse=True)[:10]:
        out.append(f"{ICON.get(t['outcome'], '•')} {display_symbol(t['symbol'])} "
                   f"{pd.Timestamp(t['time']):%m-%d %H:%M} {t['score']}/3 {t['outcome']} {t['r']:+.2f}R"
                   f"{'' if t['taken'] else ' (低分不做)'}")
    out.append("说明：统计不含手续费/资金费/滑点；RS 排名只在实时扫描计算，回放里不含 RS")
    return out


def send_cases(trades, frames, cfg, ex_name, run_dir, notifier, state):
    """先发最近的成功案例，不足 show_cases 个时用最近的其他历史入场（持仓中/保本/止损）补足。"""
    k = cfg["validation"]["show_cases"]
    sc = cfg["strategy"]
    # 刚在最新一根K线触发的入场，已经作为实时信号发过，不再当历史案例重复发
    # 只发高分（会做的）案例；刚在最新一根K线触发的已作为实时信号发过，不再重复
    recent = sorted([t for t in trades if t["taken"] and not (t["open"] and t["exit_idx"] - t["idx"] < 1)],
                    key=lambda t: t["time"], reverse=True)
    wins = [t for t in recent if t["success"]]
    cases = wins[:k] + [t for t in recent if not t["success"]][: max(0, k - len(wins))]
    for t in cases:
        df = frames[t["symbol"]]
        d0 = pd.Timestamp(t["time"]).strftime("%Y%m%d_%H%M")
        print(f"  历史案例 {display_symbol(t['symbol'])} {d0} {t['outcome']} {t['r']:+.2f}R")
        key = f"case2|{ex_name}|{t['symbol']}|{d0}|{t['outcome']}"
        if key in state or not notifier.enabled:
            continue
        img = None
        if cfg.get("charts", "all") != "none":
            img = str(run_dir / f"CASE_{display_symbol(t['symbol'])}_{d0}.png")
            end = min(len(df), t["exit_idx"] + 20)
            plot_signal(df.iloc[:end], t["sig"], img, tf=sc["exec_tf"],
                        title_extra=f"HISTORY {EN.get(t['outcome'], '')} {t['r']:+.2f}R",
                        entry_idx=t["idx"], exit_idx=None if t["open"] else t["exit_idx"],
                        outcome=EN.get(t["outcome"], ""))
        notifier.send(case_text(t, df, sc, ex_name), img)
        state[key] = time.time()


def single(cfg: dict, symbol: str):
    ex, vols = get_exchange(cfg.get("exchange", "auto"))
    symbol = symbol.replace(".P", "")
    if symbol not in vols:
        # 允许输入 BTCUSDT 自动转换成 OKX / Gate 的写法
        alt = {"okx": symbol.replace("USDT", "-USDT-SWAP"), "gate": symbol.replace("USDT", "_USDT")}.get(ex.name)
        if alt in vols:
            symbol = alt
    df, sig, trades = analyse(ex, symbol, cfg)
    sc = cfg["strategy"]
    print(signal_text(sig, sc, ex.name))
    print("（单币模式没有其他币可比，RS 相对强度不计算；完整扫描时才会排名）")
    if sig.reasons:
        print("\n未触发信号的原因: " + "; ".join(sig.reasons))
    print("\n" + "\n".join(validation_stats(trades, cfg)))
    OUT.mkdir(exist_ok=True)
    img = str(OUT / f"{display_symbol(symbol)}_analysis.png")
    plot_signal(df, sig, img, tf=sc["exec_tf"], title_extra=sig.status)
    print(f"\n图表: {img}")


def demo(cfg: dict):
    from tests.synthetic import make_two_base_4h
    sc = cfg["strategy"]
    df, d1 = make_two_base_4h(breakout=True)
    sig = analyze("DEMOUSDT", df, d1, sc)
    print(signal_text(sig, sc, "demo"))
    OUT.mkdir(exist_ok=True)
    img = str(OUT / "DEMO_signal.png")
    plot_signal(df, sig, img, tf=sc["exec_tf"], title_extra=f"{sig.status} (synthetic)")
    print(f"\n图表已保存: {img}")


def main():
    ap = argparse.ArgumentParser(description="Minervini VCP 扫描器（USDT 永续）")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--loop", action="store_true", help="持续运行")
    ap.add_argument("--symbol", help="只分析一个币，如 CTRUSDT")
    ap.add_argument("--exchange", help="覆盖 config 里的交易所")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--find-chat-id", action="store_true")
    a = ap.parse_args()

    cfg = load_config(a.config)
    if a.exchange:
        cfg["exchange"] = a.exchange
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
