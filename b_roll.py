"""
================================================================================
 b_roll.py - Etapa 4 del canal de YouTube automatico (v2)
================================================================================

 Que hace:
   1. Lee el JSON de guion generado por la etapa 2
   2. Pide a Groq que extraiga keywords VISUALES por seccion
   3. Si Groq falla (rate limit, error), usa FALLBACK LOCAL con keywords
      genericas de finanzas - el pipeline NUNCA se rompe por culpa de Groq
   4. Busca y descarga clips de stock video gratis desde Pexels
   5. Guarda los MP4 en data/broll/section_XX/ y un manifest JSON

 Coste: 0 EUR
================================================================================
"""

import os
import json
import time
import re
import random
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

GROQ_API_KEY    = os.getenv("GROQ_API_KEY")  # legacy, no usado
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
PEXELS_API_KEY  = os.getenv("PEXELS_API_KEY")

GEMINI_MODEL    = "gemini-2.5-flash"
GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

PEXELS_ENDPOINT = "https://api.pexels.com/videos/search"

INPUT_DIR       = Path("data")
OUTPUT_DIR      = Path("data/broll")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TODAY           = datetime.now(timezone.utc).strftime("%Y-%m-%d")

KEYWORDS_PER_SECTION = {
    "hook":      2,
    "intro":     3,
    "body":      3,
    "retention": 2,
    "outro":     2,
}

CLIPS_PER_KEYWORD     = 2
TARGET_WIDTH          = 1280
MIN_CLIP_DURATION_SEC = 5
MAX_CLIP_DURATION_SEC = 60


# Pool de keywords visuales evergreen para finanzas (fallback si Groq falla)
FINANCE_BROLL_POOL = [
    "stock market chart",
    "wall street building",
    "businessman office",
    "calculator money",
    "graph rising arrow",
    "city skyline night",
    "trading floor stocks",
    "computer screen data",
    "newspaper finance",
    "coins stacking",
    "office meeting business",
    "smartphone trading app",
    "bank vault",
    "credit card payment",
    "wallet money cash",
    "businesswoman laptop",
    "global economy map",
    "downtown financial district",
    "graph red downturn",
    "investor analyzing charts",
    "money transfer mobile",
    "piggy bank savings",
    "house keys mortgage",
    "boardroom executives",
    "retirement planning",
]


# ---------------------------------------------------------------------------
# Carga del guion
# ---------------------------------------------------------------------------
def load_todays_script() -> dict:
    path = INPUT_DIR / f"script_{TODAY}.json"
    if not path.exists():
        candidates = sorted(INPUT_DIR.glob("script_*.json"), reverse=True)
        if not candidates:
            raise FileNotFoundError("No hay script_*.json. Ejecuta script_writer.py.")
        path = candidates[0]
        print(f"   . Usando script mas reciente: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Extraccion de keywords con Groq (con fallback)
# ---------------------------------------------------------------------------
KEYWORDS_PROMPT = """You are a video editor's research assistant. For each script section below, suggest visual keywords that would work as stock footage b-roll on Pexels.

RULES:
- Each keyword MUST be VISUAL (something you can film/photograph). 
  BAD: "compound interest", "tax law", "Federal Reserve policy"
  GOOD: "businessman calculator", "stock market chart", "city skyline morning", "graph rising", "office meeting"
- Keep keywords SHORT (2-4 words each). Pexels search works better with short queries.
- Mix abstract concepts (e.g. "growth chart") with concrete imagery (e.g. "wall street building")
- Vary keywords ACROSS sections - don't repeat. Each section should have visually different b-roll.
- Use finance/business/economy themed visuals.
- Avoid copyrighted entities (no "Tesla logo", no "Bitcoin coin closeup").

Video topic: {video_title}
Thumbnail: {thumbnail}

Script sections:
{sections_block}

For each section, return the requested number of keywords (specified per section).

OUTPUT FORMAT - return ONE JSON object with this exact shape:
{{
  "section_keywords": [
    {{"section_index": 1, "keywords": ["keyword one", "keyword two"]}},
    {{"section_index": 2, "keywords": ["...", "...", "..."]}},
    ...
  ]
}}

Output ONLY the JSON. No prose. No markdown fences.
"""


def call_groq(prompt: str, max_tokens: int = 2000) -> dict | None:
    """
    Llama a Gemini (nombre por compat con el resto del pipeline).
    Devuelve dict si OK, None si agota reintentos.
    NO lanza excepcion: la decision de fallback la toma el caller.
    """
    if not GEMINI_API_KEY:
        print("   ! Falta GEMINI_API_KEY, no puedo llamar a IA")
        return None

    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "temperature":      0.7,
            "maxOutputTokens":  max_tokens,
            "responseMimeType": "application/json",
        },
    }
    max_attempts = 3  # menos reintentos para no inflar tiempo total
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(
                GEMINI_ENDPOINT,
                headers = {
                    "x-goog-api-key": GEMINI_API_KEY,
                    "Content-Type":   "application/json",
                },
                json    = payload,
                timeout = 90,
            )
        except Exception as e:
            print(f"   . Gemini conexion fallo: {e}")
            return None

        if r.status_code == 200:
            try:
                data = r.json()
                content = data["candidates"][0]["content"]["parts"][0]["text"]
                # Parseo tolerante: si vino con texto extra alrededor del JSON, extraer
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    i, j = content.find("{"), content.rfind("}")
                    if i != -1 and j != -1:
                        return json.loads(content[i:j+1])
                    print(f"   ! Gemini devolvio sin JSON: {content[:200]}")
                    return None
            except Exception as e:
                print(f"   ! Gemini parseo fallo: {e}")
                return None

        if r.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
            wait = 30 * attempt
            snippet = r.text[:150].replace("\n", " ")
            print(f"   . Gemini devolvio {r.status_code}: {snippet}. Espero {wait}s...")
            time.sleep(wait)
            continue

        # Otros errores o ultimo intento fallido
        print(f"   ! Gemini error final: {r.status_code} - {r.text[:200]}")
        return None

    return None


def fallback_keywords(script_data: dict, sections: list) -> list[list[str]]:
    """
    Genera keywords visuales SIN llamar a Groq. Mezcla:
      1. Tags del video (filtradas, solo las visualmente plausibles)
      2. Pool evergreen de finanzas
    """
    print("   ! Usando FALLBACK LOCAL para keywords (Gemini no disponible)")

    tags = script_data.get("tags", [])
    # Filtrar tags poco visuales (terminos abstractos)
    abstract_terms = {
        "investing", "investment", "investments", "personal finance", "money",
        "finance", "wealth", "education", "advice", "tips", "strategy",
        "strategies", "planning", "retirement"
    }
    visual_tags = [t for t in tags if t.lower() not in abstract_terms and len(t) < 30]

    # Mezcla aleatoria controlada (semilla fija para resultados estables)
    rng = random.Random(42)
    pool = list(set(visual_tags + FINANCE_BROLL_POOL))
    rng.shuffle(pool)

    result = []
    pool_idx = 0
    for section in sections:
        stype = section.get("type", "body")
        n = KEYWORDS_PER_SECTION.get(stype, 2)
        section_kws = []
        for _ in range(n):
            section_kws.append(pool[pool_idx % len(pool)])
            pool_idx += 1
        result.append(section_kws)
    return result


def extract_keywords(script_data: dict) -> list[list[str]]:
    sections = script_data.get("sections", [])
    if not sections:
        return []

    sections_block_lines = []
    for i, s in enumerate(sections, 1):
        stype = s.get("type", "body")
        n_kw = KEYWORDS_PER_SECTION.get(stype, 2)
        text = s.get("text", "").strip()
        if len(text) > 400:
            text = text[:400] + "..."
        sections_block_lines.append(
            f"{i}. [{stype.upper()}] (need {n_kw} keywords)\n   {text}"
        )
    sections_block = "\n\n".join(sections_block_lines)

    prompt = KEYWORDS_PROMPT.format(
        video_title    = script_data.get("youtube_title", ""),
        thumbnail      = script_data.get("thumbnail_text", ""),
        sections_block = sections_block,
    )

    print(f"   . Pidiendo keywords a Groq para {len(sections)} secciones...")
    raw = call_groq(prompt)

    # Si Groq fallo, fallback completo
    if raw is None:
        return fallback_keywords(script_data, sections)

    # Parseo defensivo
    result = [[] for _ in sections]
    for entry in raw.get("section_keywords", []):
        idx = entry.get("section_index", 0) - 1
        if 0 <= idx < len(sections):
            kws = entry.get("keywords", [])
            result[idx] = [str(k).strip() for k in kws if k]

    # Fallback PARCIAL: para secciones sueltas que Groq dejo vacias
    fb = None
    for i, kws in enumerate(result):
        if not kws:
            if fb is None:
                fb = fallback_keywords(script_data, sections)
            result[i] = fb[i]
            print(f"   . Seccion {i+1} sin keywords de Groq, uso fallback parcial")

    return result


# ---------------------------------------------------------------------------
# Pexels: busqueda y descarga
# ---------------------------------------------------------------------------
def search_pexels(query: str, per_page: int = 5) -> list[dict]:
    if not PEXELS_API_KEY:
        raise RuntimeError("Falta PEXELS_API_KEY")

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.get(
                PEXELS_ENDPOINT,
                headers = {"Authorization": PEXELS_API_KEY},
                params  = {
                    "query":       query,
                    "per_page":    per_page,
                    "orientation": "landscape",
                    "size":        "medium",
                },
                timeout = 30,
            )
        except Exception as e:
            print(f"     ! pexels conexion: {e}")
            return []

        if r.status_code == 200:
            return r.json().get("videos", [])
        if r.status_code == 429 and attempt < max_attempts:
            wait = 20 * attempt
            print(f"     ! pexels rate limit, espero {wait}s...")
            time.sleep(wait)
            continue
        print(f"     ! pexels devolvio {r.status_code} para '{query}'")
        return []
    return []


def pick_video_file(video: dict, target_width: int = TARGET_WIDTH) -> dict | None:
    candidates = [
        f for f in video.get("video_files", [])
        if f.get("file_type") == "video/mp4" and f.get("link")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda c: abs(c.get("width", 9999) - target_width))
    return candidates[0]


def download_clip(url: str, dest_path: Path) -> int:
    r = requests.get(url, stream=True, timeout=180)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
    return dest_path.stat().st_size


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    t0 = time.time()
    print(f"\n=== B-ROLL DOWNLOAD . {TODAY} ===\n")

    if not PEXELS_API_KEY:
        raise RuntimeError("Falta PEXELS_API_KEY en variables de entorno")

    print("1/4  Cargando guion...")
    script_data = load_todays_script()
    sections = script_data.get("sections", [])
    print(f"      -> '{script_data.get('youtube_title', '?')}'")
    print(f"      -> {len(sections)} secciones")

    print(f"\n2/4  Extrayendo keywords visuales...")
    section_keywords = extract_keywords(script_data)

    for i, (s, kws) in enumerate(zip(sections, section_keywords), 1):
        print(f"      sec {i:02d} [{s.get('type'):9s}]: {', '.join(kws)}")

    print(f"\n3/4  Descargando clips de Pexels...")
    manifest_sections = []
    seen_video_ids = set()
    total_bytes = 0
    total_clips = 0
    total_failures = 0

    for i, (section, keywords) in enumerate(zip(sections, section_keywords), 1):
        section_id   = f"section_{i:02d}"
        section_dir  = OUTPUT_DIR / section_id
        section_dir.mkdir(exist_ok=True)

        section_clips = []
        clip_counter  = 0

        for kw in keywords:
            print(f"\n   sec {i:02d} '{kw}'")
            videos = search_pexels(kw, per_page=CLIPS_PER_KEYWORD * 3)
            if not videos:
                print(f"     (sin resultados)")
                continue

            downloaded_for_kw = 0
            for video in videos:
                if downloaded_for_kw >= CLIPS_PER_KEYWORD:
                    break

                video_id = video.get("id")
                if video_id in seen_video_ids:
                    continue

                duration = video.get("duration", 0)
                if duration < MIN_CLIP_DURATION_SEC or duration > MAX_CLIP_DURATION_SEC:
                    continue

                vf = pick_video_file(video)
                if not vf:
                    continue

                clip_counter += 1
                filename = f"clip_{clip_counter:02d}.mp4"
                dest = section_dir / filename

                try:
                    size = download_clip(vf["link"], dest)
                    seen_video_ids.add(video_id)
                    total_bytes += size
                    total_clips += 1
                    downloaded_for_kw += 1
                    print(f"     . {filename}  {duration}s  {vf.get('width')}x{vf.get('height')}  {size/1024/1024:.1f}MB")

                    section_clips.append({
                        "filename":     str(dest.relative_to(Path("data"))),
                        "keyword":      kw,
                        "pexels_id":    video_id,
                        "pexels_url":   video.get("url", ""),
                        "duration_sec": duration,
                        "width":        vf.get("width"),
                        "height":       vf.get("height"),
                        "size_bytes":   size,
                    })
                except Exception as e:
                    total_failures += 1
                    print(f"     ! fallo descarga: {e}")

        manifest_sections.append({
            "section_id":    section_id,
            "section_type":  section.get("type"),
            "section_title": section.get("title", ""),
            "keywords":      keywords,
            "clips":         section_clips,
            "n_clips":       len(section_clips),
        })

    if total_clips == 0:
        raise RuntimeError("No se descargo ningun clip. Revisa la API key de Pexels.")

    print(f"\n4/4  Guardando manifest...")
    manifest_path = OUTPUT_DIR / f"broll_manifest_{TODAY}.json"
    manifest_path.write_text(json.dumps({
        "date":          TODAY,
        "youtube_title": script_data.get("youtube_title", ""),
        "n_sections":    len(sections),
        "n_clips_total": total_clips,
        "total_size_mb": round(total_bytes / 1024 / 1024, 2),
        "n_failures":    total_failures,
        "sections":      manifest_sections,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n  Listo en {time.time()-t0:.1f}s")
    print(f"     Clips:     {OUTPUT_DIR}/")
    print(f"     Manifest:  {manifest_path}\n")

    print(f"=== RESUMEN ===")
    print(f"  Clips descargados:  {total_clips}")
    print(f"  Tamano total:       {total_bytes/1024/1024:.1f} MB")
    print(f"  Fallos:             {total_failures}")

    print(f"\n  Desglose:")
    for m in manifest_sections:
        print(f"    {m['section_id']} [{m['section_type']:9s}]  {m['n_clips']} clips")
    print()


if __name__ == "__main__":
    main()
