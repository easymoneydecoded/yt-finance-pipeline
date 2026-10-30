"""Foto de perfil: "Press triangle" (▲ de subida hecho de columnas de periodico).

Uso: python branding/make_avatar.py <carpeta>  -> avatar_final.png + preview_avatar_final.png
"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

OUT = Path(sys.argv[1])
NAVY, NAVY_2 = (9, 13, 22), (24, 34, 54)
GREEN = (0, 214, 125)
S = 2400


def background():
    small = Image.new("RGB", (96, 96), NAVY)
    d = ImageDraw.Draw(small)
    for r in range(70, 0, -1):
        k = r / 70
        c = tuple(int(NAVY[i] + (NAVY_2[i] - NAVY[i]) * (1 - k) ** 1.5) for i in range(3))
        d.ellipse([48 - r, 42 - r, 48 + r, 42 + r], fill=c)
    return small.resize((S, S), Image.BICUBIC).filter(ImageFilter.GaussianBlur(10))


def press_triangle(draw, cx, top, base, half_base, n=5, gap_ratio=0.42):
    """▲ formado por n franjas horizontales (columnas de periodico)."""
    h = (base - top) / (n + (n - 1) * gap_ratio)
    gap = h * gap_ratio
    for i in range(n):
        y0 = top + i * (h + gap)
        y1 = y0 + h
        w0 = half_base * (y0 - top) / (base - top)
        w1 = half_base * (y1 - top) / (base - top)
        draw.polygon([(cx - w0, y0), (cx + w0, y0), (cx + w1, y1), (cx - w1, y1)], fill=GREEN)


img = background()
d = ImageDraw.Draw(img)
# Centrado optico: el triangulo pesa abajo, se sube un poco
press_triangle(d, S / 2, S * 0.215, S * 0.745, S * 0.31)
img = img.resize((800, 800), Image.LANCZOS)
img.save(OUT / "avatar_final.png")

# Previsualizacion: circulo grande + tamanos reales, sobre fondo claro y oscuro (temas de YouTube)
sheet = Image.new("RGB", (1000, 400), (255, 255, 255))
sheet.paste((15, 15, 15), (500, 0, 1000, 400))
for ox in (0, 500):
    for size, (px, py) in [(260, (ox + 20, 70)), (88, (ox + 300, 90)), (48, (ox + 300, 210)), (32, (ox + 300, 290))]:
        m = Image.new("L", (size, size), 0)
        ImageDraw.Draw(m).ellipse([0, 0, size - 1, size - 1], fill=255)
        sheet.paste(img.resize((size, size), Image.LANCZOS), (px, py), m)
sheet.save(OUT / "preview_avatar_final.png")
print("ok")
