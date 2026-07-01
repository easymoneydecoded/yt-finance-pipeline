"""
================================================================================
 shorts_clipper.py - Etapa 6 del canal de YouTube automatico (v2)
================================================================================

 Que hace:
   1. Lee el guion y el audio_manifest del dia
   2. Localiza las secciones marcadas como shorts_candidate = true
   3. Usa el video LIMPIO (sin subs quemados) generado por la etapa 5
   4. Recorta el segmento y transforma a 9:16 vertical (1080x1920)
   5. Transcribe SOLO ese segmento con Whisper (rapido, 15-30s por Short)
   6. Genera subtitulos .ass optimizados para vertical (lineas cortas, centrado)
   7. Quema los subs nuevos encima del video vertical
   8. Guarda cada Short como short_N_YYYY-MM-DD.mp4

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
TEMP_DIR       = Path("data/tmp_shorts")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

TODAY          = datetime.now(timezone.utc).strftime("%Y-%m-%d")

TARGET_SHORT_DURATION = 55.0
MAX_SHORTS = 3

SHORT_WIDTH  = 1080
SHORT_HEIGHT = 1920

WHISPER_MODEL = "tiny.en"

# Limite de caracteres por linea de subtitulos (calibrado para 1080px de ancho)
MAX_CHARS_PER_LINE = 22


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


def load_data() -> tuple[dict, dict, Path]:
    """Carga script, audio_manifest y localiza el VIDEO LIMPIO del dia."""
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

    # Priorizamos el video LIMPIO (sin subs) generado por editor v2
    video_path = OUTPUT_DIR / f"video_clean_{TODAY}.mp4"
    if not video_path.exists():
        # Fallback: buscar el video_clean mas reciente
        clean_candidates = sorted(OUTPUT_DIR.glob("video_clean_*.mp4"), reverse=True)
        if clean_candidates:
            video_path = clean_candidates[0]
        else:
            # Ultimo fallback: usar el video con subs (aunque se vera doble)
            print("   ! No hay video_clean, uso video con subs quemados (subs saldran cortados)")
            video_path = _latest("video_*.mp4", OUTPUT_DIR)

    print(f"   . script: {script_path.name}")
    print(f"   . audio:  {audio_path.name}")
    print(f"   . video:  {video_path.name}")

    return (
        json.loads(script_path.read_text(encoding="utf-8")),
        json.loads(audio_path.read_text(encoding="utf-8")),
        video_path,
    )


# ---------------------------------------------------------------------------
# Localizar secciones candidatas y sus timestamps
# ---------------------------------------------------------------------------
def find_short_candidates(script_data: dict, audio_manifest: dict) -> list[dict]:
    script_sections = script_data.get("sections", [])
    audio_sections  = audio_manifest.get("sections", [])

    duration_by_id = {
        a.get("section_id"): a.get("duration_sec", 0)
        for a in audio_sections
    }

    candidates = []
    cursor = 0.0

    for i, sect in enumerate(script_sections, start=1):
        section_id = f"section_{i:02d}"
        section_dur = duration_by_id.get(section_id, 0)

        if sect.get("shorts_candidate"):
            candidates.append({
                "section_id":   section_id,
                "title":        sect.get("title", ""),
                "start_sec":    cursor,
                "end_sec":      cursor + section_dur,
                "duration":     section_dur,
                "alt_hook":     sect.get("shorts_alt_hook", ""),
                "text_preview": sect.get("text", "")[:100],
            })

        cursor += section_dur

    return candidates


def pick_short_window(candidate: dict) -> tuple[float, float]:
    start = candidate["start_sec"]
    end   = candidate["end_sec"]
    dur   = end - start

    if dur <= TARGET_SHORT_DURATION:
        return (start, end)

    return (start, start + TARGET_SHORT_DURATION)


# ---------------------------------------------------------------------------
# Recorte a vertical (SIN subs, base limpia)
# ---------------------------------------------------------------------------
def extract_vertical_segment(source_video: Path, start: float, end: float, output: Path) -> None:
    """
    Extrae [start, end] del video largo LIMPIO y transforma a 1080x1920.
    Sin subs. Devuelve un MP4 al que luego le quemamos subs nuevos.
    """
    duration = end - start

    vf = (
        f"scale=-2:{SHORT_HEIGHT},"
        f"crop={SHORT_WIDTH}:{SHORT_HEIGHT}:(iw-{SHORT_WIDTH})/2:0"
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
        str(output),
    ], f"recortando vertical {start:.1f}s -> {end:.1f}s")


# ---------------------------------------------------------------------------
# Transcripcion Whisper del Short (rapido)
# ---------------------------------------------------------------------------
_whisper_model = None  # cache del modelo (evitar recargar por cada Short)

def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        import whisper
        print(f"   . cargando modelo whisper '{WHISPER_MODEL}'...")
        _whisper_model = whisper.load_model(WHISPER_MODEL)
    return _whisper_model


def transcribe_short(short_path: Path) -> list[dict]:
    model = get_whisper_model()
    result = model.transcribe(
        str(short_path),
        language="en",
        fp16=False,
        verbose=False,
    )
    return [
        {"start": s["start"], "end": s["end"], "text": s["text"].strip()}
        for s in result.get("segments", [])
    ]


# ---------------------------------------------------------------------------
# Subtitulos verticales optimizados
# ---------------------------------------------------------------------------
# Estilo optimizado para 1080x1920:
# - Fuente ligeramente mas pequena que el largo (60pt vs 72pt)
# - Alignment 2 = centro-abajo, con margen vertical grande
# - Se rompe el texto en lineas de max 22 chars
ASS_HEADER_SHORT = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,5,2,2,40,40,500,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def wrap_text(text: str, max_chars: int = MAX_CHARS_PER_LINE) -> str:
    """
    Divide un texto en lineas de max_chars caracteres, cortando por palabras.
    Devuelve las lineas separadas por \\N (nueva linea en formato ASS).
    """
    words = text.split()
    lines = []
    current = ""

    for word in words:
        # Si anadir esta palabra excederia el limite, cerrar linea y empezar otra
        if current and len(current) + 1 + len(word) > max_chars:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word

    if current:
        lines.append(current)

    # ASS usa \\N (dos caracteres) para separar lineas dentro de un dialogo
    return "\\N".join(lines)


def build_short_subtitles(segments: list[dict], output_path: Path) -> Path:
    lines = [ASS_HEADER_SHORT]
    for seg in segments:
        text = seg["text"].replace("\n", " ")
        text = text.replace("{", "").replace("}", "")

        # Envolver a lineas cortas
        wrapped = wrap_text(text, MAX_CHARS_PER_LINE)

        line = f"Dialogue: 0,{ass_time(seg['start'])},{ass_time(seg['end'])},Default,,0,0,0,,{wrapped}"
        lines.append(line)

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def burn_subs_on_short(source: Path, subs: Path, output: Path) -> None:
    """Quema los subs en el Short y produce el archivo final."""
    subs_arg = str(subs).replace("\\", "/").replace(":", "\\:")

    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(source),
        "-vf", f"ass={subs_arg}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "copy",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output),
    ], "quemando subs verticales")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def cleanup_temp() -> None:
    import shutil
    try:
        shutil.rmtree(TEMP_DIR)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    t0 = time.time()
    print(f"\n=== SHORTS CLIPPER . {TODAY} ===\n")

    print("1/4  Cargando datos...")
    script_data, audio_manifest, video_path = load_data()

    video_duration = ffprobe_duration(video_path)
    print(f"      video base: {video_duration:.1f}s ({video_duration/60:.2f} min)")

    print("\n2/4  Localizando secciones candidatas a Short...")
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

    selected = candidates[:MAX_SHORTS]

    print(f"\n3/4  Generando {len(selected)} Shorts (recorte + subs propios)...")
    manifest_shorts = []

    for i, c in enumerate(selected, start=1):
        start, end = pick_short_window(c)
        print(f"\n   Short {i}/{len(selected)} desde {c['section_id']}:")

        # a) Recortar vertical sin subs
        raw_short = TEMP_DIR / f"raw_short_{i}.mp4"
        try:
            extract_vertical_segment(video_path, start, end, raw_short)
        except Exception as e:
            print(f"      ! fallo recortando: {e}")
            continue

        # b) Transcribir el Short
        print(f"      . transcribiendo con Whisper...")
        try:
            segments = transcribe_short(raw_short)
            print(f"      . {len(segments)} lineas de subtitulos")
        except Exception as e:
            print(f"      ! fallo transcribiendo: {e}")
            continue

        # c) Generar .ass y quemarlo
        subs_path = TEMP_DIR / f"subs_{i}.ass"
        build_short_subtitles(segments, subs_path)

        final_short = OUTPUT_DIR / f"short_{i}_{TODAY}.mp4"
        try:
            burn_subs_on_short(raw_short, subs_path, final_short)
        except Exception as e:
            print(f"      ! fallo quemando subs: {e}")
            continue

        size_mb = final_short.stat().st_size / 1024 / 1024
        duration_actual = ffprobe_duration(final_short)
        print(f"      -> {final_short.name}  {duration_actual:.1f}s  {size_mb:.1f}MB")

        manifest_shorts.append({
            "short_id":       f"short_{i}",
            "filename":       final_short.name,
            "source_section": c["section_id"],
            "section_title":  c["title"],
            "start_sec":      round(start, 2),
            "end_sec":        round(end, 2),
            "duration_sec":   round(duration_actual, 2),
            "size_bytes":     final_short.stat().st_size,
            "alt_hook":       c["alt_hook"],
            "text_preview":   c["text_preview"],
        })

    if not manifest_shorts:
        raise RuntimeError("No se genero ningun Short")

    manifest_path = OUTPUT_DIR / f"shorts_manifest_{TODAY}.json"
    manifest_path.write_text(json.dumps({
        "date":           TODAY,
        "youtube_title":  script_data.get("youtube_title", ""),
        "source_video":   video_path.name,
        "n_shorts":       len(manifest_shorts),
        "shorts":         manifest_shorts,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n4/4  Limpieza de temporales...")
    cleanup_temp()

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
