"""
================================================================================
 instagram.py - Publica Reels en Instagram (API oficial, gratis)
================================================================================

 Acepta dos tipos de token (se detecta solo):
   - Token de Instagram ("IG..."): API con inicio de sesion de Instagram
     (graph.instagram.com). Dura 60 dias y se renueva solo.
   - Token de pagina de Facebook ("EAA..."): API con inicio de sesion de
     Facebook (graph.facebook.com), con el Instagram vinculado a una pagina.
     Si se saca de un token de usuario de larga duracion, NO caduca.
 Flujo de publicacion (igual en los dos):
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


def _is_facebook_token(token: str) -> bool:
    return token.startswith("EAA")


def _graph(token: str) -> str:
    host = "graph.facebook.com" if _is_facebook_token(token) else "graph.instagram.com"
    return f"https://{host}/{API_VERSION}"
REFRESH_DAYS  = 20
PROCESS_WAIT  = 10 * 60   # Instagram puede tardar varios minutos en procesar el video
UPLOAD_ATTEMPTS = 3       # la subida a rupload falla a veces sin motivo; se reintenta con contenedor nuevo


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
    if _is_facebook_token(token):
        return token  # token de pagina de Facebook: no caduca, no se renueva
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


def _find_user_id(token: str) -> str:
    """ID de la cuenta de Instagram si no esta en INSTAGRAM_USER_ID."""
    if not _is_facebook_token(token):
        return _check(requests.get(f"{_graph(token)}/me", timeout=30,
                                   params={"fields": "user_id,username", "access_token": token}))["user_id"]
    # Token de Facebook: la cuenta de Instagram vinculada a la pagina
    me = _check(requests.get(f"{_graph(token)}/me", timeout=30,
                             params={"fields": "instagram_business_account", "access_token": token}))
    if me.get("instagram_business_account"):
        return me["instagram_business_account"]["id"]
    pages = _check(requests.get(f"{_graph(token)}/me/accounts", timeout=30,
                                params={"fields": "instagram_business_account", "access_token": token}))
    for page in pages.get("data", []):
        if page.get("instagram_business_account"):
            return page["instagram_business_account"]["id"]
    raise InstagramError("ninguna pagina de Facebook del token tiene un Instagram vinculado")


def publish_reel(video: Path, caption: str) -> str:
    """Publica el Reel y devuelve su id de Instagram."""
    token = get_token()
    if not token:
        raise InstagramError("falta INSTAGRAM_TOKEN")
    GRAPH = _graph(token)
    user_id = os.getenv("INSTAGRAM_USER_ID") or _find_user_id(token)

    size = video.stat().st_size
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        container = _check(requests.post(f"{GRAPH}/{user_id}/media", timeout=60, data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption,
            "share_to_feed": "true", "access_token": token,
        }))
        cid = container["id"]
        # No usar container["uri"]: Meta devuelve la URL de su version mas nueva (v26) y rupload
        # rechaza el video (ProcessingFailedError, 0 bytes) si no coincide con la del contenedor.
        upload_uri = f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}/{cid}"
        time.sleep(5)                      # el contenedor recien creado a veces aun no "existe" en rupload
        with open(video, "rb") as fh:
            r = requests.post(upload_uri, data=fh.read(), timeout=600, headers={
                "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
            })
        if r.ok:
            break
        # ProcessingFailedError es intermitente (el mismo video sube bien al reintentar)
        print(f"   . subida a Instagram fallo ({r.status_code}), intento {attempt}/{UPLOAD_ATTEMPTS}")
        if attempt == UPLOAD_ATTEMPTS:
            _check(r)
        time.sleep(30 * attempt)

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


def permalink(media_id: str) -> str:
    """URL publica del Reel (o '' si no se puede obtener)."""
    try:
        token = get_token()
        return _check(requests.get(f"{_graph(token)}/{media_id}", timeout=30,
                                   params={"fields": "permalink", "access_token": token})).get("permalink", "")
    except Exception:
        return ""


def debug_upload(video: Path) -> None:
    """Diagnostico de la subida: crea contenedores y sube el video SIN publicar nada."""
    token = get_token()
    GRAPH = _graph(token)
    user_id = os.getenv("INSTAGRAM_USER_ID") or _find_user_id(token)
    size = video.stat().st_size
    print(f"Token {'Facebook' if _is_facebook_token(token) else 'Instagram'} · host {GRAPH} · video {size / 1e6:.1f} MB")
    sample = "https://download.samplelib.com/mp4/sample-5s.mp4"   # video publico de prueba
    variants = {
        "C (rupload con la version del contenedor)": {"fixed_version": True},
        "D (file_url: Meta descarga un video publico)": {"file_url": sample},
    }
    for name, opt in variants.items():
        c = requests.post(f"{GRAPH}/{user_id}/media", timeout=60, data={
            "media_type": "REELS", "upload_type": "resumable", "caption": "debug", "access_token": token}).json()
        print(f"\n--- {name} · contenedor: {c}")
        if "id" not in c:
            continue
        uri = c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}/{c['id']}"
        if opt.get("fixed_version"):
            uri = f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}/{c['id']}"
            with open(video, "rb") as fh:
                r = requests.post(uri, data=fh.read(), timeout=600, headers={
                    "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)})
        else:
            r = requests.post(uri, timeout=120, headers={"Authorization": f"OAuth {token}",
                                                         "file_url": opt["file_url"]})
        print(f"subida {uri}: HTTP {r.status_code} {r.text[:400]}")
        for _ in range(8):
            st = requests.get(f"{GRAPH}/{c['id']}", timeout=30, params={
                "fields": "status_code,status,video_status", "access_token": token}).json()
            print(f"estado: {st}")
            if st.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
                break
            time.sleep(15)


def check() -> None:
    """Comprueba token y permisos sin publicar nada (python instagram.py check)."""
    token = get_token()
    if not token:
        raise SystemExit("Falta INSTAGRAM_TOKEN")
    user_id = os.getenv("INSTAGRAM_USER_ID") or _find_user_id(token)
    graph = _graph(token)
    tipo = "pagina/usuario del sistema de Facebook (no caduca)" if _is_facebook_token(token) else "Instagram (60 dias)"
    print(f"Token: {tipo}")
    acc = _check(requests.get(f"{graph}/{user_id}", timeout=30,
                              params={"fields": "username,name,media_count", "access_token": token}))
    print(f"Cuenta: @{acc.get('username')} ({acc.get('name')}) - {acc.get('media_count')} publicaciones")
    # Este endpoint solo responde si el token tiene permiso de publicar
    limit = _check(requests.get(f"{graph}/{user_id}/content_publishing_limit", timeout=30,
                                params={"fields": "quota_usage,config", "access_token": token}))
    data = (limit.get("data") or [{}])[0]
    print(f"Permiso de publicar: OK - usadas {data.get('quota_usage', 0)} de "
          f"{data.get('config', {}).get('quota_total', '?')} publicaciones por API en 24 h")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["check"]:
        check()
    elif sys.argv[1:] == ["debug"]:
        import tempfile
        import shorts_queue as sq
        item = sq.pending_items(sq.load_queue())[0]
        with tempfile.TemporaryDirectory() as tmp:
            debug_upload(sq.download_asset(item["asset_id"], Path(tmp) / "debug.mp4"))
