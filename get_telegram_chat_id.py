"""
================================================================================
 get_telegram_chat_id.py - Saca tu TELEGRAM_CHAT_ID y manda un mensaje de prueba
================================================================================

 1. Crea el bot con @BotFather en Telegram y pon el token en tu .env:
        TELEGRAM_BOT_TOKEN=123456:ABC...
 2. Abre el chat con tu bot en Telegram y enviale cualquier mensaje ("hola")
 3. python get_telegram_chat_id.py
 4. Guarda en GitHub los secrets TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID
================================================================================
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or input("TELEGRAM_BOT_TOKEN: ").strip()
    api = f"https://api.telegram.org/bot{token}"

    me = requests.get(f"{api}/getMe", timeout=30).json()
    if not me.get("ok"):
        raise SystemExit(f"El token no es valido: {me.get('description')}")
    print(f"Bot: @{me['result']['username']}")

    updates = requests.get(f"{api}/getUpdates", timeout=30).json().get("result", [])
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or chat.get("username")
    if not chats:
        raise SystemExit(f"No hay mensajes. Abre https://t.me/{me['result']['username']}, "
                         "enviale 'hola' al bot y vuelve a ejecutar este script.")

    chat_id, name = list(chats.items())[-1]
    print(f"\n=== TELEGRAM_CHAT_ID ===\n\n{chat_id}   ({name})\n")
    ok = requests.post(f"{api}/sendMessage", timeout=30, data={
        "chat_id": chat_id, "text": "👋 Ticker Press: avisos de Telegram conectados."}).json().get("ok")
    print("Mensaje de prueba enviado: mira tu Telegram." if ok else "No se pudo enviar el mensaje de prueba.")
    print("\nGuarda en GitHub > Settings > Secrets and variables > Actions:")
    print("  TELEGRAM_BOT_TOKEN  (el token de @BotFather)")
    print(f"  TELEGRAM_CHAT_ID    {chat_id}\n")


if __name__ == "__main__":
    main()
