"""Generate the Bellwether app icon: a bell (the bellwether's bell) on a dark gradient
squircle, in a warm brass/gold with a cool blue accent for the clapper and ringing lines.
Rendered at 2x and downscaled for crisp anti-aliased edges. Outputs icon_1024.png and icon.ico."""

import math

from PIL import Image, ImageDraw, ImageFilter

SS = 2          # supersample factor for anti-aliasing
S = 1024        # final size
W = S * SS      # working size
RADIUS = int(W * 0.235)

GOLD_HI = (255, 231, 150)    # bell top (bright warm gold)
GOLD_LO = (210, 150, 60)     # bell bottom (deep brass)
GLOW_GOLD = (250, 200, 110)  # warm glow
ACCENT = (91, 140, 255)      # clapper + ringing lines (the app's blue)
ACCENT_HI = (150, 182, 255)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def sc(v):
    return v * SS


def smoothstep(a, b, x):
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


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


# ---- bell geometry (1024-space, centred on x=512) ----
CX = 512.0
Y_TOP = 372.0   # shoulder line
Y_RIM = 688.0   # top of the flared mouth
Y_BOT = 748.0   # bottom lip
W_SH = 104.0    # shoulder half-width
W_BODY = 196.0  # body growth target


def half_at(u):
    """Half-width at fraction u (0 = shoulder, 1 = rim). The sin term bulges the shoulders out
    early (full, rounded body, not a pinched cone); the smoothstep adds the rim flare."""
    return W_SH + (W_BODY - W_SH) * math.sin(u * math.pi / 2) + 42 * smoothstep(0.74, 1.0, u)


def bell_outline():
    """A single closed polygon for the bell body: full rounded dome, bulging shoulders, flaring
    lipped mouth with a gently open (concave-up) bottom."""
    rim_h = half_at(1.0)
    # rounded dome cap across the top (left shoulder -> right shoulder, bulging up)
    dome = []
    for i in range(25):
        t = i / 24
        dome.append(((CX - W_SH) + 2 * W_SH * t, Y_TOP - 52 * math.sin(math.pi * t)))
    # right side, shoulder down to the rim
    right = []
    for i in range(65):
        u = i / 64
        right.append((CX + half_at(u), Y_TOP + (Y_RIM - Y_TOP) * u))
    right.append((CX + rim_h + 18, Y_RIM + 10))   # lip flares out
    right.append((CX + rim_h + 10, Y_BOT))         # down the lip
    # open mouth: gentle concave-up bottom edge, right lip -> left lip
    bottom = []
    x_r, x_l = CX + rim_h + 10, CX - (rim_h + 10)
    for i in range(29):
        t = i / 28
        bottom.append((x_r + (x_l - x_r) * t, Y_BOT - 16 * math.sin(math.pi * t)))
    left = [(2 * CX - px, py) for px, py in reversed(right)]
    return dome + right + bottom + left


OUTLINE = bell_outline()
CROWN = [470, 250, 554, 350]                  # crown loop bbox
CLAP = (512, 766, 26)                         # clapper x, y, r
RINGS = [((704, 372), (774, 344)), ((716, 420), (796, 410))]  # right-side ringing lines (mirrored)


def scaled(points):
    return [(sc(px), sc(py)) for px, py in points]


# ---- glow layer (bright shapes, blurred, composited underneath) ----
glow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.polygon(scaled(OUTLINE), fill=GLOW_GOLD + (255,))
gd.ellipse([sc(v) for v in CROWN], outline=GLOW_GOLD + (255,), width=int(sc(20)))
gd.ellipse([sc(CLAP[0] - CLAP[2]), sc(CLAP[1] - CLAP[2]), sc(CLAP[0] + CLAP[2]), sc(CLAP[1] + CLAP[2])],
           fill=ACCENT + (255,))
for (ax, ay), (bx, by) in RINGS:
    for x0, y0, x1, y1 in [(ax, ay, bx, by), (2 * CX - ax, ay, 2 * CX - bx, by)]:
        gd.line([(sc(x0), sc(y0)), (sc(x1), sc(y1))], fill=ACCENT + (255,), width=int(sc(17)))
glow = glow.filter(ImageFilter.GaussianBlur(int(sc(16))))
img.alpha_composite(glow)

draw = ImageDraw.Draw(img)


def grad_polygon(points, c_top, c_bot):
    """Fill an arbitrary polygon (W-space points) with a vertical gradient."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, y0, x1, y1 = int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1
    w, h = x1 - x0, y1 - y0
    grad = vgradient((w, h), c_top, c_bot).convert("RGBA")
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).polygon([(px - x0, py - y0) for px, py in points], fill=255)
    img.paste(grad, (x0, y0), m)


# ---- bell body (gradient fill) ----
grad_polygon(scaled(OUTLINE), GOLD_HI, GOLD_LO)

# ---- crown loop (ring + a touch of sheen) ----
draw.ellipse([sc(v) for v in CROWN], outline=GOLD_LO, width=int(sc(20)))
draw.arc([sc(v) for v in CROWN], 150, 250, fill=GOLD_HI, width=int(sc(7)))

# ---- clapper (accent dot + highlight) ----
cx, cy, cr = CLAP
draw.ellipse([sc(cx - cr), sc(cy - cr), sc(cx + cr), sc(cy + cr)], fill=ACCENT)
draw.ellipse([sc(cx - 9), sc(cy - 13), sc(cx + 3), sc(cy - 1)], fill=ACCENT_HI)

# ---- ringing lines (both sides), rounded caps ----
for (ax, ay), (bx, by) in RINGS:
    for x0, y0, x1, y1 in [(ax, ay, bx, by), (2 * CX - ax, ay, 2 * CX - bx, by)]:
        draw.line([(sc(x0), sc(y0)), (sc(x1), sc(y1))], fill=ACCENT, width=int(sc(17)))
        for px, py in [(x0, y0), (x1, y1)]:
            r = sc(8)
            draw.ellipse([sc(px) - r, sc(py) - r, sc(px) + r, sc(py) + r], fill=ACCENT)

# ---- subtle top sheen for depth ----
sheen = Image.new("RGBA", (W, W), (0, 0, 0, 0))
ImageDraw.Draw(sheen).rounded_rectangle(
    [sc(4), sc(4), W - 1 - sc(4), W - 1 - sc(4)], radius=RADIUS, outline=(255, 255, 255, 38), width=int(sc(3))
)
img.alpha_composite(sheen)

# downscale for anti-aliasing
final = img.resize((S, S), Image.LANCZOS)
final.save("icon_1024.png")

# Windows .ico (multi-resolution; electron-builder embeds it on --win builds)
final.resize((256, 256), Image.LANCZOS).save(
    "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
)
print("wrote icon_1024.png and icon.ico")
