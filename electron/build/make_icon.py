"""Generate the app icon: rising candlesticks and an upward trend arrow on a
dark gradient squircle, with a neon glow and gradient fills. Rendered at 2x and
downscaled for crisp anti-aliased edges. Outputs icon_1024.png."""

import math

from PIL import Image, ImageDraw, ImageFilter

SS = 2          # supersample factor for anti-aliasing
S = 1024        # final size
W = S * SS      # working size
RADIUS = int(W * 0.235)

GREEN = (74, 222, 128)
GREEN_HI = (170, 252, 200)   # candle top (bright mint)
GREEN_LO = (46, 190, 120)    # candle bottom (emerald)
ACCENT = (91, 140, 255)
ACCENT_HI = (150, 182, 255)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def sc(v):
    return int(v * SS)


def vgradient(size, top, bottom):
    """Vertical gradient RGB image."""
    w, h = size
    g = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(g)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=lerp(top, bottom, y / max(1, h - 1)))
    return g


# ---- background squircle ----
bg = vgradient((W, W), (34, 46, 72), (9, 12, 19))
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=RADIUS, fill=255)
img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
img.paste(bg, (0, 0), mask)

# geometry in 1024-space: x, wick_top, wick_bot, body_top, body_bot
candles = [
    (300, 590, 805, 650, 775),
    (468, 495, 715, 555, 690),
    (636, 405, 655, 465, 618),
    (792, 298, 575, 358, 540),
]
BW = 82
A, B = (226, 715), (814, 320)  # trend line endpoints

# ---- glow layer (bright shapes, blurred, composited underneath) ----
glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
for x, _, _, bt, bb in candles:
    gd.rounded_rectangle([sc(x - BW // 2), sc(bt), sc(x + BW // 2), sc(bb)], radius=sc(14), fill=GREEN + (255,))
gd.line([(sc(A[0]), sc(A[1])), (sc(B[0]), sc(B[1]))], fill=ACCENT + (255,), width=sc(26))
glow = glow.filter(ImageFilter.GaussianBlur(sc(17)))
img.alpha_composite(glow)

draw = ImageDraw.Draw(img)


def grad_round_rect(box, c_top, c_bot, radius):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    grad = vgradient((w, h), c_top, c_bot).convert("RGBA")
    rmask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(rmask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    img.paste(grad, (x0, y0), rmask)


# ---- candles (wick + gradient body) ----
for x, wt, wb, bt, bb in candles:
    draw.line([(sc(x), sc(wt)), (sc(x), sc(wb))], fill=GREEN + (210,), width=sc(9))
    grad_round_rect([sc(x - BW // 2), sc(bt), sc(x + BW // 2), sc(bb)], GREEN_HI, GREEN_LO, sc(14))

# ---- trend arrow (rounded line + arrowhead) ----
draw.line([(sc(A[0]), sc(A[1])), (sc(B[0]), sc(B[1]))], fill=ACCENT, width=sc(28), joint="curve")
for p in (A, B):  # round the caps
    r = sc(14)
    draw.ellipse([sc(p[0]) - r, sc(p[1]) - r, sc(p[0]) + r, sc(p[1]) + r], fill=ACCENT)

ang = math.atan2(B[1] - A[1], B[0] - A[0])
L, spread = 140, math.radians(29)
tip = (B[0] + 14 * math.cos(ang), B[1] + 14 * math.sin(ang))
left = (B[0] - L * math.cos(ang - spread), B[1] - L * math.sin(ang - spread))
right = (B[0] - L * math.cos(ang + spread), B[1] - L * math.sin(ang + spread))
draw.polygon(
    [(sc(tip[0]), sc(tip[1])), (sc(left[0]), sc(left[1])), (sc(right[0]), sc(right[1]))],
    fill=ACCENT_HI,
)

# ---- subtle top sheen for depth ----
sheen = Image.new("RGBA", (W, W), (0, 0, 0, 0))
ImageDraw.Draw(sheen).rounded_rectangle(
    [sc(4), sc(4), W - 1 - sc(4), W - 1 - sc(4)], radius=RADIUS, outline=(255, 255, 255, 38), width=sc(3)
)
img.alpha_composite(sheen)

# downscale for anti-aliasing
img.resize((S, S), Image.LANCZOS).save("icon_1024.png")
print("wrote icon_1024.png")
