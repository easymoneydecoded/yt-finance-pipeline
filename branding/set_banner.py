"""
================================================================================
 branding/set_banner.py - Sube la cabecera del canal a YouTube
================================================================================

 Lo ejecuta el workflow "Branding (cabecera)" (manual). Usa los secrets de
 GitHub; necesita el permiso youtube.force-ssl del token.

 Uso: python branding/set_banner.py [ruta_imagen]   (por defecto branding/banner.jpg)

 La FOTO DE PERFIL no se puede cambiar por API: se sube a mano en
 YouTube Studio > Personalizacion > Imagen de marca (branding/avatar.png).
 Para volver a la cabecera anterior: branding/old/banner_antiguo.jpg
================================================================================
"""

import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "branding/banner.jpg"
    creds = Credentials(
        token=None, refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"], client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.force-ssl"],
    )
    creds.refresh(Request())
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    print(f"Subiendo {path}...")
    banner_url = youtube.channelBanners().insert(media_body=MediaFileUpload(path, mimetype=mime)).execute()["url"]

    channel = youtube.channels().list(part="brandingSettings", mine=True).execute()["items"][0]
    print(f"Canal: {channel['brandingSettings'].get('channel', {}).get('title', channel['id'])}")
    # Se reenvia la configuracion actual del canal (titulo, descripcion...) para no perderla
    youtube.channels().update(part="brandingSettings", body={
        "id": channel["id"],
        "brandingSettings": {
            "channel": channel["brandingSettings"].get("channel", {}),
            "image":   {"bannerExternalUrl": banner_url},
        },
    }).execute()
    print("Cabecera actualizada. Puede tardar unos minutos en verse en YouTube.")


if __name__ == "__main__":
    main()
