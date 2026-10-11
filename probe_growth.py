# -*- coding: utf-8 -*-
"""干板（无显色剂）生长信号探针：打印两种生长度量矩阵，用于调参。

用法（在 ast-demo 目录）:
    uv run --with opencv-python --with numpy python probe_growth.py <照片> [模板.json]
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
    tpl_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "templates", "dryplate_a.json")
    t = load_template(tpl_path)
    an = PlateAnalyzer(t)
    img = read_image(path)
    c = an.find_plate(img)
    print("板检测:", "OK" if c is not None else "失败")
    if c is None:
        return
    warped, M, (W, H) = an.warp(img, c)
    centers, src, score = an.locate_wells(warped)
    print(f"网格: {src} 评分={score} R={an.well_r_px}px")
    centers = an.refine_centers(warped, centers)

    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY).astype(np.float32)
    yy, xx = np.mgrid[0:H, 0:W]
    rg = int(round(an.well_r_px * 0.55))
    gap_half = int(round(an.well_r_px * 1.3))

    def patch(px_, py_, rad=3):
        if px_ - rad < 0 or py_ - rad < 0 or px_ + rad + 1 > W or py_ + rad + 1 > H:
            return None
        return float(gray[py_ - rad:py_ + rad + 1, px_ - rad:px_ + rad + 1].mean())

    def spot_of(cx, cy):
        """暗点占比%：局部去光照后低于 -8 灰阶的像素比例（点状菌落敏感）"""
        sq = int(round(an.well_r_px * 2.6))
        mrad = max(6, int(round(an.well_r_px * 0.72)))
        x0, y0 = cx - sq // 2, cy - sq // 2
        if x0 < 0 or y0 < 0 or x0 + sq > W or y0 + sq > H:
            return None
        p = gray[y0:y0 + sq, x0:x0 + sq]
        det = p - cv2.GaussianBlur(p, (0, 0), max(6.0, an.well_r_px * 0.6))
        yy2, xx2 = np.mgrid[0:sq, 0:sq]
        m = (xx2 - sq / 2) ** 2 + (yy2 - sq / 2) ** 2 <= mrad * mrad
        return float((det[m] < -8).mean()) * 100

    m1, m2 = {}, {}
    for pos, (cx, cy) in centers.items():
        mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= rg * rg
        px = gray[mask]
        bg = float(np.median(px))
        gv = spot_of(cx, cy)
        m1[pos] = gv if gv is not None else 0.0
        gaps = [patch(cx + gap_half, cy), patch(cx - gap_half, cy),
                patch(cx, cy + gap_half), patch(cx, cy - gap_half)]
        gaps = [v for v in gaps if v is not None and v > bg - 60]
        m2[pos] = (float(np.mean(gaps)) - bg) if gaps else 0.0

    ctrls = (t.get("controls") or {}).get("positive") or []
    pcs = ctrls if isinstance(ctrls, list) else [ctrls]

    for name, mm in (("暗点占比%", m1), ("浊度度量 gap-中心", m2)):
        refs = [mm[p] for p in pcs if p in mm]
        ref = float(np.median(refs)) if refs else 1.0
        print(f"\n== {name} ==  C+ 分数={[round(mm[p], 1) for p in pcs if p in mm]}  ref={round(ref, 1)}")
        for L in ROWS:
            print(f"  {L} " + " ".join(f"{mm[f'{L}{j}']:6.0f}" for j in range(1, 13)))

    print("\n--- 完整管线判读 ---")
    an2 = PlateAnalyzer(t)
    res = an2.run(path, outdir=os.path.join(HERE, "outputs"))
    print("网格:", res["sample"].get("grid_source"))
    print("对照孔:", {k: v["ok"] for k, v in res["controls"].items()})
    for m in res["mic"]:
        flag = ("  ⚠ " + "；".join(m["flags"])) if m["flags"] else ""
        print(f"  {m['code']:5s} MIC={m['mic']:>6s} [{m.get('mic_well')}] 置信度={m['confidence']}{flag}")
    for w in res["warnings"]:
        print("  ⚠", w)


if __name__ == "__main__":
    main()
