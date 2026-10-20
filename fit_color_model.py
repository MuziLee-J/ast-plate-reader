# -*- coding: utf-8 -*-
"""用人工复核数据拟合五色判读阈值（颜色模型标定）。

输入：网页上「导出复核数据（训练用 CSV）」得到的 CSV（含每孔 色相/饱和度/明度 + 复核判读）。
做的是网格搜索：找出让「五色规则 → 生长/无生长」与人工复核结论最一致的阈值。

用法（在 ast-demo 目录）:
    uv run python fit_color_model.py 复核数据_训练用.csv [模板.json]

输出:
    - 控制台：当前默认阈值 vs 学习后阈值的符合率
    - learned_color_thresholds.json：可粘贴到模板 detection.color_thresholds（或做进模板后上传）
"""
import csv
import json
import sys

DEFAULT_TH = {"white_max_s": 30, "pink_min_s": 65,
              "purple_h": [128, 150], "blue_h": [95, 128]}
STATUS = {"blue": "no_growth", "purple": "partial", "pink": "growth",
          "pink_white": "growth", "white": "growth"}


def classify(h, s, th):
    if s <= th["white_max_s"]:
        return "white"
    if h >= 150 or h <= 15:
        return "pink" if s > th["pink_min_s"] else "pink_white"
    if th["purple_h"][0] < h <= th["purple_h"][1]:
        return "purple"
    if th["blue_h"][0] <= h <= th["blue_h"][1]:
        return "blue"
    return "purple" if s > th["pink_min_s"] else "pink_white"


def predict(color, drug, purple_growth):
    st = STATUS[color]
    if st == "partial":
        return "growth" if drug in purple_growth else "no_growth"
    return st


def score(rows, th, purple_growth):
    hit = n = 0
    for r in rows:
        st = predict(classify(r["h"], r["s"], th), r["drug"], purple_growth)
        n += 1
        hit += (st == r["label"])
    return (hit / n if n else 0.0), hit, n


def load_rows(path):
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                h = float(r["色相H"])
                s = float(r["饱和度S"])
            except (KeyError, ValueError):
                continue
            label = (r.get("复核判读") or r.get("软件判读") or "").strip()
            if label not in ("growth", "no_growth"):
                continue
            rows.append({"h": h, "s": s, "drug": (r.get("药物") or "").strip(),
                         "label": label})
    return rows


def main():
    if len(sys.argv) < 2:
        print("用法: uv run python fit_color_model.py <复核数据.csv> [模板.json]")
        return
    rows = load_rows(sys.argv[1])
    if len(rows) < 20:
        print(f"有效复核孔太少（{len(rows)} 个），至少需要 ~20 个生长/无生长标注孔再拟合。")
        return
    purple_growth = {"AMB"}
    if len(sys.argv) > 2:
        try:
            tpl = json.load(open(sys.argv[2], encoding="utf-8"))
            det = tpl.get("detection") or {}
            pg = det.get("purple_as_growth_for")
            if pg:
                purple_growth = set(pg)
        except Exception:
            pass

    base_acc, base_hit, n = score(rows, DEFAULT_TH, purple_growth)
    print(f"样本：{n} 孔（生长/无生长标注）")
    print(f"当前默认阈值符合率: {base_acc * 100:.1f}%（{base_hit}/{n}）")

    best = None
    # 每个参数的默认值排在最前：得分相同时优先保持当前默认阈值（更保守）
    for white_s in (30, 25, 35, 20, 40, 45):
        for pink_s in (65, 60, 70, 50, 80, 90):
            for purple_lo in (128, 130, 125, 135, 120, 140):
                for blue_lo in (95, 100, 90, 105, 85):
                    th = {"white_max_s": white_s, "pink_min_s": pink_s,
                          "purple_h": [purple_lo, 150], "blue_h": [blue_lo, purple_lo]}
                    acc, hit, _ = score(rows, th, purple_growth)
                    if best is None or acc > best[0]:
                        best = (acc, hit, th)

    acc, hit, th = best
    print(f"学习后阈值符合率:     {acc * 100:.1f}%（{hit}/{n}）")
    print(f"建议阈值: {json.dumps(th, ensure_ascii=False)}")
    out = {"detection": {"color_thresholds": th}}
    with open("learned_color_thresholds.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("已写入 learned_color_thresholds.json —— 把 detection.color_thresholds 粘进模板后，")
    print("用页面「上传模板 JSON」即可生效（原模板可先「下载模板 JSON」获取）。")


if __name__ == "__main__":
    main()
