"""
================================================================================
 editor.py - Etapa 5 del canal de YouTube automatico (v2)
================================================================================

 Que hace:
   1. Concatena los MP3 de las secciones (voz continua)
   2. Mezcla la voz con musica de fondo al 8% de volumen
   3. Empalma clips de b-roll cada 4-6s escalados a 1920x1080
   4. Transcribe el audio con Whisper para obtener timings
   5. Genera subtitulos .ass frase por frase (para vídeo largo)
   6. Renderiza DOS versiones:
        - video_YYYY-MM-DD.mp4       -> con subs quemados (final para YouTube)
        - video_clean_YYYY-MM-DD.mp4 -> SIN subs (base para etapa 6 Shorts)

 Coste: 0 EUR
================================================================================
"""

import os
import json
import time
import random
import subprocess
import shutil
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
INPUT_DIR      = Path("data")
AUDIO_DIR      = Path("data/audio")
BROLL_DIR      = Path("data/broll")
MUSIC_DIR      = Path("music")
OUTPUT_DIR     = Path("data/output")
TEMP_DIR       = Path("data/tmp_editor")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

TODAY          = datetime.now(timezone.utc).strftime("%Y-%m-%d")

TARGET_WIDTH   = 1920
TARGET_HEIGHT  = 1080
TARGET_FPS     = 30

CLIP_DURATION_MIN = 2.5
CLIP_DURATION_MAX = 4.0

MUSIC_VOLUME   = 0.08
VOICE_VOLUME   = 1.0

WHISPER_MODEL  = "tiny.en"

random.seed(int(datetime.now(timezone.utc).timestamp()))


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


def load_manifests() -> tuple[dict, dict, dict]:
    def _latest(pattern: str) -> Path:
        candidates = sorted(INPUT_DIR.rglob(pattern), reverse=True)
        if not candidates:
            raise FileNotFoundError(f"No se encontro {pattern}")
        return candidates[0]

    script_path = INPUT_DIR / f"script_{TODAY}.json"
    if not script_path.exists():
        script_path = _latest("script_*.json")
    audio_path = AUDIO_DIR / f"audio_manifest_{TODAY}.json"
    if not audio_path.exists():
        audio_path = _latest("audio_manifest_*.json")
    broll_path = BROLL_DIR / f"broll_manifest_{TODAY}.json"
    if not broll_path.exists():
        broll_path = _latest("broll_manifest_*.json")

    print(f"   . script:  {script_path.name}")
    print(f"   . audio:   {audio_path.name}")
    print(f"   . b-roll:  {broll_path.name}")

    return (
        json.loads(script_path.read_text(encoding="utf-8")),
        json.loads(audio_path.read_text(encoding="utf-8")),
        json.loads(broll_path.read_text(encoding="utf-8")),
    )


# ---------------------------------------------------------------------------
# PASO 1 - Concatenar voz
# ---------------------------------------------------------------------------
def concat_voice_audio(audio_manifest: dict) -> Path:
    sections = audio_manifest.get("sections", [])
    if not sections:
        raise RuntimeError("Manifest de audio sin secciones")

    list_file = TEMP_DIR / "voice_list.txt"
    with open(list_file, "w") as f:
        for s in sections:
            audio_path = (INPUT_DIR / s["audio_file"]).resolve()
            f.write(f"file '{audio_path}'\n")

    voice_path = TEMP_DIR / "voice.wav"
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c:a", "pcm_s16le",
        "-ar", "44100",
        str(voice_path),
    ], "concatenando voz")

    return voice_path


# ---------------------------------------------------------------------------
# PASO 2 - Mezclar con musica
# ---------------------------------------------------------------------------
def mix_voice_with_music(voice_path: Path, target_duration: float) -> Path:
    music_files = sorted(MUSIC_DIR.glob("*.mp3")) + sorted(MUSIC_DIR.glob("*.wav"))
    if not music_files:
        print("   ! Carpeta music/ vacia - sigo SIN musica de fondo.")
        return voice_path

    chosen = random.choice(music_files)
    print(f"   . musica elegida: {chosen.name}")

    mixed_path = TEMP_DIR / "voice_music.wav"

    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(voice_path),
        "-stream_loop", "-1", "-i", str(chosen),
        "-filter_complex",
        (
            f"[0:a]volume={VOICE_VOLUME}[voice];"
            f"[1:a]volume={MUSIC_VOLUME}[music];"
            f"[voice][music]amix=inputs=2:duration=first:dropout_transition=2[out]"
        ),
        "-map", "[out]",
        "-c:a", "pcm_s16le",
        "-t", f"{target_duration:.3f}",
        str(mixed_path),
    ], "mezclando voz + musica")

    return mixed_path


# ---------------------------------------------------------------------------
# PASO 3 - B-roll
# ---------------------------------------------------------------------------
def prepare_broll_track(broll_manifest: dict, target_duration: float) -> Path:
    all_clips = []
    for section in broll_manifest.get("sections", []):
        for clip in section.get("clips", []):
            clip_path = INPUT_DIR / clip["filename"]
            if clip_path.exists():
                all_clips.append(clip_path)

    if not all_clips:
        raise RuntimeError("No hay clips de b-roll disponibles")

    random.shuffle(all_clips)

    segments = []
    accumulated = 0.0
    clip_idx = 0

    while accumulated < target_duration:
        clip_path = all_clips[clip_idx % len(all_clips)]
        clip_idx += 1

        clip_len = ffprobe_duration(clip_path)
        if clip_len < 2:
            continue

        segment_dur = random.uniform(CLIP_DURATION_MIN, CLIP_DURATION_MAX)
        segment_dur = min(segment_dur, clip_len)
        segment_dur = min(segment_dur, target_duration - accumulated)

        if segment_dur < 1.5:
            break

        max_start = max(0.0, clip_len - segment_dur)
        start_time = random.uniform(0, max_start * 0.5)

        segments.append({
            "clip": clip_path,
            "start": start_time,
            "duration": segment_dur,
        })
        accumulated += segment_dur

    print(f"   . {len(segments)} segmentos de b-roll para {accumulated:.1f}s")

    normalized_segments = []
    for i, seg in enumerate(segments):
        out_path = TEMP_DIR / f"seg_{i:03d}.mp4"
        run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{seg['start']:.3f}",
            "-i", str(seg["clip"]),
            "-t", f"{seg['duration']:.3f}",
            "-vf",
            (
                f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={TARGET_WIDTH}:{TARGET_HEIGHT},"
                f"fps={TARGET_FPS},"
                f"zoompan=z='min(zoom+0.001,1.1)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={TARGET_WIDTH}x{TARGET_HEIGHT}"
            ),
            "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            str(out_path),
        ], f"normalizando segmento {i+1}/{len(segments)}")
        normalized_segments.append(out_path)

    list_file = TEMP_DIR / "broll_list.txt"
    with open(list_file, "w") as f:
        for seg in normalized_segments:
            f.write(f"file '{seg.resolve()}'\n")

    broll_track = TEMP_DIR / "broll_track.mp4"
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(broll_track),
    ], "concatenando b-roll")

    return broll_track


# ---------------------------------------------------------------------------
# PASO 4 - Whisper
# ---------------------------------------------------------------------------
def transcribe_audio(audio_path: Path) -> list[dict]:
    import whisper

    print(f"   . cargando modelo whisper '{WHISPER_MODEL}'...")
    model = whisper.load_model(WHISPER_MODEL)

    print("   . transcribiendo audio...")
    result = model.transcribe(
        str(audio_path),
        language="en",
        fp16=False,
        verbose=False,
    )

    segments = [
        {"start": s["start"], "end": s["end"], "text": s["text"].strip()}
        for s in result.get("segments", [])
    ]
    print(f"   . {len(segments)} segmentos transcritos")
    return segments


# ---------------------------------------------------------------------------
# PASO 5 - Subtitulos .ass (para el vídeo largo)
# ---------------------------------------------------------------------------
ASS_HEADER_LONG = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,5,2,2,60,60,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass_subtitles(segments: list[dict], output_path: Path) -> Path:
    lines = [ASS_HEADER_LONG]
    for seg in segments:
        text = seg["text"].replace("\n", " ").replace(",", ",")
        text = text.replace("{", "").replace("}", "")
        if len(text) > 90:
            text = text[:87] + "..."
        line = f"Dialogue: 0,{ass_time(seg['start'])},{ass_time(seg['end'])},Default,,0,0,0,,{text}"
        lines.append(line)

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# PASO 6 - Renders finales (dos versiones)
# ---------------------------------------------------------------------------
def render_final_video(
    broll_track: Path,
    mixed_audio: Path,
    subtitles: Path,
    total_duration: float,
) -> Path:
    """Version con subtitulos quemados - la que se sube a YouTube largo."""
    output_path = OUTPUT_DIR / f"video_{TODAY}.mp4"

    subs_arg = str(subtitles).replace("\\", "/").replace(":", "\\:")

    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(broll_track),
        "-i", str(mixed_audio),
        "-vf", f"ass={subs_arg}",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-t", f"{total_duration:.3f}",
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ], "render final CON subs")

    return output_path


def render_clean_video(
    broll_track: Path,
    mixed_audio: Path,
    total_duration: float,
) -> Path:
    """Version SIN subtitulos - base limpia para recortar Shorts en la etapa 6."""
    output_path = OUTPUT_DIR / f"video_clean_{TODAY}.mp4"

    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(broll_track),
        "-i", str(mixed_audio),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-t", f"{total_duration:.3f}",
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ], "render limpio SIN subs (base para Shorts)")

    return output_path


def cleanup_temp() -> None:
    try:
        shutil.rmtree(TEMP_DIR)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    t0 = time.time()
    print(f"\n=== EDITOR . {TODAY} ===\n")

    print("0/7  Cargando manifests...")
    script_data, audio_manifest, broll_manifest = load_manifests()

    total_voice_duration = audio_manifest.get("total_duration_sec", 0)
    if total_voice_duration < 60:
        raise RuntimeError(f"Duracion de voz sospechosa: {total_voice_duration}s")

    print(f"      voz total: {total_voice_duration:.1f}s ({total_voice_duration/60:.1f} min)")

    print("\n1/7  Concatenando voz...")
    voice_path = concat_voice_audio(audio_manifest)
    real_voice_duration = ffprobe_duration(voice_path)
    print(f"      voz concatenada: {real_voice_duration:.1f}s")

    print("\n2/7  Mezclando voz + musica...")
    mixed_audio = mix_voice_with_music(voice_path, real_voice_duration)

    print("\n3/7  Preparando pista de b-roll...")
    broll_track = prepare_broll_track(broll_manifest, real_voice_duration)

    print("\n4/7  Transcribiendo audio con Whisper...")
    segments = transcribe_audio(mixed_audio)

    print("\n5/7  Generando subtitulos...")
    subs_path = TEMP_DIR / "subtitles.ass"
    build_ass_subtitles(segments, subs_path)
    print(f"      subtitulos: {len(segments)} lineas")

    print("\n6/7  Renderizando video LARGO con subtitulos...")
    final_video = render_final_video(broll_track, mixed_audio, subs_path, real_voice_duration)

    print("\n7/7  Renderizando video LIMPIO (base para Shorts)...")
    clean_video = render_clean_video(broll_track, mixed_audio, real_voice_duration)

    final_size_mb = final_video.stat().st_size / 1024 / 1024
    clean_size_mb = clean_video.stat().st_size / 1024 / 1024
    final_duration = ffprobe_duration(final_video)

    cleanup_temp()

    print(f"\n  Listo en {time.time()-t0:.1f}s")
    print(f"     Final (con subs):  {final_video}  ({final_size_mb:.1f} MB)")
    print(f"     Limpio (sin subs): {clean_video}  ({clean_size_mb:.1f} MB)")

    print(f"\n=== RESUMEN ===")
    print(f"  Duracion:  {final_duration:.1f}s ({final_duration/60:.2f} min)")
    print(f"  Titulo:    {script_data.get('youtube_title', '?')}")


if __name__ == "__main__":
    main()
