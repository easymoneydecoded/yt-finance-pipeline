"""
================================================================================
 analytics.py - Etapa 0: aprender de los resultados del canal
================================================================================

 Que hace:
   1. Lee el rendimiento de los videos del historial:
        - Con OAuth (YOUTUBE_REFRESH_TOKEN con permisos de lectura):
          visitas, likes, comentarios (Data API) + retencion media,
          duracion media vista y suscriptores ganados (Analytics API)
        - Sin esos permisos: visitas publicas leidas de la pagina del canal
   2. Guarda las metricas dentro de history/videos.json ("stats")
   3. Las etapas 1 y 2 usan esas metricas para repetir lo que funciona
      (formatos, estilos de titulo, temas) y evitar lo que no

 Para activar las metricas completas, genera un token nuevo con
 get_refresh_token.py (incluye los permisos de lectura) y actualiza el
 secret YOUTUBE_REFRESH_TOKEN. Coste: 0 EUR.
================================================================================
"""

import os
import re
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

import content_history as ch

load_dotenv()

YT_CLIENT_ID     = os.getenv("YOUTUBE_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN")
CHANNEL_HANDLE   = os.getenv("YOUTUBE_CHANNEL_HANDLE", "@tickerpressyt")

READ_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

LOOKBACK_DAYS = 120


# ---------------------------------------------------------------------------
# Via API (metricas completas)
# ---------------------------------------------------------------------------
def _clients():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None, refresh_token=YT_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YT_CLIENT_ID, client_secret=YT_CLIENT_SECRET,
        scopes=READ_SCOPES,
    )
    creds.refresh(Request())
    return (build("youtube", "v3", credentials=creds, cache_discovery=False),
            build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False))


def fetch_api_stats(video_ids: list[str], since: str) -> dict[str, dict]:
    youtube, yta = _clients()
    stats: dict[str, dict] = {}

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = youtube.videos().list(part="statistics", id=",".join(batch)).execute()
        for item in resp.get("items", []):
            s = item.get("statistics", {})
            stats[item["id"]] = {
                "views":    int(s.get("viewCount", 0)),
                "likes":    int(s.get("likeCount", 0)),
                "comments": int(s.get("commentCount", 0)),
            }

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for i in range(0, len(video_ids), 200):
        batch = video_ids[i:i + 200]
        try:
            report = yta.reports().query(
                ids="channel==MINE", startDate=since, endDate=today,
                metrics="views,averageViewDuration,averageViewPercentage,subscribersGained",
                dimensions="video", filters="video==" + ",".join(batch),
                maxResults=200,
            ).execute()
        except Exception as e:
            print(f"   ! Analytics API fallo: {e}")
            break
        for vid, _views, avg_dur, avg_pct, subs in report.get("rows", []):
            stats.setdefault(vid, {}).update({
                "avg_view_sec": round(float(avg_dur), 1),
                "avg_view_pct": round(float(avg_pct), 1),
                "subs_gained":  int(subs),
            })
    return stats


# ---------------------------------------------------------------------------
# Sin permisos: visitas publicas de la pagina del canal
# ---------------------------------------------------------------------------
def fetch_public_views() -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for tab in ("videos", "shorts"):
        try:
            html = requests.get(
                f"https://www.youtube.com/{CHANNEL_HANDLE}/{tab}",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130",
                         "Accept-Language": "en-US"},
                cookies={"CONSENT": "YES+1", "SOCS": "CAI"},
                timeout=30,
            ).text
        except requests.RequestException as e:
            print(f"   ! no pude leer /{tab}: {e}")
            continue
        # Cada video aparece como "videoId":"XXXXXXXXXXX" seguido (mas adelante) de "N views"
        for m in re.finditer(r'"(?:videoId|contentId)":"([\w-]{11})"', html):
            vid = m.group(1)
            if vid in stats:
                continue
            window = html[m.end():m.end() + 9000]
            # Largos: "label":"5 views" | Shorts: "accessibilityText":"titulo, 5 views - play Short"
            v = re.search(r'"(?:accessibilityLabel|label|accessibilityText)":"(?:[^"]*?, )?([\d,\.]+[KM]?|No) views?', window)
            if not v:
                continue
            raw = v.group(1)
            if raw == "No":
                views = 0
            else:
                mult = 1000 if raw.endswith("K") else 1_000_000 if raw.endswith("M") else 1
                views = int(float(raw.rstrip("KM").replace(",", "")) * mult)
            stats[vid] = {"views": views}
    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print("\n=== ANALYTICS (aprender de los resultados) ===\n")
    history = ch.load_history()
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    ids = []
    for v in history:
        if v.get("date", "") >= since:
            ids += [v.get("video_id")] + list(v.get("shorts_ids", []))
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        print("   . historial vacio, nada que medir")
        return

    stats, source = {}, ""
    if all([YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN]):
        try:
            stats, source = fetch_api_stats(ids, since), "api"
        except Exception as e:
            print(f"   ! Sin permisos de lectura en el token ({str(e)[:120]}).")
            print("     Genera uno nuevo con get_refresh_token.py para metricas completas.")
    if not stats:
        stats, source = fetch_public_views(), "public"

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updated = 0
    for v in history:
        s = stats.get(v.get("video_id"))
        if s:
            v["stats"] = {**v.get("stats", {}), **s, "updated": now, "source": source}
            updated += 1
        shorts_views = [stats[i]["views"] for i in v.get("shorts_ids", []) if i in stats]
        if shorts_views:
            v.setdefault("stats", {})["shorts_views"] = sum(shorts_views)
    ch.save_history(history)

    print(f"   . metricas ({source}) actualizadas en {updated} videos")
    print("\n" + ch.performance_report(history))


if __name__ == "__main__":
    main()
