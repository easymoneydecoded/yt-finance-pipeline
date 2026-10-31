"""
================================================================================
 get_threads_token.py - Obtiene el THREADS_TOKEN (una sola vez)
================================================================================

 1. En developers.facebook.com, tu app de Meta con el caso de uso "Threads API"
    (permisos threads_basic y threads_content_publish) y como URL de
    redireccion https://tickerpress.net/
 2. Pon THREADS_APP_ID y THREADS_APP_SECRET en tu .env (son los de Threads,
    distintos de los de Instagram)
 3. python get_threads_token.py
 4. Abre el enlace y autoriza con la cuenta de Threads del canal. Te lleva a
    tickerpress.net (si en Espana no carga por el futbol, da igual): copia la
    URL COMPLETA de la barra del navegador y pegala aqui
 5. El token largo (60 dias) y el user id se guardan en el .env; insider_social.py
    lo renueva solo despues (secret_store.py)
================================================================================
"""

import os
import re
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

REDIRECT = os.getenv("THREADS_REDIRECT_URI", "https://tickerpress.net/")
ENV_FILE = Path(".env")


def save_env(values: dict) -> None:
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    for key, value in values.items():
        if re.search(rf"^{key}=.*$", text, re.M):
            text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_FILE.write_text(text, encoding="utf-8")


def main() -> None:
    app_id = os.getenv("THREADS_APP_ID") or input("THREADS_APP_ID: ").strip()
    secret = os.getenv("THREADS_APP_SECRET") or input("THREADS_APP_SECRET: ").strip()
    state = secrets.token_urlsafe(12)
    url = "https://threads.net/oauth/authorize?" + urlencode({
        "client_id": app_id, "redirect_uri": REDIRECT, "response_type": "code", "state": state,
        "scope": "threads_basic,threads_content_publish"})
    print(f"\n1) Abre este enlace y autoriza con la cuenta de Threads de Ticker Press:\n\n{url}\n")
    back = input("2) Pega aqui la URL completa a la que te ha llevado: ").strip()
    params = parse_qs(urlparse(back).query)
    if params.get("state", [""])[0] != state:
        raise SystemExit("La URL no corresponde a esta autorizacion (state distinto). Repite el proceso.")
    code = params["code"][0].removesuffix("#_")

    short = requests.post("https://graph.threads.net/oauth/access_token", timeout=30, data={
        "client_id": app_id, "client_secret": secret, "grant_type": "authorization_code",
        "redirect_uri": REDIRECT, "code": code}).json()
    if "access_token" not in short:
        raise SystemExit(f"Threads no devolvio el token: {short}")
    long = requests.get("https://graph.threads.net/access_token", timeout=30, params={
        "grant_type": "th_exchange_token", "client_secret": secret,
        "access_token": short["access_token"]}).json()
    if "access_token" not in long:
        raise SystemExit(f"No pude cambiarlo por el token largo: {long}")

    me = requests.get("https://graph.threads.net/v1.0/me", timeout=30,
                      params={"fields": "id,username", "access_token": long["access_token"]}).json()
    save_env({"THREADS_TOKEN": long["access_token"], "THREADS_USER_ID": me["id"]})
    print(f"\nListo: cuenta @{me.get('username')} (id {me['id']}), token valido "
          f"{long.get('expires_in', 0) // 86400} dias. Guardado en .env como THREADS_TOKEN y THREADS_USER_ID.")


if __name__ == "__main__":
    main()
