"""
================================================================================
 content_history.py - Memoria del canal (anti-repeticion)
================================================================================

 Que hace:
   - Guarda en history/videos.json todo lo que ya se ha publicado: titulo,
     tema, entidades, texto de miniatura, formato, estilo de miniatura y clips
     de Pexels usados.
   - El workflow de GitHub Actions hace commit de este fichero tras cada
     subida, asi la siguiente ejecucion (que arranca en una maquina limpia)
     sabe que temas, titulos y miniaturas NO debe repetir.
   - Ofrece helpers de similitud para detectar duplicados aunque el modelo
     cambie un par de palabras ("Harvard Just Made $12B On SpaceX" vs
     "Harvard Just Bet Everything On SpaceX").

================================================================================
"""

import json
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

HISTORY_DIR  = Path("history")
HISTORY_FILE = HISTORY_DIR / "videos.json"

# Ventanas de memoria
TOPIC_WINDOW_DAYS   = 60   # un tema (mismas entidades) no se repite en 60 dias
PROMPT_RECENT_LIMIT = 60   # cuantos videos recientes se muestran al modelo
MEDIA_RECENT_LIMIT  = 20   # clips/estilos de los ultimos N videos se evitan

# Umbrales de similitud
TITLE_SIM_THRESHOLD = 0.68
TOPIC_SIM_THRESHOLD = 0.55

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "by", "with", "from", "is", "are", "was", "were", "be", "been", "it", "its",
    "this", "that", "these", "those", "your", "you", "my", "our", "their",
    "why", "how", "what", "who", "when", "just", "now", "about", "into", "out",
    "nobody", "talking", "actually", "really", "will", "can", "could", "has",
    "have", "had", "do", "does", "did", "not", "no", "yes", "all", "every",
    "everything", "as", "up", "down", "over", "after", "before", "new",
}


# ---------------------------------------------------------------------------
# Carga / guardado
# ---------------------------------------------------------------------------
def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"   ! history corrupto ({e}), lo ignoro")
        return []
    videos = data.get("videos", []) if isinstance(data, dict) else data
    _merge_queue_shorts(videos)
    # Mas reciente primero
    return sorted(videos, key=lambda v: v.get("date", ""), reverse=True)


def _merge_queue_shorts(videos: list[dict]) -> None:
    """Los Shorts de YouTube publicados desde la cola (social_publisher) se anaden a su video."""
    queue_file = HISTORY_DIR / "shorts_queue.json"
    if not queue_file.exists():
        return
    try:
        items = json.loads(queue_file.read_text(encoding="utf-8")).get("items", [])
    except (json.JSONDecodeError, OSError):
        return
    by_id = {v.get("video_id"): v for v in videos if v.get("video_id")}
    for it in items:
        yt = it.get("platforms", {}).get("youtube", {})
        parent = by_id.get(it.get("parent_video_id"))
        if yt.get("status") != "done" or not parent or yt.get("id") in parent.get("shorts_ids", []):
            continue
        parent.setdefault("shorts_ids", []).append(yt["id"])
        parent.setdefault("shorts_titles", []).append(it.get("youtube_title") or it.get("title", ""))


def save_history(videos: list[dict]) -> None:
    HISTORY_DIR.mkdir(exist_ok=True)
    videos = sorted(videos, key=lambda v: v.get("date", ""), reverse=True)
    HISTORY_FILE.write_text(json.dumps({
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_videos": len(videos),
        "videos": videos,
    }, indent=2, ensure_ascii=False), encoding="utf-8")


def append_video(entry: dict) -> None:
    videos = load_history()
    # Si ya hay una entrada con el mismo video_id la sustituimos
    vid = entry.get("video_id")
    if vid:
        videos = [v for v in videos if v.get("video_id") != vid]
    videos.append(entry)
    save_history(videos)


def already_uploaded_on(date: str, history: list[dict] | None = None) -> dict | None:
    """Devuelve la entrada si ya se subio un video largo pipeline-generado ese dia."""
    for v in history if history is not None else load_history():
        if v.get("date") == date and v.get("video_id") and not v.get("seed"):
            return v
    return None


# ---------------------------------------------------------------------------
# Normalizacion y similitud
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9$%. ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _stem(word: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(word) > 4 and word.endswith(suf):
            return word[: -len(suf)]
    return word


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9$%]+", (text or "").lower())
    return {_stem(w) for w in words if w not in STOPWORDS and len(w) > 1}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _content_string(text: str) -> str:
    words = re.findall(r"[a-z0-9$%.,]+", (text or "").lower())
    return " ".join(_stem(w.strip(".,")) for w in words if w.strip(".,") not in STOPWORDS)


def text_similarity(a: str, b: str) -> float:
    """
    Similitud solo sobre palabras con contenido: "Why X Just ... Again" no debe
    hacer parecidos dos titulos de temas distintos.
    """
    if normalize(a) == normalize(b) and normalize(a):
        return 1.0
    ca, cb = _content_string(a), _content_string(b)
    if not ca or not cb:
        return 0.0
    return max(SequenceMatcher(None, ca, cb).ratio(), jaccard(tokens(a), tokens(b)))


def normalize_entities(entities) -> set[str]:
    out = set()
    for e in entities or []:
        e = normalize(str(e))
        e = re.sub(r"\b(inc|corp|corporation|co|ltd|plc|group|the)\b", "", e).strip()
        if e:
            out.add(e)
    return out


def shared_entities(a: set[str], b: set[str]) -> set[str]:
    """Entidades comunes; "harvard" coincide con "harvard university"."""
    out = set()
    for x in a:
        for y in b:
            xs, ys = set(x.split()), set(y.split())
            if x == y or xs <= ys or ys <= xs:
                out.add(min(x, y, key=len))
    return out


def _within_days(entry: dict, days: int, today: str) -> bool:
    try:
        d = datetime.strptime(entry.get("date", "")[:10], "%Y-%m-%d")
        t = datetime.strptime(today, "%Y-%m-%d")
    except ValueError:
        return True  # sin fecha fiable -> lo tratamos como reciente
    return (t - d) <= timedelta(days=days)


def find_duplicate(candidate: dict, history: list[dict], today: str) -> tuple[dict, str] | None:
    """
    candidate: {title, topic_key?, entities?, thumbnail_text?}
    Devuelve (entrada_historial, motivo) si es repetido, o None si es nuevo.
    """
    c_title   = candidate.get("title", "")
    c_topic   = candidate.get("topic_key", "") or c_title
    c_ents    = normalize_entities(candidate.get("entities"))
    c_thumb   = normalize(candidate.get("thumbnail_text", ""))

    for old in history:
        old_titles = [old.get("title", "")] + list(old.get("shorts_titles", []))
        for ot in old_titles:
            sim = text_similarity(c_title, ot)
            if sim >= TITLE_SIM_THRESHOLD:
                return old, f"titulo casi igual ({sim:.2f}) a '{ot}'"

        if c_thumb and c_thumb == normalize(old.get("thumbnail_text", "")):
            return old, f"texto de miniatura identico '{old.get('thumbnail_text')}'"

        if not _within_days(old, TOPIC_WINDOW_DAYS, today):
            continue

        o_ents = normalize_entities(old.get("entities"))
        shared = shared_entities(c_ents, o_ents)
        o_topic = old.get("topic_key", "") or old.get("title", "")
        topic_sim = jaccard(tokens(c_topic), tokens(o_topic))

        if len(shared) >= 2:
            return old, f"mismas entidades {sorted(shared)}"
        if shared and topic_sim >= 0.3:
            return old, f"mismo tema ({sorted(shared)}, sim {topic_sim:.2f})"
        if topic_sim >= TOPIC_SIM_THRESHOLD:
            return old, f"topic_key casi igual ({topic_sim:.2f}) a '{o_topic}'"
    return None


# ---------------------------------------------------------------------------
# Resumenes para prompts y para variar medios
# ---------------------------------------------------------------------------
def recent_for_prompt(history: list[dict], limit: int = PROMPT_RECENT_LIMIT) -> str:
    if not history:
        return "  (no videos published yet)"
    lines = []
    for v in history[:limit]:
        bits = [f'"{v.get("title", "")}"']
        if v.get("topic_key"):
            bits.append(f"topic: {v['topic_key']}")
        if v.get("thumbnail_text"):
            bits.append(f"thumb: {v['thumbnail_text']}")
        lines.append(f"  - {v.get('date', '?')[:10]} | " + " | ".join(bits))
    return "\n".join(lines)


def overused_title_openers(history: list[dict], last_n: int = 30) -> list[str]:
    """Primeras palabras de titulo usadas en >=20% de los ultimos videos."""
    counts: dict[str, int] = {}
    recent = history[:last_n]
    for v in recent:
        words = (v.get("title") or "").split()
        if words:
            w = words[0].strip("\"'").lower()
            counts[w] = counts.get(w, 0) + 1
    if not recent:
        return []
    limit = max(2, int(len(recent) * 0.2))
    return sorted(w for w, n in counts.items() if n >= limit)


def recent_values(history: list[dict], key: str, last_n: int = MEDIA_RECENT_LIMIT) -> list:
    """Valores de un campo (escalar o lista) en los ultimos N videos."""
    out = []
    for v in history[:last_n]:
        val = v.get(key)
        if isinstance(val, list):
            out.extend(val)
        elif val not in (None, ""):
            out.append(val)
    return out


def used_source_urls(history: list[dict], last_n: int = 90) -> set[str]:
    return {u for u in recent_values(history, "source_urls", last_n) if u}


def used_source_headlines(history: list[dict], last_n: int = 90) -> set[str]:
    return {normalize(h) for h in recent_values(history, "source_headlines", last_n) if h}


# ---------------------------------------------------------------------------
# Aprendizaje: que funciona en este canal (lo rellena analytics.py)
# ---------------------------------------------------------------------------
MIN_AGE_DAYS_FOR_SCORE = 3   # un video de 1 dia aun no tiene datos fiables


def _age_days(entry: dict) -> int:
    try:
        d = datetime.strptime(entry.get("date", "")[:10], "%Y-%m-%d")
    except ValueError:
        return 0
    return (datetime.now(timezone.utc).replace(tzinfo=None) - d).days


def performance_score(entry: dict) -> float | None:
    """Visitas ponderadas por retencion. None si no hay datos o es muy reciente."""
    s = entry.get("stats")
    if not s or "views" not in s or _age_days(entry) < MIN_AGE_DAYS_FOR_SCORE:
        return None
    retention = s.get("avg_view_pct")
    factor = 0.5 + retention / 100 if retention is not None else 1.0
    return (s["views"] + 0.2 * s.get("shorts_views", 0)) * factor


def scored(history: list[dict]) -> list[tuple[float, dict]]:
    out = [(performance_score(v), v) for v in history if not v.get("seed") or v.get("stats")]
    return sorted([(sc, v) for sc, v in out if sc is not None], key=lambda x: x[0], reverse=True)


def performance_report(history: list[dict], n: int = 5) -> str:
    """Texto para los prompts: mejores y peores videos con su formato y estilo."""
    ranked = scored(history)
    if len(ranked) < 4:
        return "  (not enough performance data yet - explore different formats)"

    def _line(sc, v):
        s = v["stats"]
        ret = f", {s['avg_view_pct']}% avg viewed" if s.get("avg_view_pct") is not None else ""
        extra = " | ".join(x for x in (v.get("format"), v.get("title_style")) if x)
        return f'  - "{v.get("title")}" -> {s["views"]} views{ret}' + (f" [{extra}]" if extra else "")

    lines = ["BEST performing videos (do more of what made these work):"]
    lines += [_line(sc, v) for sc, v in ranked[:n]]
    lines += ["WORST performing videos (avoid their topics/angles/title patterns):"]
    lines += [_line(sc, v) for sc, v in ranked[-n:]]
    return "\n".join(lines)


def weighted_choice(options: list[str], history: list[dict], key: str, rng=None,
                    exclude: set | None = None) -> str:
    """
    Elige una opcion favoreciendo las que mejor rinden en este canal, pero
    dejando siempre margen para explorar (las nunca probadas pesan 1.3).
    """
    import random as _random
    rng = rng or _random
    pool = [o for o in options if o not in (exclude or set())] or list(options)

    ranked = scored(history)
    by_opt: dict[str, list[float]] = {}
    for sc, v in ranked:
        val = v.get(key)
        if val:
            by_opt.setdefault(val, []).append(sc)
    mean_all = (sum(sc for sc, _ in ranked) / len(ranked)) if ranked else 0

    weights = []
    for o in pool:
        tag = o.split(":")[0]
        scores = by_opt.get(o) or by_opt.get(tag)
        if not scores or mean_all <= 0:
            weights.append(1.3)
        else:
            weights.append(min(3.0, max(0.4, (sum(scores) / len(scores)) / mean_all)))
    return rng.choices(pool, weights=weights, k=1)[0]
