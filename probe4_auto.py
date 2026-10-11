# -*- coding: utf-8 -*-
"""调试自动网格拟合为何未被采纳。"""
import os
import sys

import numpy as np

from ast_reader import PlateAnalyzer, load_template, read_image

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "samples", "real_01.jpg")
    t = load_template(os.path.join(HERE, "template.json"))
    an = PlateAnalyzer(t)
    img = read_image(path)
    c = an.find_plate(img)
    warped, M, (W, H) = an.warp(img, c)
    print(f"warped {W}x{H}")

    prof_x, prof_y = an._darkness_profiles(warped)
    print("pitch_x =", an._pitch_of(prof_x), " pitch_y =", an._pitch_of(prof_y))

    auto, pa, tr = an.fit_grid(warped)
    if auto is None:
        print("fit_grid 返回 None")
    else:
        cols = sorted({v[0] for v in auto.values()})
        rows = sorted({v[1] for v in auto.values()})
        print("auto 列中心:", cols)
        print("auto 行中心:", rows)
        print("auto 列距:", [int(x) for x in np.diff(cols)])
        print("auto 行距:", [int(x) for x in np.diff(rows)])
        print("score(auto) =", an.grid_score(warped, auto))
    tpl = an.template_centers(W, H)
    print("score(template) =", an.grid_score(warped, tpl))

    # 手工检查：相位扫描的得分曲线（横向）
    sm = an._smooth(prof_x)
    pitch = an._pitch_of(prof_x)
    scores = []
    for phi in range(pitch):
        xs = np.arange(phi, W, pitch)
        xs = xs[(xs >= W * 0.02) & (xs <= W * 0.95)]
        scores.append((phi, float(sm[xs].sum())))
    scores.sort(key=lambda z: -z[1])
    print("横向相位前5:", [(p, round(s, 1)) for p, s in scores[:5]])

    smy = an._smooth(prof_y)
    pitchy = an._pitch_of(prof_y)
    scoresy = []
    for phi in range(pitchy):
        ys = np.arange(phi, H, pitchy)
        ys = ys[(ys >= H * 0.02) & (ys <= H * 0.95)]
        scoresy.append((phi, float(smy[ys].sum())))
    scoresy.sort(key=lambda z: -z[1])
    print("纵向相位前5:", [(p, round(s, 1)) for p, s in scoresy[:5]])


if __name__ == "__main__":
    main()
