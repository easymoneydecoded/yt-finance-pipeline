"""
================================================================================
 get_refresh_token.py - Genera un YOUTUBE_REFRESH_TOKEN con todos los permisos
================================================================================

 Ejecutalo UNA vez en tu PC:
     python get_refresh_token.py

 1. Lee YOUTUBE_CLIENT_ID y YOUTUBE_CLIENT_SECRET de tu .env (los mismos que
    ya usas en GitHub).
 2. Abre el navegador para que autorices la cuenta del canal.
 3. Imprime el refresh token nuevo -> pegalo en GitHub:
    Settings > Secrets and variables > Actions > YOUTUBE_REFRESH_TOKEN

 Permisos: subir videos, leer estadisticas y analiticas (aprendizaje) y
 gestionar comentarios y listas de reproduccion (engagement.py).
 Si el cliente OAuth es de tipo "Web", anade http://localhost:8080/ como URI
 de redireccion en Google Cloud Console.
================================================================================
"""

import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",   # comentarios + listas
]


def main() -> None:
    client_id     = os.getenv("YOUTUBE_CLIENT_ID") or input("YOUTUBE_CLIENT_ID: ").strip()
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET") or input("YOUTUBE_CLIENT_SECRET: ").strip()

    flow = InstalledAppFlow.from_client_config({
        "installed": {
            "client_id":     client_id,
            "client_secret": client_secret,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost:8080/"],
        }
    }, scopes=SCOPES)

    creds = flow.run_local_server(port=8080, access_type="offline", prompt="consent")
    print("\n=== NUEVO YOUTUBE_REFRESH_TOKEN ===\n")
    print(creds.refresh_token)
    print("\nPegalo en GitHub > Settings > Secrets and variables > Actions > YOUTUBE_REFRESH_TOKEN\n")


if __name__ == "__main__":
    main()
