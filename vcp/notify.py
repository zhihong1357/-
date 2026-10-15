"""提醒：Telegram 机器人（免费）。也可选 Discord Webhook。"""
from __future__ import annotations

import requests


class Notifier:
    def __init__(self, cfg: dict):
        tg = cfg.get("telegram") or {}
        self.tg_token = (tg.get("bot_token") or "").strip()
        # 支持多个接收人：chat_id 用英文逗号分隔，例如 "123456,1134208027"
        self.tg_chats = [c.strip() for c in str(tg.get("chat_id") or "").replace("，", ",").split(",") if c.strip()]
        self.discord = ((cfg.get("discord") or {}).get("webhook_url") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool((self.tg_token and self.tg_chats) or self.discord)

    def send(self, text: str, image: str | None = None) -> None:
        if self.tg_token:
            for chat in self.tg_chats:
                self._telegram(chat, text, image)
        if self.discord:
            self._discord(text, image)

    def _telegram(self, chat: str, text: str, image: str | None):
        base = f"https://api.telegram.org/bot{self.tg_token}"
        try:
            if image:
                # 图片说明最多 1024 字，太长就图片 + 文字分两条发
                cap = text if len(text) <= 1024 else text.split("\n")[0]
                with open(image, "rb") as f:
                    r = requests.post(base + "/sendPhoto", data={"chat_id": chat, "caption": cap},
                                      files={"photo": f}, timeout=30)
                if r.ok and len(text) > 1024:
                    r = requests.post(base + "/sendMessage", data={"chat_id": chat, "text": text[:4096]}, timeout=30)
            else:
                r = requests.post(base + "/sendMessage", data={"chat_id": chat, "text": text[:4096]}, timeout=30)
            if not r.ok:
                print(f"[Telegram 发送失败] chat_id={chat} {r.status_code} {r.text[:200]}")
                if "bot can't send messages to the bot" in r.text or chat == self.tg_token.split(":")[0]:
                    print("  → chat_id 填成了机器人自己的 ID（token 冒号前面那串数字）。"
                          "请在 Telegram 搜索 @userinfobot，点 Start，它回复的 Id 才是你的 chat_id。")
                elif "chat not found" in r.text or "bot was blocked" in r.text:
                    print(f"  → chat_id={chat} 这个账号要先在 Telegram 里打开你的机器人，点 Start（开始），之后才能收到消息。")
                elif r.status_code == 401:
                    print("  → bot_token 不正确，请到 @BotFather 重新复制。")
        except Exception as e:
            print(f"[Telegram 发送失败] chat_id={chat} {e}")

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
