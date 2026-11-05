"""Build the device avatar expression frames from web/whale_front.webp.

Output:
  firmware/components/ui_avatar/assets/avatar_frames.bin
      Four RGB565A8 frames (eyes open/closed x mouth closed/open). Each frame
      is a w*h*2 RGB565 colour plane followed by a w*h alpha plane, which is
      the layout LV_COLOR_FORMAT_RGB565A8 expects.
  firmware/components/ui_avatar/include/avatar_frames.h
      Declares the embedded symbol and frame geometry.

The alpha plane keeps the character silhouette transparent, so the avatar
blends into the screen instead of appearing as an opaque square. Frames are
baked offline and embedded with CMake EMBED_FILES because the firmware has
no image decoder.

Run: python scripts/gen_avatar_frames.py
     python scripts/gen_avatar_frames.py --preview /tmp/avatar.png
     python scripts/gen_avatar_frames.py --layout-preview /tmp/screen.png
"""

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "web" / "whale_front.webp"
ASSETS = REPO / "firmware" / "components" / "ui_avatar" / "assets"
INCLUDE = REPO / "firmware" / "components" / "ui_avatar" / "include"
BINARY = ASSETS / "avatar_frames.bin"
HEADER = INCLUDE / "avatar_frames.h"

# Half-body portrait: keep the neck, collar, arms and apron visible so the
# character reads as a person rather than a floating face. The lower edge is
# feathered where the crop cuts the body; side feathering softens the
# outermost silhouette.
CROP = (0, 0, 318, 330)
SIZE = (300, 311)
BG = (16, 20, 28)  # RGB padding for alpha-edge compositing; device uses a gradient

# Feature coordinates in source image pixels.
EYES = [(98, 138, 140, 180), (180, 138, 222, 180)]
EYE_ARCS = [(101, 150, 137, 166), (183, 150, 219, 166)]
MOUTH = (149, 183, 169, 200)

FRAME_NAMES = ["idle", "talk", "blink", "blink_talk"]


def skin_color(rgb):
    """Average a clean patch of cheek to cover the open eyes with."""
    patch = rgb[188:198, 140:180].reshape(-1, 3)
    return tuple(int(v) for v in patch.mean(axis=0))


def paint_eyes_closed(img, skin):
    d = ImageDraw.Draw(img)
    for box in EYES:
        d.ellipse(box, fill=skin + (255,))
    for box in EYE_ARCS:
        d.arc(box, start=195, end=345, fill=(46, 58, 92, 255), width=4)


def paint_mouth_open(img):
    d = ImageDraw.Draw(img)
    d.ellipse(MOUTH, fill=(122, 56, 68, 255), outline=(90, 40, 52, 255), width=2)
    d.ellipse((154, 192, 164, 199), fill=(214, 116, 124, 255))


def frame_bytes(img):
    """RGB565A8: colour plane (little-endian u16) then the alpha plane."""
    a = np.asarray(img.convert("RGBA"), dtype=np.uint16)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    colour = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    plane = colour.astype("<u2").tobytes()
    alpha = a[:, :, 3].astype(np.uint8).tobytes()
    return plane + alpha


def build():
    src = Image.open(SOURCE).convert("RGBA")
    clean_specks(src)
    # Composite onto BG only to get correct edge colours; alpha is kept
    # separately below so the silhouette stays transparent.
    composed = Image.new("RGBA", src.size, BG + (255,))
    composed.alpha_composite(src)
    skin = skin_color(np.asarray(composed.convert("RGB")))
    print(f"skin {skin}, source {src.size}")
    frames = {}
    for eyes_closed in (False, True):
        for mouth_open in (False, True):
            img = composed.copy()
            if eyes_closed:
                paint_eyes_closed(img, skin)
            if mouth_open:
                paint_mouth_open(img)
            crop = img.crop(CROP).resize(SIZE, Image.LANCZOS)
            # Restore the true alpha after compositing: fully transparent
            # pixels stay transparent so no square shows on screen.
            alpha = src.crop(CROP).getchannel("A").resize(SIZE, Image.LANCZOS)
            feather_edges(alpha, bottom=22, sides=18)
            crop.putalpha(alpha)
            frames[frame_key(eyes_closed, mouth_open)] = crop
    return frames


def clean_specks(src):
    """Keep only the character's main alpha island, dropping isolated
    highlight specks left by the character-sheet cutout."""
    from scipy import ndimage

    alpha = np.asarray(src.getchannel("A")).copy()
    opaque = alpha > 8
    labels, count = ndimage.label(opaque)
    if count == 0:
        return
    sizes = ndimage.sum(opaque, labels, range(1, count + 1))
    main = int(np.argmax(sizes)) + 1
    alpha[labels != main] = 0
    src.putalpha(Image.fromarray(alpha, mode="L"))


def feather_edges(alpha, bottom, sides):
    """Feather the crop's lower and side edges into the screen so the
    character reads as a silhouette rather than a rectangular bitmap."""
    a = np.asarray(alpha, dtype=np.float32).copy()
    h, w = a.shape
    for y in range(bottom):
        a[h - bottom + y, :] *= (1.0 - y / (bottom - 1)) ** 0.8
    for x in range(sides):
        t = x / (sides - 1)
        a[:, x] *= t ** 0.8
        a[:, w - sides + x] *= (1.0 - t) ** 0.8
    alpha.paste(Image.fromarray(a.astype(np.uint8), mode="L"))


def frame_key(eyes_closed, mouth_open):
    if not mouth_open:
        return "blink" if eyes_closed else "idle"
    return "blink_talk" if eyes_closed else "talk"


def write_binary(frames):
    w, h = SIZE
    size = w * h * 3
    ASSETS.mkdir(parents=True, exist_ok=True)
    with BINARY.open("wb") as out:
        for name in FRAME_NAMES:
            raw = frame_bytes(frames[name])
            assert len(raw) == size, (name, len(raw), size)
            out.write(raw)
    print(f"wrote {BINARY} ({BINARY.stat().st_size} bytes)")


def write_header():
    w, h = SIZE
    HEADER.parent.mkdir(parents=True, exist_ok=True)
    HEADER.write_text("\n".join([
        "/* Generated by scripts/gen_avatar_frames.py -- do not edit by hand.",
        " * Four expression frames for the dialogue-driven avatar.",
        " * Source: web/whale_front.webp.",
        " * Format: LV_COLOR_FORMAT_RGB565A8 (colour plane + alpha plane).",
        " * Frame order matches avatar_frames.bin and the enum below. */",
        "#pragma once",
        "",
        "#include <stdint.h>",
        "",
        f"#define AVATAR_FRAME_W {w}",
        f"#define AVATAR_FRAME_H {h}",
        f"#define AVATAR_FRAME_STRIDE (AVATAR_FRAME_W * 2)",
        "#define AVATAR_FRAME_BYTES (AVATAR_FRAME_W * AVATAR_FRAME_H * 3)",
        "#define AVATAR_FRAME_COUNT 4",
        "",
        "enum avatar_frame_t {",
        "    AVATAR_FRAME_IDLE = 0,   /* eyes open, mouth closed */",
        "    AVATAR_FRAME_TALK,       /* eyes open, mouth open  */",
        "    AVATAR_FRAME_BLINK,      /* eyes closed, mouth closed */",
        "    AVATAR_FRAME_BLINK_TALK, /* eyes closed, mouth open */",
        "};",
        "",
    ]) + "\n", encoding="utf-8")
    print(f"wrote {HEADER}")


def preview(frames, path):
    sheet = Image.new("RGB", (SIZE[0] * 4, SIZE[1]), BG)
    for i, name in enumerate(FRAME_NAMES):
        sheet.paste(frames[name].convert("RGB"), (i * SIZE[0], 0))
    sheet.save(path)
    print(f"preview {path}")


def layout_preview(frames, path):
    """Whole-screen approximation of the ambient scene and idle state."""
    width, height = 410, 502
    screen = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(screen, "RGBA")
    top, bottom = (20, 35, 55), (11, 18, 28)
    for y in range(height):
        t = y / (height - 1)
        color = tuple(round(a * (1 - t) + b * t) for a, b in zip(top, bottom))
        draw.line((0, y, width, y), fill=color + (255,))

    draw.rounded_rectangle((45, 342, 365, 446), radius=52,
                           fill=(33, 65, 90, 102))
    for x, y, size, opacity in (
        (30, 142, 8, 45), (374, 178, 6, 40), (24, 286, 5, 35),
        (380, 314, 9, 40), (48, 366, 4, 30), (358, 104, 4, 30),
    ):
        draw.ellipse((x, y, x + size, y + size), fill=(121, 207, 255, opacity))

    cx, cy = 205, 251 - 2
    avatar = frames["idle"]
    screen.alpha_composite(avatar, (cx - SIZE[0] // 2, cy - SIZE[1] // 2))

    draw.rounded_rectangle((71, 18, 339, 70), radius=26,
                           fill=(27, 45, 66, 230), outline=(169, 188, 208, 178))
    for i in range(3):
        x = 93 + i * 12
        opacity = 255 if i == 0 else 153
        draw.ellipse((x, 40, x + 8, 48), fill=(169, 188, 208, opacity))
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 24)
    except OSError:
        font = ImageFont.load_default()
    draw.text((135, 44), "HELLO", font=font, fill=(169, 188, 208, 255),
              anchor="lm")
    draw.text((205, 462), "Tap to say hi", font=font,
              fill=(169, 188, 208, 255), anchor="mm")

    screen.convert("RGB").save(path)
    print(f"layout preview {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview")
    ap.add_argument("--layout-preview")
    args = ap.parse_args()
    if not SOURCE.exists():
        sys.exit(f"missing {SOURCE}")
    frames = build()
    if args.preview:
        preview(frames, args.preview)
    if args.layout_preview:
        layout_preview(frames, args.layout_preview)
    write_binary(frames)
    write_header()


if __name__ == "__main__":
    main()
