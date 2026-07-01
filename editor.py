"""
================================================================================
 editor.py - Etapa 5 del canal de YouTube automatico
================================================================================

 Que hace (en 6 pasos):
   1. Concatena los MP3 de las secciones en un audio unico (voz continua)
   2. Elige una musica al azar de music/ y la mezcla al 10% de volumen bajo la voz
   3. Empalma los clips de b-roll cambiando cada 4-6 segundos (aleatorio)
      - Escala/recorta cada clip a 1920x1080 uniforme (evita saltos raros)
      - Bucle si el total de b-roll no cubre la duracion completa del audio
   4. Transcribe el audio final con Whisper (modelo tiny) para tener el timing
   5. Genera un archivo .ass con subtitulos frase por frase, estilo pro
   6. Renderiza el video final con FFmpeg: video + audio mezclado + subs quemados

 Output:  data/output/video_YYYY-MM-DD.mp4  (1080p, H.264, listo para YouTube)

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

# Video final: 1080p, 30 fps
TARGET_WIDTH   = 1920
TARGET_HEIGHT  = 1080
TARGET_FPS     = 30

# B-roll: cambio de clip cada 4-6 segundos (aleatorio para ritmo natural)
CLIP_DURATION_MIN = 4.0
CLIP_DURATION_MAX = 6.0

# Musica de fondo: nivel bajo bajo la voz
MUSIC_VOLUME   = 0.08   # 8% del volumen original (bajo pero audible)
VOICE_VOLUME   = 1.0    # 100% voz principal

# Whisper: modelo pequeno y rapido, suficiente para audio TTS limpio
WHISPER_MODEL  = "tiny.en"

# Semilla para que dentro de un mismo vídeo el b-roll y música sean reproducibles
random.seed(int(datetime.now(timezone.utc).timestamp()))


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def run(cmd: list[str], desc: str = "") -> None:
    """Ejecuta un comando de shell. Si falla, para el pipeline."""
    if desc:
        print(f"   . {desc}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"   ! FALLO: {' '.join(cmd[:5])}...")
        print(f"   ! stderr: {result.stderr[-500:]}")
        raise RuntimeError(f"FFmpeg fallo: {desc}")


def ffprobe_duration(path: Path) -> float:
    """Devuelve la duracion de un archivo audio/video en segundos."""
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
    """Carga los 3 manifest: script, audio, broll."""
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
# PASO 1 — Concatenar los MP3 de las secciones
# ---------------------------------------------------------------------------
def concat_voice_audio(audio_manifest: dict) -> Path:
    """
    Une los MP3 de todas las secciones en un solo WAV (para mejor calidad de mezcla).
    """
    sections = audio_manifest.get("sections", [])
    if not sections:
        raise RuntimeError("Manifest de audio sin secciones")

    # Fichero de lista para FFmpeg concat demuxer
    list_file = TEMP_DIR / "voice_list.txt"
    with open(list_file, "w") as f:
        for s in sections:
            # Los paths del manifest son relativos a data/
            audio_path = (INPUT_DIR / s["audio_file"]).resolve()
            # Escapar para el concat demuxer (comilla simple)
            f.write(f"file '{audio_path}'\n")

    voice_path = TEMP_DIR / "voice.wav"
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c:a", "pcm_s16le",  # WAV sin comprimir para mezcla limpia
        "-ar", "44100",
        str(voice_path),
    ], "concatenando voz")

    return voice_path


# ---------------------------------------------------------------------------
# PASO 2 — Elegir musica y mezclar con la voz
# ---------------------------------------------------------------------------
def mix_voice_with_music(voice_path: Path, target_duration: float) -> Path:
    """
    Mezcla la voz con una musica de fondo (elegida al azar) al volumen configurado.
    Si la musica es mas corta que el video, la loopea. Si es mas larga, la corta.
    """
    music_files = sorted(MUSIC_DIR.glob("*.mp3")) + sorted(MUSIC_DIR.glob("*.wav"))
    if not music_files:
        print("   ! Carpeta music/ vacia — sigo SIN musica de fondo.")
        return voice_path  # No hay musica: devolvemos la voz tal cual

    chosen = random.choice(music_files)
    print(f"   . musica elegida: {chosen.name}")

    mixed_path = TEMP_DIR / "voice_music.wav"

    # -stream_loop -1 en el input de musica = loopea infinitamente
    # amix con weights para que la voz domine y la musica quede de fondo
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
# PASO 3 — Empalmar los clips de b-roll
# ---------------------------------------------------------------------------
def prepare_broll_track(broll_manifest: dict, target_duration: float) -> Path:
    """
    Construye una pista de video continua a partir de los clips de b-roll,
    cambiando cada 4-6 segundos, escalando todo a 1920x1080.
    """
    # Recolectar todos los clips en orden de seccion
    all_clips = []
    for section in broll_manifest.get("sections", []):
        for clip in section.get("clips", []):
            clip_path = INPUT_DIR / clip["filename"]
            if clip_path.exists():
                all_clips.append(clip_path)

    if not all_clips:
        raise RuntimeError("No hay clips de b-roll disponibles")

    # Barajar para variedad visual (no todos los "hook" al principio)
    random.shuffle(all_clips)

    # Vamos cortando trozos de cada clip hasta cubrir target_duration
    segments = []
    accumulated = 0.0
    clip_idx = 0

    while accumulated < target_duration:
        clip_path = all_clips[clip_idx % len(all_clips)]
        clip_idx += 1

        clip_len = ffprobe_duration(clip_path)
        if clip_len < 2:  # clip corrupto o muy corto, saltar
            continue

        # Duracion aleatoria de este segmento, sin exceder la del clip original
        segment_dur = random.uniform(CLIP_DURATION_MIN, CLIP_DURATION_MAX)
        segment_dur = min(segment_dur, clip_len)
        # No pasar del total del video
        segment_dur = min(segment_dur, target_duration - accumulated)

        if segment_dur < 1.5:  # ultimo trocito, mejor cortar aqui
            break

        # Punto de inicio aleatorio (evita que muchos clips empiecen en frame 0)
        max_start = max(0.0, clip_len - segment_dur)
        start_time = random.uniform(0, max_start * 0.5)

        segments.append({
            "clip": clip_path,
            "start": start_time,
            "duration": segment_dur,
        })
        accumulated += segment_dur

    print(f"   . {len(segments)} segmentos de b-roll para {accumulated:.1f}s")

    # Renderizar cada segmento por separado, todos con el mismo tamano/fps
    # (necesario para que el concat final no falle)
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
                f"fps={TARGET_FPS}"
            ),
            "-an",  # sin audio (los clips de Pexels tienen audio residual)
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            str(out_path),
        ], f"normalizando segmento {i+1}/{len(segments)}")
        normalized_segments.append(out_path)

    # Concatenar todos los segmentos normalizados
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
# PASO 4 — Transcribir con Whisper
# ---------------------------------------------------------------------------
def transcribe_audio(audio_path: Path) -> list[dict]:
    """
    Transcribe el audio con Whisper y devuelve la lista de segmentos:
      [{"start": 0.0, "end": 3.4, "text": "..."}]
    """
    import whisper  # import local: solo lo necesitamos aqui

    print(f"   . cargando modelo whisper '{WHISPER_MODEL}'...")
    model = whisper.load_model(WHISPER_MODEL)

    print("   . transcribiendo audio (2-4 minutos, sin GPU)...")
    result = model.transcribe(
        str(audio_path),
        language="en",
        fp16=False,  # CPU en GitHub runners
        verbose=False,
    )

    segments = [
        {"start": s["start"], "end": s["end"], "text": s["text"].strip()}
        for s in result.get("segments", [])
    ]
    print(f"   . {len(segments)} segmentos transcritos")
    return segments


# ---------------------------------------------------------------------------
# PASO 5 — Generar subtitulos .ass frase por frase
# ---------------------------------------------------------------------------
ASS_HEADER = """[Script Info]
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
    """Formato de tiempo ASS: H:MM:SS.CS"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass_subtitles(segments: list[dict], output_path: Path) -> Path:
    """
    Genera un .ass con los segmentos de Whisper. Cada segmento = una frase
    en pantalla durante su ventana temporal.
    """
    lines = [ASS_HEADER]
    for seg in segments:
        # Limpiar comas de linea para no romper el formato ASS
        text = seg["text"].replace("\n", " ").replace(",", ",")
        # Escapar llaves (ASS las usa para override tags)
        text = text.replace("{", "").replace("}", "")
        # Truncar frases MUY largas (Whisper a veces suelta bloques enteros)
        if len(text) > 90:
            text = text[:87] + "..."
        line = f"Dialogue: 0,{ass_time(seg['start'])},{ass_time(seg['end'])},Default,,0,0,0,,{text}"
        lines.append(line)

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# PASO 6 — Renderizado final
# ---------------------------------------------------------------------------
def render_final_video(
    broll_track: Path,
    mixed_audio: Path,
    subtitles: Path,
    total_duration: float,
) -> Path:
    """
    Junta la pista de b-roll + audio mezclado + subtitulos quemados
    en el MP4 final.
    """
    output_path = OUTPUT_DIR / f"video_{TODAY}.mp4"

    # El path de subtitulos hay que escaparlo para el filtro ass=
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
        "-movflags", "+faststart",  # metadata al inicio, mejor para streaming
        str(output_path),
    ], "render final")

    return output_path


# ---------------------------------------------------------------------------
# Cleanup del tmp
# ---------------------------------------------------------------------------
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

    print("0/6  Cargando manifests...")
    script_data, audio_manifest, broll_manifest = load_manifests()

    total_voice_duration = audio_manifest.get("total_duration_sec", 0)
    if total_voice_duration < 60:
        raise RuntimeError(f"Duracion de voz sospechosa: {total_voice_duration}s")

    print(f"      voz total: {total_voice_duration:.1f}s ({total_voice_duration/60:.1f} min)")

    print("\n1/6  Concatenando voz de todas las secciones...")
    voice_path = concat_voice_audio(audio_manifest)
    real_voice_duration = ffprobe_duration(voice_path)
    print(f"      voz concatenada: {real_voice_duration:.1f}s")

    print("\n2/6  Mezclando voz con musica de fondo...")
    mixed_audio = mix_voice_with_music(voice_path, real_voice_duration)

    print("\n3/6  Preparando pista de b-roll...")
    broll_track = prepare_broll_track(broll_manifest, real_voice_duration)

    print("\n4/6  Transcribiendo audio con Whisper...")
    segments = transcribe_audio(mixed_audio)

    print("\n5/6  Generando subtitulos...")
    subs_path = TEMP_DIR / "subtitles.ass"
    build_ass_subtitles(segments, subs_path)
    print(f"      subtitulos: {len(segments)} lineas")

    print("\n6/6  Renderizando video final...")
    final_video = render_final_video(broll_track, mixed_audio, subs_path, real_voice_duration)

    final_size_mb = final_video.stat().st_size / 1024 / 1024
    final_duration = ffprobe_duration(final_video)

    cleanup_temp()

    print(f"\n  Listo en {time.time()-t0:.1f}s")
    print(f"     Video:  {final_video}")

    print(f"\n=== RESUMEN ===")
    print(f"  Duracion:  {final_duration:.1f}s ({final_duration/60:.2f} min)")
    print(f"  Tamano:    {final_size_mb:.1f} MB")
    print(f"  Titulo:    {script_data.get('youtube_title', '?')}")


if __name__ == "__main__":
    main()
