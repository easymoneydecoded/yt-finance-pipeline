"""
================================================================================
 uploader.py - Etapa 8 del canal de YouTube automatico (v2)
================================================================================

 Que hace:
   1. Lee el guion, el video largo, el manifest de Shorts y el thumbnail
   2. Se autentica en YouTube Data API v3 usando el refresh token
   3. Sube el video largo con metadata + establece thumbnail custom
   4. Sube los 3 Shorts como Shorts (usando #Shorts en la descripcion)
   5. Guarda un manifest final con los video_id de YouTube
   6. Registra el video en history/videos.json (memoria anti-repeticion)

 Programacion (las primeras horas deciden si YouTube empuja el video):
   - Video largo: PUBLISH_HOUR_UTC (14:00 UTC = 10:00 Nueva York)
   - Shorts: se encolan y los publica social_publisher.py a las 15/19/23 UTC
     en YouTube, Instagram y TikTok (la cuota de YouTube no da para todo el mismo dia)
   - Capitulos automaticos en la descripcion (timeline del editor)
   - Lista de reproduccion por tema + comentario del canal (engagement.py)

 Protecciones:
   - Si hoy ya se subio un video (re-ejecucion del workflow), no sube otro
   - Si el titulo/miniatura repite un video anterior, aborta en vez de publicar
   - Los titulos de Shorts tampoco pueden repetir titulos ya publicados

 Cuota YouTube Data API: upload=1600, thumbnail=50. Total: 4 uploads + 1 thumb = 6450/10000.

================================================================================
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

import content_history as ch
import engagement
import shorts_queue

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
INPUT_DIR      = Path("data")
OUTPUT_DIR     = Path("data/output")

TODAY          = datetime.now(timezone.utc).strftime("%Y-%m-%d")

YT_CLIENT_ID     = os.getenv("YOUTUBE_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN")

YT_TOKEN_URI     = "https://oauth2.googleapis.com/token"
YT_SCOPES        = ["https://www.googleapis.com/auth/youtube.upload"]

CATEGORY_ID      = "27"  # Education

MADE_FOR_KIDS    = False

PUBLISH_HOUR_UTC = int(os.getenv("PUBLISH_HOUR_UTC", "14"))
LATE_CUTOFF_UTC  = 23                   # hasta las 18:00 de Nueva York se publica ya; despues, manana a las 14:00
MIN_LEAD_MIN     = 20                   # YouTube necesita margen para procesar

HASHTAGS = "#finance #investing #money"


# ---------------------------------------------------------------------------
# Programacion y capitulos
# ---------------------------------------------------------------------------
def long_publish_time(now: datetime | None = None) -> datetime | None:
    """
    Hora de publicacion del video largo; None = publicar ya.
      - antes de las 14:00 UTC       -> programado a las 14:00 de hoy
      - entre las 14:00 y las 23:00  -> ya (sigue siendo buena hora en EE. UU.)
      - despues de las 23:00 UTC     -> programado a las 14:00 de manana
    (GitHub puede retrasar las ejecuciones programadas cuando esta saturado)
    """
    now = now or datetime.now(timezone.utc)
    slot = now.replace(hour=PUBLISH_HOUR_UTC, minute=0, second=0, microsecond=0)
    if slot - now >= timedelta(minutes=MIN_LEAD_MIN):
        return slot
    if now.hour < LATE_CUTOFF_UTC:
        return None
    return slot + timedelta(days=1)


def status_block(publish_at: datetime | None) -> dict:
    status = {
        "privacyStatus":           "private" if publish_at else "public",
        "selfDeclaredMadeForKids": MADE_FOR_KIDS,
        "embeddable":              True,
        "publicStatsViewable":     True,
    }
    if publish_at:
        status["publishAt"] = publish_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return status


def _ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def build_chapters(timeline: dict) -> str:
    """Capitulos de YouTube: empiezan en 0:00, minimo 3, cada uno >= 10s."""
    chapters = [(0.0, "Intro")]
    for sec in timeline.get("sections", []):
        if sec.get("type") == "body" and sec.get("title"):
            chapters.append((sec["start"], sec["title"]))
        elif sec.get("type") == "outro":
            chapters.append((sec["start"], "The bottom line"))
    clean = [chapters[0]]
    for start, title in chapters[1:]:
        if start - clean[-1][0] >= 10:
            clean.append((start, title.strip()[:60]))
    if len(clean) < 3:
        return ""
    return "Chapters:\n" + "\n".join(f"{_ts(st)} {t}" for st, t in clean)


# ---------------------------------------------------------------------------
# Carga de datos
# ---------------------------------------------------------------------------
def load_data() -> tuple[dict, dict, Path, Path]:
    """Carga script, shorts manifest y localiza video largo + thumbnail."""
    def _latest(pattern: str, base: Path = INPUT_DIR) -> Path:
        candidates = sorted(base.rglob(pattern), reverse=True)
        if not candidates:
            raise FileNotFoundError(f"No se encontro {pattern}")
        return candidates[0]

    script_path = INPUT_DIR / f"script_{TODAY}.json"
    if not script_path.exists():
        script_path = _latest("script_*.json")

    shorts_manifest_path = OUTPUT_DIR / f"shorts_manifest_{TODAY}.json"
    shorts_manifest = None
    if shorts_manifest_path.exists():
        shorts_manifest = json.loads(shorts_manifest_path.read_text(encoding="utf-8"))
    else:
        candidates = sorted(OUTPUT_DIR.glob("shorts_manifest_*.json"), reverse=True)
        if candidates:
            shorts_manifest = json.loads(candidates[0].read_text(encoding="utf-8"))

    video_path = OUTPUT_DIR / f"video_{TODAY}.mp4"
    if not video_path.exists():
        candidates = sorted(OUTPUT_DIR.glob("video_*.mp4"), reverse=True)
        candidates = [c for c in candidates if not c.name.startswith("video_clean")]
        if not candidates:
            raise FileNotFoundError("No hay video_*.mp4 en output/")
        video_path = candidates[0]

    # Thumbnail (opcional pero recomendada) - puede ser PNG o JPG
    thumb_path = OUTPUT_DIR / f"thumbnail_{TODAY}.png"
    if not thumb_path.exists():
        thumb_path = OUTPUT_DIR / f"thumbnail_{TODAY}.jpg"
    if not thumb_path.exists():
        candidates = sorted(
            list(OUTPUT_DIR.glob("thumbnail_*.png")) + list(OUTPUT_DIR.glob("thumbnail_*.jpg")),
            reverse=True,
        )
        thumb_path = candidates[0] if candidates else None

    print(f"   . script:    {script_path.name}")
    print(f"   . video:     {video_path.name}")
    if shorts_manifest:
        print(f"   . shorts:    {len(shorts_manifest.get('shorts', []))} clips")
    if thumb_path and thumb_path.exists():
        print(f"   . thumbnail: {thumb_path.name}")
    else:
        print(f"   ! sin thumbnail custom, YouTube usara frame automatico")

    return (
        json.loads(script_path.read_text(encoding="utf-8")),
        shorts_manifest,
        video_path,
        thumb_path,
    )


def load_optional_json(pattern_today: Path, glob: str, base: Path) -> dict:
    if pattern_today.exists():
        return json.loads(pattern_today.read_text(encoding="utf-8"))
    candidates = sorted(base.glob(glob), reverse=True)
    return json.loads(candidates[0].read_text(encoding="utf-8")) if candidates else {}


def build_history_entry(script_data: dict, long_video: dict, shorts: list[dict]) -> dict:
    idea   = script_data.get("source_idea", {})
    thumb  = load_optional_json(OUTPUT_DIR / f"thumbnail_meta_{TODAY}.json", "thumbnail_meta_*.json", OUTPUT_DIR)
    broll  = load_optional_json(INPUT_DIR / "broll" / f"broll_manifest_{TODAY}.json", "broll_manifest_*.json", INPUT_DIR / "broll")
    broll_ids = [c.get("pexels_id") for sec in broll.get("sections", []) for c in sec.get("clips", []) if c.get("pexels_id")]
    return {
        "date":             TODAY,
        "video_id":         long_video["video_id"],
        "url":              long_video["url"],
        "title":            long_video["title"],
        "thumbnail_text":   script_data.get("thumbnail_text", ""),
        "topic_key":        script_data.get("topic_key") or idea.get("topic_key", ""),
        "entities":         script_data.get("entities") or idea.get("entities", []),
        "format":           script_data.get("format") or idea.get("format", ""),
        "title_style":      script_data.get("title_style", ""),
        "keywords":         idea.get("target_keywords", []),
        "source_headlines": idea.get("source_signals", []),
        "source_urls":      idea.get("source_urls", []),
        "thumb_layout":     thumb.get("thumb_layout", ""),
        "thumb_color":      thumb.get("thumb_color", ""),
        "thumb_pexels_id":  thumb.get("thumb_pexels_id"),
        "broll_pexels_ids": broll_ids,
        "shorts_titles":    [s["title"].replace(" #Shorts", "") for s in shorts],
        "shorts_ids":       [s["video_id"] for s in shorts],
        "publish_at":       long_video.get("publish_at", ""),
        "shorts_publish_at": [s.get("publish_at", "") for s in shorts],
        "pinned_comment":   script_data.get("pinned_comment", ""),
        "playlist":         long_video.get("playlist", script_data.get("playlist", "")),
        "quality":          script_data.get("quality", {}).get("overall"),
        "voice":            load_optional_json(INPUT_DIR / "audio" / f"audio_manifest_{TODAY}.json",
                                               "audio_manifest_*.json", INPUT_DIR / "audio").get("voice", ""),
    }


# ---------------------------------------------------------------------------
# Autenticacion
# ---------------------------------------------------------------------------
def get_youtube_client():
    if not all([YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN]):
        raise RuntimeError("Faltan credenciales YOUTUBE_*")

    credentials = Credentials(
        token          = None,
        refresh_token  = YT_REFRESH_TOKEN,
        token_uri      = YT_TOKEN_URI,
        client_id      = YT_CLIENT_ID,
        client_secret  = YT_CLIENT_SECRET,
        scopes         = YT_SCOPES,
    )
    credentials.refresh(Request())
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


# ---------------------------------------------------------------------------
# Metadata builders
# ---------------------------------------------------------------------------
def build_long_video_metadata(script_data: dict, publish_at: datetime | None = None,
                              timeline: dict | None = None) -> dict:
    title       = script_data.get("youtube_title", "Untitled")[:100]
    chapters    = build_chapters(timeline or {})
    description = "\n\n".join(x for x in (
        script_data.get("description", "").strip(),
        engagement.ctas(script_data.get("playlist", ""), title),
        chapters,
        "Data: Yahoo Finance, FRED (Federal Reserve Bank of St. Louis) and the news sources cited.",
        HASHTAGS,
    ) if x)[:4900]
    tags        = script_data.get("tags", [])[:25]

    return {
        "snippet": {
            "title":       title,
            "description": description,
            "tags":        tags,
            "categoryId":  CATEGORY_ID,
            "defaultLanguage":      "en",
            "defaultAudioLanguage": "en",
        },
        "status": {
        },
        "status": status_block(publish_at),
    }


def build_short_metadata(short_info: dict, script_data: dict, short_index: int,
                         taken: list[dict], publish_at: datetime | None = None) -> dict:
    parent_title  = script_data.get("youtube_title", "Untitled")
    own_title     = short_info.get("title", "").strip()
    section_title = short_info.get("section_title", "").strip()
    alt_hook      = short_info.get("alt_hook", "").strip()

    # Primer candidato que no repita ningun titulo publicado (ni de este mismo lote)
    candidates = [c for c in (own_title, alt_hook, section_title) if c]
    candidates.append(f"{parent_title} - Part {short_index}")
    short_title = candidates[-1]
    for c in candidates:
        if not ch.find_duplicate({"title": c}, taken, TODAY):
            short_title = c
            break
    taken.append({"title": short_title, "date": TODAY})

    short_title = f"{short_title} #Shorts"[:100]

    first_line = (short_info.get("text", "").split(". ")[0] or section_title).strip()
    description = (
        f"{first_line}.\n\n"
        f"Full breakdown: {parent_title}\n\n"
        f"{engagement.ctas(script_data.get('playlist', ''), parent_title)}\n\n"
        f"Education, not financial advice.\n\n"
        f"#Shorts {HASHTAGS}"
    )

    tags = script_data.get("tags", [])[:15]

    return {
        "snippet": {
            "title":       short_title,
            "description": description,
            "tags":        tags,
            "categoryId":  CATEGORY_ID,
            "defaultLanguage":      "en",
            "defaultAudioLanguage": "en",
        },
        "status": status_block(publish_at),
    }


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
def upload_video(youtube, video_path: Path, metadata: dict, label: str = "") -> str:
    print(f"   . preparando upload: {video_path.name} ({video_path.stat().st_size/1024/1024:.1f}MB)")

    media = MediaFileUpload(
        str(video_path),
        mimetype    = "video/mp4",
        resumable   = True,
        chunksize   = 5 * 1024 * 1024,
    )

    request = youtube.videos().insert(
        part = "snippet,status",
        body = metadata,
        media_body = media,
    )

    response  = None
    error     = None
    retry     = 0
    max_retries = 5

    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                percent = int(status.progress() * 100)
                print(f"     upload {label}: {percent}%")
        except HttpError as e:
            if e.resp.status in [500, 502, 503, 504]:
                error = f"error servidor {e.resp.status}"
            else:
                raise
        except Exception as e:
            error = f"excepcion: {e}"

        if error is not None:
            retry += 1
            if retry > max_retries:
                raise RuntimeError(f"Upload fallo tras {max_retries} reintentos: {error}")
            wait = 2 ** retry
            print(f"     ! {error}, reintento {retry}/{max_retries} en {wait}s...")
            time.sleep(wait)
            error = None

    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"Upload no devolvio video_id. Response: {response}")

    return video_id


def set_video_thumbnail(youtube, video_id: str, thumbnail_path: Path) -> bool:
    """Sube y establece una thumbnail custom para el video."""
    try:
        mime = "image/png" if thumbnail_path.suffix.lower() == ".png" else "image/jpeg"
        media = MediaFileUpload(
            str(thumbnail_path),
            mimetype = mime,
            resumable = False,
        )
        youtube.thumbnails().set(
            videoId    = video_id,
            media_body = media,
        ).execute()
        return True
    except Exception as e:
        print(f"   ! fallo subiendo thumbnail: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    t0 = time.time()
    print(f"\n=== UPLOADER . {TODAY} ===\n")

    print("1/5  Cargando datos...")
    script_data, shorts_manifest, video_path, thumb_path = load_data()

    history = ch.load_history()
    done = ch.already_uploaded_on(TODAY, history)
    if done:
        print(f"\n  ! Hoy ya se subio un video ({done.get('url')}: '{done.get('title')}').")
        print("    No subo otro para no duplicar contenido. Fin.")
        return

    dup = ch.find_duplicate({
        "title":          script_data.get("youtube_title", ""),
        "thumbnail_text": script_data.get("thumbnail_text", ""),
        "topic_key":      script_data.get("topic_key", ""),
        "entities":       script_data.get("entities", []),
    }, history, TODAY)
    if dup:
        raise RuntimeError(
            f"ABORTADO: el video repite contenido ya publicado ({dup[1]}; "
            f"{dup[0].get('url', '')}). No se sube nada."
        )

    print("\n2/5  Autenticando en YouTube API...")
    youtube = get_youtube_client()
    print(f"      cliente creado")

    uploaded = {
        "date":         TODAY,
        "youtube_title": script_data.get("youtube_title", ""),
        "long_video":   None,
        "shorts":       [],
        "errors":       [],
    }

    # -----------------------------------------------------------------------
    # Video largo
    # -----------------------------------------------------------------------
    print(f"\n3/5  Subiendo video largo...")
    publish_at = long_publish_time()
    timeline = load_optional_json(OUTPUT_DIR / f"timeline_{TODAY}.json", "timeline_*.json", OUTPUT_DIR)
    long_metadata = build_long_video_metadata(script_data, publish_at, timeline)
    print(f"      titulo: {long_metadata['snippet']['title']}")
    print(f"      publicacion: {publish_at.isoformat() if publish_at else 'inmediata'}")
    try:
        long_video_id = upload_video(youtube, video_path, long_metadata, label="LONG")
        long_video_url = f"https://youtu.be/{long_video_id}"
        print(f"      -> {long_video_url}")

        uploaded["long_video"] = {
            "video_id":  long_video_id,
            "url":       long_video_url,
            "title":     long_metadata["snippet"]["title"],
            "filename":  video_path.name,
            "publish_at": (publish_at or datetime.now(timezone.utc)).isoformat(timespec="minutes"),
        }
    except Exception as e:
        error_msg = f"Fallo subiendo video largo: {e}"
        print(f"      ! {error_msg}")
        uploaded["errors"].append(error_msg)
        raise

    # Registrar ya en el historial: aunque fallen los Shorts, el tema queda marcado
    ch.append_video(build_history_entry(script_data, uploaded["long_video"], []))

    # -----------------------------------------------------------------------
    # Thumbnail custom
    # -----------------------------------------------------------------------
    print(f"\n4/5  Estableciendo thumbnail custom...")
    if thumb_path and thumb_path.exists():
        ok = set_video_thumbnail(youtube, long_video_id, thumb_path)
        if ok:
            print(f"      thumbnail aplicada: {thumb_path.name}")
            uploaded["long_video"]["thumbnail"] = thumb_path.name
        else:
            uploaded["errors"].append("Fallo subir thumbnail")
    else:
        print(f"      ! sin thumbnail custom, YouTube usara frame automatico")

    # Lista de reproduccion del tema (necesita el permiso youtube.force-ssl)
    yt_manage = engagement.client()
    if yt_manage:
        added = engagement.add_to_playlist(yt_manage, long_video_id, script_data.get("playlist", ""))
        if added:
            uploaded["long_video"]["playlist"] = added
            print(f"      anadido a la lista: {added}")

    # -----------------------------------------------------------------------
    # Shorts
    # -----------------------------------------------------------------------
    print(f"\n5/5  Encolando Shorts (los publica social_publisher.py 3 veces al dia)...")
    if not shorts_manifest or not shorts_manifest.get("shorts"):
        print("      ! no hay Shorts para encolar")
    else:
        try:
            shorts_queue.enqueue(shorts_manifest["shorts"], OUTPUT_DIR,
                                 {**script_data, "date": TODAY}, uploaded["long_video"])
        except Exception as e:
            uploaded["errors"].append(f"Fallo encolando Shorts: {e}")
            print(f"      ! fallo encolando Shorts: {e}")

    ch.append_video(build_history_entry(script_data, uploaded["long_video"], uploaded["shorts"]))
    print(f"\n  Historial actualizado: {ch.HISTORY_FILE}")

    # -----------------------------------------------------------------------
    # Guardar manifest
    # -----------------------------------------------------------------------
    manifest_path = OUTPUT_DIR / f"uploaded_{TODAY}.json"
    manifest_path.write_text(json.dumps(uploaded, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n  Listo en {time.time()-t0:.1f}s")
    print(f"     Manifest: {manifest_path}")

    print(f"\n=== RESUMEN ===")
    if uploaded["long_video"]:
        print(f"  Video largo: {uploaded['long_video']['url']}")
        if uploaded["long_video"].get("thumbnail"):
            print(f"  Thumbnail:   {uploaded['long_video']['thumbnail']}")
    print(f"  Shorts subidos: {len(uploaded['shorts'])}")
    for s in uploaded["shorts"]:
        print(f"    - {s['url']}")
    if uploaded["errors"]:
        print(f"  ! Errores: {len(uploaded['errors'])}")
        for e in uploaded["errors"]:
            print(f"    - {e}")
    print()


if __name__ == "__main__":
    main()
