# -*- coding: utf-8 -*-
"""显色板颜色探针：打印逐孔 色相/饱和度 + 分类字母（G生长/N无生长/P临界）。"""
import os
import sys

import cv2
import numpy as np

from ast_reader import PlateAnalyzer, load_template, read_image

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    path = sys.argv[1]
    tpl_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "templates", "chromogenic_plate.json")
    t = load_template(tpl_path)
    an = PlateAnalyzer(t)
    img = read_image(path)
    c = an.find_plate(img)
    warped, M, (W, H) = an.warp(img, c)
    centers, src, score = an.locate_wells(warped)
    print(f"网格: {src} R={an.well_r_px}")
    centers = an.refine_centers(warped, centers)

    hsv = cv2.cvtColor(warped, cv2.COLOR_BGR2HSV)
    yy, xx = np.mgrid[0:H, 0:W]
    rr = int(round(an.well_r_px * 0.42))

    for L in "ABCDEFGH":
        row_h, row_s, row_c = [], [], []
        for j in range(1, 13):
            cx, cy = centers[f"{L}{j}"]
            m = (xx - cx) ** 2 + (yy - cy) ** 2 <= rr * rr
            px = hsv[m]
            h, s = float(np.median(px[:, 0])), float(np.median(px[:, 1]))
            if (h >= 150 or h <= 15) and s > 55:
                c_ = "G"
            elif h <= 138 and s > 40:
                c_ = "N"
            else:
                c_ = "P"
            row_h.append(int(h))
            row_s.append(int(s))
            row_c.append(c_)
        print(f"  {L} H " + " ".join(f"{v:3d}" for v in row_h))
        print(f"    S " + " ".join(f"{v:3d}" for v in row_s))
        print(f"    类 " + "  ".join(v for v in row_c))


if __name__ == "__main__":
    main()
