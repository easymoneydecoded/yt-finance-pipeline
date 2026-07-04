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

 Cuota YouTube Data API: upload=1600, thumbnail=50. Total: 4 uploads + 1 thumb = 6450/10000.

================================================================================
"""

import os
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

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
YT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

CATEGORY_ID      = "27"  # Education

PRIVACY_STATUS   = "public"
MADE_FOR_KIDS    = False
NOTIFY_SUBS      = True


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

    # Thumbnail (opcional pero recomendada)
    thumb_path = OUTPUT_DIR / f"thumbnail_{TODAY}.jpg"
    if not thumb_path.exists():
        candidates = sorted(OUTPUT_DIR.glob("thumbnail_*.jpg"), reverse=True)
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
def build_long_video_metadata(script_data: dict) -> dict:
    title       = script_data.get("youtube_title", "Untitled")[:100]
    description = script_data.get("description", "")
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
            "privacyStatus":           PRIVACY_STATUS,
            "selfDeclaredMadeForKids": MADE_FOR_KIDS,
            "embeddable":              True,
            "publicStatsViewable":     True,
        },
    }


def build_short_metadata(short_info: dict, script_data: dict, short_index: int) -> dict:
    parent_title  = script_data.get("youtube_title", "Untitled")
    section_title = short_info.get("section_title", "")
    alt_hook      = short_info.get("alt_hook", "").strip()

    if alt_hook:
        short_title = f"{alt_hook} #Shorts"
    elif section_title:
        short_title = f"{section_title} #Shorts"
    else:
        short_title = f"{parent_title} - Part {short_index} #Shorts"

    short_title = short_title[:100]

    description = (
        f"{section_title}\n\n"
        f"Watch the full video for the deep breakdown.\n\n"
        f"This is for education, not financial advice.\n\n"
        f"#Shorts #Finance #Investing #Money"
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
        "status": {
            "privacyStatus":           PRIVACY_STATUS,
            "selfDeclaredMadeForKids": MADE_FOR_KIDS,
            "embeddable":              True,
            "publicStatsViewable":     True,
        },
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
        media = MediaFileUpload(
            str(thumbnail_path),
            mimetype = "image/jpeg",
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
    long_metadata = build_long_video_metadata(script_data)
    print(f"      titulo: {long_metadata['snippet']['title']}")
    try:
        long_video_id = upload_video(youtube, video_path, long_metadata, label="LONG")
        long_video_url = f"https://youtu.be/{long_video_id}"
        print(f"      -> {long_video_url}")

        uploaded["long_video"] = {
            "video_id":  long_video_id,
            "url":       long_video_url,
            "title":     long_metadata["snippet"]["title"],
            "filename":  video_path.name,
        }
    except Exception as e:
        error_msg = f"Fallo subiendo video largo: {e}"
        print(f"      ! {error_msg}")
        uploaded["errors"].append(error_msg)
        raise

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

    # -----------------------------------------------------------------------
    # Shorts
    # -----------------------------------------------------------------------
    print(f"\n5/5  Subiendo Shorts...")
    if not shorts_manifest or not shorts_manifest.get("shorts"):
        print("      ! no hay Shorts para subir")
    else:
        for i, short_info in enumerate(shorts_manifest["shorts"], 1):
            short_filename = short_info.get("filename")
            short_path = OUTPUT_DIR / short_filename
            if not short_path.exists():
                print(f"      ! Short {i} no existe: {short_path}")
                uploaded["errors"].append(f"Short {i} no encontrado: {short_filename}")
                continue

            metadata = build_short_metadata(short_info, script_data, i)
            metadata["snippet"]["description"] = (
                metadata["snippet"]["description"] +
                f"\n\nFull video: {uploaded['long_video']['url']}"
            )

            print(f"\n   Short {i}/{len(shorts_manifest['shorts'])}: {metadata['snippet']['title'][:60]}")
            try:
                short_id = upload_video(youtube, short_path, metadata, label=f"SHORT{i}")
                short_url = f"https://youtu.be/{short_id}"
                print(f"      -> {short_url}")

                uploaded["shorts"].append({
                    "video_id":  short_id,
                    "url":       short_url,
                    "title":     metadata["snippet"]["title"],
                    "filename":  short_filename,
                })

                if i < len(shorts_manifest["shorts"]):
                    print(f"      pausa 10s antes del siguiente...")
                    time.sleep(10)

            except Exception as e:
                error_msg = f"Fallo subiendo Short {i}: {e}"
                print(f"      ! {error_msg}")
                uploaded["errors"].append(error_msg)

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
