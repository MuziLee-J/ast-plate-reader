# -*- coding: utf-8 -*-
"""网格检测实验：对比 原始亮度 / 高通滤波 两种信号在低对比度干板上的效果。"""
import os
import sys

import cv2
import numpy as np

from ast_reader import PlateAnalyzer, load_template, read_image

HERE = os.path.dirname(os.path.abspath(__file__))


def axis_positions(prof, size, count):
    q = prof - prof.mean()
    ac = np.correlate(q, q, "full")[len(q) - 1:]
    ac = ac / ac[0]
    lo, hi = 40, min(240, len(prof) // 3)
    pitch = int(max(range(lo, hi), key=lambda L: ac[L]))
    sm = np.convolve(prof.astype(np.float32), np.ones(5, np.float32) / 5, mode="same")
    lo2, hi2 = size * 0.02, size * 0.95
    best_phi, bs = 0, -1e18
    for phi in range(pitch):
        xs = np.arange(phi, size, pitch)
        xs = xs[(xs >= lo2) & (xs <= hi2)]
        if len(xs) == 0:
            continue
        sc = float(sm[xs].sum())
        if sc > bs:
            best_phi, bs = phi, sc
    pos = np.arange(best_phi, size, pitch)
    pos = pos[(pos >= lo2) & (pos <= hi2)]
    if len(pos) < count:
        return None, pitch, 0.0
    best, bi = -1e18, 0
    for i in range(len(pos) - count + 1):
        sc = float(sm[pos[i:i + count]].sum())
        if sc > best:
            best, bi = sc, i
    return pos[bi:bi + count], pitch, round(float(ac[pitch]), 3)


def fit_from(prof_x, prof_y, W, H, nrow=8, ncol=12):
    cs, px_, ax = axis_positions(prof_x, W, ncol)
    rs, py_, ay = axis_positions(prof_y, H, nrow)
    if cs is None or rs is None:
        return None, (px_, ax, py_, ay)
    centers = {}
    for i, L in enumerate("ABCDEFGH"):
        for j in range(12):
            centers[f"{L}{j+1}"] = (int(cs[j]), int(rs[i]))
    return centers, (px_, ax, py_, ay)


def rim_contrast(gray, centers, R):
    H, W = gray.shape
    vals = []
    for (cx, cy) in centers.values():
        def pt(x, y, rad=3):
            if x - rad < 0 or y - rad < 0 or x + rad + 1 > W or y + rad + 1 > H:
                return None
            return float(gray[y - rad:y + rad + 1, x - rad:x + rad + 1].mean())
        c0 = pt(cx, cy)
        rs = [pt(cx + R, cy), pt(cx - R, cy), pt(cx, cy + R), pt(cx, cy - R)]
        rs = [v for v in rs if v is not None]
        if c0 is None or not rs:
            continue
        vals.append(np.mean(rs) - c0)
    return float(np.mean(vals)) if vals else -1e9


def main():
    path = sys.argv[1]
    t = load_template(os.path.join(HERE, "templates", "dryplate_a.json"))
    an = PlateAnalyzer(t)
    img = read_image(path)
    c = an.find_plate(img)
    warped, M, (W, H) = an.warp(img, c)
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY).astype(np.float32)

    def profiles(g, band=0.12):
        dark = 255.0 - g
        px_ = dark[int(H * band):int(H * (1 - band)), :].mean(axis=0)
        py_ = dark[:, int(W * 0.08):int(W * 0.92)].mean(axis=1)
        return px_, py_

    base_R = int(round(an.well_r_px * 0.9))

    # 1. 模板网格
    tmpl = an.template_centers(W, H)
    print(f"模板网格 rim对比 = {rim_contrast(gray, tmpl, base_R):.1f}  (R={base_R})")

    # 2. 原始亮度
    fx, fy = profiles(gray)
    grid_raw, meta_raw = fit_from(fx, fy, W, H)
    if grid_raw:
        print(f"原始亮度拟合: pitch=({meta_raw[0]},{meta_raw[2]}) ac=({meta_raw[1]},{meta_raw[3]})"
              f"  rim对比 = {rim_contrast(gray, grid_raw, base_R):.1f}")
        # 用检测 pitch 推算半径
        R2 = int(round((meta_raw[0] + meta_raw[2]) / 2 * (t["well_diameter_mm"] / 9.0) / 2 * 0.9))
        print(f"    按检测孔距的 R={R2}, rim对比 = {rim_contrast(gray, grid_raw, R2):.1f}")

    # 3. 高通滤波
    hp = gray - cv2.GaussianBlur(gray, (0, 0), 25)
    fx2, fy2 = profiles(hp)
    grid_hp, meta_hp = fit_from(fx2, fy2, W, H)
    if grid_hp:
        print(f"高通滤波拟合: pitch=({meta_hp[0]},{meta_hp[2]}) ac=({meta_hp[1]},{meta_hp[3]})"
              f"  rim对比 = {rim_contrast(gray, grid_hp, base_R):.1f}")
        R3 = int(round((meta_hp[0] + meta_hp[2]) / 2 * (t["well_diameter_mm"] / 9.0) / 2 * 0.9))
        print(f"    按检测孔距的 R={R3}, rim对比 = {rim_contrast(gray, grid_hp, R3):.1f}")


if __name__ == "__main__":
    main()
