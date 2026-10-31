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
from datetime import datetime, timedelta

import requests

import insider_alerts as ia

SITE = "tickerpress.net"


def day_and_buys() -> tuple[str, list[dict]]:
    state = ia.load_state()
    day = (datetime.now(ia.ET_TZ) - timedelta(hours=4)).date().isoformat()
    buys = sorted([e for e in state["events"] if e["filed"] == day and e["kind"] == "buy" and ia.alertable(e)],
                  key=ia.rank_key)
    return day, buys


def recap_post(day: str, buys: list[dict], link: str) -> str:
    pretty = datetime.fromisoformat(day).strftime("%b %d").replace(" 0", " ")
    lines = [f"📊 Insider buying {pretty}: {len(buys)} open-market buys, {ia.money(sum(b['value'] for b in buys))}", ""]
    for i, b in enumerate(buys[:3], 1):
        lines.append(f"{i}. ${b['ticker']} {ia.money(b['value'])} ({b['role'].split(',')[0].strip()})")
    lines += ["", link]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bluesky
# ---------------------------------------------------------------------------
def bluesky(text: str) -> str | None:
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
    record = {"$type": "app.bsky.feed.post", "text": text, "facets": facets,
              "createdAt": datetime.utcnow().isoformat(timespec="seconds") + "Z", "langs": ["en"]}
    r = requests.post(f"{api}/com.atproto.repo.createRecord", timeout=30,
                      headers={"Authorization": f"Bearer {s['accessJwt']}"},
                      json={"repo": s["did"], "collection": "app.bsky.feed.post", "record": record}).json()
    if "uri" not in r:
        raise RuntimeError(f"Bluesky post: {r.get('message')}")
    return f"https://bsky.app/profile/{handle}/post/{r['uri'].rsplit('/', 1)[-1]}"


# ---------------------------------------------------------------------------
# X (OAuth 1.0a de usuario)
# ---------------------------------------------------------------------------
def x_post(text: str) -> str | None:
    keys = [os.getenv(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")]
    if not all(keys):
        print("   . X no configurado")
        return None
    from requests_oauthlib import OAuth1
    r = requests.post("https://api.x.com/2/tweets", json={"text": text}, timeout=30, auth=OAuth1(*keys))
    if r.status_code not in (200, 201):
        raise RuntimeError(f"X {r.status_code}: {r.text[:200]}")
    return f"https://x.com/i/status/{r.json()['data']['id']}"


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------
def threads(text: str) -> str | None:
    token, user = os.getenv("THREADS_TOKEN"), os.getenv("THREADS_USER_ID", "me")
    if not token:
        print("   . Threads no configurado")
        return None
    api = "https://graph.threads.net/v1.0"
    c = requests.post(f"{api}/{user}/threads", timeout=30,
                      data={"media_type": "TEXT", "text": text, "access_token": token}).json()
    if "id" not in c:
        raise RuntimeError(f"Threads: {c.get('error', {}).get('message')}")
    time.sleep(5)                                  # Meta recomienda esperar antes de publicar
    p = requests.post(f"{api}/{user}/threads_publish", timeout=30,
                      data={"creation_id": c["id"], "access_token": token}).json()
    if "id" not in p:
        raise RuntimeError(f"Threads publish: {p.get('error', {}).get('message')}")
    return p["id"]


def main() -> None:
    state = ia.load_state()
    day, buys = day_and_buys()
    done = state.setdefault("social", [])
    if day in done and not ia.DRY_RUN:
        print(f"El resumen de {day} ya se publico en redes")
        return
    if not buys:
        print(f"Sin compras de {ia.money(ia.MIN_BUY_USD)}+ el {day}: no hay post")
        return
    day_url = f"{SITE}/day/{day}/"
    posts = {"Bluesky": (bluesky, recap_post(day, buys, f"All buys: {day_url}")),
             "X": (x_post, recap_post(day, buys, "Full list + free alerts: link in bio")),
             "Threads": (threads, recap_post(day, buys, f"All buys: https://{day_url}"))}
    ok = False
    for name, (fn, text) in posts.items():
        if ia.DRY_RUN:
            print(f"--- DRY RUN {name} ({len(text)} caracteres) ---\n{text}\n")
            continue
        try:
            url = fn(text)
            if url:
                ok = True
                print(f"   ✓ {name}: {url}")
        except Exception as e:
            print(f"   ! {name}: {e}")
    if ok:
        done.append(day)
        state["social"] = done[-60:]
        ia.save_state(state)


if __name__ == "__main__":
    if sys.argv[1:2] == ["check"]:
        os.environ["DRY_RUN"] = "true"
        ia.DRY_RUN = True
    main()
