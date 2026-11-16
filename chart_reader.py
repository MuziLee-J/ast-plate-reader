# -*- coding: utf-8 -*-
"""结果图判读：直接读取仪器导出的显色结果图（白底 + 彩色方格阵列，如 150 真菌板 15×10）。

用法（网页端选「150 真菌显色板（结果图）」模板后上传结果图即可）：
    analyze_chart(image_path, template, outdir=..., sample_no=..., genus=...)
"""
import datetime
import os

import cv2
import numpy as np

from ast_reader import PlateAnalyzer, read_image, write_image


def _bands(proj, ratio=0.2, min_size=15):
    """在一维投影上找连续的高密度带（行带/列带）。"""
    thr = max(10.0, ratio * float(proj.max() if proj.size else 0))
    out, start = [], None
    for i, val in enumerate(proj):
        if val > thr and start is None:
            start = i
        elif val <= thr and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(proj) - 1))
    return [(a, b) for a, b in out if b - a >= min_size]


def analyze_chart(img_path, template, outdir=None, sample_no="", genus=None):
    """判读仪器结果图。返回结构与照片判读一致（sample/controls/mic/wells/warnings/annotated）。"""
    outdir = outdir or os.path.dirname(img_path)
    os.makedirs(outdir, exist_ok=True)
    img = read_image(img_path)
    H, W = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # 彩色方格饱和度很高；文字/白底饱和度低 → 二值掩膜后投影找格子
    mask = (hsv[:, :, 1] > 60).astype(np.uint8) * 255
    rows_bands = _bands(mask.sum(axis=1) / 255.0)
    cols_bands = _bands(mask.sum(axis=0) / 255.0)

    grid = template.get("grid") or {}
    n_rows = int(grid.get("rows", 15))
    n_cols = int(grid.get("cols", 10))
    row_labels = grid.get("row_labels") or [str(i + 1) for i in range(n_rows)]
    col_labels = grid.get("col_labels") or list("JIHGFEDCBA")[:n_cols]
    if len(rows_bands) != n_rows or len(cols_bands) != n_cols:
        raise ValueError(
            f"结果图网格识别失败（找到 {len(rows_bands)} 行 × {len(cols_bands)} 列，"
            f"应为 {n_rows} × {n_cols}）：请使用仪器导出的原始结果图（白色背景、彩色方格阵列、无遮挡）")

    an = PlateAnalyzer(template)
    an.genus = genus
    color_conf = {"blue": 0.8, "purple": 0.5, "pink": 0.8,
                  "pink_white": 0.7, "white": 0.68}

    wells = {}
    for ri, (y0, y1) in enumerate(rows_bands):
        for ci, (x0, x1) in enumerate(cols_bands):
            pos = f"{col_labels[ci]}{row_labels[ri]}"
            role, code, _name, conc = an._well_meta(pos)
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
            rec = {"pos": pos, "role": role,
                   "xy": [round((x0 + x1) / 2.0, 1), round((y0 + y1) / 2.0, 1)]}
            if role == "drug":
                rec["drug"] = code
                rec["conc"] = conc
            if role == "unused":
                rec.update(status="unused", growth=None, score=None, confidence=None)
                wells[pos] = rec
                continue
            yh, xh = max(2, (y1 - y0) // 4), max(2, (x1 - x0) // 4)
            patch = hsv[cy - yh:cy + yh + 1, cx - xh:cx + xh + 1]
            h, s, v = (float(x) for x in np.median(patch.reshape(-1, 3), axis=0))
            color = an._color_class(h, s)
            st = an._color_status(color)
            rec.update(status=st, growth=(st == "growth"), score=None,
                       color=color, color_label=an._color_label(color),
                       hsv=[round(h, 1), round(s, 1), round(v, 1)],
                       confidence=round(float(color_conf.get(color, 0.5)), 2))
            wells[pos] = rec

    mic_list, controls, extra = an._derive_mic_tracks(wells)
    warnings = list(extra)

    # 标注图：空心圈（蓝=无生长、粉=生长、紫=临界，绿圈=MIC）
    vis = img.copy()
    for pos, w_ in wells.items():
        cx, cy = [int(round(v)) for v in w_["xy"]]
        r = int(round(min(min(y1 - y0 for y0, y1 in rows_bands),
                          min(x1 - x0 for x0, x1 in cols_bands)) * 0.42))
        st = w_.get("status")
        col = {"growth": (255, 60, 110), "no_growth": (230, 100, 40),
               "partial": (200, 90, 150)}.get(st)
        if col:
            cv2.circle(vis, (cx, cy), r, col, 2)
        if w_.get("role") in ("positive_control", "negative_control"):
            lab = "C+" if w_["role"] == "positive_control" else "C-"
            cv2.putText(vis, lab, (cx - r, cy - r - 4), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (0, 170, 255), 1, cv2.LINE_AA)
    for m in mic_list:
        wp = m.get("mic_well")
        if wp and wp in wells:
            cx, cy = [int(round(v)) for v in wells[wp]["xy"]]
            r = int(round(min(min(y1 - y0 for y0, y1 in rows_bands),
                              min(x1 - x0 for x0, x1 in cols_bands)) * 0.46))
            cv2.circle(vis, (cx, cy), r, (120, 160, 40), 2)
            cv2.putText(vis, f"{m['code']}={m['mic']}", (cx + r, cy - r),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (120, 160, 40), 1, cv2.LINE_AA)

    stem = os.path.splitext(os.path.basename(img_path))[0]
    ann_path = os.path.join(outdir, f"{stem}_chart_annotated.png")
    write_image(ann_path, vis)

    cell_w = float(np.median([x1 - x0 for x0, x1 in cols_bands]))
    cell_h = float(np.median([y1 - y0 for y0, y1 in rows_bands]))
    well_list = sorted(wells.values(),
                       key=lambda r: (int("".join(ch for ch in r["pos"] if ch.isdigit()) or 0),
                                      r["pos"][0]))
    return {
        "sample": {"file": os.path.basename(img_path), "sample_no": sample_no,
                   "template": template.get("name", ""),
                   "date": datetime.date.today().isoformat(),
                   "plate_detected": True, "grid_source": "chart",
                   "size": [W, H],
                   "well_r_orig": round(min(cell_w, cell_h) * 0.42, 1)},
        "warnings": warnings,
        "controls": controls,
        "mic": mic_list,
        "wells": well_list,
        "annotated": ann_path,
        "warped": ann_path,
        "layout": {"row_labels": row_labels, "col_labels": col_labels,
                   "pos_order": grid.get("pos_order", "colfirst")},
    }
