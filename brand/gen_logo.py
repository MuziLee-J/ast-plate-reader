#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AST-Plate-Reader 品牌标识生成器（v3）
单一几何源 -> SVG（矢量源）+ PNG（栅格成品）+ ICO（exe/快捷方式图标）

设计意图
  外框 + 内板 = 96 孔板真实的「凸边 + 板面」结构；
  孔位按药物浓度由空到实铺开，正方形孔距（与真板 9mm 等距一致）；
  一个绿色圆环落在由「生长」转为「不生长」的那一孔 —— MIC 判定终点；
  绿圈取自软件界面里 MIC 判定孔的原样式，符号与产品 UI 同源。

方案
  hero —— 只突出中间一行（一行就是一个药物的浓度梯度，即判读动作本身）
  grid —— 整版孔位同等权重（信息量最大，远看即一张板）
"""
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 调色板
INK0 = (10, 23, 28)
INK1 = (20, 50, 60)
CARD_EDGE = (226, 236, 240)   # 板外框（凸边）
PANEL = (252, 254, 255)       # 板面
RIM = (176, 198, 206)         # 空孔圈
RIM_FAINT = (206, 219, 224)
MIC_GREEN = (26, 127, 55)
TXT_INK = (11, 30, 37)
TXT_GRAY = (92, 115, 123)
TXT_LIGHT = (255, 255, 255)

RAMP_SIGNAL = [(240, 176, 194), (231, 128, 156), (216, 78, 114), (186, 38, 80), (146, 16, 58)]
RAMP_INK = [(198, 214, 220), (146, 172, 180), (94, 128, 138), (48, 84, 94), (18, 46, 56)]

# ---------------------------------------------------------------- 几何
S = 1024
BG_R = 232
OUTER_W, OUTER_H = 848, 604
INSET = 18
PLATE_BOX = ((S - OUTER_W) // 2, (S - OUTER_H) // 2,
             (S + OUTER_W) // 2, (S + OUTER_H) // 2)
OUTER_R, PANEL_R = 62, 46
COLS, ROWS = 8, 5
MX = 74
MIC_COL = 3
HERO_ROW = 2


def inner_box():
    x0, y0, x1, y1 = PLATE_BOX
    return (x0 + INSET, y0 + INSET, x1 - INSET, y1 - INSET)


def geom():
    x0, y0, x1, y1 = inner_box()
    w, h = x1 - x0, y1 - y0
    px = (w - 2 * MX) / COLS          # 正方形孔距
    my = (h - ROWS * px) / 2
    r = px * 0.33
    centers = [[(x0 + MX + px * (c + .5), y0 + my + px * (r_ + .5))
                for c in range(COLS)] for r_ in range(ROWS)]
    return centers, px, r, my


def ramp_color(col, ramp):
    n = COLS - MIC_COL - 1
    t = (col - MIC_COL - 1) / max(1, n - 1)
    pos = t * (len(ramp) - 1)
    i = min(int(pos), len(ramp) - 2)
    f = pos - i
    a, b = ramp[i], ramp[i + 1]
    return tuple(int(round(a[k] + (b[k] - a[k]) * f)) for k in range(3))


def well_style(col, row, concept, variant):
    """hero 方案里，非主行一律走中性灰，只让一行承担颜色 —— 层次更干净"""
    faint = concept == 'hero' and row != HERO_ROW
    if faint:
        if col <= MIC_COL:
            return PANEL, RIM_FAINT, 5, 175
        return (206, 219, 224), None, 0, 175
    if col <= MIC_COL:
        return (255, 255, 255), RIM, 6, 255
    c = ramp_color(col, RAMP_SIGNAL if variant == 'signal' else RAMP_INK)
    return c, None, 0, 255


def BK(c):
    return '%02X%02X%02X' % (c[0], c[1], c[2])


# ---------------------------------------------------------------- 栅格
def make_mark(size=1024, bg='ink', concept='hero', variant='signal', ss=4):
    N = size * ss
    k = N / S
    img = Image.new('RGBA', (N, N), (0, 0, 0, 0))

    if bg == 'ink':
        grad = np.zeros((N, N, 3), dtype=np.uint8)
        for i in range(3):
            grad[:, :, i] = np.linspace(INK0[i], INK1[i], N, dtype=np.uint8)[:, None]
        layer = Image.fromarray(grad, 'RGB').convert('RGBA')
        mask = Image.new('L', (N, N), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, N - 1, N - 1), radius=BG_R * k, fill=255)
        img.paste(layer, (0, 0), mask)

    box = tuple(int(v * k) for v in PLATE_BOX)
    sh = Image.new('RGBA', (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        (box[0], box[1] + int(20 * k), box[2], box[3] + int(20 * k)),
        radius=OUTER_R * k, fill=(0, 0, 0, 135))
    img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(26 * k)))

    d = ImageDraw.Draw(img)
    d.rounded_rectangle(box, radius=OUTER_R * k, fill=CARD_EDGE + (255,))

    ibox = tuple(int(v * k) for v in inner_box())
    d.rounded_rectangle(ibox, radius=PANEL_R * k, fill=PANEL + (255,))

    centers, px, wr, _ = geom()
    order = [r for r in range(ROWS) if r != HERO_ROW] + [HERO_ROW]
    for r in order:
        for c in range(COLS):
            cx, cy = centers[r][c]
            fill, outline, ow, alpha = well_style(c, r, concept, variant)
            layer = Image.new('RGBA', (N, N), (0, 0, 0, 0))
            dl = ImageDraw.Draw(layer)
            bb = ((cx - wr) * k, (cy - wr) * k, (cx + wr) * k, (cy + wr) * k)
            if outline:
                dl.ellipse(bb, fill=fill + (alpha,), outline=outline + (alpha,),
                           width=max(1, int(ow * k)))
            else:
                dl.ellipse(bb, fill=fill + (alpha,))
            img = Image.alpha_composite(img, layer)

    d = ImageDraw.Draw(img)
    for r in ([HERO_ROW] if concept == 'hero' else range(ROWS)):
        cx, cy = centers[r][MIC_COL]
        rr, rw = wr * 1.46, max(2, int(7.4 * k))
        d.ellipse(((cx - rr) * k, (cy - rr) * k, (cx + rr) * k, (cy + rr) * k),
                  outline=MIC_GREEN + (255,), width=rw)

    return img.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- SVG
def mark_svg(bg='ink', concept='hero', variant='signal'):
    centers, px, wr, _ = geom()
    x0, y0, x1, y1 = PLATE_BOX
    ix0, iy0, ix1, iy1 = inner_box()
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}" width="{S}" height="{S}" '
         f'role="img" aria-label="AST Plate Reader">', '<defs>',
         f'<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
         f'<stop offset="0" stop-color="#{BK(INK0)}"/><stop offset="1" stop-color="#{BK(INK1)}"/>'
         f'</linearGradient>',
         '<filter id="sh" x="-30%" y="-30%" width="160%" height="160%">'
         '<feDropShadow dx="0" dy="20" stdDeviation="22" flood-color="#000" flood-opacity=".5"/>'
         '</filter>', '</defs>']
    if bg == 'ink':
        o.append(f'<rect width="{S}" height="{S}" rx="{BG_R}" fill="url(#bg)"/>')
    o.append(f'<rect x="{x0}" y="{y0}" width="{x1-x0}" height="{y1-y0}" rx="{OUTER_R}" '
             f'fill="#{BK(CARD_EDGE)}" filter="url(#sh)"/>')
    o.append(f'<rect x="{ix0}" y="{iy0}" width="{ix1-ix0}" height="{iy1-iy0}" rx="{PANEL_R}" '
             f'fill="#{BK(PANEL)}"/>')
    for r in range(ROWS):
        for c in range(COLS):
            cx, cy = centers[r][c]
            fill, outline, ow, alpha = well_style(c, r, concept, variant)
            op = f' opacity="{alpha/255:.2f}"' if alpha < 255 else ''
            if outline:
                o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{wr:.1f}" fill="#{BK(fill)}" '
                         f'stroke="#{BK(outline)}" stroke-width="{ow}"{op}/>')
            else:
                o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{wr:.1f}" fill="#{BK(fill)}"{op}/>')
    for r in ([HERO_ROW] if concept == 'hero' else range(ROWS)):
        cx, cy = centers[r][MIC_COL]
        o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{wr*1.46:.1f}" fill="none" '
                 f'stroke="#{BK(MIC_GREEN)}" stroke-width="7.4"/>')
    o.append('</svg>')
    return '\n'.join(o)


# ---------------------------------------------------------------- favicon（16-32px 专用简化版）
def favicon_svg():
    """64 视框：深色圆角底 + 白牌 + 一行 5 孔（空、判定圈、三段生长）"""
    holes = [(13.0, RIM), (22.0, None), (31.0, (238, 143, 169)),
             (40.0, (196, 58, 102)), (49.0, (146, 14, 58))]
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">',
         f'<defs><linearGradient id="fb" x1="0" y1="0" x2="0" y2="1">'
         f'<stop offset="0" stop-color="#{BK(INK0)}"/><stop offset="1" stop-color="#{BK(INK1)}"/>'
         f'</linearGradient></defs>',
         '<rect width="64" height="64" rx="15" fill="url(#fb)"/>',
         f'<rect x="8" y="17" width="48" height="30" rx="6.5" fill="#{BK(PANEL)}" '
         f'stroke="#{BK(CARD_EDGE)}" stroke-width="1.6"/>']
    for x, col in holes:
        if col is None:
            p.append(f'<circle cx="{x}" cy="32" r="5.4" fill="none" stroke="#{BK(MIC_GREEN)}" stroke-width="2.4"/>')
            p.append(f'<circle cx="{x}" cy="32" r="3.1" fill="#{BK(PANEL)}" stroke="#{BK(RIM)}" stroke-width="1.4"/>')
        else:
            p.append(f'<circle cx="{x}" cy="32" r="3.1" fill="#{BK(col)}"/>')
    p.append('</svg>')
    return '\n'.join(p)


# ---------------------------------------------------------------- 字体
F_BOLD = r'C:\Windows\Fonts\bahnschrift.ttf'
F_SEMI = r'C:\Windows\Fonts\seguisb.ttf'
F_CN = r'C:\Windows\Fonts\msyh.ttc'
F_CNB = r'C:\Windows\Fonts\msyhbd.ttc'


def font(path, size, variation=None):
    f = ImageFont.truetype(path, size)
    if variation:
        try:
            f.set_variation_by_name(variation)
        except Exception:
            pass
    return f


def tracked(d, xy, text, f, tracking, fill):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill + (255,))
        x += d.textlength(ch, font=f) + tracking


def make_lockup(scale=2, dark=False, concept='hero', variant='signal'):
    W, H = 1900, 560
    img = Image.new('RGBA', (W * scale, H * scale), (0, 0, 0, 0))
    img.alpha_composite(make_mark(420 * scale, 'none' if dark else 'ink', concept, variant),
                        (8 * scale, 70 * scale))
    d = ImageDraw.Draw(img)
    c1, c2, c3 = ((TXT_LIGHT, (176, 197, 204), (138, 162, 170)) if dark
                  else (TXT_INK, TXT_GRAY, (144, 162, 169)))
    x = 512 * scale
    d.rounded_rectangle((x, 132 * scale, x + 5 * scale, 300 * scale),
                        radius=3 * scale, fill=MIC_GREEN + (255,))
    x += 44 * scale
    tracked(d, (x, 96 * scale), 'AST', font(F_BOLD, 224 * scale, 'Bold'), 6 * scale, c1)
    tracked(d, (x + 4 * scale, 352 * scale), 'PLATE READER',
            font(F_SEMI, 48 * scale), 10 * scale, c2)
    tracked(d, (x + 4 * scale, 424 * scale), '药敏板智能判读系统',
            font(F_CN, 34 * scale), 2 * scale, c3)
    bb = img.getbbox()
    pad = 22 * scale
    return img.crop((max(0, bb[0] - pad), max(0, bb[1] - pad),
                     min(img.width, bb[2] + pad), min(img.height, bb[3] + pad)))


def lockup_svg(dark=False, concept='hero', variant='signal'):
    W, H = 1900, 560
    wm = BK(TXT_LIGHT if dark else TXT_INK)
    sub = BK((176, 197, 204) if dark else TXT_GRAY)
    cn = BK((138, 162, 170) if dark else (144, 162, 169))
    inner = mark_svg('none' if dark else 'ink', concept, variant)
    inner = inner.split('>', 1)[1].rsplit('</svg>', 1)[0]
    for a, b in (('id="bg"', 'id="bgL"'), ('url(#bg)', 'url(#bgL)'),
                 ('id="sh"', 'id="shL"'), ('url(#sh)', 'url(#shL)')):
        inner = inner.replace(a, b)
    return '\n'.join([
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="AST Plate Reader">',
        f'<g transform="translate(8,70) scale({420/S:.6f})">{inner}</g>',
        f'<rect x="512" y="132" width="5" height="168" rx="3" fill="#{BK(MIC_GREEN)}"/>',
        f'<text x="556" y="296" font-family="Bahnschrift, Segoe UI Semibold, Segoe UI, Arial, sans-serif" '
        f'font-size="224" font-weight="700" letter-spacing="6" fill="#{wm}">AST</text>',
        f'<text x="560" y="392" font-family="Segoe UI Semibold, Segoe UI, Arial, sans-serif" '
        f'font-size="48" letter-spacing="10" fill="#{sub}">PLATE READER</text>',
        f'<text x="560" y="448" font-family="Microsoft YaHei, PingFang SC, sans-serif" '
        f'font-size="34" letter-spacing="2" fill="#{cn}">药敏板智能判读系统</text>',
        '</svg>'])


# ---------------------------------------------------------------- 输出
def main():
    os.makedirs(HERE, exist_ok=True)
    w = lambda n, s: open(os.path.join(HERE, n), 'w', encoding='utf-8').write(s)

    for concept in ('grid', 'hero'):
        w(f'ast-logo-app-{concept}.svg', mark_svg('ink', concept, 'signal'))
        w(f'ast-logo-app-{concept}-ink.svg', mark_svg('ink', concept, 'ink'))
        w(f'ast-logo-horizontal-{concept}.svg', lockup_svg(False, concept, 'signal'))
        w(f'ast-logo-horizontal-{concept}-dark.svg', lockup_svg(True, concept, 'signal'))

    for sz in (512, 256, 128, 64, 32, 16):
        make_mark(sz, 'ink', 'hero', 'signal').save(os.path.join(HERE, f'ast-logo-{sz}.png'))
    make_mark(512, 'none', 'hero', 'signal').save(os.path.join(HERE, 'ast-logo-mark-512.png'))
    make_mark(256, 'ink', 'hero', 'signal').save(
        os.path.join(HERE, 'ast-logo.ico'),
        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    make_lockup(2, False, 'hero', 'signal').save(os.path.join(HERE, 'ast-logo-horizontal@2x.png'))
    make_lockup(2, True, 'hero', 'signal').save(os.path.join(HERE, 'ast-logo-horizontal-dark@2x.png'))

    # 预览（按区块顺序自上而下堆叠，避免重叠）
    fb = font(F_CNB, 27)
    fl = font(F_CN, 25)
    canvas = Image.new('RGBA', (1600, 3600), (246, 248, 249, 255))
    d = ImageDraw.Draw(canvas)
    y = 26

    def cap(text, yy, dx=64, f=None):
        d.text((dx, yy), text, font=f or fl, fill=TXT_GRAY + (255,))

    d.text((64, y), 'A  主推 · hero —— 全板走中性灰，只让一行上色（一行 = 一个药物的浓度梯度）',
           font=fb, fill=TXT_INK + (255,))
    y += 54
    canvas.alpha_composite(make_mark(384, 'ink', 'hero', 'signal'), (64, y))
    canvas.alpha_composite(make_mark(384, 'ink', 'hero', 'ink'), (500, y))
    cap('彩色版（signal）', y + 396)
    cap('单色版（ink）：印刷、铜版、单色场景', y + 396, 500)
    y += 448

    d.text((64, y), 'B  备选 · grid —— 整版孔位同等权重', font=fb, fill=TXT_INK + (255,))
    y += 54
    canvas.alpha_composite(make_mark(384, 'ink', 'grid', 'signal'), (64, y))
    canvas.alpha_composite(make_mark(384, 'ink', 'grid', 'ink'), (500, y))
    cap('彩色版（signal）', y + 396)
    cap('单色版（ink）', y + 396, 500)
    y += 448

    d.text((64, y), 'C  尺寸适配（同一图形缩到 16px 仍可辨轮廓）', font=fb, fill=TXT_INK + (255,))
    y += 50
    for i, sz in enumerate((128, 96, 64, 48, 32, 16)):
        x = 64 + i * 175
        canvas.alpha_composite(make_mark(sz, 'ink', 'hero', 'signal'), (x, y + (128 - sz)))
        cap(f'{sz}px', y + 150, x)
    y += 200

    d.text((64, y), 'D  板面结构与细节', font=fb, fill=TXT_INK + (255,))
    y += 50
    zoom = make_mark(1024, 'none', 'hero', 'signal')
    x0, y0, x1, y1 = [int(v) for v in PLATE_BOX]
    zoom = zoom.crop((x0 - 44, y0 - 44, x1 + 44, y1 + 44))
    zw = 760
    zoom = zoom.resize((zw, int(zoom.height * zw / zoom.width)), Image.LANCZOS)
    canvas.alpha_composite(zoom, (64, y))
    cap('凸边 + 板面 + 正方形孔距\n（对应真板 9mm 等距）\n\n'
        '孔位由空到实 = 浓度梯度\n\n'
        '绿圈 = MIC 判定终点\n（与软件界面同款样式）', y + 20, 830)
    y += zoom.height + 40

    d.text((64, y), 'E  横版组合', font=fb, fill=TXT_INK + (255,))
    y += 46
    lock = make_lockup(2, False, 'hero', 'signal')
    lw = 1400
    lock = lock.resize((lw, int(lock.height * lw / lock.width)), Image.LANCZOS)
    canvas.alpha_composite(lock, (64, y))
    y += lock.height + 34

    l2 = make_lockup(2, True, 'hero', 'signal')
    l2 = l2.resize((1150, int(l2.height * 1150 / l2.width)), Image.LANCZOS)
    bh = l2.height + 64
    band = Image.new('RGBA', (1600, bh), (16, 38, 46, 255))
    band.alpha_composite(l2, (64, 32))
    canvas.alpha_composite(band, (0, y))
    y += bh
    canvas.crop((0, 0, 1600, min(canvas.height, y + 20))).convert('RGB').save(
        os.path.join(HERE, 'preview.png'))

    dk_l = make_lockup(2, True, 'hero', 'signal')
    dk_l = dk_l.resize((1300, int(dk_l.height * 1300 / dk_l.width)), Image.LANCZOS)
    dk = Image.new('RGBA', (1480, dk_l.height + 80), (16, 38, 46, 255))
    dk.alpha_composite(dk_l, (90, 40))
    dk.convert('RGB').save(os.path.join(HERE, 'preview-dark.png'))

    w('ast-favicon.svg', favicon_svg())

    lt = make_lockup(2, False, 'hero', 'signal')
    lt = lt.resize((1300, int(lt.height * 1300 / lt.width)), Image.LANCZOS)
    bg = Image.new('RGBA', (1480, lt.height + 80), (255, 255, 255, 255))
    bg.alpha_composite(lt, (90, 40))
    bg.convert('RGB').save(os.path.join(HERE, 'preview-light.png'))
    print('done')


if __name__ == '__main__':
    main()
