"""Uygulama ikonunu üretir: kickindirici/assets/icon.ico ve static/icon.png."""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent / "kickindirici"


def draw(size: int = 512) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=(14, 15, 17, 255))
    g = (83, 252, 24, 255)
    # aşağı ok
    w = s * 0.16
    cx = s / 2
    d.rectangle((cx - w / 2, s * 0.18, cx + w / 2, s * 0.52), fill=g)
    d.polygon([(s * 0.24, s * 0.48), (s * 0.76, s * 0.48), (cx, s * 0.74)], fill=g)
    # taban çizgisi
    d.rounded_rectangle((s * 0.22, s * 0.79, s * 0.78, s * 0.86), radius=int(s * 0.03), fill=g)
    return img


if __name__ == "__main__":
    img = draw()
    (ROOT / "assets").mkdir(exist_ok=True)
    img.save(ROOT / "assets" / "icon.png")
    img.resize((180, 180), Image.LANCZOS).save(ROOT / "static" / "icon.png")
    img.save(ROOT / "assets" / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("ikon üretildi")
