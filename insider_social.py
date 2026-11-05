"""
================================================================================
 insider_social.py - Resumen diario de compras de directivos en Bluesky, X y Threads
================================================================================

 Lo ejecuta alerts.yml despues de publicar la web (para que el enlace al dia ya
 exista). Usa los mismos datos que el resumen de Telegram (insider_alerts.py).
 Son las cuentas propias de Ticker Press: un post al dia, nada de spam.

   Bluesky : BLUESKY_HANDLE + BLUESKY_APP_PASSWORD (gratis)
   X       : X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET
             (pago por uso: un post con enlace cuesta ~0,20 $, sin enlace ~0,015 $,
              por eso en X el enlace va en la bio)
   Threads : THREADS_TOKEN + THREADS_USER_ID (app de Meta con threads_content_publish)
 Sin credenciales de una red, esa red se salta.

 Uso: python insider_social.py [check]
================================================================================
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from pathlib import Path

import insider_alerts as ia

SITE = "tickerpress.net"
BSKY_TAGS     = ["#stocks", "#investing", "#insidertrading"]
X_TAGS        = ["#InsiderTrading", "#stocks"]   # pocos: en X muchos hashtags restan alcance
THREADS_TOPIC = "#Stocks"


def day_and_buys() -> tuple[str, list[dict]]:
    state = ia.load_state()
    day, _ = ia.recap_day()           # el mismo dia que el resumen de Telegram (aunque GitHub llegue tarde)
    buys = sorted([e for e in state["events"] if e["filed"] == day and e["kind"] == "buy" and ia.alertable(e)],
                  key=ia.rank_key)
    return day, buys


def recap_post(day: str, buys: list[dict], link: str) -> str:
    pretty = datetime.fromisoformat(day).strftime("%b %d").replace(" 0", " ")
    lines = [f"📊 Insider buying {pretty}: {len(buys)} insider buys, {ia.money(sum(b['value'] for b in buys))}", ""]
    for i, b in enumerate(buys[:3], 1):
        lines.append(f"{i}. ${b['ticker']} {ia.money(b['value'])} ({b['role'].split(',')[0].strip()})")
    lines += ["", link]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bluesky
# ---------------------------------------------------------------------------
IMAGE_URL = None   # URL publica de la imagen tras subirla a Bluesky (Threads la descarga de ahi)


def bluesky(text: str, image: Path | None = None, alt: str = "") -> str | None:
    global IMAGE_URL
    handle, password = os.getenv("BLUESKY_HANDLE"), os.getenv("BLUESKY_APP_PASSWORD")
    if not handle or not password:
        print("   . Bluesky no configurado")
        return None
    api = "https://bsky.social/xrpc"
    s = requests.post(f"{api}/com.atproto.server.createSession", timeout=30,
                      json={"identifier": handle, "password": password}).json()
    if "accessJwt" not in s:
        raise RuntimeError(f"Bluesky login: {s.get('message')}")
    raw = text.encode("utf-8")
    facets = []
    for m in re.finditer(rb"(https?://\S+|(?:tickerpress\.net|t\.me/)\S*)", raw):    # enlaces clicables
        uri = m.group(0).decode()
        facets.append({"index": {"byteStart": m.start(), "byteEnd": m.end()},
                       "features": [{"$type": "app.bsky.richtext.facet#link",
                                     "uri": uri if uri.startswith("http") else f"https://{uri}"}]})
    for m in re.finditer(rb"(?<![\w&])#([A-Za-z]\w{1,63})", raw):      # hashtags -> feeds por tema
        facets.append({"index": {"byteStart": m.start(), "byteEnd": m.end()},
                       "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1).decode()}]})
    record = {"$type": "app.bsky.feed.post", "text": text, "facets": facets,
              "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "langs": ["en"]}
    if image:
        try:
            blob = requests.post(f"{api}/com.atproto.repo.uploadBlob", timeout=60, data=image.read_bytes(),
                                 headers={"Authorization": f"Bearer {s['accessJwt']}", "Content-Type": "image/png"}).json()["blob"]
            record["embed"] = {"$type": "app.bsky.embed.images", "images": [
                {"image": blob, "alt": alt[:900], "aspectRatio": {"width": 1200, "height": 675}}]}
            IMAGE_URL = f"https://cdn.bsky.app/img/feed_fullsize/plain/{s['did']}/{blob['ref']['$link']}@png"
        except Exception as e:
            print(f"   ! Bluesky: imagen no subida ({e}), va sin imagen")
    r = requests.post(f"{api}/com.atproto.repo.createRecord", timeout=30,
                      headers={"Authorization": f"Bearer {s['accessJwt']}"},
                      json={"repo": s["did"], "collection": "app.bsky.feed.post", "record": record}).json()
    if "uri" not in r:
        raise RuntimeError(f"Bluesky post: {r.get('message')}")
    return f"https://bsky.app/profile/{handle}/post/{r['uri'].rsplit('/', 1)[-1]}"


# ---------------------------------------------------------------------------
# X (OAuth 1.0a de usuario)
# ---------------------------------------------------------------------------
def one_cashtag(text: str) -> str:
    """X solo admite un cashtag ($NVDA) por post: se deja el primero y los demas van sin $."""
    pat = r"\$([A-Z][A-Z.]{0,5})(?![\w.])"
    first = re.search(pat, text)
    if not first:
        return text
    return text[:first.end()] + re.sub(pat, r"\1", text[first.end():])


def x_media(image: Path, auth) -> str | None:
    """Sube la imagen a X (API v2; si falla, la v1.1). Devuelve el media_id o None."""
    try:
        r = requests.post("https://api.x.com/2/media/upload", auth=auth, timeout=60,
                          files={"media": (image.name, image.read_bytes(), "image/png")},
                          data={"media_category": "tweet_image"})
        if r.ok:
            return r.json()["data"]["id"]
        r = requests.post("https://upload.twitter.com/1.1/media/upload.json", auth=auth, timeout=60,
                          files={"media": image.read_bytes()})
        if r.ok:
            return r.json()["media_id_string"]
        print(f"   ! X: imagen no subida ({r.status_code} {r.text[:120]}), va sin imagen")
    except Exception as e:
        print(f"   ! X: imagen no subida ({e}), va sin imagen")
    return None


def x_tags(text: str) -> str:
    """Anade los hashtags de tema que quepan. X cuenta los emojis como 2 caracteres: se deja margen sobre 280."""
    for tags in (X_TAGS, X_TAGS[:1]):
        if len(text) + len(" ".join(tags)) + 1 <= 265:
            return text + "\n" + " ".join(tags)
    return text


def x_post(text: str, image: Path | None = None, alt: str = "") -> str | None:
    text = x_tags(one_cashtag(text))
    keys = [os.getenv(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")]
    if not all(keys):
        print("   . X no configurado")
        return None
    from requests_oauthlib import OAuth1
    body = {"text": text}
    media = x_media(image, OAuth1(*keys)) if image else None
    if media:
        body["media"] = {"media_ids": [media]}
    r = requests.post("https://api.x.com/2/tweets", json=body, timeout=30, auth=OAuth1(*keys))
    if r.status_code not in (200, 201):
        raise RuntimeError(f"X {r.status_code}: {r.text[:200]}")
    return f"https://x.com/i/status/{r.json()['data']['id']}"


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------
THREADS_REFRESH_DAYS = 20   # el token largo dura 60 dias; se renueva mucho antes


def threads_token() -> str | None:
    """Token de Threads; el renovado se guarda cifrado igual que el de Instagram (secret_store)."""
    import secret_store
    stored = secret_store.get("threads")
    token = stored.get("token") or os.getenv("THREADS_TOKEN")
    if not token:
        return None
    refreshed = stored.get("refreshed_at")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(refreshed)).days if refreshed else 99
    if age >= THREADS_REFRESH_DAYS:
        r = requests.get("https://graph.threads.net/refresh_access_token", timeout=30,
                         params={"grant_type": "th_refresh_token", "access_token": token}).json()
        if "access_token" in r:
            token = r["access_token"]
            secret_store.put("threads", {"token": token,
                                         "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            print(f"   . token de Threads renovado (valido {r.get('expires_in', 0) // 86400} dias)")
        else:
            print(f"   ! no pude renovar el token de Threads: {r.get('error', {}).get('message')}")
    return token


def threads(text: str, image: Path | None = None, alt: str = "") -> str | None:
    token, user = threads_token(), os.getenv("THREADS_USER_ID", "me")
    if not token:
        print("   . Threads no configurado")
        return None
    api = "https://graph.threads.net/v1.0"
    data = {"media_type": "TEXT", "text": text, "access_token": token}
    if image and IMAGE_URL:                        # la imagen ya esta publicada en el CDN de Bluesky
        data.update({"media_type": "IMAGE", "image_url": IMAGE_URL, "alt_text": alt[:900]})
    c = requests.post(f"{api}/{user}/threads", timeout=30, data=data).json()
    if "id" not in c and data["media_type"] == "IMAGE":
        print(f"   ! Threads: imagen rechazada ({c.get('error', {}).get('message')}), va sin imagen")
        data = {"media_type": "TEXT", "text": text, "access_token": token}
        c = requests.post(f"{api}/{user}/threads", timeout=30, data=data).json()
    if "id" not in c:
        raise RuntimeError(f"Threads: {c.get('error', {}).get('message')}")
    time.sleep(15 if data["media_type"] == "IMAGE" else 5)   # Meta recomienda esperar antes de publicar
    p = requests.post(f"{api}/{user}/threads_publish", timeout=30,
                      data={"creation_id": c["id"], "access_token": token}).json()
    if "id" not in p:
        raise RuntimeError(f"Threads publish: {p.get('error', {}).get('message')}")
    return p["id"]


def main() -> None:
    state = ia.load_state()
    day, buys = day_and_buys()
    done = state.setdefault("social", [])
    if day in done and not ia.DRY_RUN and not os.getenv("RECAP_DAY"):
        print(f"El resumen de {day} ya se publico en redes")
        return
    if not buys:
        print(f"Sin compras de {ia.money(ia.MIN_BUY_USD)}+ el {day}: no hay post")
        return
    day_url = f"{SITE}/day/{day}/"
    # Etiquetas de tema: en Bluesky llevan el post a los feeds de #stocks/#investing; en Threads
    # el primer hashtag es la "etiqueta de tema" (solo una). En X ya estan los cashtags ($NVDA).
    bsky_text = recap_post(day, buys, f"All buys: {day_url}")
    for tags in (BSKY_TAGS, BSKY_TAGS[:2], BSKY_TAGS[:1]):        # limite de Bluesky: 300 caracteres
        if len(bsky_text) + len(" ".join(tags)) + 1 <= 300:
            bsky_text += "\n" + " ".join(tags)
            break
    import insider_cards
    card = insider_cards.daily_card(day, buys)
    alt = (f"Ticker Press card: insiders bought {ia.money(sum(b['value'] for b in buys))} of stock in {len(buys)} "
           f"insider purchases. Top buy: {buys[0]['ticker']} {ia.money(buys[0]['value'])}, with its 6-month price chart.")
    posts = {"Bluesky": (bluesky, bsky_text),
             "X": (x_post, recap_post(day, buys, "Full list + free alerts: link in bio")),
             "Threads": (threads, recap_post(day, buys, f"All buys: https://{day_url}") + f"\n{THREADS_TOPIC}")}
    ok = False
    for name, (fn, text) in posts.items():
        if ia.DRY_RUN:
            print(f"--- DRY RUN {name} ({len(text)} caracteres) ---\n{text}\n")
            continue
        try:
            url = fn(text, card, alt)
            if url:
                ok = True
                print(f"   ✓ {name}: {url}")
        except Exception as e:
            print(f"   ! {name}: {e}")
    if ok:
        if day not in done:
            done.append(day)
        state["social"] = done[-60:]
        ia.save_state(state)


def weekly_post(link: str, n_items: int = 3) -> tuple[str, str] | None:
    """Resumen de la semana (los mismos datos que el de Telegram de insider_reddit.py). Devuelve (clave, texto)."""
    import insider_reddit as ir
    start, end, week = ir.week_data()
    if not week:
        return None
    by, clusters, people, span = ir.week_summary(start, end, week)
    lines = [f"📅 Insider buying, week of {span}: {len(week)} insider buys, "
             f"{ia.money(sum(b['value'] for b in week))} across {len(by)} stocks", ""]
    items = [f"🔥 ${t}: {len({b['owner_cik'] for b in bs})} insiders buying, {ia.money(sum(b['value'] for b in bs))}"
             for t, bs in clusters[:2]]
    items += [f"💰 ${b['ticker']} {ia.money(b['value'])} ({b['role'].split(',')[0].strip()})" for b in people]
    lines += items[:n_items] + ["", link]
    return end.isoformat(), "\n".join(lines)


def weekly() -> None:
    """Domingo: el resumen semanal tambien en Bluesky, X y Threads (una vez por semana)."""
    state = ia.load_state()
    done = state.setdefault("social_weekly", [])
    first = weekly_post("")
    if not first:
        print("Sin compras esta semana: no hay post")
        return
    key = first[0]
    # Una marca por red ("2026-10-30:X"): si una falla, se reintenta sin repetir las que ya salieron.
    # Una marca sin red es del primer domingo (1-nov), en el que solo salio Bluesky.
    published = {d.split(":", 1)[1] for d in done if d.startswith(key + ":")} | ({"Bluesky"} if key in done else set())
    bsky = None
    for n in (3, 2, 1):                                           # limite de Bluesky: 300 caracteres
        text = weekly_post(f"All buys: https://{SITE}", n)[1]
        for tags in (BSKY_TAGS, BSKY_TAGS[:1], []):
            cand = text + ("\n" + " ".join(tags) if tags else "")
            if len(cand) <= 300:
                bsky = cand
                break
        if bsky:
            break
    import insider_cards
    import insider_reddit as ir
    start, end, week = ir.week_data()
    by, clusters, people, span = ir.week_summary(start, end, week)
    card = insider_cards.weekly_card(span, week, by, clusters, people)
    alt = (f"Ticker Press weekly recap, {span}: {len(week)} insider buys worth {ia.money(sum(b['value'] for b in week))} "
           f"across {len(by)} stocks, with a bar chart of the stocks insiders bought most.")
    posts = {"Bluesky": (bluesky, bsky),
             "X": (x_post, weekly_post("Every buy + free alerts: link in bio")[1]),
             "Threads": (threads, weekly_post(f"All buys: https://{SITE}")[1] + f"\n{THREADS_TOPIC}")}
    ok = False
    for name, (fn, text) in posts.items():
        if not text:
            continue
        if name in published and not ia.DRY_RUN:
            print(f"   . {name}: el resumen de la semana {key} ya estaba publicado")
            continue
        if ia.DRY_RUN:
            print(f"--- DRY RUN {name} ({len(text)} caracteres) ---\n{text}\n")
            continue
        try:
            url = fn(text, card, alt)
            if url:
                ok = True
                done.append(f"{key}:{name}")
                print(f"   ✓ {name}: {url}")
        except Exception as e:
            print(f"   ! {name}: {e}")
    if ok:
        state["social_weekly"] = done[-60:]
        ia.save_state(state)


if __name__ == "__main__":
    if "check" in sys.argv[1:]:
        os.environ["DRY_RUN"] = "true"
        ia.DRY_RUN = True
    weekly() if "weekly" in sys.argv[1:] else main()
