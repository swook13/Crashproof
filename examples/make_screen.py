"""Render a synthetic Fanuc-style TOOL OFFSET page for the demo (clearly marked as synthetic).

The values match the bracket setup except H3, which holds 98.807 instead of the 89.807 mm the
presetter measured for T3: a single transposed digit typed at the control.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent
ROWS = [(1, 98.214, 0.0, 10.0), (2, 131.552, 0.0, 6.8), (3, 98.807, 0.0, 6.0), (4, 92.415, 0.0, 8.0),
        (5, 94.120, 0.0, 8.0), (6, 0.0, 0.0, 0.0), (7, 0.0, 0.0, 0.0), (8, 0.0, 0.0, 0.0)]


def font(size):
    for name in ("consola.ttf", "cour.ttf", "DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main():
    w, h = 1280, 860
    img = Image.new("RGB", (w, h), (8, 14, 20))
    d = ImageDraw.Draw(img)
    big, mid, small = font(34), font(30), font(22)
    fg, dim, hi = (210, 235, 210), (120, 150, 130), (255, 230, 120)
    d.rectangle([20, 20, w - 20, 90], outline=dim, width=2)
    d.text((40, 36), "TOOL OFFSET (MILLING)", font=big, fill=fg)
    d.text((w - 330, 42), "O1001 N00000", font=mid, fill=fg)
    hdr_y = 120
    cols = [60, 200, 470, 740, 1010]
    for x, t in zip(cols, ["NO.", "LENGTH(H)", "WEAR(H)", "RADIUS(D)", "WEAR(D)"]):
        d.text((x, hdr_y), t, font=mid, fill=dim)
    y = hdr_y + 56
    for no, hgeo, hwear, dia in ROWS:
        vals = [f"{no:03d}", f"{hgeo:9.3f}", f"{hwear:9.3f}", f"{dia / 2:9.3f}", f"{0.0:9.3f}"]
        for x, v in zip(cols, vals):
            d.text((x, y), v, font=big, fill=fg)
        y += 62
    d.rectangle([20, h - 150, w - 20, h - 90], outline=dim, width=2)
    d.text((40, h - 138), "ACTUAL POSITION(RELATIVE)  X  0.000  Y  0.000  Z  0.000", font=small, fill=dim)
    d.text((40, h - 70), "MEM **** *** ***   12:04:33   [ OFFSET ][ SETTING ][ WORK ][      ][ (OPRT) ]", font=small, fill=dim)
    d.text((w - 360, h - 40), "SYNTHETIC EXAMPLE SCREEN", font=small, fill=hi)
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    # a little perspective and glare, like a phone photo of a control
    img = img.transform((w, h), Image.QUAD, (30, 10, 0, h, w, h - 20, w - 10, 0), resample=Image.BICUBIC)
    glare = Image.new("RGB", (w, h), (255, 255, 255))
    mask = Image.radial_gradient("L").resize((w, h)).point(lambda v: max(0, 40 - v // 6))
    img = Image.composite(glare, img, mask)
    out = HERE / "offset_screen_example.png"
    img.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main()
