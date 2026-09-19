"""Cut the four views out of the avatar character sheet.

Each character is a separate connected component on a pure-white sheet, so
a border flood fill (threshold 250) removes the background without eating
enclosed whites like the apron, and a one-pixel erosion strips the
anti-aliased halo. Component bounds are in VIEWS; re-measure them if the
sheet changes.

Usage: scripts/extract_avatar_views.py <sheet> <out-dir>
"""

import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

# (name, x0, x1) in sheet pixels; characters do not touch, labels sit
# below Y1.
VIEWS = {
    "front": (20, 338),
    "back": (340, 652),
    "left": (655, 916),
    "right": (922, 1176),
}
Y0, Y1 = 40, 545


def extract(img, out_dir, name, x0, x1):
    img = img[Y0:Y1, x0:x1]
    rgb = img[:, :, :3].astype(int)
    white = (rgb >= 250).all(axis=2)

    h, w = white.shape
    bg = np.zeros_like(white)
    dq = deque()
    for x in range(w):
        for y in (0, h - 1):
            if white[y, x] and not bg[y, x]:
                bg[y, x] = True
                dq.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if white[y, x] and not bg[y, x]:
                bg[y, x] = True
                dq.append((y, x))
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and white[ny, nx] and not bg[ny, nx]:
                bg[ny, nx] = True
                dq.append((ny, nx))

    alpha = img[:, :, 3]
    alpha[bg] = 0
    op = alpha > 0
    er = (op & np.roll(op, 1, 0) & np.roll(op, -1, 0)
          & np.roll(op, 1, 1) & np.roll(op, -1, 1))
    alpha[~er] = 0

    ys, xs = np.where(er)
    img = img[max(ys.min() - 6, 0) : ys.max() + 6,
              max(xs.min() - 10, 0) : xs.max() + 10]
    Image.fromarray(img).save(out_dir / f"whale_{name}.webp", lossless=True)
    print(name, img.shape[:2])


def main():
    sheet, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    img = np.array(Image.open(sheet).convert("RGBA"))
    for name, (x0, x1) in VIEWS.items():
        extract(img, out_dir, name, x0, x1)


if __name__ == "__main__":
    main()
