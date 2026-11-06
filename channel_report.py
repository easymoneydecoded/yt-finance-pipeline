"""
channel_report.py - Informe de solo lectura del canal (YouTube Analytics) -> data/channel_report.json

Lo lanza el workflow channel-report.yml (el token del canal solo esta en los secrets de GitHub).
Saca, para los ultimos DAYS dias:
  - por dia y tipo de contenido (Shorts / videos largos): vistas, minutos vistos, suscriptores
  - totales por tipo en las dos mitades del periodo (para ver la tendencia)
  - top videos del periodo con titulo, duracion media vista y % visto
  - fuentes de trafico por tipo de contenido
"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import analytics

DAYS = int(os.getenv("REPORT_DAYS", "56"))


def q(ya, **kw) -> dict:
    try:
        return ya.reports().query(ids="channel==MINE", **kw).execute()
    except Exception as e:                       # un informe que falle no tumba el resto
        return {"error": str(e)[:300]}


def rows(r: dict) -> list[dict]:
    if "error" in r:
        return [r]
    cols = [h["name"] for h in r.get("columnHeaders", [])]
    return [dict(zip(cols, x)) for x in r.get("rows", [])]


def main() -> None:
    yt, ya = analytics._clients()
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=DAYS - 1)
    rng = {"startDate": start.isoformat(), "endDate": end.isoformat()}
    rep = {"range": rng}

    rep["daily"] = rows(q(ya, **rng, dimensions="day,creatorContentType",
                          metrics="views,estimatedMinutesWatched,subscribersGained,subscribersLost", sort="day"))
    rep["by_type"] = rows(q(ya, **rng, dimensions="creatorContentType",
                            metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
                                    "subscribersGained,likes,comments,shares"))
    rep["traffic"] = rows(q(ya, **rng, dimensions="insightTrafficSourceType,creatorContentType",
                            metrics="views,estimatedMinutesWatched", sort="-views"))
    rep["country"] = rows(q(ya, **rng, dimensions="country", metrics="views", sort="-views", maxResults=10))
    rep["totals"] = rows(q(ya, **rng, metrics="views,estimatedMinutesWatched,subscribersGained,subscribersLost"))

    top = rows(q(ya, **rng, dimensions="video",
                 metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,likes",
                 sort="-views", maxResults=60))
    ids = [t["video"] for t in top if "video" in t]
    meta = {}
    for i in range(0, len(ids), 50):
        r = yt.videos().list(part="snippet,contentDetails", id=",".join(ids[i:i + 50])).execute()
        for v in r.get("items", []):
            meta[v["id"]] = {"title": v["snippet"]["title"], "published": v["snippet"]["publishedAt"][:10],
                             "duration": v["contentDetails"]["duration"]}
    for t in top:
        t.update(meta.get(t.get("video"), {}))
    rep["top_videos"] = top

    ch = yt.channels().list(part="statistics", mine=True).execute().get("items", [{}])[0].get("statistics", {})
    rep["channel"] = ch

    out = Path("data/channel_report.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    errs = [k for k, v in rep.items() if isinstance(v, list) and v and "error" in v[0]]
    print(f"Informe {start} -> {end}: {len(top)} videos; secciones con error: {errs or 'ninguna'}")


if __name__ == "__main__":
    sys.exit(main())
