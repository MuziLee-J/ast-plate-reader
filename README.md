# 药敏板智能判读系统

上传药敏板照片或仪器结果图 → 自动定位孔位、判读生长、推导 MIC → 网页逐孔复核 → 导出 Excel。

适用于 96 孔微量肉汤显色板、真菌微量肉汤干板、150 孔显色板仪器结果图、链球菌微量肉汤板等多种板型，具体板面由模板配置决定。

## 功能

- **自动配准**：板检测 → 透视校正 → 孔位网格自校准（不要求板边距严格符合标准），带清晰度检查。
- **判读**：以对照孔为基准做颜色/浊度判读（显色板五色模型、干板 50% 生长、链球菌沉淀度量），输出置信度与「跳孔」告警。
- **复核**：网页点击照片或板面切换孔位状态、下拉修正 MIC；**软件判读值不可变，人工值与重算值分别标注**。
- **导出**：Excel（MIC 报告 / MIC 登记表 / 复核明细 / 逐孔明细）与复核数据 CSV。

## 板型模板

板面内容（药物、浓度梯度、对照孔、判读规则）属于内部资料，**不随本仓库分发**。

- 运行时把模板 JSON 放在程序目录的 `templates/` 下，或在网页里「上传模板」导入；
- 模板格式见程序内「下载模板 JSON」（随模板一起分发，仓库不含任何板面）；
- 质控范围由实验室在程序目录放置 `qc_rules.json` 提供，仓库不含药物与浓度信息。

## 运行

```bash
uv run --with fastapi --with uvicorn --with opencv-python --with numpy --with openpyxl --with pillow --with python-multipart python app.py
```

浏览器打开 <http://127.0.0.1:8765>（手机同网段可拍照直传）。
环境变量：`AST_PORT` 换端口，`AST_HOST=0.0.0.0` 开放内网。

## 打包 Windows exe

```bash
uv run --with pyinstaller --with fastapi --with uvicorn --with opencv-python --with numpy --with openpyxl --with pillow --with python-multipart python -m PyInstaller --noconfirm --clean AST-Plate-Reader.spec
```

产物 `dist/AST-Plate-Reader.exe`，单文件、免安装 Python。数据目录为 exe 同级的 `outputs/`、`templates/`。

## 主要文件

| 文件 | 作用 |
|---|---|
| `app.py` | Web 服务与单页前端 |
| `ast_reader.py` | 判读管线（配准、取色、判读、MIC、导出） |
| `chart_reader.py` | 仪器结果图（150 孔方格阵列）识别 |
| `judge_plates.py` | 命令行批量判读（需自备模板与照片） |
| `evaluate_training.py` / `fit_color_model.py` | 复核数据评估 / 颜色阈值标定 |
| `verify.py` / `generate_samples.py` | 合成样本与准确率自检（需自备模板；脚本本身不含任何板面） |

## 判读方法依据

- 药敏试验方法：WS/T 421（酵母）、WS/T 411（丝状真菌）、CLSI M27 / M38；
- 判读规则与质控范围：试剂盒说明书，CLSI M27M44S；
- 本仓库不含任何标准文件原文，折点/质控请以实验室现行受控版本为准。

## 免责声明

本软件为辅助判读工具，结果必须经人工复核后方可使用，不替代临床微生物检验人员的判断。
