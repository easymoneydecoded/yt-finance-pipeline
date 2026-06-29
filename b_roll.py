"""
================================================================================
 b_roll.py - Etapa 4 del canal de YouTube automatico
================================================================================

 Que hace:
   1. Lee el JSON de guion generado por la etapa 2
   2. Pide a Groq que extraiga keywords VISUALES por seccion
   3. Busca y descarga clips de stock video gratis desde Pexels
   4. Guarda los MP4 en data/broll/section_XX/ y un manifest JSON

 Coste: 0 EUR (Pexels free tier: 200 peticiones/hora, 20.000/mes)

 Nota: los MP4 son grandes (~250MB/dia) y NO se suben al artifact final.
 Se consumen en la etapa 5 (montaje) y se descartan al terminar el workflow.

================================================================================
"""

import os
import json
import time
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
PEXELS_API_KEY  = os.getenv("PEXELS_API_KEY")

GROQ_MODEL      = "llama-3.3-70b-versatile"
GROQ_ENDPOINT   = "https://api.groq.com/openai/v1/chat/completions"

PEXELS_ENDPOINT = "https://api.pexels.com/videos/search"

INPUT_DIR       = Path("data")
OUTPUT_DIR      = Path("data/broll")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TODAY           = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Cuantas keywords visuales pedir a Groq por tipo de seccion
KEYWORDS_PER_SECTION = {
    "hook":      2,
    "intro":     3,
    "body":      3,
    "retention": 2,
    "outro":     2,
}

# Cuantos clips descargar por keyword
CLIPS_PER_KEYWORD = 2

# Resolucion target: 1280 = HD 720p (buen balance calidad/tamano)
TARGET_WIDTH = 1280

# Filtros de calidad para descartar clips malos
MIN_CLIP_DURATION_SEC = 5      # Descartar clips de menos de 5 segundos
MAX_CLIP_DURATION_SEC = 60     # Descartar clips de mas de 60 segundos


# ---------------------------------------------------------------------------
# Carga del guion del dia
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
# Extraccion de keywords visuales con Groq
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


def call_groq(prompt: str, max_tokens: int = 2000) -> dict:
    payload = {
        "model":       GROQ_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens":  max_tokens,
        "response_format": {"type": "json_object"},
    }
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        r = requests.post(
            GROQ_ENDPOINT,
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type":  "application/json",
            },
            json    = payload,
            timeout = 90,
        )
        if r.status_code == 200:
            break
        if r.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
            wait = 45 * attempt
            print(f"   . Groq devolvio {r.status_code}, reintento en {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def extract_keywords(script_data: dict) -> list[list[str]]:
    """
    Llama a Groq UNA SOLA VEZ con todas las secciones y devuelve una lista
    paralela de keywords por seccion.
    """
    sections = script_data.get("sections", [])
    if not sections:
        return []

    # Construir el bloque de secciones para el prompt
    sections_block_lines = []
    for i, s in enumerate(sections, 1):
        stype = s.get("type", "body")
        n_kw = KEYWORDS_PER_SECTION.get(stype, 2)
        text = s.get("text", "").strip()
        # Truncar texto para no inflar el prompt
        if len(text) > 400:
            text = text[:400] + "..."
        sections_block_lines.append(
            f"{i}. [{stype.upper()}] (need {n_kw} keywords)\n   {text}"
        )
    sections_block = "\n\n".join(sections_block_lines)

    prompt = KEYWORDS_PROMPT.format(
        video_title   = script_data.get("youtube_title", ""),
        thumbnail     = script_data.get("thumbnail_text", ""),
        sections_block = sections_block,
    )

    print(f"   . Pidiendo keywords visuales a Groq para {len(sections)} secciones...")
    raw = call_groq(prompt)

    # Parseo defensivo: convertir a lista paralela
    result = [[] for _ in sections]
    for entry in raw.get("section_keywords", []):
        idx = entry.get("section_index", 0) - 1  # 1-indexed -> 0-indexed
        if 0 <= idx < len(sections):
            kws = entry.get("keywords", [])
            result[idx] = [str(k).strip() for k in kws if k]

    # Fallback: si alguna seccion se quedo sin keywords, usar el titulo del video
    fallback_kw = script_data.get("youtube_title", "business finance").lower()
    for i, kws in enumerate(result):
        if not kws:
            print(f"   ! Seccion {i+1} sin keywords, usando fallback")
            result[i] = [fallback_kw]

    return result


# ---------------------------------------------------------------------------
# Pexels: busqueda y descarga
# ---------------------------------------------------------------------------
def search_pexels(query: str, per_page: int = 5) -> list[dict]:
    """Devuelve la lista de videos de Pexels para una query."""
    if not PEXELS_API_KEY:
        raise RuntimeError("Falta PEXELS_API_KEY")

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
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
        if r.status_code == 200:
            return r.json().get("videos", [])
        if r.status_code == 429 and attempt < max_attempts:
            wait = 20 * attempt
            print(f"     ! Pexels rate limit, espero {wait}s...")
            time.sleep(wait)
            continue
        print(f"     ! Pexels devolvio {r.status_code} para '{query}'")
        return []
    return []


def pick_video_file(video: dict, target_width: int = TARGET_WIDTH) -> dict | None:
    """De los video_files disponibles, escoge el MP4 mas cercano a target_width."""
    candidates = [
        f for f in video.get("video_files", [])
        if f.get("file_type") == "video/mp4" and f.get("link")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda c: abs(c.get("width", 9999) - target_width))
    return candidates[0]


def download_clip(url: str, dest_path: Path) -> int:
    """Descarga un MP4 a disco. Devuelve el tamano en bytes."""
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
    if not GROQ_API_KEY:
        raise RuntimeError("Falta GROQ_API_KEY en variables de entorno")

    print("1/4  Cargando guion...")
    script_data = load_todays_script()
    sections = script_data.get("sections", [])
    print(f"      -> '{script_data.get('youtube_title', '?')}'")
    print(f"      -> {len(sections)} secciones")

    print(f"\n2/4  Extrayendo keywords visuales con Groq...")
    section_keywords = extract_keywords(script_data)

    # Mostrar las keywords
    for i, (s, kws) in enumerate(zip(sections, section_keywords), 1):
        print(f"      sec {i:02d} [{s.get('type'):9s}]: {', '.join(kws)}")

    print(f"\n3/4  Descargando clips de Pexels...")
    manifest_sections = []
    seen_video_ids = set()  # Evitar descargar el mismo clip dos veces
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
                    continue  # Ya descargado para otra seccion/keyword

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
            "section_id":     section_id,
            "section_type":   section.get("type"),
            "section_title":  section.get("title", ""),
            "keywords":       keywords,
            "clips":          section_clips,
            "n_clips":        len(section_clips),
        })

    if total_clips == 0:
        raise RuntimeError("No se descargo ningun clip. Revisa la API key de Pexels.")

    print(f"\n4/4  Guardando manifest...")
    manifest_path = OUTPUT_DIR / f"broll_manifest_{TODAY}.json"
    manifest_path.write_text(json.dumps({
        "date":                TODAY,
        "youtube_title":       script_data.get("youtube_title", ""),
        "n_sections":          len(sections),
        "n_clips_total":       total_clips,
        "total_size_mb":       round(total_bytes / 1024 / 1024, 2),
        "n_failures":          total_failures,
        "sections":            manifest_sections,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n  Listo en {time.time()-t0:.1f}s")
    print(f"     Clips:     {OUTPUT_DIR}/")
    print(f"     Manifest:  {manifest_path}\n")

    print(f"=== RESUMEN ===")
    print(f"  Clips descargados:  {total_clips}")
    print(f"  Tamano total:       {total_bytes/1024/1024:.1f} MB")
    print(f"  Fallos:             {total_failures}")

    # Desglose por seccion
    print(f"\n  Desglose:")
    for m in manifest_sections:
        print(f"    {m['section_id']} [{m['section_type']:9s}]  {m['n_clips']} clips")
    print()


if __name__ == "__main__":
    main()
