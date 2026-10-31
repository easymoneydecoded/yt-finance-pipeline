"""
================================================================================
 notify.py - Avisos por Telegram
================================================================================

 Envia un mensaje a tu Telegram cuando:
   - termina el pipeline diario (miniatura + titulo + enlace + nota del juez),
     o falla (con la etapa que fallo y el enlace al log)
   - social_publisher.py publica un Short (enlaces + texto para TikTok)

 Secrets: TELEGRAM_BOT_TOKEN (de @BotFather) y TELEGRAM_CHAT_ID (sale con
 get_telegram_chat_id.py). Sin ellos no se envia nada y nada se rompe.

 Uso desde el workflow:  python notify.py pipeline
================================================================================
"""

import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
API       = f"https://api.telegram.org/bot{BOT_TOKEN}"
TODAY     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
DATA      = Path("data")


def enabled() -> bool:
    return bool(BOT_TOKEN and CHAT_ID)


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def run_url() -> str:
    server, repo, run = os.getenv("GITHUB_SERVER_URL"), os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_RUN_ID")
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else ""


def send(text: str, photo: Path | None = None) -> bool:
    """Envia texto (HTML de Telegram) o foto con pie. Nunca lanza excepcion."""
    if not enabled():
        print("   . Telegram no configurado, no envio aviso")
        return False
    try:
        if photo and photo.exists() and len(text) <= 1024:
            with open(photo, "rb") as fh:
                r = requests.post(f"{API}/sendPhoto", timeout=60, files={"photo": fh},
                                  data={"chat_id": CHAT_ID, "caption": text, "parse_mode": "HTML"})
        else:
            r = requests.post(f"{API}/sendMessage", timeout=30, data={
                "chat_id": CHAT_ID, "text": text[:4000], "parse_mode": "HTML",
                "disable_web_page_preview": "true"})
        if not r.ok:
            print(f"   ! Telegram respondio {r.status_code}: {r.text[:200]}")
        return r.ok
    except requests.RequestException as e:
        print(f"   ! no pude enviar el aviso de Telegram: {e}")
        return False


def send_video(video: Path, caption: str) -> bool:
    """Envia el MP4 (para publicarlo a mano desde el movil, p. ej. en TikTok). Nunca lanza excepcion."""
    if not enabled() or not video.exists():
        return False
    try:
        with open(video, "rb") as fh:
            r = requests.post(f"{API}/sendVideo", timeout=300, files={"video": fh}, data={
                "chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML",
                "supports_streaming": "true"})
        if not r.ok:
            print(f"   ! Telegram (video) respondio {r.status_code}: {r.text[:200]}")
        return r.ok
    except requests.RequestException as e:
        print(f"   ! no pude enviar el video por Telegram: {e}")
        return False


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Resumen del pipeline diario
# ---------------------------------------------------------------------------
STAGES = [
    (DATA / f"ideas_{TODAY}.json",                        "1 · Ideas"),
    (DATA / f"script_{TODAY}.json",                       "2 · Guion (o el juez no aprobo ningun guion)"),
    (DATA / "audio" / f"audio_manifest_{TODAY}.json",     "3 · Voz"),
    (DATA / "broll" / f"broll_manifest_{TODAY}.json",     "4 · B-roll"),
    (DATA / "output" / f"video_{TODAY}.mp4",              "5 · Montaje del video"),
    (DATA / "output" / f"shorts_manifest_{TODAY}.json",   "6 · Shorts"),
    (DATA / "output" / f"thumbnail_meta_{TODAY}.json",    "7 · Miniatura"),
    (DATA / "output" / f"uploaded_{TODAY}.json",          "8 · Subida a YouTube"),
]


def failed_stage(dry_run: bool) -> str:
    stages = STAGES[:-1] if dry_run else STAGES
    for path, name in stages:
        if not path.exists():
            return name
    return "despues de la subida (historial o artefactos)"


def pipeline_message() -> tuple[str, Path | None]:
    status = os.getenv("JOB_STATUS", "success")
    dry_run = os.getenv("DRY_RUN", "false") == "true"
    script = _load(DATA / f"script_{TODAY}.json")
    uploaded = _load(DATA / "output" / f"uploaded_{TODAY}.json")
    thumb = next(iter(sorted((DATA / "output").glob(f"thumbnail_{TODAY}.*"))), None) if (DATA / "output").exists() else None
    link = run_url()

    if status != "success":
        lines = ["❌ <b>Fallo el pipeline diario</b>",
                 f"Etapa: <b>{esc(failed_stage(dry_run))}</b>"]
        if script.get("youtube_title"):
            lines.append(f"Video: {esc(script['youtube_title'])}")
        lines.append("Hoy no se ha publicado video." if not uploaded.get("long_video") else
                     "El video SI se subio; el fallo fue despues.")
        if link:
            lines.append(f'<a href="{link}">Ver el log en GitHub</a>')
        return "\n".join(lines), None

    quality = script.get("quality", {}).get("overall")
    shorts = len(_load(DATA / "output" / f"shorts_manifest_{TODAY}.json").get("shorts", []))
    if dry_run:
        lines = ["🧪 <b>Prueba completada (no se ha subido nada)</b>",
                 f"🎬 {esc(script.get('youtube_title'))}",
                 f"⭐ Calidad: {quality}/10 · {shorts} Shorts",
                 f'<a href="{link}">Descargar los videos (artefacto)</a>' if link else ""]
        return "\n".join(l for l in lines if l), thumb

    long_video = uploaded.get("long_video")
    if not long_video:
        return ("ℹ️ <b>Pipeline terminado sin subir video</b>\n"
                "Hoy ya habia un video publicado (proteccion contra duplicados).", None)

    when = long_video.get("publish_at", "")
    try:
        dt = datetime.fromisoformat(when)
        when_txt = f"{dt:%d/%m %H:%M} UTC" if dt > datetime.now(timezone.utc) else "ya publicado"
    except ValueError:
        when_txt = when or "?"
    lines = ["✅ <b>Video del dia listo</b>",
             f"🎬 <b>{esc(long_video.get('title'))}</b>",
             f"🕑 Publicacion: {when_txt}",
             f"⭐ Calidad del guion: {quality}/10",
             f"📱 {shorts} Shorts en cola (salen a las 15:07, 19:07 y 23:07 UTC)"]
    if long_video.get("playlist"):
        lines.append(f"📂 Lista: {esc(long_video['playlist'])}")
    if uploaded.get("errors"):
        lines.append("⚠️ Avisos: " + esc("; ".join(uploaded["errors"])[:300]))
    lines.append(f'▶️ <a href="{long_video.get("url")}">{esc(long_video.get("url"))}</a>')
    return "\n".join(lines), thumb


if __name__ == "__main__":
    if sys.argv[1:2] == ["pipeline"]:
        text, photo = pipeline_message()
        send(text, photo)
    elif sys.argv[1:2] == ["social_failed"]:
        send(f"❌ <b>Fallo la publicacion de Shorts</b>\n<a href=\"{run_url()}\">Ver el log en GitHub</a>")
    elif sys.argv[1:2] == ["test"]:
        ok = send("👋 <b>Ticker Press</b>: los avisos de Telegram funcionan.")
        print("Mensaje enviado" if ok else "No se pudo enviar")
        sys.exit(0 if ok else 1)
    else:
        print("Uso: python notify.py pipeline | social_failed | test")
