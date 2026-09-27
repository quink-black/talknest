#!/usr/bin/env bash
# Render the iPad app icon set from the repository's whale.webp.
#
# The asset catalog is committed, so this only needs to run when the source
# image or the required icon sizes change.
set -euo pipefail
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO_ROOT/ios/Resources/Assets.xcassets/AppIcon.appiconset"
SOURCE="${1:-$REPO_ROOT/whale.webp}"

mkdir -p "$OUT"
SOURCE="$SOURCE" OUT="$OUT" python3 <<'PY'
import json
import os
from PIL import Image

out = os.environ["OUT"]
src = Image.open(os.environ["SOURCE"]).convert("RGBA")

# Sizes iOS 10 and later ask for. The 1x iPad sizes are gone: they only
# applied to iOS 9 and earlier, and actool warns about them.
sizes = {
    "iphone-20@2x": 40,
    "iphone-20@3x": 60,
    "iphone-29@2x": 58,
    "iphone-29@3x": 87,
    "iphone-40@2x": 80,
    "iphone-40@3x": 120,
    "iphone-60@2x": 120,
    "iphone-60@3x": 180,
    "ipad-20@2x": 40,
    "ipad-29@2x": 58,
    "ipad-40@2x": 80,
    "ipad-76@2x": 152,
    "ipad-83.5@2x": 167,
    "app-store-1024": 1024,
}

# iOS masks the icon to a rounded square, so a full-bleed image loses its
# corners; the whale is inset on a light background instead.
BG = (250, 249, 246, 255)


def render(size: int) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), BG)
    inset = round(size * 0.12)
    box = size - 2 * inset
    art = src.copy()
    art.thumbnail((box, box), Image.LANCZOS)
    canvas.paste(art, ((size - art.width) // 2, (size - art.height) // 2), art)
    return canvas.convert("RGB")


images = []
for name, px in sizes.items():
    filename = f"{name}-{px}.png"
    render(px).save(os.path.join(out, filename))
    if name.startswith("ipad"):
        idiom = "ipad"
    elif name.startswith("iphone"):
        idiom = "iphone"
    else:
        idiom = "ios-marketing"
    size_pt = name.split("-")[-1]
    pt = size_pt.split("@")[0]
    images.append({
        "filename": filename,
        "idiom": idiom,
        "size": f"{pt}x{pt}",
        "scale": size_pt.split("@")[1] if "@" in size_pt else "1x",
    })

images.sort(key=lambda i: (i["idiom"], float(i["size"].split("x")[0])))
catalog = {"images": images, "info": {"author": "xcode", "version": 1}}
with open(os.path.join(out, "Contents.json"), "w") as fh:
    json.dump(catalog, fh, indent=2)
    fh.write("\n")
print(f"[make_icons] wrote {len(images)} icons to {out}")
PY
