# -*- coding: utf-8 -*-
"""细查配准：用霍夫圆检测真实孔位网格，与模板网格对比，定位漂移。

用法（在 ast-demo 目录）:
    uv run --with opencv-python --with numpy python probe2_align.py <照片路径>
"""
import os
import sys

import cv2
import numpy as np

from ast_reader import PlateAnalyzer, load_template, read_image, write_image

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = "ABCDEFGH"


def clusters(vals, tol):
    vals = np.sort(np.asarray(vals, float))
    groups = [[vals[0]]]
    for v in vals[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [float(np.mean(g)) for g in groups]


def main():
    path = sys.argv[1]
    img = read_image(path)
    t = load_template(os.path.join(HERE, "template.json"))
    an = PlateAnalyzer(t)
    c = an.find_plate(img)
    warped, M, (W, H) = an.warp(img, c)
    write_image(os.path.join(HERE, "outputs", "probe_warped.png"), warped)
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)

    # 1. 霍夫圆检测真实孔位
    blur = cv2.medianBlur(gray, 5)
    circles = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp=1.5,
                               minDist=int(an.well_r_px * 1.5), param1=120, param2=28,
                               minRadius=int(an.well_r_px * 0.55),
                               maxRadius=int(an.well_r_px * 1.5))
    n = 0 if circles is None else len(circles[0])
    print("霍夫圆检出:", n)
    xs = ys = None
    if circles is not None:
        xs = clusters([c0[0] for c0 in circles[0]], 25)
        ys = clusters([c0[1] for c0 in circles[0]], 25)
        print(f"检出网格: {len(ys)} 行 x {len(xs)} 列")
        print("列中心:", [round(x, 1) for x in xs])
        print("行中心:", [round(y, 1) for y in ys])

    tc = an.template_centers(W, H)
    tmpl_xs = sorted({tc[f"A{j + 1}"][0] for j in range(12)})
    tmpl_ys = sorted({tc[f"{L}1"][1] for L in ROWS})
    print("模板列中心:", tmpl_xs)
    print("模板行中心:", tmpl_ys)

    # 2. 逐孔对比度（正 = 孔中心比孔间缝隙暗，说明采到孔内）
    def patch(px, py, r=3):
        px, py = int(px), int(py)
        if px - r < 0 or py - r < 0 or px + r + 1 > W or py + r + 1 > H:
            return None
        return float(gray[py - r:py + r + 1, px - r:px + r + 1].mean())

    half_px = int(round(an.grid.get("pitch_mm", 9.0) / 2 * W / t["plate_mm"][0]))
    strength, neg = {}, []
    for L in ROWS:
        for j in range(1, 13):
            cx, cy = tc[f"{L}{j}"]
            ci = patch(cx, cy)
            gaps = [patch(cx + half_px, cy), patch(cx - half_px, cy),
                    patch(cx, cy + half_px), patch(cx, cy - half_px)]
            gaps = [g for g in gaps if g is not None]
            if ci is None or not gaps:
                continue
            con = float(np.mean(gaps) - ci)
            strength[f"{L}{j}"] = con
            if con <= 0:
                neg.append(f"{L}{j}({con:.0f})")
    print("\n逐孔对比度（模板定位，正=采到孔内）:")
    for L in ROWS:
        print(f"  {L}  " + " ".join(f"{strength.get(f'{L}{j}', 0):4.0f}" for j in range(1, 13)))
    print("负对比孔:", "、".join(neg) if neg else "无")

    # 3. 全孔 HSV 表（模板定位）
    hsv = cv2.cvtColor(warped, cv2.COLOR_BGR2HSV)
    rr = int(an.well_r_px * 0.42)
    yy, xx = np.mgrid[0:H, 0:W]
    print("\n中位 HSV 表 H/S（模板定位）:")
    for L in ROWS:
        line = []
        for j in range(1, 13):
            cx, cy = tc[f"{L}{j}"]
            mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= rr * rr
            h, s, v = np.median(hsv[mask], axis=0)
            line.append(f"{int(h):3d}/{int(s):3d}")
        print(f"  {L}  " + " ".join(line))

    # 4. 若霍夫网格为 8x12，用它重新采样颜色
    if xs is not None and len(xs) == 12 and len(ys) == 8:
        print("\n用霍夫网格重新采样分色图:")
        for L, cy in zip(ROWS, ys):
            line = ""
            for cx in xs:
                mask = (xx - int(cx)) ** 2 + (yy - int(cy)) ** 2 <= rr * rr
                h, s, v = np.median(hsv[mask], axis=0)
                line += "P" if (h >= 150 and s > 60) else ("b" if (90 <= h <= 135 and s > 50) else "?")
            print(f"  {L}  {line}")


if __name__ == "__main__":
    main()
