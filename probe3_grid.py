# -*- coding: utf-8 -*-
"""网格诊断：ASCII 预览 + 孔距自相关 + 圆盘匹配滤波（用于确认真实孔位网格）。

用法（在 ast-demo 目录）:
    uv run --with opencv-python --with numpy python probe3_grid.py <照片路径>
"""
import os
import sys

import cv2
import numpy as np

from ast_reader import PlateAnalyzer, load_template, read_image

HERE = os.path.dirname(os.path.abspath(__file__))


def ascii_art(warped, cols=190):
    H, W = warped.shape[:2]
    cw = W / cols
    rows = int(H / (cw * 2))
    ch = H / rows
    out = []
    for i in range(rows):
        line = ""
        for j in range(cols):
            x0, x1 = int(j * cw), min(W, max(int((j + 1) * cw), int(j * cw) + 1))
            y0, y1 = int(i * ch), min(H, max(int((i + 1) * ch), int(i * ch) + 1))
            b, g, r = warped[y0:y1, x0:x1].mean(axis=(0, 1))
            px = np.uint8([[[b, g, r]]])
            h, s, v = cv2.cvtColor(px, cv2.COLOR_BGR2HSV)[0, 0]
            if h >= 145 and s >= 55:
                line += "P"
            elif 95 <= h <= 140 and s >= 45:
                line += "B"
            elif s < 45 and v >= 170:
                line += "."
            elif v < 150 and s < 70:
                line += "#"
            elif 30 <= h <= 60 and s >= 30:
                line += "y"
            else:
                line += "?"
        out.append(line)
    return "\n".join(out)


def pitch_peaks(profile):
    p = profile.astype(np.float64)
    p = p - p.mean()
    ac = np.correlate(p, p, "full")[len(p) - 1:]
    ac = ac / ac[0]
    peaks = []
    for lag in range(20, min(len(ac) - 1, 500)):
        if ac[lag] > ac[lag - 1] and ac[lag] >= ac[lag + 1] and ac[lag] > 0.1:
            peaks.append((lag, round(float(ac[lag]), 3)))
    return peaks[:8]


def clusters1d(vals, tol):
    vals = np.sort(np.asarray(vals, float))
    groups = [[vals[0]]]
    for v in vals[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [float(np.mean(g)) for g in groups], [len(g) for g in groups]


def main():
    path = sys.argv[1]
    img = read_image(path)
    t = load_template(os.path.join(HERE, "template.json"))
    an = PlateAnalyzer(t)
    c = an.find_plate(img)
    print("板子四角(原图坐标):", c.round(0).astype(int).tolist())
    h0, w0 = img.shape[:2]
    print(f"原图 {w0}x{h0}")
    warped, M, (W, H) = an.warp(img, c)
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)

    print("\n=== 校正后 ASCII 预览（P=粉红 B=蓝 .=亮底 y=黄 ?=其他 #=暗）===")
    print(ascii_art(warped))

    print("\n=== 孔距自相关（峰 lag ≈ 孔位间距 px）===")
    print("X:", pitch_peaks(gray.mean(axis=0)))
    print("Y:", pitch_peaks(gray.mean(axis=1)))

    print("\n=== 圆盘匹配滤波峰值 ===")
    g = gray.astype(np.float32)
    for r in (16, 20, 26):
        disk = np.zeros((2 * r + 1, 2 * r + 1), np.float32)
        cv2.circle(disk, (r, r), r, 1.0, -1)
        disk /= disk.sum()
        resp = cv2.filter2D(-g, -1, disk)
        m = cv2.dilate(resp, np.ones((r + 3, r + 3)))
        local = resp >= m - 1e-6
        for k in (1.0, 1.5):
            thr = resp.mean() + k * resp.std()
            ys, xs = np.nonzero(local & (resp > thr))
            if len(xs) == 0:
                continue
            cx, nc = clusters1d(xs, r * 0.8)
            cy, nr = clusters1d(ys, r * 0.8)
            print(f"r={r} k={k}: 峰 {len(xs)} 个，列簇 {len(cx)}，行簇 {len(cy)}")
            if 6 <= len(cx) <= 30:
                print("  列簇间距:", [round(float(x), 1) for x in np.diff(cx)])
                print("  行簇间距:", [round(float(x), 1) for x in np.diff(cy)])


if __name__ == "__main__":
    main()
