"""
================================================================================
 engagement.py - Comentario del canal + listas de reproduccion por tema
================================================================================

 1. Listas de reproduccion: cada video largo se anade a la lista de su tema
    (se crea sola la primera vez). Alarga las sesiones de visionado.
    -> la llama uploader.py justo despues de subir.
 2. Comentario del canal: una pregunta que invita a responder (los
    comentarios ayudan al algoritmo) y, en cada Short, un comentario con el
    enlace al video completo. YouTube no deja comentar un video programado
    hasta que se publica, asi que este script se ejecuta varias veces al dia
    (workflow engagement.yml) y publica los comentarios pendientes.

 NOTA: la API de YouTube NO permite fijar comentarios. El comentario sale con
 la insignia de creador; fijarlo es un clic en YouTube Studio si se quiere.

 Requiere el permiso youtube.force-ssl en YOUTUBE_REFRESH_TOKEN
 (get_refresh_token.py ya lo incluye). Sin el, todo esto se salta sin romper
 la subida. Cuota: 50 unidades por comentario / alta en lista.
================================================================================
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

import content_history as ch

load_dotenv()

YT_CLIENT_ID     = os.getenv("YOUTUBE_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN")
SCOPES           = ["https://www.googleapis.com/auth/youtube.force-ssl"]

PLAYLISTS_FILE   = ch.HISTORY_DIR / "playlists.json"
COMMENT_WINDOW_D = 14   # solo se buscan comentarios pendientes de videos recientes

# Canal gratis del bot Insider Alerts (insider_alerts.py): va en descripciones y comentarios
TELEGRAM_URL     = os.getenv("TELEGRAM_CHANNEL_URL", "https://t.me/tickerpress_insiders")
TELEGRAM_CTA     = f"📲 Free daily insider-buying alerts (SEC Form 4) on Telegram: {TELEGRAM_URL}"
CTA_PER_RUN      = 6    # descripciones antiguas actualizadas por ejecucion (51 unidades de cuota cada una)

PLAYLISTS = {
    "Housing & Mortgages":          "Mortgage rates, home prices and what the housing market means for your money.",
    "Stock Market Explained":       "What is moving the stock market and how it hits your portfolio, explained with real data.",
    "Retirement & Your 401k":       "401k, IRAs and retirement: the numbers that decide when you can stop working.",
    "Savings, Rates & Banks":       "Savings accounts, interest rates and what banks don't tell you.",
    "Inflation & The Economy":      "Inflation, jobs, the Fed and the economy - what it means for your wallet.",
    "Big Money Moves":              "What billionaires, Wall Street giants and big institutions are doing with their money.",
    "Jobs, Paychecks & Taxes":      "Layoffs, wages and taxes: how the economy reaches your paycheck.",
    "Crypto & Alternative Assets":  "Bitcoin, gold and alternative assets, explained with real data.",
}


# ---------------------------------------------------------------------------
# Cliente con permisos de gestion
# ---------------------------------------------------------------------------
def client():
    """Cliente con youtube.force-ssl, o None si el token no tiene ese permiso."""
    if not all([YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN]):
        print("   ! faltan credenciales YOUTUBE_*, salto engagement")
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        creds = Credentials(token=None, refresh_token=YT_REFRESH_TOKEN,
                            token_uri="https://oauth2.googleapis.com/token",
                            client_id=YT_CLIENT_ID, client_secret=YT_CLIENT_SECRET, scopes=SCOPES)
        creds.refresh(Request())
        return build("youtube", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"   ! sin permiso de comentarios/listas ({str(e)[:100]}).")
        print("     Genera un token nuevo con get_refresh_token.py para activarlo.")
        return None


# ---------------------------------------------------------------------------
# Listas de reproduccion
# ---------------------------------------------------------------------------
def _load_playlists() -> dict:
    try:
        return json.loads(PLAYLISTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def playlist_id(youtube, title: str) -> str | None:
    if title not in PLAYLISTS:
        title = "Stock Market Explained"
    cache = _load_playlists()
    if cache.get(title):
        return cache[title]
    # Puede que ya exista (creada a mano o en una ejecucion sin cache)
    resp = youtube.playlists().list(part="snippet", mine=True, maxResults=50).execute()
    for item in resp.get("items", []):
        if item["snippet"]["title"] == title:
            cache[title] = item["id"]
            break
    else:
        created = youtube.playlists().insert(part="snippet,status", body={
            "snippet": {"title": title, "description": PLAYLISTS[title], "defaultLanguage": "en"},
            "status":  {"privacyStatus": "public"},
        }).execute()
        cache[title] = created["id"]
        print(f"   . lista creada: {title}")
    ch.HISTORY_DIR.mkdir(exist_ok=True)
    PLAYLISTS_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return cache[title]


def add_to_playlist(youtube, video_id: str, title: str) -> str | None:
    """Anade el video a la lista del tema. Devuelve el titulo de la lista o None."""
    try:
        pid = playlist_id(youtube, title)
        youtube.playlistItems().insert(part="snippet", body={
            "snippet": {"playlistId": pid, "position": 0,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id}},
        }).execute()
        return title if title in PLAYLISTS else "Stock Market Explained"
    except Exception as e:
        print(f"   ! no pude anadir a la lista: {e}")
        return None


# ---------------------------------------------------------------------------
# Comentarios pendientes
# ---------------------------------------------------------------------------
def post_comment(youtube, video_id: str, text: str) -> str:
    resp = youtube.commentThreads().insert(part="snippet", body={
        "snippet": {"videoId": video_id,
                    "topLevelComment": {"snippet": {"textOriginal": text}}},
    }).execute()
    return resp["id"]


def _due(iso: str, now: datetime) -> bool:
    try:
        at = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at + timedelta(minutes=5) <= now


def post_pending_comments(youtube, history: list[dict]) -> int:
    now = datetime.now(timezone.utc)
    posted = 0
    for v in history:
        if v.get("seed") or not v.get("video_id"):
            continue
        if not _due(v.get("publish_at", ""), now) or v.get("publish_at", "") < (now - timedelta(days=COMMENT_WINDOW_D)).isoformat():
            continue

        if v.get("pinned_comment") and not v.get("comment_id"):
            try:
                v["comment_id"] = post_comment(youtube, v["video_id"], f"{v['pinned_comment']}\n\n{TELEGRAM_CTA}")
                posted += 1
                print(f"   . comentario en '{v['title'][:50]}'")
            except Exception as e:
                print(f"   ! comentario en {v['video_id']} fallo (reintento luego): {str(e)[:120]}")

        done = set(v.get("shorts_commented", []))
        for sid, at in zip(v.get("shorts_ids", []), v.get("shorts_publish_at", [])):
            if sid in done or not _due(at, now):
                continue
            try:
                post_comment(youtube, sid, f"Full breakdown with all the data: https://youtu.be/{v['video_id']}\n\n{TELEGRAM_CTA}")
                done.add(sid)
                posted += 1
                print(f"   . comentario en Short {sid}")
            except Exception as e:
                print(f"   ! comentario en Short {sid} fallo: {str(e)[:120]}")
        v["shorts_commented"] = sorted(done)
    return posted


def add_cta_to_descriptions(youtube, history: list[dict]) -> int:
    """Anade el enlace de Telegram a la descripcion de los videos subidos antes de tenerlo."""
    pending = [vid for v in history if not v.get("seed") and v.get("video_id")
               for vid in [v["video_id"], *v.get("shorts_ids", [])]
               if vid not in set(v.get("cta_done", []))]
    updated = 0
    for vid in pending[:CTA_PER_RUN]:
        owner = next(v for v in history if vid in [v.get("video_id"), *v.get("shorts_ids", [])])
        try:
            items = youtube.videos().list(part="snippet", id=vid).execute().get("items", [])
            if items:
                snippet = items[0]["snippet"]
                if TELEGRAM_URL not in snippet.get("description", ""):
                    snippet["description"] = f"{snippet.get('description', '').rstrip()}\n\n{TELEGRAM_CTA}"[:5000]
                    youtube.videos().update(part="snippet", body={"id": vid, "snippet": {
                        k: snippet[k] for k in ("title", "description", "tags", "categoryId",
                                                "defaultLanguage", "defaultAudioLanguage") if k in snippet}}).execute()
                    updated += 1
                    print(f"   . enlace de Telegram en la descripcion de {vid}")
            owner["cta_done"] = sorted(set(owner.get("cta_done", [])) | {vid})
        except Exception as e:
            print(f"   ! no pude actualizar la descripcion de {vid}: {str(e)[:120]}")
    return updated


def main() -> None:
    print("\n=== ENGAGEMENT (comentarios pendientes) ===\n")
    youtube = client()
    if youtube is None:
        return
    history = ch.load_history()
    n = post_pending_comments(youtube, history)
    before = sum(len(v.get("cta_done", [])) for v in history)
    add_cta_to_descriptions(youtube, history)
    if n or sum(len(v.get("cta_done", [])) for v in history) != before:
        ch.save_history(history)
    print(f"   . {n} comentarios publicados")


if __name__ == "__main__":
    main()
