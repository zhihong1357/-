"""提醒：Telegram 机器人（免费）。也可选 Discord Webhook。"""
from __future__ import annotations

import requests


class Notifier:
    def __init__(self, cfg: dict):
        tg = cfg.get("telegram") or {}
        self.tg_token = (tg.get("bot_token") or "").strip()
        self.tg_chat = str(tg.get("chat_id") or "").strip()
        self.discord = ((cfg.get("discord") or {}).get("webhook_url") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool((self.tg_token and self.tg_chat) or self.discord)

    def send(self, text: str, image: str | None = None) -> None:
        if self.tg_token and self.tg_chat:
            self._telegram(text, image)
        if self.discord:
            self._discord(text, image)

    def _telegram(self, text: str, image: str | None):
        base = f"https://api.telegram.org/bot{self.tg_token}"
        try:
            if image:
                with open(image, "rb") as f:
                    r = requests.post(base + "/sendPhoto", data={"chat_id": self.tg_chat, "caption": text[:1000]},
                                      files={"photo": f}, timeout=30)
            else:
                r = requests.post(base + "/sendMessage", data={"chat_id": self.tg_chat, "text": text}, timeout=30)
            if not r.ok:
                print(f"[Telegram 发送失败] {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"[Telegram 发送失败] {e}")

    def _discord(self, text: str, image: str | None):
        try:
            if image:
                with open(image, "rb") as f:
                    requests.post(self.discord, data={"content": text[:1900]}, files={"file": f}, timeout=30)
            else:
                requests.post(self.discord, json={"content": text[:1900]}, timeout=30)
        except Exception as e:
            print(f"[Discord 发送失败] {e}")


def find_chat_id(token: str) -> None:
    """给机器人发一条消息后运行，打印出你的 chat_id。"""
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
    if not r.get("ok"):
        print("Token 不正确：", r)
        return
    ids = {}
    for u in r.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat:
            ids[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name")
    if not ids:
        print("没找到消息。请先在 Telegram 里给你的机器人随便发一句话（例如 hi），再运行一次。")
    for k, v in ids.items():
        print(f"chat_id = {k}    ({v})")
