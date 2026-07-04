"""
================================================================================
 thumbnail_maker.py - Etapa 7 (v2, estilo LIMPIO CON PUNCH)
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

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

INPUT_DIR      = Path("data")
BROLL_DIR      = Path("data/broll")
OUTPUT_DIR     = Path("data/output")
FONTS_DIR      = Path("data/fonts")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
FONTS_DIR.mkdir(parents=True, exist_ok=True)

TODAY          = datetime.now(timezone.utc).strftime("%Y-%m-%d")

THUMB_W        = 1280
THUMB_H        = 720

COLOR_BG_BAR   = (10, 10, 10)
COLOR_GREEN    = (0, 255, 136)
COLOR_ORANGE   = (255, 107, 0)
COLOR_WHITE    = (255, 255, 255)
COLOR_GREY     = (140, 140, 140)
COLOR_BLACK    = (0, 0, 0)

# Overlay MENOS oscuro (45% vs 55% antes) - deja ver mas el b-roll
OVERLAY_ALPHA  = 115

# Fuente: Anton (gratis, ancha, muy YouTube - similar a Impact)
ANTON_URL      = "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf"
ANTON_PATH     = FONTS_DIR / "Anton-Regular.ttf"

# Fallback siempre disponible
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
        print(f"   . fallback: DejaVu Condensed Bold")
        return DEJAVU_BOLD
    if DEJAVU_FALLBACK.exists():
        print(f"   . fallback: DejaVu Sans Bold")
        return DEJAVU_FALLBACK
    raise RuntimeError("No hay fuente disponible")


def pick_best_broll_clip(broll_manifest: dict) -> Path:
    body_clips = []
    all_clips = []
    for section in broll_manifest.get("sections", []):
        for clip in section.get("clips", []):
            clip_path = INPUT_DIR / clip["filename"]
            if not clip_path.exists():
                continue
            entry = (clip_path, clip.get("duration_sec", 0))
            all_clips.append(entry)
            if section.get("section_type") == "body":
                body_clips.append(entry)
    pool = body_clips or all_clips
    if not pool:
        raise RuntimeError("No hay clips de b-roll disponibles")
    good = [(p, d) for p, d in pool if d >= 10]
    chosen_pool = good or pool
    random.seed(hash(TODAY))
    chosen_path, _ = random.choice(chosen_pool)
    return chosen_path


def extract_frame(clip_path: Path, out_path: Path, offset_sec: float = 3.0) -> None:
    result = subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{offset_sec:.2f}",
        "-i", str(clip_path),
        "-vframes", "1",
        "-q:v", "2",
        str(out_path),
    ], capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists():
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(clip_path),
            "-vframes", "1",
            "-q:v", "2",
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

    # Mejoras de calidad para compensar la compresion del b-roll:
    # 1. Sharpen (nitidez) para definir bordes
    img = img.filter(ImageFilter.UnsharpMask(radius=2.5, percent=140, threshold=2))
    # 2. Realce ligero de contraste
    img = ImageEnhance.Contrast(img).enhance(1.10)
    # 3. Realce ligero de saturacion (colores mas vivos)
    img = ImageEnhance.Color(img).enhance(1.12)

    return img


def wrap_text_for_font(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        w = bbox[2] - bbox[0]
        if w <= max_width or not current:
            current = test
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_text_thick_outline(draw, xy, text, font, fill, outline_w=8):
    """Borde negro MUY grueso para maximo contraste."""
    x, y = xy
    for dx in range(-outline_w, outline_w + 1, 2):
        for dy in range(-outline_w, outline_w + 1, 2):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=COLOR_BLACK)
    draw.text((x, y), text, font=font, fill=fill)


def split_number_from_text(text: str) -> tuple[str, str]:
    """Separa un numero destacado. Devuelve (texto_restante, numero_destacado)."""
    patterns = [
        r'\$[\d,\.]+[KMB]?',
        r'\d+[\.,]?\d*\s*%',
        r'\d+[KMB]',
    ]
    for pat in patterns:
        match = re.search(pat, text)
        if match:
            number = match.group().strip()
            rest = (text[:match.start()] + text[match.end():]).strip()
            return (rest, number)
    return (text, "")


def build_thumbnail(frame_path: Path, thumbnail_text: str, font_path: Path, output_path: Path) -> None:
    # 1. Fondo
    bg = Image.open(frame_path).convert("RGB")
    bg = crop_center(bg, THUMB_W, THUMB_H)

    # 2. Overlay oscuro (45%)
    bg = bg.convert("RGBA")
    overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, OVERLAY_ALPHA))
    bg = Image.alpha_composite(bg, overlay)

    # 3. Barra superior
    top_bar_h = 60
    draw = ImageDraw.Draw(bg)
    draw.rectangle([0, 0, THUMB_W, top_bar_h], fill=COLOR_BG_BAR)
    draw.rectangle([0, top_bar_h, THUMB_W, top_bar_h + 4], fill=COLOR_GREEN)

    font_small = ImageFont.truetype(str(font_path), 30)

    # LIVE
    draw.ellipse([25, 22, 41, 38], fill=COLOR_GREEN)
    draw.text((52, 15), "LIVE", font=font_small, fill=COLOR_GREEN)
    # TICKER PRESS
    draw.text((155, 15), "TICKER PRESS", font=font_small, fill=COLOR_WHITE)
    # Handle derecha
    handle = "@TICKERPRESSYT"
    bbox = draw.textbbox((0, 0), handle, font=font_small)
    h_w = bbox[2] - bbox[0]
    draw.text((THUMB_W - 30 - h_w, 15), handle, font=font_small, fill=COLOR_GREEN)

    # 4. BADGE ALERT naranja en la esquina superior derecha (debajo de la barra)
    badge_font = ImageFont.truetype(str(font_path), 42)
    badge_text = "⚡ ALERT"
    bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
    badge_w = bbox[2] - bbox[0] + 40
    badge_h = 60
    badge_x = THUMB_W - badge_w - 20
    badge_y = 90
    draw.rectangle([badge_x, badge_y, badge_x + badge_w, badge_y + badge_h], fill=COLOR_ORANGE)
    draw.text((badge_x + 20, badge_y + 6), badge_text, font=badge_font, fill=COLOR_BLACK)

    # 5. TEXTO PRINCIPAL - Separar numero del resto
    main_text = thumbnail_text.upper()
    text_before, big_number = split_number_from_text(main_text)

    # Buscamos la fuente MAS GRANDE que quepa
    max_text_width = THUMB_W - 80
    font_main = None
    for size in range(200, 90, -10):
        f = ImageFont.truetype(str(font_path), size)
        lines_before = wrap_text_for_font(text_before, f, max_text_width, draw) if text_before else []
        num_line = [big_number] if big_number else []
        all_lines = lines_before + num_line
        line_h = size + 15
        total_h = len(all_lines) * line_h
        if total_h < (THUMB_H - top_bar_h - 60 - 60):
            font_main = f
            break
    if font_main is None:
        font_main = ImageFont.truetype(str(font_path), 90)

    lines_before = wrap_text_for_font(text_before, font_main, max_text_width, draw) if text_before else []
    line_h = font_main.size + 15
    total_h = (len(lines_before) + (1 if big_number else 0)) * line_h
    y_start = top_bar_h + ((THUMB_H - top_bar_h - 60 - total_h) // 2)

    # Blanco con borde negro GRUESO
    y = y_start
    for line in lines_before:
        bbox = draw.textbbox((0, 0), line, font=font_main)
        line_w = bbox[2] - bbox[0]
        x = (THUMB_W - line_w) // 2
        draw_text_thick_outline(draw, (x, y), line, font_main, COLOR_WHITE, outline_w=10)
        y += line_h

    # NUMERO EN NARANJA con borde negro grueso
    if big_number:
        bbox = draw.textbbox((0, 0), big_number, font=font_main)
        num_w = bbox[2] - bbox[0]
        x = (THUMB_W - num_w) // 2
        draw_text_thick_outline(draw, (x, y), big_number, font_main, COLOR_ORANGE, outline_w=10)

    # 6. Barra inferior
    bot_bar_h = 55
    draw.rectangle([0, THUMB_H - bot_bar_h, THUMB_W, THUMB_H], fill=COLOR_BG_BAR)
    draw.rectangle([0, THUMB_H - bot_bar_h - 4, THUMB_W, THUMB_H - bot_bar_h], fill=COLOR_GREEN)

    tagline = "MARKET NEWS THAT ACTUALLY MOVES YOU"
    font_tag = ImageFont.truetype(str(font_path), 22)
    draw.text((30, THUMB_H - 40), tagline, font=font_tag, fill=COLOR_GREY)

    # Mini logo TP
    font_logo = ImageFont.truetype(str(font_path), 38)
    tp_logo = "TP"
    bbox = draw.textbbox((0, 0), tp_logo, font=font_logo)
    logo_w = bbox[2] - bbox[0]
    draw.text((THUMB_W - 30 - logo_w, THUMB_H - 48), tp_logo, font=font_logo, fill=COLOR_WHITE)

    # Guardar
    bg.convert("RGB").save(str(output_path), "JPEG", quality=95, optimize=True, subsampling=0)


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

    print("\n3/4  Extrayendo frame de b-roll...")
    clip = pick_best_broll_clip(broll_manifest)
    print(f"      clip elegido: {clip.name}")

    frame_path = OUTPUT_DIR / f"_thumb_frame_{TODAY}.jpg"
    extract_frame(clip, frame_path, offset_sec=3.0)

    print("\n4/4  Componiendo thumbnail...")
    output_path = OUTPUT_DIR / f"thumbnail_{TODAY}.jpg"
    build_thumbnail(frame_path, thumbnail_text, font_path, output_path)

    try:
        frame_path.unlink()
    except Exception:
        pass

    size_kb = output_path.stat().st_size / 1024
    print(f"\n  Listo")
    print(f"     Thumbnail: {output_path}")
    print(f"     Tamano:    {size_kb:.1f} KB")


if __name__ == "__main__":
    main()
