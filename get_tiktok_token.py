"""
================================================================================
 get_tiktok_token.py - Obtiene el TIKTOK_REFRESH_TOKEN (una sola vez)
================================================================================

 1. Pon TIKTOK_CLIENT_KEY y TIKTOK_CLIENT_SECRET en tu .env (de developers.tiktok.com;
    mientras la app no este aprobada, los del entorno Sandbox)
 2. python get_tiktok_token.py
 3. Abre el enlace, autoriza con la cuenta de TikTok del canal
 4. TikTok te redirige a la URL de redireccion registrada en la app (puede
    dar error de pagina, da igual): copia la URL COMPLETA de la barra del
    navegador y pegala aqui
 5. Guarda el refresh token que imprime en GitHub como TIKTOK_REFRESH_TOKEN
================================================================================
"""

import os
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

# Los permisos deben coincidir con los de la app en developers.tiktok.com.
# Borradores: user.info.basic,video.upload. Si activas Direct Post, anade ",video.publish".
SCOPES = os.getenv("TIKTOK_SCOPES", "user.info.basic,video.upload")


def main() -> None:
    key = os.getenv("TIKTOK_CLIENT_KEY") or input("TIKTOK_CLIENT_KEY: ").strip()
    secret = os.getenv("TIKTOK_CLIENT_SECRET") or input("TIKTOK_CLIENT_SECRET: ").strip()
    redirect = os.getenv("TIKTOK_REDIRECT_URI") or input("Redirect URI registrada en la app (https://...): ").strip()
    state = secrets.token_urlsafe(12)

    url = "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
        "client_key": key, "scope": SCOPES, "response_type": "code",
        "redirect_uri": redirect, "state": state,
    })
    print(f"\n1) Abre este enlace y autoriza con la cuenta del canal:\n\n{url}\n")
    back = input("2) Pega aqui la URL completa a la que te ha redirigido: ").strip()
    params = parse_qs(urlparse(back).query)
    if params.get("state", [""])[0] != state:
        raise SystemExit("La URL no corresponde a esta autorizacion (state distinto). Repite el proceso.")
    code = params["code"][0]

    r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", timeout=30,
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      data={"client_key": key, "client_secret": secret, "code": code,
                            "grant_type": "authorization_code", "redirect_uri": redirect})
    data = r.json()
    if "refresh_token" not in data:
        raise SystemExit(f"TikTok no devolvio el token: {data}")
    print("\n=== NUEVO TIKTOK_REFRESH_TOKEN ===\n")
    print(data["refresh_token"])
    print(f"\nPermisos concedidos: {data.get('scope')}")
    print("Pegalo en GitHub > Settings > Secrets and variables > Actions > TIKTOK_REFRESH_TOKEN\n")


if __name__ == "__main__":
    main()
