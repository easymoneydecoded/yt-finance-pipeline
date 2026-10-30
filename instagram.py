"""
================================================================================
 instagram.py - Publica Reels en Instagram (API oficial, gratis)
================================================================================

 API: "Instagram API with Instagram Login" (cuenta profesional, sin pagina
 de Facebook). Flujo:
   1. crear contenedor REELS con subida "resumable"
   2. subir el MP4 directamente a rupload.facebook.com (sin alojarlo fuera)
   3. esperar a que Instagram lo procese (status_code = FINISHED)
   4. media_publish

 Token: INSTAGRAM_TOKEN (largo, 60 dias) en los secrets. Se renueva solo
 cada ~20 dias y la version renovada se guarda cifrada (secret_store.py).
 Limite de Instagram: 100 publicaciones por API cada 24 h (vamos a ~3).
================================================================================
"""

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import secret_store

load_dotenv()

API_VERSION   = os.getenv("INSTAGRAM_API_VERSION", "v23.0")
GRAPH         = f"https://graph.instagram.com/{API_VERSION}"
REFRESH_DAYS  = 20
PROCESS_WAIT  = 10 * 60   # Instagram puede tardar varios minutos en procesar el video


class InstagramError(RuntimeError):
    pass


def _check(r: requests.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    if r.status_code >= 400 or "error" in data:
        raise InstagramError(f"{r.status_code}: {str(data.get('error', data))[:300]}")
    return data


def get_token() -> str | None:
    """Token vigente: el guardado (renovado) o el de los secrets; lo renueva si toca."""
    stored = secret_store.get("instagram")
    token = stored.get("token") or os.getenv("INSTAGRAM_TOKEN")
    if not token:
        return None
    refreshed = stored.get("refreshed_at")
    age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(refreshed)).days if refreshed else 99
    if age_days >= REFRESH_DAYS:
        try:
            data = _check(requests.get("https://graph.instagram.com/refresh_access_token", timeout=30,
                                       params={"grant_type": "ig_refresh_token", "access_token": token}))
            token = data["access_token"]
            secret_store.put("instagram", {"token": token,
                                           "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            print(f"   . token de Instagram renovado (valido {data.get('expires_in', 0) // 86400} dias)")
        except Exception as e:
            print(f"   ! no pude renovar el token de Instagram: {e}")
    return token


def enabled() -> bool:
    return bool(secret_store.get("instagram").get("token") or os.getenv("INSTAGRAM_TOKEN"))


def publish_reel(video: Path, caption: str) -> str:
    """Publica el Reel y devuelve su id de Instagram."""
    token = get_token()
    if not token:
        raise InstagramError("falta INSTAGRAM_TOKEN")
    user_id = os.getenv("INSTAGRAM_USER_ID") or _check(
        requests.get(f"{GRAPH}/me", params={"fields": "user_id,username", "access_token": token}, timeout=30))["user_id"]

    container = _check(requests.post(f"{GRAPH}/{user_id}/media", timeout=60, data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption,
        "share_to_feed": "true", "access_token": token,
    }))
    cid = container["id"]
    upload_uri = container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}/{cid}"

    size = video.stat().st_size
    with open(video, "rb") as fh:
        _check(requests.post(upload_uri, data=fh, timeout=600, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
        }))

    deadline = time.time() + PROCESS_WAIT
    while True:
        st = _check(requests.get(f"{GRAPH}/{cid}", timeout=30,
                                 params={"fields": "status_code,status", "access_token": token}))
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise InstagramError(f"Instagram rechazo el video: {st.get('status')}")
        if time.time() > deadline:
            raise InstagramError("Instagram no termino de procesar el video a tiempo")
        time.sleep(15)

    published = _check(requests.post(f"{GRAPH}/{user_id}/media_publish", timeout=60,
                                     data={"creation_id": cid, "access_token": token}))
    return published["id"]
