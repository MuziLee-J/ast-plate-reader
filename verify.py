# -*- coding: utf-8 -*-
"""批量跑判读并与真实值比对，输出准确率。

用法:
    uv run --with opencv-python --with numpy --with openpyxl --with pillow python verify.py
"""
import glob
import json
import os

from ast_reader import PlateAnalyzer, load_template, read_image

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    template = load_template(os.path.join(HERE, "template.json"))
    outdir = os.path.join(HERE, "outputs")
    os.makedirs(outdir, exist_ok=True)

    total_ok = total_n = mic_ok = mic_n = 0
    for truth_path in sorted(glob.glob(os.path.join(HERE, "samples", "*_truth.json"))):
        stem = os.path.basename(truth_path).replace("_truth.json", "")
        img_path = os.path.join(HERE, "samples", f"{stem}.jpg")
        with open(truth_path, encoding="utf-8") as f:
            truth = json.load(f)

        analyzer = PlateAnalyzer(template)
        result = analyzer.run(img_path, outdir=outdir, sample_no=stem)

        # 逐孔比对（药物孔 + 对照孔）
        n_ok = n_total = 0
        errors = []
        for w in result["wells"]:
            if w.get("status") in ("growth", "no_growth") and w["pos"] in truth["wells"]:
                n_total += 1
                t_growth = truth["wells"][w["pos"]] == "growth"
                if w["growth"] == t_growth:
                    n_ok += 1
                else:
                    errors.append((w["pos"], w["growth"], t_growth))
        m_ok = sum(1 for m in result["mic"]
                   if truth["mic"][str(m["col"])]["display"] == m["mic"])
        total_ok += n_ok
        total_n += n_total
        mic_ok += m_ok
        mic_n += len(result["mic"])

        print(f"{stem}: 孔级 {n_ok}/{n_total} ({100 * n_ok / max(n_total, 1):.1f}%)，"
              f"MIC {m_ok}/{len(result['mic'])}，警告 {len(result['warnings'])} 条")
        for e in errors:
            print(f"    孔位不一致 {e}")
        for m in result["mic"]:
            if m["flags"]:
                print(f"    {m['code']} MIC={m['mic']} 标记: {'；'.join(m['flags'])}")
        for wmsg in result["warnings"]:
            print(f"    警告: {wmsg}")

        # 标注图冒烟检查：MIC 高亮环为纯绿色，验证标注与孔位对齐
        ann = read_image(result["annotated"])
        g, r, b = ann[:, :, 1].astype(int), ann[:, :, 0].astype(int), ann[:, :, 2].astype(int)
        green_px = int(((g > 120) & (g > r * 1.2) & (g > b * 1.2)).sum())
        print(f"    标注图 MIC 绿色高亮像素: {green_px}（>50 视为标注对齐正常）")

    print(f"\n总体: 孔级 {total_ok}/{total_n} ({100 * total_ok / max(total_n, 1):.1f}%)，"
          f"MIC {mic_ok}/{mic_n}")


if __name__ == "__main__":
    main()
