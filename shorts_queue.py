"""
================================================================================
 shorts_queue.py - Cola de Shorts para publicar ~3 al dia en todas las redes
================================================================================

 - El uploader (etapa 8) encola los Shorts del dia: el MP4 se guarda como
   asset del release privado "shorts-queue" del repo y la ficha en
   history/shorts_queue.json.
 - social_publisher.py (workflow social.yml, 3 veces al dia) publica el
   siguiente de la cola en YouTube, Instagram y TikTok y, cuando todas las
   plataformas estan hechas, borra el MP4 del release.

 Por que una cola: la API de YouTube da 10.000 unidades/dia y cada subida
 gasta 1.600 -> el video largo + 6 Shorts el mismo dia no caben.
================================================================================
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

QUEUE_FILE   = Path("history/shorts_queue.json")
RELEASE_TAG  = "shorts-queue"
PLATFORMS    = ("youtube", "instagram", "tiktok")
MAX_ATTEMPTS = 3
KEEP_DONE    = 120   # fichas ya publicadas que se conservan como registro

BASE_HASHTAGS = ["#finance", "#investing", "#money", "#stockmarket", "#personalfinance"]


# ---------------------------------------------------------------------------
# Fichero de la cola
# ---------------------------------------------------------------------------
def load_queue() -> list[dict]:
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8")).get("items", [])
    except (OSError, json.JSONDecodeError):
        return []


def save_queue(items: list[dict]) -> None:
    QUEUE_FILE.parent.mkdir(exist_ok=True)
    active = [i for i in items if not is_finished(i)]
    finished = [i for i in items if is_finished(i)][-KEEP_DONE:]
    QUEUE_FILE.write_text(json.dumps({
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pending": len(active),
        "items": finished + active,
    }, indent=2, ensure_ascii=False), encoding="utf-8")


def is_finished(item: dict) -> bool:
    return all(item["platforms"].get(p, {}).get("status") in ("done", "failed", "disabled") for p in PLATFORMS)


def pending_items(items: list[dict]) -> list[dict]:
    return sorted([i for i in items if not is_finished(i)], key=lambda i: i["created"])


# ---------------------------------------------------------------------------
# Textos para cada red
# ---------------------------------------------------------------------------
def _hashtags(tags: list[str], n: int) -> list[str]:
    out = []
    for t in tags:
        tag = "#" + re.sub(r"[^A-Za-z0-9]", "", t.title())
        if 3 < len(tag) <= 30 and tag.lower() not in {h.lower() for h in out + BASE_HASHTAGS}:
            out.append(tag)
    return (out[: max(0, n - 3)] + BASE_HASHTAGS)[:n]


def first_sentence(text: str) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", (text or "").strip())
    return (m.group(1) if m else (text or "")[:150]).strip()


def caption_instagram(item: dict) -> str:
    return "\n\n".join([
        item["title"],
        first_sentence(item["text"]),
        "Full breakdown with all the data on YouTube: Ticker Press (link in bio).",
        "📲 Free daily insider-buying alerts on Telegram: @tickerpress_insiders",
        "Education, not financial advice.",
        " ".join(_hashtags(item.get("tags", []), 5)),   # Instagram: maximo 5 hashtags por publicacion (2026)
    ])[:2200]


def caption_tiktok(item: dict) -> str:
    return f"{item['title']} {' '.join(_hashtags(item.get('tags', []), 5))}"[:2200]


# ---------------------------------------------------------------------------
# Release de GitHub como almacen de los MP4
# ---------------------------------------------------------------------------
def _gh():
    token, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        return None, None
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, repo


def _release(headers, repo) -> dict:
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{RELEASE_TAG}", headers=headers, timeout=30)
    if r.status_code == 200:
        return r.json()
    r = requests.post(f"https://api.github.com/repos/{repo}/releases", headers=headers, timeout=30, json={
        "tag_name": RELEASE_TAG, "name": "Cola de Shorts (automatica)", "prerelease": True,
        "body": "Almacen temporal de Shorts pendientes de publicar. Lo gestiona social_publisher.py: no tocar.",
    })
    r.raise_for_status()
    return r.json()


def upload_asset(path: Path, name: str) -> int:
    headers, repo = _gh()
    rel = _release(headers, repo)
    url = rel["upload_url"].split("{")[0]
    with open(path, "rb") as fh:
        r = requests.post(url, params={"name": name}, data=fh, timeout=600,
                          headers={**headers, "Content-Type": "video/mp4"})
    if r.status_code == 422:  # ya existe un asset con ese nombre (re-ejecucion): se sustituye
        for a in rel.get("assets", []):
            if a["name"] == name:
                delete_asset(a["id"])
        with open(path, "rb") as fh:
            r = requests.post(url, params={"name": name}, data=fh, timeout=600,
                              headers={**headers, "Content-Type": "video/mp4"})
    r.raise_for_status()
    return r.json()["id"]


def download_asset(asset_id: int, dest: Path) -> Path:
    headers, repo = _gh()
    with requests.get(f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}", stream=True, timeout=600,
                      headers={**headers, "Accept": "application/octet-stream"}) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    return dest


def signed_asset_url(asset_id: int) -> str:
    """URL de descarga temporal (unos minutos) del MP4, sin autenticacion: la usa Instagram
    para bajarse el video el mismo cuando la subida directa falla."""
    headers, repo = _gh()
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}", timeout=60,
                     headers={**headers, "Accept": "application/octet-stream"}, allow_redirects=False)
    if r.status_code not in (301, 302, 307) or "Location" not in r.headers:
        raise RuntimeError(f"GitHub no dio URL temporal del asset ({r.status_code})")
    return r.headers["Location"]


def delete_asset(asset_id: int) -> None:
    headers, repo = _gh()
    requests.delete(f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}", headers=headers, timeout=30)


# ---------------------------------------------------------------------------
# Encolar (lo llama uploader.py)
# ---------------------------------------------------------------------------
def enqueue(shorts: list[dict], output_dir: Path, script_data: dict, long_video: dict) -> int:
    headers, _ = _gh()
    if not headers:
        print("   ! sin GITHUB_TOKEN/GITHUB_REPOSITORY: no puedo encolar (solo funciona en GitHub Actions)")
        return 0
    items = load_queue()
    known = {i["id"] for i in items}
    date = script_data.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    added = 0
    for n, sh in enumerate(shorts, 1):
        item_id = f"{date}_s{n}"
        path = output_dir / sh.get("filename", "")
        if item_id in known or not path.exists():
            continue
        try:
            asset_id = upload_asset(path, f"{item_id}.mp4")
        except Exception as e:
            print(f"   ! no pude guardar el Short {n} en la cola: {e}")
            continue
        items.append({
            "id":              item_id,
            "created":         datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "date":            date,
            "asset_id":        asset_id,
            "size":            path.stat().st_size,
            "title":           sh.get("title") or sh.get("section_title") or script_data.get("youtube_title", ""),
            "text":            sh.get("text", ""),
            "parent_title":    script_data.get("youtube_title", ""),
            "parent_video_id": long_video["video_id"],
            "parent_url":      long_video["url"],
            "tags":            script_data.get("tags", [])[:15],
            "platforms":       {p: {"status": "pending", "attempts": 0} for p in PLATFORMS},
        })
        added += 1
    save_queue(items)
    print(f"   . {added} Shorts encolados ({len(pending_items(items))} pendientes en total)")
    return added
