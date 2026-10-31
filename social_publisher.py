"""
================================================================================
 social_publisher.py - Publica el siguiente Short en YouTube, Instagram y TikTok
================================================================================

 Lo ejecuta el workflow social.yml 3 veces al dia (15:00, 19:00, 23:00 UTC =
 11:00, 15:00, 19:00 en Nueva York), despues del video largo de las 14:00.

 En cada ejecucion:
   1. Coge el Short mas antiguo pendiente de history/shorts_queue.json
      (si se acumulan mas de BACKLOG_EXTRA, publica uno extra)
   2. YouTube Shorts (publico) + comentario con el enlace al video completo
   3. Instagram Reels (automatico)
   4. TikTok (borrador en la bandeja, o directo si TIKTOK_MODE=direct)
   5. Cuando todas las plataformas estan hechas, borra el MP4 de la cola
   6. Publica tambien los comentarios pendientes de los videos largos que
      ya son publicos (antes era un workflow aparte; asi se ahorran minutos)

 Una plataforma que falla se reintenta en la siguiente ejecucion (max 3).
 Una plataforma sin credenciales se marca "disabled" y no bloquea la cola.
================================================================================
"""

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import content_history as ch
import engagement
import instagram
import notify
import shorts_queue as sq
import tiktok

load_dotenv()

BACKLOG_EXTRA = 6   # si hay mas pendientes que esto, se publica uno extra en esta ejecucion
SUMMARY = os.getenv("GITHUB_STEP_SUMMARY")
TELEGRAM: list[str] = []   # lineas del aviso de Telegram de esta ejecucion


def _summary(line: str) -> None:
    print(line)
    if SUMMARY:
        with open(SUMMARY, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _mark(item: dict, platform: str, ok: bool, value: str = "") -> None:
    st = item["platforms"][platform]
    if ok:
        st.update({"status": "done", "id": value, "at": datetime.now(timezone.utc).isoformat(timespec="minutes")})
        return
    st["attempts"] = st.get("attempts", 0) + 1
    st["last_error"] = value[:300]
    if st["attempts"] >= sq.MAX_ATTEMPTS:
        st["status"] = "failed"


# ---------------------------------------------------------------------------
# Plataformas
# ---------------------------------------------------------------------------
def post_youtube(item: dict, video: Path, all_items: list[dict]) -> None:
    import uploader as up
    youtube = up.get_youtube_client()
    history = ch.load_history()
    taken = [{"title": t, "date": v.get("date", "")} for v in history for t in [v.get("title", "")] + v.get("shorts_titles", [])]
    taken += [{"title": i["title"], "date": i["date"]} for i in all_items
              if i is not item and i["platforms"]["youtube"].get("status") == "done"]
    meta = up.build_short_metadata(
        {"title": item["title"], "text": item["text"]},
        {"youtube_title": item["parent_title"], "tags": item.get("tags", [])},
        int(item["id"].rsplit("_s", 1)[-1]) if "_s" in item["id"] else 1, taken, None)
    meta["snippet"]["description"] += f"\n\nFull video: {item['parent_url']}"
    vid = up.upload_video(youtube, video, meta, label="SHORT")
    item["youtube_title"] = meta["snippet"]["title"].replace(" #Shorts", "")
    _mark(item, "youtube", True, vid)
    _summary(f"- YouTube: https://youtube.com/shorts/{vid}")
    TELEGRAM.append(f"▶️ YouTube: https://youtube.com/shorts/{vid}")

    yt_manage = engagement.client()
    if yt_manage:
        try:
            engagement.post_comment(yt_manage, vid, f"Full breakdown with all the data: {item['parent_url']}"
                                                    f"\n\n{engagement.TELEGRAM_CTA}")
        except Exception as e:
            print(f"   ! comentario en el Short fallo: {e}")


def post_instagram(item: dict, video: Path) -> None:
    media_id = instagram.publish_reel(video, sq.caption_instagram(item))
    _mark(item, "instagram", True, media_id)
    link = instagram.permalink(media_id)
    _summary(f"- Instagram: Reel publicado {link or '(id ' + media_id + ')'}")
    TELEGRAM.append(f"📸 Instagram: {link or 'Reel publicado'}")


def post_tiktok(item: dict, video: Path) -> None:
    caption = sq.caption_tiktok(item)
    publish_id = tiktok.upload(video, caption)
    _mark(item, "tiktok", True, publish_id)
    if tiktok.MODE == "direct":
        _summary("- TikTok: publicado")
        TELEGRAM.append("🎵 TikTok: publicado")
    else:
        TELEGRAM.append("🎵 TikTok: <b>borrador en tu bandeja</b>. Texto para pegar:\n"
                        f"<code>{notify.esc(caption)}</code>")
        _summary("- TikTok: **borrador enviado a tu bandeja**. Abre TikTok > notificaciones/borradores, "
                 "pega este texto y publica:")
        _summary(f"\n  ```\n  {caption}\n  ```\n")


PLATFORM_CHECKS = {
    "youtube":   lambda: all(os.getenv(k) for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")),
    "instagram": instagram.enabled,
    "tiktok":    tiktok.enabled,
}


def publish_item(item: dict, all_items: list[dict]) -> None:
    _summary(f"\n### {item['title']}\n")
    todo = []
    for p in sq.PLATFORMS:
        if item["platforms"][p]["status"] != "pending":
            continue
        if not PLATFORM_CHECKS[p]():
            item["platforms"][p]["status"] = "disabled"
            print(f"   . {p}: sin credenciales, desactivado")
        else:
            todo.append(p)
    if not todo:
        return

    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / f"{item['id']}.mp4"
        try:
            sq.download_asset(item["asset_id"], video)
        except Exception as e:
            print(f"   ! no pude descargar el Short de la cola: {e}")
            for p in todo:
                _mark(item, p, False, f"descarga: {e}")
            return
        for p in todo:
            try:
                {"youtube": lambda: post_youtube(item, video, all_items),
                 "instagram": lambda: post_instagram(item, video),
                 "tiktok": lambda: post_tiktok(item, video)}[p]()
            except Exception as e:
                _mark(item, p, False, str(e))
                _summary(f"- {p}: **fallo** ({str(e)[:150]}), se reintenta en la siguiente ejecucion")
                final = item["platforms"][p]["status"] == "failed"
                TELEGRAM.append(f"⚠️ {p}: fallo ({notify.esc(str(e)[:120])}). "
                                + ("Agotados los reintentos." if final else "Se reintenta en la siguiente franja."))

    if sq.is_finished(item):
        try:
            sq.delete_asset(item["asset_id"])
        except Exception as e:
            print(f"   ! no pude borrar el MP4 de la cola: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print(f"\n=== SOCIAL PUBLISHER . {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC ===\n")
    items = sq.load_queue()
    pending = sq.pending_items(items)
    _summary(f"## Shorts en cola: {len(pending)}")

    # Reintentos (Shorts ya publicados en alguna red) aparte: no deben gastar la franja del Short nuevo
    retries = [i for i in pending if any(i["platforms"][p]["status"] == "done" for p in sq.PLATFORMS)]
    fresh = [i for i in pending if i not in retries]
    batch = retries + (fresh[:2] if len(fresh) > BACKLOG_EXTRA else fresh[:1])
    for item in batch:
        TELEGRAM.clear()
        publish_item(item, items)
        sq.save_queue(items)   # guardar tras cada Short: si algo falla despues, no se repite
        if TELEGRAM:
            left = len(sq.pending_items(items))
            head = "🔁 <b>Reintento</b>" if item in retries else "📱 <b>Short publicado</b>"
            notify.send(f"{head}: {notify.esc(item.get('youtube_title') or item['title'])}\n"
                        + "\n".join(TELEGRAM) + f"\n\n🗂 Quedan {left} en cola")
    if not batch and notify.enabled() and datetime.now(timezone.utc).hour == 23:
        notify.send("⚠️ <b>La cola de Shorts esta vacia</b>: hoy no ha llegado ningun Short nuevo. "
                    "Revisa si el pipeline diario fallo.")

    # Comentarios pendientes de los videos largos ya publicados
    yt_manage = engagement.client()
    if yt_manage:
        history = ch.load_history()
        before = sum(len(v.get("cta_done", [])) for v in history)
        changed = engagement.post_pending_comments(yt_manage, history)
        engagement.add_cta_to_descriptions(yt_manage, history)
        if changed or sum(len(v.get("cta_done", [])) for v in history) != before:
            ch.save_history(history)


if __name__ == "__main__":
    main()
