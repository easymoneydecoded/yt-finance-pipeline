"""
================================================================================
 setup_telegram_channels.py - Crea los canales de Insider Alerts (una sola vez)
================================================================================

 Un bot no puede crear canales; esto entra con TU cuenta de Telegram y:
   1. Crea el canal publico (gratis) y el privado (premium), o reutiliza los
      que ya existan con el mismo nombre
   2. Les pone la foto del canal (branding/avatar.png) y la descripcion
   3. Anade el bot como administrador (publicar + invitar usuarios)
   4. Guarda ALERTS_PREMIUM_CHAT y ALERTS_FREE_CHAT en el .env

 La sesion vive solo en memoria y al terminar se cierra (log_out): no queda
 ningun acceso a tu cuenta guardado en el PC.

 Necesita api_id y api_hash de https://my.telegram.org > API development tools.
 Uso:  pip install telethon  &&  python setup_telegram_channels.py
================================================================================
"""

import asyncio
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

load_dotenv()

FREE_TITLE    = "Ticker Press · Insider Buys"
PREMIUM_TITLE = "Ticker Press · Insider Alerts Premium"
FREE_ABOUT    = ("Daily recap of what company insiders are buying with their own money "
                 "(SEC Form 4). Real-time alerts in Premium. Not financial advice.")
PREMIUM_ABOUT = ("Real-time alerts of insider open-market buys, cluster buys and big insider "
                 "sales, straight from SEC Form 4 filings. Not financial advice.")
USERNAMES     = ["tickerpress_insiders", "tickerpressinsiders", "tickerpress_insider_buys",
                 "insiderbuys_tickerpress"]
AVATAR        = Path("branding/avatar.png")
ENV_FILE      = Path(".env")


def bot_username() -> str:
    token = os.getenv("TELEGRAM_ALERTS_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=30).json()
    if not me.get("ok"):
        raise SystemExit(f"El token del bot del .env no vale: {me.get('description')}")
    return me["result"]["username"]


def save_env(values: dict) -> None:
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    for key, value in values.items():
        line = f"{key}={value}"
        if re.search(rf"^{key}=.*$", text, re.M):
            text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n{line}\n"
    ENV_FILE.write_text(text, encoding="utf-8")


async def get_or_create(client, title: str, about: str):
    async for d in client.iter_dialogs():
        if d.is_channel and d.title == title:
            print(f"   = ya existe: {title}")
            return d.entity
    res = await client(functions.channels.CreateChannelRequest(title=title, about=about, broadcast=True))
    channel = res.chats[0]
    print(f"   + creado: {title}")
    if AVATAR.exists():
        photo = await client.upload_file(str(AVATAR))
        await client(functions.channels.EditPhotoRequest(channel, types.InputChatUploadedPhoto(file=photo)))
    return channel


async def set_public_username(client, channel) -> str:
    if getattr(channel, "username", None):
        return channel.username
    for name in USERNAMES:
        try:
            if await client(functions.channels.CheckUsernameRequest(channel, name)):
                await client(functions.channels.UpdateUsernameRequest(channel, name))
                return name
        except Exception as e:
            print(f"   . @{name} no disponible ({type(e).__name__})")
    raise SystemExit("Ningun nombre de usuario libre: anade otro a USERNAMES")


async def make_bot_admin(client, channel, bot: str) -> None:
    rights = types.ChatAdminRights(post_messages=True, edit_messages=True, delete_messages=True,
                                   invite_users=True, pin_messages=True)
    await client(functions.channels.EditAdminRequest(channel=channel, user_id=bot,
                                                     admin_rights=rights, rank="bot"))


async def main() -> None:
    bot = bot_username()
    print(f"Bot: @{bot}\n")
    api_id = os.getenv("TELEGRAM_API_ID") or input("api_id (de my.telegram.org): ").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH") or input("api_hash: ").strip()

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.start()          # pide telefono, el codigo que te llega y la clave 2FA si la tienes
    try:
        free = await get_or_create(client, FREE_TITLE, FREE_ABOUT)
        premium = await get_or_create(client, PREMIUM_TITLE, PREMIUM_ABOUT)
        username = await set_public_username(client, free)
        for ch in (free, premium):
            await make_bot_admin(client, ch, bot)
        print(f"   ✓ @{bot} es administrador de los dos canales")

        values = {"ALERTS_FREE_CHAT": f"@{username}", "ALERTS_PREMIUM_CHAT": f"-100{premium.id}"}
        save_env(values)
        print("\nGuardado en .env:")
        for k, v in values.items():
            print(f"   {k}={v}")
        print(f"\nCanal gratis: https://t.me/{username}")
    finally:
        await client.log_out()    # cierra la sesion: no queda acceso guardado
        print("\nSesion de Telegram cerrada.")


if __name__ == "__main__":
    asyncio.run(main())
