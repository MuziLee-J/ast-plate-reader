# -*- coding: utf-8 -*-
"""命令行批量判读：指定模板 + 多张照片。

用法（在 ast-demo 目录）:
    uv run --with opencv-python --with numpy --with openpyxl --with pillow python judge_plates.py <模板.json> <照片1> [照片2 ...]
"""
import os
import sys

from ast_reader import PlateAnalyzer, load_template

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    tpl_path = sys.argv[1]
    paths = sys.argv[2:]
    tpl = load_template(tpl_path)
    for p in paths:
        print("=" * 24, os.path.basename(p))
        try:
            an = PlateAnalyzer(tpl)
            res = an.run(p, outdir=os.path.join(HERE, "outputs"))
            print("  孔位校准:", res["sample"].get("grid_source"))
            ctrl_txt = "；".join(f"{k} {v['role']}{'正常' if v['ok'] else '异常'}"
                                for k, v in res["controls"].items())
            print("  对照孔:", ctrl_txt)
            for m in res["mic"]:
                flag = ("  ⚠ " + "；".join(m["flags"])) if m["flags"] else ""
                note = f"  ({m['note']})" if m.get("note") else ""
                well = f" [{m['mic_well']}]" if m.get("mic_well") else ""
                print(f"  {m['code']:4s} MIC={m['mic']:>6s}{well} 置信度={m['confidence']}{note}{flag}")
            for w in res["warnings"]:
                print("  ⚠", w)
        except Exception as e:
            print("  判读失败:", type(e).__name__, e)


if __name__ == "__main__":
    main()
