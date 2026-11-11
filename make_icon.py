"""Generate app_icon.ico for AFR interrogator.

Run from the project root:  python make_icon.py
"""
import math
import os

from PIL import Image, ImageDraw

SIZE = 1024


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius, fill=255)
    return mask


def background(img):
    draw = ImageDraw.Draw(img)
    top = (18, 49, 77)
    bottom = (5, 13, 23)
    for y in range(SIZE):
        draw.line(((0, y), (SIZE - 1, y)), fill=lerp(top, bottom, y / (SIZE - 1)))

    grid = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    pad, step = 128, 96
    for x in range(pad, SIZE - pad + 1, step):
        gd.line(((x, pad), (x, SIZE - pad)), fill=(120, 215, 255, 22), width=3)
    for y in range(pad, SIZE - pad + 1, step):
        gd.line(((pad, y), (SIZE - pad, y)), fill=(120, 215, 255, 22), width=3)
    img.alpha_composite(grid)

    border = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(border).rounded_rectangle(
        (4, 4, SIZE - 5, SIZE - 5), 216, outline=(52, 190, 235, 150), width=8
    )
    img.alpha_composite(border)


def spectrum_points():
    peaks = ((300, 300, 58), (512, 430, 74), (724, 285, 58))
    baseline = 742
    points = []
    for x in range(112, 913, 2):
        y = baseline
        for cx, amp, sigma in peaks:
            y -= amp * math.exp(-0.5 * ((x - cx) / sigma) ** 2)
        points.append((x, y))
    return points


def spectrum(img):
    points = spectrum_points()
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    for width, alpha in ((64, 18), (38, 38), (22, 80)):
        draw.line(points, fill=(70, 205, 255, alpha), width=width, joint="curve")

    left, mid, right = (63, 214, 255), (94, 140, 255), (198, 96, 255)
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        t = (x1 - 112) / (912 - 112)
        color = lerp(left, mid, t / 0.55) if t < 0.55 else lerp(mid, right, (t - 0.55) / 0.45)
        draw.line(((x1, y1), (x2, y2)), fill=color + (255,), width=15)

    for cx, amp, _ in ((300, 300, 0), (512, 430, 0), (724, 285, 0)):
        cy = 742 - amp
        for r, alpha, color in (
            (58, 70, (120, 230, 255)),
            (40, 150, (185, 243, 255)),
            (26, 255, (255, 255, 255)),
        ):
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color + (alpha,))

    img.alpha_composite(layer)


def main():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    background(img)
    spectrum(img)
    img.putalpha(Image.composite(img.getchannel("A"), Image.new("L", (SIZE, SIZE), 0), rounded_mask(SIZE, 216)))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico")
    img.resize((256, 256), Image.LANCZOS).save(
        out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )
    img.resize((256, 256), Image.LANCZOS).save(os.path.join(os.path.dirname(out), "app_icon_preview.png"))
    print("Saved", out)


if __name__ == "__main__":
    main()
