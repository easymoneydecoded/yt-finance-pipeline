"""
================================================================================
 tiktok.py - Envia los Shorts a TikTok (Content Posting API oficial)
================================================================================

 Modos (variable TIKTOK_MODE):
   draft  (por defecto) -> el video llega a la bandeja de TikTok del canal
                           como borrador; se publica con un toque en la app.
                           Funciona sin que TikTok haya auditado la app.
   direct              -> publicacion directa con titulo. Hasta que TikTok
                           audite la app solo permite privacidad SELF_ONLY,
                           asi que activalo cuando la auditoria este aprobada.

 En modo direct el video se marca como contenido generado con IA (is_aigc).
 En modo draft esa etiqueta se activa en la app al publicar.

 Credenciales: TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET y TIKTOK_REFRESH_TOKEN
 (se obtiene una vez con get_tiktok_token.py). El access token dura 24 h y se
 renueva en cada ejecucion; el refresh token renovado se guarda cifrado.
================================================================================
"""

import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

import secret_store

load_dotenv()

API          = "https://open.tiktokapis.com/v2"
MODE         = os.getenv("TIKTOK_MODE", "draft")
PRIVACY      = os.getenv("TIKTOK_PRIVACY", "PUBLIC_TO_EVERYONE")
MAX_CHUNK    = 64 * 1024 * 1024
MIN_CHUNK    = 5 * 1024 * 1024


class TikTokError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(os.getenv("TIKTOK_CLIENT_KEY") and os.getenv("TIKTOK_CLIENT_SECRET")
                and (secret_store.get("tiktok").get("refresh_token") or os.getenv("TIKTOK_REFRESH_TOKEN")))


def access_token() -> str:
    refresh = secret_store.get("tiktok").get("refresh_token") or os.getenv("TIKTOK_REFRESH_TOKEN")
    r = requests.post(f"{API}/oauth/token/", timeout=30, headers={"Content-Type": "application/x-www-form-urlencoded"},
                      data={"client_key": os.getenv("TIKTOK_CLIENT_KEY"), "client_secret": os.getenv("TIKTOK_CLIENT_SECRET"),
                            "grant_type": "refresh_token", "refresh_token": refresh})
    data = r.json()
    if "access_token" not in data:
        raise TikTokError(f"no pude renovar el token: {str(data)[:300]}")
    if data.get("refresh_token") and data["refresh_token"] != refresh:
        secret_store.put("tiktok", {"refresh_token": data["refresh_token"]})
    return data["access_token"]


def _chunks(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunks) segun las reglas de TikTok (5-64 MB, el ultimo absorbe el resto)."""
    if size <= MAX_CHUNK:
        return size, 1
    chunk = MAX_CHUNK
    return chunk, size // chunk


def _api(token: str, path: str, body: dict) -> dict:
    r = requests.post(f"{API}{path}", json=body, timeout=60, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
    data = r.json()
    err = data.get("error", {})
    if r.status_code >= 400 or (err.get("code") not in (None, "ok")):
        raise TikTokError(f"{path}: {r.status_code} {str(err)[:300]}")
    return data.get("data", {})


def upload(video: Path, caption: str) -> str:
    """Sube el video (borrador o directo segun TIKTOK_MODE). Devuelve el publish_id."""
    token = access_token()
    size = video.stat().st_size
    chunk, total = _chunks(size)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": total}

    if MODE == "direct":
        init = _api(token, "/post/publish/video/init/", {
            "post_info": {"title": caption, "privacy_level": PRIVACY, "disable_comment": False,
                          "disable_duet": False, "disable_stitch": False,
                          # Etiqueta "contenido generado con IA" (voz sintetica), como dicen las paginas legales
                          "is_aigc": True},
            "source_info": source})
    else:
        init = _api(token, "/post/publish/inbox/video/init/", {"source_info": source})

    with open(video, "rb") as fh:
        for i in range(total):
            start = i * chunk
            end = size - 1 if i == total - 1 else start + chunk - 1
            fh.seek(start)
            part = fh.read(end - start + 1)
            r = requests.put(init["upload_url"], data=part, timeout=600, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(part)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise TikTokError(f"subida fallo en el trozo {i + 1}/{total}: {r.status_code} {r.text[:200]}")

    publish_id = init["publish_id"]
    # Comprobar que TikTok lo ha aceptado (no bloquea si tarda)
    for _ in range(12):
        status = _api(token, "/post/publish/status/fetch/", {"publish_id": publish_id}).get("status", "")
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            break
        if status == "FAILED":
            raise TikTokError("TikTok marco la subida como fallida")
        time.sleep(10)
    return publish_id
