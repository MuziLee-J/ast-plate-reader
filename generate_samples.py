# -*- coding: utf-8 -*-
"""生成模拟的 96 孔药敏板照片（含真实值 ground truth），用于演示和验证判读管线。

用法:
    uv run --with opencv-python --with numpy python generate_samples.py

输出:
    samples/sample_01.jpg ~ sample_03.jpg  模拟照片（含透视、光照、噪声、JPEG 压缩）
    samples/sample_XX_truth.json           逐孔真实值（金标准）
"""
import json
import os

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "samples")

# 96 孔板几何（SBS 标准，毫米）
PLATE_W_MM, PLATE_H_MM = 127.76, 85.48
PITCH_MM = 9.0
ORIGIN_MM = (14.38, 11.24)   # 第一个孔中心 (A1) 距板左上角
WELL_D_MM = 6.9

ROWS, COLS = 8, 12
ROW_LETTERS = "ABCDEFGH"
POS_CONTROL, NEG_CONTROL = "H12", "H11"   # 阳性对照 / 阴性对照
UNUSED_COLS = [11, 12]


def load_template_concs():
    """从 template.json 读取各药物列的浓度（A→H 从高到低），保证与判读管线一致。"""
    with open(os.path.join(HERE, "template.json"), encoding="utf-8") as f:
        t = json.load(f)
    return {int(k): v["conc"] for k, v in t["drugs"].items()}


def hsv_to_bgr(h, s, v):
    px = np.uint8([[[h, s, v]]])
    return tuple(int(c) for c in cv2.cvtColor(px, cv2.COLOR_HSV2BGR)[0, 0])


def well_color(status, rng):
    """按孔状态返回 BGR 颜色（带随机浮动）。"""
    if status == "growth":        # 粉红：刃天青被细菌还原
        h, s, v = rng.uniform(165, 176), rng.uniform(150, 200), rng.uniform(215, 245)
    elif status == "no_growth":   # 蓝：刃天青未被还原
        h, s, v = rng.uniform(102, 117), rng.uniform(150, 200), rng.uniform(215, 245)
    else:                         # 未使用孔：近白
        h, s, v = rng.uniform(100, 115), rng.uniform(5, 25), rng.uniform(240, 252)
    return hsv_to_bgr(h, s, v)


def make_truth(rng, concs, skip_col=None, overrides=None):
    """生成真实值：每个药物列沿 A→H 先生长后不生长，MIC = 第一个无生长孔的浓度。"""
    overrides = overrides or {}
    wells, mic = {}, {}
    for col in sorted(concs):
        if col == skip_col:
            growth_rows = [0, 1, 2, 4, 5]          # 跳跃孔异常：D 无生长、E/F 又有生长
        elif col in overrides:
            growth_rows = list(overrides[col])
        else:
            growth_rows = list(range(int(rng.integers(0, 9))))   # 0=全无生长, 8=全生长
        for i in range(ROWS):
            wells[f"{ROW_LETTERS[i]}{col}"] = "growth" if i in growth_rows else "no_growth"
        first = next((i for i in range(ROWS) if i not in growth_rows), None)
        c = concs[col]
        if first is None:
            mic[str(col)] = {"display": f">{c[0]}", "row": None}
        elif first == 0:
            mic[str(col)] = {"display": f"≤{c[-1]}", "row": 0}
        else:
            mic[str(col)] = {"display": str(c[first]), "row": first}
    wells[POS_CONTROL] = "growth"
    wells[NEG_CONTROL] = "no_growth"
    for col in UNUSED_COLS:
        for i in range(ROWS):
            pos = f"{ROW_LETTERS[i]}{col}"
            if pos not in wells:
                wells[pos] = "unused"
    return {"wells": wells, "mic": mic,
            "controls": {POS_CONTROL: "growth", NEG_CONTROL: "no_growth"}}


def build_plate(truth, rng):
    """渲染一张正俯视的板面图（白色板身 + 96 个孔）。"""
    W, H = 1500, 1004
    img = np.full((H, W, 3), 236, np.uint8)
    img = np.clip(img.astype(np.float32) + rng.normal(0, 2.5, (H, W, 3)), 0, 255).astype(np.uint8)
    sx, sy = W / PLATE_W_MM, H / PLATE_H_MM
    well_r = int(WELL_D_MM / 2 * sx)
    for i in range(ROWS):
        for j in range(COLS):
            pos = f"{ROW_LETTERS[i]}{j + 1}"
            status = truth["wells"].get(pos, "unused")
            cx = int((ORIGIN_MM[0] + j * PITCH_MM) * sx)
            cy = int((ORIGIN_MM[1] + i * PITCH_MM) * sy)
            color = well_color(status, rng)
            cv2.circle(img, (cx, cy), well_r, color, -1)                       # 孔内液体
            light = tuple(min(255, int(c * 0.85) + 45) for c in color)         # 中心略亮
            cv2.circle(img, (cx, cy), int(well_r * 0.6), light, -1)
            cv2.circle(img, (cx, cy), well_r, (70, 70, 70), 2)                 # 孔壁阴影
    return img


def compose_scene(plate, rng, lighting):
    """把板子透视投影到深色桌面上，叠加大光照渐变与色温。"""
    bg_h, bg_w = 1600, 2400
    bg = np.full((bg_h, bg_w, 3), 55, np.uint8)
    bg = np.clip(bg.astype(np.float32) + rng.normal(0, 6, (bg_h, bg_w, 3)), 0, 255).astype(np.uint8)

    ph, pw = plate.shape[:2]
    src = np.float32([[0, 0], [pw - 1, 0], [pw - 1, ph - 1], [0, ph - 1]])
    s = rng.uniform(0.55, 0.70)                                   # 缩放（板子占画面主体）
    ang = np.deg2rad(rng.uniform(-14, 14))                        # 旋转
    ctr = np.float32([rng.uniform(0.42, 0.58) * bg_w, rng.uniform(0.42, 0.58) * bg_h])
    rot = np.float32([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
    pts = (src - np.float32([pw / 2, ph / 2])) * s
    dst = (pts @ rot.T + ctr) + rng.uniform(-35, 35, (4, 2)).astype(np.float32)  # 透视扰动
    dst = np.clip(dst, [10, 10], [bg_w - 11, bg_h - 11]).astype(np.float32)

    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(plate, M, (bg_w, bg_h))
    mask = cv2.warpPerspective(np.full((ph, pw), 255, np.uint8), M, (bg_w, bg_h))
    bg[mask > 0] = warped[mask > 0]

    x = np.linspace(0, 1, bg_w, dtype=np.float32)[None, :, None]
    grad = 0.78 + 0.32 * x                                         # 横向光照渐变
    temp = np.float32([lighting["r"], 1.0, lighting["b"]]).reshape(1, 1, 3)   # 色温
    bg = np.clip(bg.astype(np.float32) * grad * temp, 0, 255).astype(np.uint8)
    if lighting.get("blur", 0) > 0:
        bg = cv2.GaussianBlur(bg, (5, 5), lighting["blur"])
    return bg


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    concs = load_template_concs()
    scenarios = [
        # (名称, 随机种子, 光照, 跳跃孔列, 覆盖的生长行)
        ("sample_01", 1, {"r": 1.0,  "b": 1.0,  "blur": 0.6}, None, {1: list(range(8))}),  # 常规；AK 全生长 → >64
        ("sample_02", 2, {"r": 1.10, "b": 0.88, "blur": 1.4}, None, {3: []}),              # 暖光略糊；CIP 全无生长 → ≤0.06
        ("sample_03", 3, {"r": 0.92, "b": 1.08, "blur": 0.8}, 4, None),                    # 冷光；IPM 跳跃孔异常
    ]
    for name, seed, lighting, skip_col, overrides in scenarios:
        rng = np.random.default_rng(seed)
        truth = make_truth(rng, concs, skip_col=skip_col, overrides=overrides)
        scene = compose_scene(build_plate(truth, rng), rng, lighting)
        img_path = os.path.join(OUT_DIR, f"{name}.jpg")
        cv2.imwrite(img_path, scene, [cv2.IMWRITE_JPEG_QUALITY, 88])
        with open(os.path.join(OUT_DIR, f"{name}_truth.json"), "w", encoding="utf-8") as f:
            json.dump(truth, f, ensure_ascii=False, indent=2)
        print(f"已生成 {img_path}（真实值: {name}_truth.json）")


if __name__ == "__main__":
    main()
