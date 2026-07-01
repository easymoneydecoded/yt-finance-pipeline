"""
================================================================================
 shorts_clipper.py - Etapa 6 del canal de YouTube automatico
================================================================================

 Que hace:
   1. Lee el guion y el audio_manifest del dia
   2. Localiza las secciones marcadas como shorts_candidate = true
   3. Calcula el timestamp exacto de esas secciones en el video largo
   4. Recorta con FFmpeg y transforma a 9:16 vertical (1080x1920)
   5. Guarda 3 Shorts en data/output/short_N_YYYY-MM-DD.mp4

 Coste: 0 EUR
================================================================================
"""

import os
import json
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
INPUT_DIR      = Path("data")
AUDIO_DIR      = Path("data/audio")
OUTPUT_DIR     = Path("data/output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TODAY          = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Objetivo de duracion de cada Short (limite de YouTube: 60s)
TARGET_SHORT_DURATION = 55.0

# Numero maximo de Shorts a generar
MAX_SHORTS = 3

# Vertical 9:16 (formato de Shorts)
SHORT_WIDTH  = 1080
SHORT_HEIGHT = 1920


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def run(cmd: list[str], desc: str = "") -> None:
    if desc:
        print(f"   . {desc}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"   ! FALLO: {' '.join(cmd[:5])}...")
        print(f"   ! stderr: {result.stderr[-500:]}")
        raise RuntimeError(f"FFmpeg fallo: {desc}")


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


def load_manifests() -> tuple[dict, dict, Path]:
    """Carga script, audio_manifest y localiza el video largo del dia."""
    def _latest(pattern: str, base: Path = INPUT_DIR) -> Path:
        candidates = sorted(base.rglob(pattern), reverse=True)
        if not candidates:
            raise FileNotFoundError(f"No se encontro {pattern}")
        return candidates[0]

    script_path = INPUT_DIR / f"script_{TODAY}.json"
    if not script_path.exists():
        script_path = _latest("script_*.json")

    audio_path = AUDIO_DIR / f"audio_manifest_{TODAY}.json"
    if not audio_path.exists():
        audio_path = _latest("audio_manifest_*.json")

    video_path = OUTPUT_DIR / f"video_{TODAY}.mp4"
    if not video_path.exists():
        candidates = sorted(OUTPUT_DIR.glob("video_*.mp4"), reverse=True)
        if not candidates:
            raise FileNotFoundError("No hay video_*.mp4 en output/. Ejecuta editor.py.")
        video_path = candidates[0]

    print(f"   . script: {script_path.name}")
    print(f"   . audio:  {audio_path.name}")
    print(f"   . video:  {video_path.name}")

    return (
        json.loads(script_path.read_text(encoding="utf-8")),
        json.loads(audio_path.read_text(encoding="utf-8")),
        video_path,
    )


# ---------------------------------------------------------------------------
# Localizar las secciones candidatas y sus timestamps
# ---------------------------------------------------------------------------
def find_short_candidates(script_data: dict, audio_manifest: dict) -> list[dict]:
    """
    Devuelve una lista de secciones candidatas con:
      { section_id, title, start_sec, end_sec, alt_hook }
    ordenadas segun orden del guion.
    """
    script_sections = script_data.get("sections", [])
    audio_sections  = audio_manifest.get("sections", [])

    # Construir un dict de duraciones por section_id
    duration_by_id = {
        a.get("section_id"): a.get("duration_sec", 0)
        for a in audio_sections
    }

    candidates = []
    cursor = 0.0  # posicion acumulada en el video largo

    for i, sect in enumerate(script_sections, start=1):
        section_id = f"section_{i:02d}"
        section_dur = duration_by_id.get(section_id, 0)

        if sect.get("shorts_candidate"):
            candidates.append({
                "section_id":  section_id,
                "title":       sect.get("title", ""),
                "start_sec":   cursor,
                "end_sec":     cursor + section_dur,
                "duration":    section_dur,
                "alt_hook":    sect.get("shorts_alt_hook", ""),
                "text_preview": sect.get("text", "")[:100],
            })

        cursor += section_dur

    return candidates


def pick_short_window(candidate: dict) -> tuple[float, float]:
    """
    Elige la ventana de TARGET_SHORT_DURATION segundos dentro de la seccion.
    Preferimos empezar por el principio (mini-hook), pero si es muy larga,
    cortamos al llegar a 55s.
    """
    start = candidate["start_sec"]
    end   = candidate["end_sec"]
    dur   = end - start

    if dur <= TARGET_SHORT_DURATION:
        return (start, end)

    # Empezamos por el principio, cortamos al llegar al target
    return (start, start + TARGET_SHORT_DURATION)


# ---------------------------------------------------------------------------
# Recorte y transformacion a vertical
# ---------------------------------------------------------------------------
def render_short(source_video: Path, start: float, end: float, output: Path) -> None:
    """
    Extrae el segmento [start, end] del video largo y lo transforma
    a 1080x1920 (9:16 vertical).

    La transformacion:
      - Escala el video de 1920x1080 a llenar 1080 de altura (=> 1920x1080 escalado
        a 3413x1920 aprox), luego recorta al centro los 1080 pixeles verticales.
      - Esto es "center crop": mantiene el centro y descarta los lados.
      - Los subtitulos del video original quedan visibles aunque un poco mas
        grandes proporcionalmente por el zoom.
    """
    duration = end - start

    # Filtro: escalar por altura, luego recortar centrado a 1080 de ancho
    vf = (
        f"scale=-2:{SHORT_HEIGHT},"       # escalar manteniendo aspect ratio para altura 1920
        f"crop={SHORT_WIDTH}:{SHORT_HEIGHT}:(iw-{SHORT_WIDTH})/2:0"  # crop centrado 1080 ancho
    )

    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-i", str(source_video),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output),
    ], f"cortando {start:.1f}s -> {end:.1f}s")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    t0 = time.time()
    print(f"\n=== SHORTS CLIPPER . {TODAY} ===\n")

    print("1/3  Cargando datos...")
    script_data, audio_manifest, video_path = load_manifests()

    video_duration = ffprobe_duration(video_path)
    print(f"      video largo: {video_duration:.1f}s ({video_duration/60:.2f} min)")

    print("\n2/3  Localizando secciones candidatas a Short...")
    candidates = find_short_candidates(script_data, audio_manifest)

    if not candidates:
        print("   ! No hay secciones marcadas como shorts_candidate")
        print("   . Saltando etapa 6 sin errores")
        return

    print(f"      {len(candidates)} candidatas encontradas:")
    for i, c in enumerate(candidates, 1):
        title = c["title"] or "(sin titulo)"
        print(f"        {i}. {c['section_id']} '{title[:40]}' "
              f"[{c['start_sec']:.0f}s -> {c['end_sec']:.0f}s = {c['duration']:.0f}s]")

    # Tomamos las primeras MAX_SHORTS (ya vienen ordenadas por orden en el guion)
    selected = candidates[:MAX_SHORTS]

    print(f"\n3/3  Generando {len(selected)} Shorts verticales (9:16)...")
    manifest_shorts = []

    for i, c in enumerate(selected, start=1):
        start, end = pick_short_window(c)
        output_name = f"short_{i}_{TODAY}.mp4"
        output_path = OUTPUT_DIR / output_name

        try:
            render_short(video_path, start, end, output_path)
            size_mb = output_path.stat().st_size / 1024 / 1024
            duration_actual = ffprobe_duration(output_path)
            print(f"      -> {output_name}  {duration_actual:.1f}s  {size_mb:.1f}MB")

            manifest_shorts.append({
                "short_id":      f"short_{i}",
                "filename":      output_name,
                "source_section": c["section_id"],
                "section_title": c["title"],
                "start_sec":     round(start, 2),
                "end_sec":       round(end, 2),
                "duration_sec":  round(duration_actual, 2),
                "size_bytes":    output_path.stat().st_size,
                "alt_hook":      c["alt_hook"],
                "text_preview":  c["text_preview"],
            })
        except Exception as e:
            print(f"      ! fallo generando {output_name}: {e}")

    if not manifest_shorts:
        raise RuntimeError("No se genero ningun Short")

    # Guardar manifest
    manifest_path = OUTPUT_DIR / f"shorts_manifest_{TODAY}.json"
    manifest_path.write_text(json.dumps({
        "date":           TODAY,
        "youtube_title":  script_data.get("youtube_title", ""),
        "source_video":   video_path.name,
        "n_shorts":       len(manifest_shorts),
        "shorts":         manifest_shorts,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n  Listo en {time.time()-t0:.1f}s")
    print(f"     Shorts:    {OUTPUT_DIR}/")
    print(f"     Manifest:  {manifest_path}\n")

    print(f"=== RESUMEN ===")
    print(f"  Shorts generados: {len(manifest_shorts)}/{len(selected)}")
    for m in manifest_shorts:
        print(f"    {m['short_id']}: {m['duration_sec']:.1f}s desde {m['source_section']}")
    print()


if __name__ == "__main__":
    main()
