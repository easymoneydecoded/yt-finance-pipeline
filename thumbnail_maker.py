"""
================================================================================
 thumbnail_maker.py - Etapa 7 (v3, MAXIMA NITIDEZ, DISEÑO AGRESIVO)
================================================================================
 Mejoras v3:
   - Re-descarga el clip elegido en HD (Full HD) desde Pexels para el frame
   - Guarda en PNG (texto sin halo de compresion JPEG)
   - Control de peso: si el PNG supera 2MB, optimiza para caber en YouTube
   - Diseño Minimalista Agresivo (Eliminación de barras, texto gigante, sombras)
================================================================================
"""

import os
import re
import json
import random
import subprocess
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from dotenv import load_dotenv

load_dotenv()

INPUT_DIR      = Path("data")
BROLL_DIR      = Path("data/broll")
OUTPUT_DIR     = Path("data/output")
FONTS_DIR      = Path("data/fonts")
TEMP_DIR       = Path("data/tmp_thumb")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
FONTS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

TODAY          = datetime.now(timezone.utc).strftime("%Y-%m-%d")

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")

THUMB_W        = 1280
THUMB_H        = 720

COLOR_WHITE    = (255, 255, 255)
COLOR_ORANGE   = (255, 107, 0)

OVERLAY_ALPHA  = 115

# YouTube limite de miniatura: 2MB
MAX_THUMB_BYTES = 2 * 1024 * 1024

ANTON_URL      = "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf"
ANTON_PATH     = FONTS_DIR / "Anton-Regular.ttf"
DEJAVU_BOLD    = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf")
DEJAVU_FALLBACK = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def load_data() -> tuple[dict, dict]:
    def _latest(pattern: str, base: Path = INPUT_DIR) -> Path:
        candidates = sorted(base.rglob(pattern), reverse=True)
        if not candidates:
            raise FileNotFoundError(f"No se encontro {pattern}")
        return candidates[0]

    script_path = INPUT_DIR / f"script_{TODAY}.json"
    if not script_path.exists():
        script_path = _latest("script_*.json")
    broll_path = BROLL_DIR / f"broll_manifest_{TODAY}.json"
    if not broll_path.exists():
        broll_path = _latest("broll_manifest_*.json")

    print(f"   . script: {script_path.name}")
    print(f"   . broll:  {broll_path.name}")
    return (
        json.loads(script_path.read_text(encoding="utf-8")),
        json.loads(broll_path.read_text(encoding="utf-8")),
    )


def download_font_if_missing() -> Path:
    if ANTON_PATH.exists() and ANTON_PATH.stat().st_size > 10000:
        return ANTON_PATH
    try:
        print(f"   . descargando fuente Anton...")
        urllib.request.urlretrieve(ANTON_URL, str(ANTON_PATH))
        if ANTON_PATH.stat().st_size > 10000:
            return ANTON_PATH
    except Exception as e:
        print(f"   ! fallo descarga Anton: {e}")
    if DEJAVU_BOLD.exists():
        return DEJAVU_BOLD
    if DEJAVU_FALLBACK.exists():
        return DEJAVU_FALLBACK
    raise RuntimeError("No hay fuente disponible")


def pick_best_broll_clip(broll_manifest: dict) -> dict:
    """Devuelve el dict completo del clip (con pexels_id) para poder re-bajar en HD."""
    body_clips = []
    all_clips = []
    for section in broll_manifest.get("sections", []):
        stype = section.get("section_type")
        for clip in section.get("clips", []):
            clip_path = INPUT_DIR / clip["filename"]
            if not clip_path.exists():
                continue
            all_clips.append(clip)
            if stype == "body":
                body_clips.append(clip)
    pool = body_clips or all_clips
    if not pool:
        raise RuntimeError("No hay clips de b-roll disponibles")
    good = [c for c in pool if c.get("duration_sec", 0) >= 10]
    chosen_pool = good or pool
    random.seed(hash(TODAY))
    return random.choice(chosen_pool)


def download_hd_clip(pexels_id: int) -> Path | None:
    """Re-descarga el clip en la mayor resolucion disponible desde Pexels."""
    if not PEXELS_API_KEY or not pexels_id:
        return None
    try:
        r = requests.get(
            f"https://api.pexels.com/videos/videos/{pexels_id}",
            headers={"Authorization": PEXELS_API_KEY},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"   ! Pexels HD devolvio {r.status_code}, uso clip local")
            return None
        data = r.json()
        # Elegir el video_file de mayor resolucion (mas cercano a 1920 de ancho)
        files = [f for f in data.get("video_files", []) if f.get("file_type") == "video/mp4"]
        if not files:
            return None
        # Preferir el mas cercano a 1920px de ancho (Full HD)
        files.sort(key=lambda f: abs(f.get("width", 0) - 1920))
        best = files[0]
        hd_path = TEMP_DIR / f"hd_clip.mp4"
        with requests.get(best["link"], stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(hd_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
        print(f"   . clip HD descargado: {best.get('width')}x{best.get('height')}")
        return hd_path
    except Exception as e:
        print(f"   ! fallo descarga HD: {e}, uso clip local")
        return None


def extract_frame(clip_path: Path, out_path: Path, offset_sec: float = 3.0) -> None:
    result = subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{offset_sec:.2f}",
        "-i", str(clip_path),
        "-vframes", "1",
        "-q:v", "1",
        str(out_path),
    ], capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists():
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(clip_path),
            "-vframes", "1",
            "-q:v", "1",
            str(out_path),
        ], check=True)


def crop_center(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    src_w, src_h = img.size
    src_ratio = src_w / src_h
    dst_ratio = target_w / target_h
    if src_ratio > dst_ratio:
        new_h = target_h
        new_w = int(src_w * (target_h / src_h))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - target_w) // 2
        img = img.crop((left, 0, left + target_w, target_h))
    else:
        new_w = target_w
        new_h = int(src_h * (target_w / src_w))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        top = (new_h - target_h) // 2
        img = img.crop((0, top, target_w, top + target_h))

    # Mejoras de calidad
    img = img.filter(ImageFilter.UnsharpMask(radius=2.5, percent=140, threshold=2))
    img = ImageEnhance.Contrast(img).enhance(1.10)
    img = ImageEnhance.Color(img).enhance(1.12)
    return img


def wrap_text_for_font(text, font, max_width, draw):
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) <= max_width or not current:
            current = test
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_text_with_shadow(draw, xy, text, font, fill):
    x, y = xy
    # Modern drop shadow: draw text slightly offset in black, then main text
    shadow_color = (0, 0, 0, 180)
    draw.text((x + 6, y + 6), text, font=font, fill=shadow_color)
    draw.text((x, y), text, font=font, fill=fill)


def split_number_from_text(text):
    for pat in [r'\$[\d,\.]+[KMB]?', r'\d+[\.,]?\d*\s*%', r'\d+[KMB]']:
        m = re.search(pat, text)
        if m:
            return (text[:m.start()] + text[m.end():]).strip(), m.group().strip()
    return text, ""


def build_thumbnail(frame_path: Path, thumbnail_text: str, font_path: Path, output_path: Path) -> None:
    bg = Image.open(frame_path).convert("RGB")
    bg = crop_center(bg, THUMB_W, THUMB_H)
    bg = bg.convert("RGBA")

    # Dramatizar el fondo: oscurecerlo más para que el texto resalte
    overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 160))
    bg = Image.alpha_composite(bg, overlay)

    draw = ImageDraw.Draw(bg)

    # Texto principal dominante
    main_text = thumbnail_text.upper()
    text_before, big_number = split_number_from_text(main_text)

    max_text_width = THUMB_W - 100
    # Fuente gigante para impacto máximo
    font_main = ImageFont.truetype(str(font_path), 140)

    # Calcular posicionamiento centralizado
    lines_before = wrap_text_for_font(text_before, font_main, max_text_width, draw) if text_before else []
    line_h = font_main.size + 20
    total_h = (len(lines_before) + (1 if big_number else 0)) * line_h
    y = (THUMB_H - total_h) // 2

    # Dibujar lineas de texto
    for line in lines_before:
        bbox = draw.textbbox((0, 0), line, font=font_main)
        x = (THUMB_W - (bbox[2] - bbox[0])) // 2
        draw_text_with_shadow(draw, (x, y), line, font_main, COLOR_WHITE)
        y += line_h

    if big_number:
        bbox = draw.textbbox((0, 0), big_number, font=font_main)
        x = (THUMB_W - (bbox[2] - bbox[0])) // 2
        draw_text_with_shadow(draw, (x, y), big_number, font_main, COLOR_ORANGE)

    # Guardar como PNG (texto perfecto, sin halo)
    final = bg.convert("RGB")
    final.save(str(output_path), "PNG", optimize=True)

    # Control de peso: si supera 2MB, reducir progresivamente
    if output_path.stat().st_size > MAX_THUMB_BYTES:
        print(f"   . PNG pesa {output_path.stat().st_size/1024/1024:.1f}MB, optimizando...")
        # Reducir colores del fondo con quantize suave manteniendo texto
        for colors in [256, 192, 128]:
            final.quantize(colors=colors, method=Image.FASTOCTREE).convert("RGB").save(
                str(output_path), "PNG", optimize=True)
            if output_path.stat().st_size <= MAX_THUMB_BYTES:
                break
        # Si aun asi se pasa, fallback a JPG calidad alta
        if output_path.stat().st_size > MAX_THUMB_BYTES:
            jpg_path = output_path.with_suffix(".jpg")
            final.save(str(jpg_path), "JPEG", quality=94, optimize=True, subsampling=0)
            output_path.unlink()
            print(f"   . usando JPG de respaldo (PNG no cabia)")
            return jpg_path

    return output_path


def cleanup_temp():
    import shutil
    try:
        shutil.rmtree(TEMP_DIR)
    except Exception:
        pass


def main() -> None:
    print(f"\n=== THUMBNAIL MAKER . {TODAY} ===\n")

    print("1/4  Cargando datos...")
    script_data, broll_manifest = load_data()
    thumbnail_text = script_data.get("thumbnail_text", "").strip()
    if not thumbnail_text:
        thumbnail_text = script_data.get("youtube_title", "MARKET UPDATE")[:30]
    print(f"      texto: '{thumbnail_text}'")

    print("\n2/4  Preparando fuente...")
    font_path = download_font_if_missing()

    print("\n3/4  Obteniendo frame en alta calidad...")
    clip = pick_best_broll_clip(broll_manifest)
    pexels_id = clip.get("pexels_id")
    local_clip = INPUT_DIR / clip["filename"]
    print(f"      clip: {local_clip.name} (pexels {pexels_id})")

    # Intentar bajar version HD; si falla, usar el clip local
    hd_clip = download_hd_clip(pexels_id)
    source_clip = hd_clip if hd_clip else local_clip

    frame_path = TEMP_DIR / "frame.png"
    extract_frame(source_clip, frame_path, offset_sec=3.0)

    print("\n4/4  Componiendo thumbnail (PNG)...")
    output_path = OUTPUT_DIR / f"thumbnail_{TODAY}.png"
    final_path = build_thumbnail(frame_path, thumbnail_text, font_path, output_path)

    cleanup_temp()

    size_kb = final_path.stat().st_size / 1024
    print(f"\n  Listo")
    print(f"     Thumbnail: {final_path}")
    print(f"     Tamano:    {size_kb:.1f} KB")


if __name__ == "__main__":
    main()
