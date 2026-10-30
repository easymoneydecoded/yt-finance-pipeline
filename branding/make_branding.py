"""Genera avatar (800x800) y banner (2560x1440) de Ticker Press.

Uso: python branding/make_branding.py <carpeta>  (la carpeta debe tener fonts/Anton-Regular.ttf
y fonts/Inter.ttf de Google Fonts). Deja avatar.png, banner.png y previsualizaciones.
"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(sys.argv[1])
FONTS = OUT / "fonts"
NAVY, NAVY_2 = (9, 13, 22), (22, 32, 52)
GREEN, WHITE, MUTED = (0, 214, 125), (245, 247, 250), (150, 162, 182)


def anton(size):
    return ImageFont.truetype(str(FONTS / "Anton-Regular.ttf"), size)


def inter(size, weight="Bold"):
    f = ImageFont.truetype(str(FONTS / "Inter.ttf"), size)
    f.set_variation_by_name(weight)
    return f


def radial_bg(w, h, center=(0.5, 0.45), inner=NAVY_2, outer=NAVY):
    small = Image.new("RGB", (128, 72 if w > h else 128), outer)
    d = ImageDraw.Draw(small)
    sw, sh = small.size
    cx, cy = sw * center[0], sh * center[1]
    for r in range(int(max(sw, sh) * 0.75), 0, -1):
        k = r / (max(sw, sh) * 0.75)
        col = tuple(int(outer[i] + (inner[i] - outer[i]) * (1 - k) ** 1.6) for i in range(3))
        d.ellipse([cx - r * 1.2, cy - r, cx + r * 1.2, cy + r], fill=col)
    return small.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(6))


def tracked(draw, xy, text, font, fill, tracking):
    """Texto con espaciado entre letras controlado."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def tracked_width(draw, text, font, tracking):
    return sum(draw.textlength(c, font=font) for c in text) + tracking * (len(text) - 1)


def press_triangle(draw, cx, top, base, half_base, fill, n=5, gap_ratio=0.42):
    """Simbolo de la marca: un ▲ formado por n franjas (columnas de periodico)."""
    h = (base - top) / (n + (n - 1) * gap_ratio)
    for i in range(n):
        y0 = top + i * h * (1 + gap_ratio)
        y1 = y0 + h
        w0 = half_base * (y0 - top) / (base - top)
        w1 = half_base * (y1 - top) / (base - top)
        draw.polygon([(cx - w0, y0), (cx + w0, y0), (cx + w1, y1), (cx - w1, y1)], fill=fill)


# ----------------------------------------------------------------------------- avatar
def make_avatar():
    S, ss = 800, 3
    W = S * ss
    img = radial_bg(W, W, center=(0.5, 0.42)).convert("RGBA")
    d = ImageDraw.Draw(img)
    # Anillo fino verde (define el circulo cuando YouTube recorta)
    m = int(W * 0.035)
    d.ellipse([m, m, W - m, W - m], outline=GREEN + (255,), width=int(W * 0.012))

    # Monograma TP
    f = anton(int(W * 0.50))
    text = "TP"
    bb = d.textbbox((0, 0), text, font=f)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    tx, ty = (W - tw) // 2 - bb[0], int(W * 0.40) - bb[1]
    d.text((tx, ty), text, font=f, fill=WHITE)

    # Linea de grafico alcista con flecha, por encima de las letras
    pts = [(0.21, 0.36), (0.36, 0.27), (0.48, 0.32), (0.63, 0.20)]
    pts = [(x * W, y * W) for x, y in pts]
    lw = int(W * 0.045)
    d.line(pts, fill=GREEN, width=lw, joint="curve")
    for x, y in pts[:-1]:
        d.ellipse([x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2], fill=GREEN)
    # Punta de flecha
    (x1, y1), (x2, y2) = pts[-2], pts[-1]
    import math
    ang = math.atan2(y2 - y1, x2 - x1)
    L, Wd = W * 0.12, W * 0.085
    tip = (x2 + math.cos(ang) * L * 0.55, y2 + math.sin(ang) * L * 0.55)
    left = (tip[0] - math.cos(ang) * L + math.sin(ang) * Wd, tip[1] - math.sin(ang) * L - math.cos(ang) * Wd)
    right = (tip[0] - math.cos(ang) * L - math.sin(ang) * Wd, tip[1] - math.sin(ang) * L + math.cos(ang) * Wd)
    d.polygon([tip, left, right], fill=GREEN)

    img = img.resize((S, S), Image.LANCZOS).convert("RGB")
    img.save(OUT / "avatar.png")
    return img


# ----------------------------------------------------------------------------- banner
def sp500_points():
    try:
        import yfinance as yf
        df = yf.Ticker("^GSPC").history(period="5y", interval="1wk")
        vals = [float(v) for v in df["Close"].dropna()]
        if len(vals) > 50:
            return vals
    except Exception as e:
        print("yfinance:", e)
    import math, random
    random.seed(7)
    return [100 + i * 0.4 + 12 * math.sin(i / 14) + random.uniform(-3, 3) for i in range(260)]


def make_banner():
    W, H, ss = 2560, 1440, 2
    img = radial_bg(W * ss, H * ss, center=(0.5, 0.5)).convert("RGBA")

    # Rejilla sutil
    grid = Image.new("RGBA", img.size, (0, 0, 0, 0))
    g = ImageDraw.Draw(grid)
    step = 80 * ss
    for x in range(0, img.width, step):
        g.line([(x, 0), (x, img.height)], fill=(255, 255, 255, 10), width=ss)
    for y in range(0, img.height, step):
        g.line([(0, y), (img.width, y)], fill=(255, 255, 255, 10), width=ss)
    img = Image.alpha_composite(img, grid)

    # Grafico real del S&P 500 (5 anos) como textura de fondo
    vals = sp500_points()
    lo, hi = min(vals), max(vals)
    top, bottom = int(H * 0.22) * ss, int(H * 0.86) * ss
    pts = [(i / (len(vals) - 1) * img.width, bottom - (v - lo) / (hi - lo) * (bottom - top)) for i, v in enumerate(vals)]
    chart = Image.new("RGBA", img.size, (0, 0, 0, 0))
    c = ImageDraw.Draw(chart)
    c.polygon(pts + [(img.width, img.height), (0, img.height)], fill=GREEN + (16,))
    c.line(pts, fill=GREEN + (95,), width=5 * ss, joint="curve")
    img = Image.alpha_composite(img, chart)

    # Viñeta central oscura: el texto siempre legible
    veil = Image.new("L", (64, 36), 0)
    ImageDraw.Draw(veil).ellipse([10, 9, 54, 27], fill=200)
    veil = veil.resize(img.size, Image.BICUBIC).filter(ImageFilter.GaussianBlur(40 * ss))
    shade = Image.new("RGBA", img.size, NAVY + (0,))
    shade.putalpha(veil)
    img = Image.alpha_composite(img, shade)

    d = ImageDraw.Draw(img, "RGBA")  # mezcla real de transparencias
    cx = img.width // 2
    # Zona segura (todos los dispositivos): 1546x423 centrada -> y 508..931
    safe_top = 508 * ss

    # Marca: ▲ + TICKER PRESS
    title_f = anton(178 * ss)
    tracking = 10 * ss
    title = "TICKER PRESS"
    tw = tracked_width(d, title, title_f, tracking)
    tri = 104 * ss
    gap = 40 * ss
    total = tri + gap + tw
    x0 = cx - total / 2
    bb = d.textbbox((0, 0), title, font=title_f)
    ty = safe_top + 10 * ss - bb[1]
    th = bb[3] - bb[1]
    tri_cy = ty + bb[1] + th / 2
    press_triangle(d, x0 + tri / 2, tri_cy - tri * 0.5, tri_cy + tri * 0.5, tri * 0.56, GREEN)
    tracked(d, (x0 + tri + gap, ty), title, title_f, WHITE, tracking)

    # Linea de acento
    ly = ty + bb[3] + 34 * ss
    d.rounded_rectangle([cx - 90 * ss, ly, cx + 90 * ss, ly + 8 * ss], radius=4 * ss, fill=GREEN)

    # Tagline
    tag_f = inter(48 * ss, "Medium")
    tag = "The money stories behind the headlines, explained with real data."
    tag_w = d.textlength(tag, font=tag_f)
    tag_y = ly + 40 * ss
    d.text((cx - tag_w / 2, tag_y), tag, font=tag_f, fill=(215, 222, 232))

    # Pildoras de calendario
    pill_f = inter(30 * ss, "Bold")
    pills = ["NEW VIDEOS  MON · WED · FRI · SUN", "SHORTS EVERY DAY", "NOT FINANCIAL ADVICE"]
    pad_x, pill_h, sep = 34 * ss, 62 * ss, 24 * ss
    widths = [d.textlength(p, font=pill_f) + 2 * pad_x for p in pills]
    px = cx - (sum(widths) + sep * (len(pills) - 1)) / 2
    py = tag_y + 98 * ss
    for i, (p, w) in enumerate(zip(pills, widths)):
        main = i == 0
        d.rounded_rectangle([px, py, px + w, py + pill_h], radius=pill_h // 2,
                            fill=GREEN if main else (24, 34, 52),
                            outline=None if main else (0, 170, 100), width=2 * ss)
        pb = d.textbbox((0, 0), p, font=pill_f)
        d.text((px + pad_x, py + (pill_h - (pb[3] - pb[1])) / 2 - pb[1]), p, font=pill_f,
               fill=NAVY if main else (225, 232, 240))
        px += w + sep
    print("altura usada en zona segura:", round((py + pill_h) / ss - 508), "de 423 px")

    img = img.resize((W, H), Image.LANCZOS).convert("RGB")
    img.save(OUT / "banner.png", optimize=True)
    return img


make_avatar()
make_banner()

# Previsualizaciones: avatar a tamanos reales y banner con la zona segura marcada
av = Image.open(OUT / "avatar.png")
prev = Image.new("RGB", (900, 330), (255, 255, 255))
mask_big = Image.new("L", (300, 300), 0); ImageDraw.Draw(mask_big).ellipse([0, 0, 299, 299], fill=255)
prev.paste(av.resize((300, 300), Image.LANCZOS), (10, 10), mask_big)
for k, s in enumerate((88, 48, 32)):
    mk = Image.new("L", (s, s), 0); ImageDraw.Draw(mk).ellipse([0, 0, s - 1, s - 1], fill=255)
    prev.paste(av.resize((s, s), Image.LANCZOS), (360 + k * 140, 120), mk)
prev.save(OUT / "preview_avatar.png")

bn = Image.open(OUT / "banner.png").convert("RGB")
dd = ImageDraw.Draw(bn)
dd.rectangle([507, 508, 2053, 931], outline=(255, 60, 60), width=4)
bn.resize((1280, 720)).save(OUT / "preview_banner.png")
bn_mobile = Image.open(OUT / "banner.png").crop((507, 508, 2053, 931))
bn_mobile.resize((773, 212)).save(OUT / "preview_banner_mobile.png")
print("ok")
