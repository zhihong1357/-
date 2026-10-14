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
        tg["chat_id"] = os.getenv("TELEGRAM_CHAT_ID")
    if os.getenv("DISCORD_WEBHOOK_URL"):
        cfg.setdefault("discord", {})["webhook_url"] = os.getenv("DISCORD_WEBHOOK_URL")
    if os.getenv("VCP_EXCHANGE"):
        cfg["exchange"] = os.getenv("VCP_EXCHANGE")
    cfg["vcp"] = {**DEFAULTS, **(cfg.get("vcp") or {})}
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


def vs_pivot(r) -> float:
    """现价相对 pivot 的百分比：负数=还在 pivot 下方，正数=已突破"""
    return (r.last_close / r.pivot - 1) * 100


def alert_text(r, ex_name: str, interval: str) -> str:
    head = {"BREAKOUT": "🚀 VCP 带量突破", "SETUP": "👀 VCP 形态成立 · 接近突破点"}[r.status]
    lines = [
        f"{head}",
        f"{r.symbol}  ({ex_name} 永续 · {interval})",
        f"收盘价: {fmt_price(r.last_close)}",
        f"Pivot 突破价: {fmt_price(r.pivot)}  (现价相对 pivot {vs_pivot(r):+.2f}%)",
        f"收缩: {r.depths_text()}  (共 {len(r.contractions)} 次)",
        f"底部长度: {r.base_bars} 根K线",
        f"参考止损(末段低点): {fmt_price(r.contractions[-1].trough)}",
    ]
    if r.breakout_volume_ratio:
        lines.append(f"突破量: {r.breakout_volume_ratio}x 均量")
    lines.append(f"评分: {r.score}")
    lines.append("⚠️ 仅为形态提醒，非投资建议")
    return "\n".join(lines)


def analyse(ex, symbol: str, cfg: dict):
    df = ex.klines(symbol, cfg["interval"], cfg["kline_limit"])
    df = drop_unclosed(df, cfg["interval"])
    return df, detect_vcp(df, symbol, cfg["vcp"])


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

    results, frames = [], {}
    workers = 3 if ex.name == "okx" else 6
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(analyse, ex, s, cfg): s for s in syms}
        for i, fut in enumerate(as_completed(futs), 1):
            s = futs[fut]
            try:
                df, r = fut.result()
                results.append(r)
                if r.valid:
                    frames[s] = df
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
        img = None
        if cfg.get("charts", "all") != "none":
            img = str(run_dir / f"{r.symbol}_{r.status}.png")
            plot_vcp(frames[r.symbol], r, img, title=f"{r.symbol} ({ex.name} perp, {cfg['interval']})")
        key = f"{ex.name}|{r.symbol}|{cfg['interval']}|{r.status}|{fmt_price(r.pivot)}"
        if r.status in alert_on and key not in state:
            if notifier.enabled:
                notifier.send(alert_text(r, ex.name, cfg["interval"]), img)
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
                 f"扫描 {len(results)} 个 USDT 永续合约",
                 f"突破 BREAKOUT: {sum(r.status == 'BREAKOUT' for r in hits)} 个",
                 f"接近突破 SETUP: {sum(r.status == 'SETUP' for r in hits)} 个"]
        if hits:
            lines.append("信号: " + ", ".join(r.symbol for r in hits[:15]))
        else:
            lines.append("本轮没有符合 VCP 的合约")
        if watch:
            lines.append(f"观察名单(形态成立、离 pivot 还远): {watch}")
        notifier.send("\n".join(lines))

    pd.DataFrame([r.to_row() for r in hits]).to_csv(run_dir / "signals.csv", index=False, encoding="utf-8-sig")
    table.to_csv(OUT / "latest_all_results.csv", index=False, encoding="utf-8-sig")
    print(f"结果已保存到: {run_dir}   用时 {time.time() - t0:.0f} 秒\n")
    return table


def single(cfg: dict, symbol: str):
    ex, vols = get_exchange(cfg.get("exchange", "auto"))
    if symbol not in vols:
        # 允许输入 BTCUSDT 自动转换成 OKX / Gate 的写法
        alt = {"okx": symbol.replace("USDT", "-USDT-SWAP"), "gate": symbol.replace("USDT", "_USDT")}.get(ex.name)
        if alt in vols:
            symbol = alt
    df, r = analyse(ex, symbol, cfg)
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
