"""Telegram bot messages (digest text + daily Excel file)."""

from __future__ import annotations

from pathlib import Path

import requests

from ..config import env

API = "https://api.telegram.org/bot{token}/{method}"


def send(digest, attachment: Path | None) -> str:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return "skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)"
    for text in digest.telegram:
        r = requests.post(API.format(token=token, method="sendMessage"), timeout=30, data={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true",
        })
        if not r.ok:
            return f"failed: {r.status_code} {r.text[:200]}"
    if attachment and Path(attachment).exists() and digest.has_news:
        with open(attachment, "rb") as fh:
            r = requests.post(API.format(token=token, method="sendDocument"), timeout=120,
                              data={"chat_id": chat_id, "caption": "Aaj ke naye items (Excel)"},
                              files={"document": (Path(attachment).name, fh)})
        if not r.ok:
            return f"messages sent, file failed: {r.status_code} {r.text[:200]}"
    return f"sent {len(digest.telegram)} message(s)"


def print_chat_ids() -> int:
    """Helper for setup: shows the chat id of everyone who messaged the bot."""
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN .env mein nahi hai. Pehle @BotFather se bot banakar token daalein.")
        return 1
    r = requests.get(API.format(token=token, method="getUpdates"), timeout=30)
    if not r.ok:
        print(f"Telegram error: {r.status_code} {r.text[:300]}")
        return 1
    chats = {}
    for update in r.json().get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = chat.get("title") or chat.get("first_name") or chat.get("username") or ""
    if not chats:
        print("Koi message nahi mila. Telegram mein apne bot ko 'hi' bhejiye, phir yeh command dobara chalayein.")
        return 1
    print("In chat id mein se apna wala .env mein TELEGRAM_CHAT_ID= ke aage likhein:")
    for chat_id, name in chats.items():
        print(f"  {chat_id}   ({name})")
    return 0
