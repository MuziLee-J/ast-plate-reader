# -*- coding: utf-8 -*-
"""检查一张照片的判读条件：板检测 → 孔位自动校准 → 颜色分色 → 完整管线判读。

用法（在 ast-demo 目录）:
    uv run --with opencv-python --with numpy --with pillow --with openpyxl python probe_photo.py <照片路径>
"""
import os
import sys

import cv2
import numpy as np

from ast_reader import PlateAnalyzer, load_template, read_image

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = "ABCDEFGH"


def main():
    path = sys.argv[1]
    img = read_image(path)
    print(f"图像尺寸: {img.shape[1]} x {img.shape[0]}")

    t = load_template(os.path.join(HERE, "template.json"))
    an = PlateAnalyzer(t)

    # 1. 板子检测
    c = an.find_plate(img)
    print("板子检测:", "成功" if c is not None else "失败")
    if c is None:
        print("→ 判读条件不满足：板子未占满画面或与背景反差不足")
        return

    # 2. 透视校正 + 孔位自动校准
    warped, M, (W, H) = an.warp(img, c)
    centers, src, score = an.locate_wells(warped)
    print(f"孔位校准: 方式={src}，对位评分={score}，孔半径≈{an.well_r_px}px")
    centers = an.refine_centers(warped, centers)

    # 3. 颜色分色（P=粉红/生长型, b=蓝/抑制型, ?=其他）
    hsv = cv2.cvtColor(warped, cv2.COLOR_BGR2HSV)
    rr = int(round(an.well_r_px * 0.42))
    yy, xx = np.mgrid[0:H, 0:W]
    print("\n分色图（P=粉红, b=蓝, ?=其他）:")
    counts = {"P": 0, "b": 0, "?": 0}
    for L in ROWS:
        line = ""
        for j in range(1, 13):
            cx, cy = centers[f"{L}{j}"]
            mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= rr * rr
            h, s, v = np.median(hsv[mask], axis=0)
            ch = "P" if (h >= 150 and s > 60) else ("b" if (90 <= h <= 135 and s > 50) else "?")
            counts[ch] += 1
            line += ch
        print(f"  {L}  {line}")
    print(f"统计: 粉红 {counts['P']}，蓝 {counts['b']}，其他 {counts['?']}")

    # 4. 完整判读管线
    print("\n--- 完整判读管线（模板为演示配置，药物名/浓度仅供参考）---")
    an2 = PlateAnalyzer(t)
    res = an2.run(path, outdir=os.path.join(HERE, "outputs"), sample_no="REAL-01")
    wmap = {w["pos"]: w for w in res["wells"]}
    print("判读矩阵（G=生长, -=无生长, u=未使用, ?=未知）:")
    for L in ROWS:
        line = ""
        for j in range(1, 13):
            st = wmap[f"{L}{j}"].get("status")
            line += {"growth": "G", "no_growth": "-", "unused": "u", "unknown": "?"}.get(st, "?")
        print(f"  {L}  {line}")
    print("对照孔:", res["controls"])
    for wmsg in res["warnings"]:
        print("警告:", wmsg)
    for m in res["mic"]:
        flag = ("  ⚠ " + "；".join(m["flags"])) if m["flags"] else ""
        print(f"  列{m['col']:2d} {m['code']:4s} MIC={m['mic']:>7s} 置信度={m['confidence']}{flag}")
    print("标注图:", res["annotated"])


if __name__ == "__main__":
    main()
