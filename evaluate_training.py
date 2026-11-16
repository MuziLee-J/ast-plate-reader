# -*- coding: utf-8 -*-
"""训练评估：用「复核数据（训练用 CSV）」评估软件判读与人工复核的一致率。

用法（在 ast-demo 目录）:
    uv run --with opencv-python --with numpy python evaluate_training.py \
        <模板.json> <复核数据1.csv> [<复核数据2.csv> ...] \
        [<另一个模板.json> <复核数据3.csv> ...]

输出:
    - 控制台报告：孔级决断一致率、MIC 完全一致率、EA（≤1 个稀释度）一致率、无效结果、主要差异
    - Markdown 报告：outputs/训练评估报告.md

指标说明:
    - 孔级一致率：软件判读 vs 复核判读（仅统计双方都明确为 growth/no_growth 的孔）
    - MIC 完全一致率：软件与复核推导出的 MIC 完全相同
    - EA（essential agreement）：MIC 差值 ≤ 1 个倍比稀释度（行业常用可接受标准）
"""
import csv
import datetime
import os
import re
import sys
import collections

from ast_reader import PlateAnalyzer, load_template

HERE = os.path.dirname(os.path.abspath(__file__))


def _to_val(mic_str):
    """MIC 字符串 → 数值（用于计算稀释度差）；无效/空返回 None。"""
    if mic_str in (None, "", "无效", "None"):
        return None
    m = re.findall(r"\d+\.?\d*", str(mic_str))
    if not m:
        return None
    return float(m[0])


def _step(a, b):
    va, vb = _to_val(a), _to_val(b)
    if va is None or vb is None:
        return None
    if va == vb:
        return 0
    hi, lo = max(va, vb), min(va, vb)
    return 1 if abs(hi / lo - 2.0) < 1e-6 else 2


def _mic_of(an, rows, photo, key):
    wells = {}
    for r in rows:
        if r["照片"] != photo:
            continue
        pos = r["孔位"]
        role, code, _n, conc = an._well_meta(pos)
        rec = {"pos": pos, "role": role, "status": r[key]}
        if role == "drug":
            rec["drug"] = code
            rec["conc"] = conc
        wells[pos] = rec
    an.genus = "candida"
    mic_list, _c, _e = an._derive_mic_tracks(wells)
    return {m["code"]: m["mic"] for m in mic_list}


def evaluate(tpl_path, csv_path):
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    photos = []
    for r in rows:
        if r["照片"] not in photos:
            photos.append(r["照片"])
    an = PlateAnalyzer(load_template(tpl_path))
    well_total = well_agree = 0
    mic_total = mic_exact = mic_ea = mic_invalid = 0
    diffs = collections.Counter()
    for ph in photos:
        for r in rows:
            if r["照片"] != ph:
                continue
            if r["软件判读"] in ("growth", "no_growth") and r["复核判读"] in ("growth", "no_growth"):
                well_total += 1
                well_agree += (r["软件判读"] == r["复核判读"])
        ms = _mic_of(an, rows, ph, "软件判读")
        mr = _mic_of(an, rows, ph, "复核判读")
        for code in mr:
            mic_total += 1
            if ms.get(code) == mr[code]:
                mic_exact += 1
                mic_ea += 1
                continue
            d = _step(ms.get(code, ""), mr[code])
            if d is None:
                mic_invalid += 1
            elif d == 1:
                mic_ea += 1
            diffs[(code, ms.get(code, ""), mr[code])] += 1
    return {
        "name": os.path.basename(csv_path),
        "template": os.path.basename(tpl_path),
        "photos": len(photos),
        "well_total": well_total, "well_agree": well_agree,
        "mic_total": mic_total, "mic_exact": mic_exact,
        "mic_ea": mic_ea, "mic_invalid": mic_invalid,
        "diffs": diffs,
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pairs = []
    cur_tpl = None
    for a in args:
        if a.lower().endswith(".json"):
            cur_tpl = a
        elif a.lower().endswith(".csv") and cur_tpl:
            pairs.append((cur_tpl, a))
    if not pairs:
        print(__doc__)
        return

    results = [evaluate(t, c) for t, c in pairs]
    lines = []
    lines.append("# 判读训练评估报告\n")
    lines.append(f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    lines.append("| 数据集 | 模板 | 照片 | 孔级一致率 | MIC 完全一致 | EA(≤1稀释) | 含无效 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in results:
        wt = r["well_total"] or 1
        mt = r["mic_total"] or 1
        lines.append(
            f"| {r['name']} | {r['template']} | {r['photos']} | "
            f"{r['well_agree']}/{r['well_total']} ({r['well_agree']/wt*100:.1f}%) | "
            f"{r['mic_exact']}/{r['mic_total']} ({r['mic_exact']/mt*100:.1f}%) | "
            f"{r['mic_ea']}/{r['mic_total']} ({r['mic_ea']/mt*100:.1f}%) | {r['mic_invalid']} |")
    lines.append("")
    lines.append("## 主要 MIC 差异（软件 → 复核）\n")
    for r in results:
        if not r["diffs"]:
            continue
        lines.append(f"**{r['name']}**")
        for (code, a, b), n in r["diffs"].most_common(8):
            lines.append(f"- {code}: {a} → {b}（{n} 次）")
        lines.append("")
    report = "\n".join(lines)
    out_dir = os.path.join(HERE, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "训练评估报告.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(report)
    print(f"\n报告已保存: {out}")


if __name__ == "__main__":
    main()
