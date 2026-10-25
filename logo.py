"""Friday's mark, "Pulse" (he chose it on 2026-10-10): her voice drawn as a ring of 40 levels around a calm
core, no letter. Defined once here in a 100x100 box; the tray icon, the window header (svg()) and the phone's
icons (android.py in the logo scratch folder, and PulseView.kt, which uses the same numbers) all come from it."""
import math

CYAN, VIOLET = (63, 214, 242), (139, 124, 246)
N, R_IN, R_BASE, R_SPAN, CORE, WIDTH = 40, 22.0, 6.0, 13.0, 7.0, 2.1


def level(i):
    """How long tick i is (0..1): a soft voice shape that never repeats exactly around the ring."""
    return 0.35 + 0.65 * abs(math.sin(i * 0.55) * math.cos(i * 0.21))


def ticks():
    """[(x1, y1, x2, y2, level, accent, angle 0..1)] in the 100x100 box."""
    out = []
    for i in range(N):
        a = i / N * 2 * math.pi - math.pi / 2
        lv = level(i)
        r2 = R_IN + R_BASE + lv * R_SPAN
        out.append((50 + R_IN * math.cos(a), 50 + R_IN * math.sin(a), 50 + r2 * math.cos(a), 50 + r2 * math.sin(a),
                    lv, lv > 0.78, i / N))
    return out


def mix(a, b, t):
    return tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))


def render(size, ring=(CYAN, VIOLET), bg=True, ink=(238, 241, 246)):
    """The mark as a PIL image (supersampled). ring = the colours of the loudest ticks (the tray tints them by state)."""
    from PIL import Image, ImageDraw
    S = size * 4
    k = S / 100
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    if bg:  # a dark rounded tile with a soft top light
        tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.23), fill=255)
        grad = Image.new("RGBA", (S, S))
        gd = ImageDraw.Draw(grad)
        for y in range(S):
            c = mix((28, 33, 48), (7, 8, 12), y / S)
            gd.line([(0, y), (S, y)], fill=c + (255,))
        tile.paste(grad, (0, 0), mask)
        img.alpha_composite(tile)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if bg:  # on its tile the ring keeps a margin, like an app icon
        k *= 0.8
        off = S * 0.1
    else:
        off = 0
    w = max(1, int(WIDTH * k * (1.25 if size <= 32 else 1)))  # a touch bolder when tiny, so it doesn't vanish
    for x1, y1, x2, y2, lv, accent, t in ticks():
        col = mix(ring[0], ring[1], t) + (255,) if accent else ink + (int(255 * (0.45 + lv * 0.55)),)
        p1, p2 = (x1 * k + off, y1 * k + off), (x2 * k + off, y2 * k + off)
        d.line([p1, p2], fill=col, width=w)
        for px, py in (p1, p2):  # round caps
            d.ellipse([px - w / 2, py - w / 2, px + w / 2, py + w / 2], fill=col)
    d.ellipse([(50 - CORE) * k + off, (50 - CORE) * k + off, (50 + CORE) * k + off, (50 + CORE) * k + off], fill=ink + (255,))
    img.alpha_composite(layer)
    return img.resize((size, size), Image.LANCZOS)


def svg():
    """The mark for her window: ticks take var(--f-fill) (light or dark theme), the loudest ones the accent colours.
    Each tick carries --i so the window can ripple them while she works."""
    lines = []
    for i, (x1, y1, x2, y2, lv, accent, t) in enumerate(ticks()):
        col = "#%02x%02x%02x" % mix(CYAN, VIOLET, t)
        stroke = f'stroke="{col}"' if accent else 'style="stroke:var(--f-fill)"'
        lines.append(f'<line class="tick" style="--i:{i}" x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" {stroke} '
                     f'stroke-width="{WIDTH}" stroke-linecap="round" opacity="{0.45 + lv * 0.55:.2f}"/>')
    return (f'<svg viewBox="6 6 88 88" class="mark">{"".join(lines)}'
            f'<circle cx="50" cy="50" r="{CORE}" class="f"/></svg>')


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    render(512).save(out + "/pulse_512.png")
    render(512, bg=False, ink=(21, 22, 26)).save(out + "/pulse_light_512.png")
    for s in (64, 32, 24, 16):
        render(s).save(out + f"/pulse_{s}.png")
