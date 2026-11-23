"""
================================================================================
 tiktok.py - Envia los Shorts a TikTok (Content Posting API oficial)
================================================================================

 Modos (variable TIKTOK_MODE; si hay ZERNIO_API_KEY y no se indica otro, se usa zernio):
   zernio              -> publicacion directa y PUBLICA a traves de Zernio (zernio.com), que
                           tiene la app de TikTok ya auditada. Solo necesita ZERNIO_API_KEY
                           (cuenta @tickerpress conectada en su panel). Desde 2026-11-23.
   draft               -> el video llega a la bandeja de TikTok del canal
                           como borrador; se publica con un toque en la app.
                           Funciona sin que TikTok haya auditado la app.
   direct              -> publicacion directa con titulo. Hasta que TikTok
                           audite la app solo permite privacidad SELF_ONLY,
                           asi que activalo cuando la auditoria este aprobada.

 Etiqueta "contenido generado con IA": desactivada por defecto. Para activarla
 en modo direct, crea la variable TIKTOK_AIGC_LABEL=true.

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
ZERNIO       = "https://zernio.com/api/v1"
ZERNIO_KEY   = os.getenv("ZERNIO_API_KEY", "").strip()
MODE         = os.getenv("TIKTOK_MODE", "").strip() or ("zernio" if ZERNIO_KEY else "draft")
PRIVACY      = os.getenv("TIKTOK_PRIVACY", "PUBLIC_TO_EVERYONE")
AIGC_LABEL   = os.getenv("TIKTOK_AIGC_LABEL", "false").lower() == "true"   # etiqueta "generado con IA"
MAX_CHUNK    = 64 * 1024 * 1024
MIN_CHUNK    = 5 * 1024 * 1024


class TikTokError(RuntimeError):
    pass


def enabled() -> bool:
    if MODE == "zernio":
        return bool(ZERNIO_KEY)
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


def _zernio(method: str, path: str, **kw) -> dict:
    r = requests.request(method, f"{ZERNIO}{path}", timeout=120,
                         headers={"Authorization": f"Bearer {ZERNIO_KEY}", **kw.pop("headers", {})}, **kw)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    if r.status_code >= 400:
        raise TikTokError(f"Zernio {path}: {r.status_code} {str(data)[:300]}")
    return data


def zernio_account() -> str:
    """_id de la cuenta de TikTok conectada en Zernio."""
    accounts = [a for a in _zernio("GET", "/accounts").get("accounts", [])
                if a.get("platform") == "tiktok" and a.get("isActive", True)]
    if not accounts:
        raise TikTokError("Zernio no tiene ninguna cuenta de TikTok conectada")
    return accounts[0]["_id"]


def zernio_upload(video: Path, caption: str, key: str = "") -> str:
    """Publica el video en TikTok (publico) via Zernio. Devuelve 'zernio:<postId>' o la URL si ya la hay."""
    account = zernio_account()
    pre = _zernio("POST", "/media/presign", json={"filename": video.name, "contentType": "video/mp4",
                                                   "size": video.stat().st_size})
    with open(video, "rb") as fh:
        r = requests.put(pre["uploadUrl"], data=fh, timeout=600, headers={"Content-Type": "video/mp4"})
    if r.status_code not in (200, 201):
        raise TikTokError(f"subida a Zernio fallo: {r.status_code} {r.text[:200]}")
    headers = {"Idempotency-Key": f"tickerpress-{key}"} if key else {}
    post = _zernio("POST", "/posts", headers=headers, json={
        "content": caption[:2200],
        "mediaItems": [{"type": "video", "url": pre["publicUrl"]}],
        "platforms": [{"platform": "tiktok", "accountId": account}],
        "tiktokSettings": {"privacy_level": PRIVACY, "allow_comment": True, "allow_duet": True,
                           "allow_stitch": True, "content_preview_confirmed": True,
                           "express_consent_given": True},
        "publishNow": True}).get("post", {})
    plat = next((p for p in post.get("platforms", []) if p.get("platform") == "tiktok"), {})
    if plat.get("status") == "failed" or post.get("status") == "failed":
        raise TikTokError(f"Zernio marco la publicacion como fallida: {str(post)[:300]}")
    return plat.get("platformPostUrl") or f"zernio:{post.get('_id', '?')}"


def upload(video: Path, caption: str, key: str = "") -> str:
    """Sube el video segun TIKTOK_MODE. Devuelve el publish_id (o la URL/id de Zernio)."""
    if MODE == "zernio":
        return zernio_upload(video, caption, key)
    token = access_token()
    size = video.stat().st_size
    chunk, total = _chunks(size)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": total}

    if MODE == "direct":
        init = _api(token, "/post/publish/video/init/", {
            "post_info": {"title": caption, "privacy_level": PRIVACY, "disable_comment": False,
                          "disable_duet": False, "disable_stitch": False,
                          "is_aigc": AIGC_LABEL},
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


def check() -> None:
    """Comprueba credenciales sin subir nada (python tiktok.py check)."""
    if MODE == "zernio":
        if not ZERNIO_KEY:
            raise SystemExit("Falta ZERNIO_API_KEY")
        print(f"TikTok via Zernio: cuenta {zernio_account()} conectada - modo zernio (publicacion directa publica)")
        return
    if not enabled():
        raise SystemExit("Faltan TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REFRESH_TOKEN")
    token = access_token()
    r = requests.get(f"{API}/user/info/", timeout=30, params={"fields": "open_id,display_name"},
                     headers={"Authorization": f"Bearer {token}"})
    data = r.json()
    user = data.get("data", {}).get("user", {})
    if not user:
        raise SystemExit(f"TikTok no devolvio la cuenta: {str(data)[:300]}")
    print(f"TikTok: cuenta '{user.get('display_name')}' conectada - token renovado OK")
    print(f"Modo: {MODE} ({'borradores en la bandeja de TikTok' if MODE != 'direct' else 'publicacion directa'})")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["check"]:
        check()
