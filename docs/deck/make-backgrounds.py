"""Turn the product screenshots into deck backgrounds.

Real teams put a darkened shot of their own product behind the title and the section
breaks. It reads as evidence rather than decoration, and it is nothing like the abstract
gradient an AI reaches for.
"""
from PIL import Image, ImageFilter, ImageEnhance, ImageDraw
import os

SRC = r"C:/Users/Parth Sarthi/Desktop/Code/hackathon/casepilot/docs/screenshots"
OUT = r"C:/Users/PARTHS~1/AppData/Local/Temp/claude/C--Users-Parth-Sarthi-Desktop-Code-hackathon/bec9da7a-a04d-483c-b9da-7d7612c8fbef/scratchpad/deckimg"
os.makedirs(OUT, exist_ok=True)

W, H = 1920, 1080
INK = (16, 22, 42)          # deep navy the deck is built on


def cover(im, w=W, h=H, focus=0.5):
    """Scale-and-crop to fill, biased vertically by `focus` (0 = top)."""
    r = max(w / im.width, h / im.height)
    im = im.resize((round(im.width * r), round(im.height * r)), Image.LANCZOS)
    x = (im.width - w) // 2
    y = int((im.height - h) * focus)
    return im.crop((x, y, x + w, y + h))


def treat(name, src, blur=6, darkness=0.58, tint=0.42, focus=0.5, vignette=True):
    im = cover(Image.open(os.path.join(SRC, src)).convert("RGB"), focus=focus)
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    im = ImageEnhance.Brightness(im).enhance(darkness)
    im = ImageEnhance.Color(im).enhance(0.32)          # pull most of the colour out

    wash = Image.new("RGB", im.size, INK)
    im = Image.blend(im, wash, tint)

    if vignette:
        # A one-directional scrim, not a vignette: dark where the headline sits, the
        # product still legible on the other side. A ring of darkness all round is the
        # stock-photo-overlay look and reads as decoration.
        grad = Image.new("L", im.size, 0)
        d = ImageDraw.Draw(grad)
        for i in range(im.height):
            t = i / im.height
            d.line([(0, i), (im.width, i)], fill=int(30 + 190 * (t ** 1.5)))
        dark = ImageEnhance.Brightness(im).enhance(0.42)
        im = Image.composite(dark, im, grad)

    path = os.path.join(OUT, name)
    im.save(path, "JPEG", quality=86, optimize=True)
    print(f"  {name:26} {os.path.getsize(path)//1024:4} KB")


print("backgrounds:")
# title: the work log, close in, heavily sunk - texture, not a readable screen
treat("bg-title.jpg", "desk-case.png", blur=5, darkness=0.62, tint=0.40, focus=0.62)
# section breaks
treat("bg-sec-problem.jpg", "desk-board.png", blur=7, darkness=0.55, tint=0.46, focus=0.35)
treat("bg-sec-build.jpg", "portal-chat.png", blur=7, darkness=0.56, tint=0.44, focus=0.45)
treat("bg-sec-proof.jpg", "desk-case.png", blur=7, darkness=0.54, tint=0.46, focus=0.18)
treat("bg-close.jpg", "portal-ticket.png", blur=8, darkness=0.56, tint=0.44, focus=0.30)

print("\nclean product shots (trimmed, not treated):")
for name, src, focus, h in [
    ("shot-chat.png", "portal-chat.png", 0.0, None),
    ("shot-ticket.png", "portal-ticket.png", 0.0, None),
    ("shot-board.png", "desk-board.png", 0.0, None),
    ("shot-case.png", "desk-case.png", 0.0, None),
]:
    im = Image.open(os.path.join(SRC, src)).convert("RGB")
    # halve the retina capture: still sharp on a 1920 stage, a third of the weight
    im = im.resize((im.width // 2, im.height // 2), Image.LANCZOS)
    p = os.path.join(OUT, name)
    im.save(p, "PNG", optimize=True)
    print(f"  {name:26} {im.width}x{im.height}  {os.path.getsize(p)//1024:4} KB")
