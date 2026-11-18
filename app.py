# -*- coding: utf-8 -*-
"""药敏板判读 Web Demo（支持单张 / 同一菌株多张批量）。

启动:
    uv run --with fastapi --with uvicorn --with opencv-python --with numpy --with openpyxl --with pillow --with python-multipart python app.py

然后浏览器打开 http://127.0.0.1:8765
"""
import base64
import datetime
import glob
import json
import os
import sys
import uuid

import cv2
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

from ast_reader import (PlateAnalyzer, export_excel, export_excel_batch,
                        export_record_sheet, load_template, merge_mic,
                        qc_in_range, read_image)
from chart_reader import analyze_chart

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = bool(getattr(sys, "frozen", False))          # PyInstaller 打包运行
RES_DIR = getattr(sys, "_MEIPASS", HERE)              # 资源目录（模板/质控随包）
DATA_DIR = os.path.dirname(sys.executable) if FROZEN else HERE  # 数据目录（原图/导出/用户模板）
_demo_path = os.path.join(RES_DIR, "template.json")
if os.path.isfile(_demo_path):
    TEMPLATES = {"demo": load_template(_demo_path)}
else:   # 仓库不含板面内容：无演示模板时用空模板占位，避免启动失败
    TEMPLATES = {"demo": {"name": "演示板", "panel_type": "microdilution_96",
                          "grid": {"rows": 8, "cols": 12},
                          "detection": {"mode": "color",
                                        "positive_control": "H12",
                                        "negative_control": "H11"},
                          "drugs": {}, "units": "µg/mL"}}
for _p in sorted(glob.glob(os.path.join(RES_DIR, "templates", "*.json"))):
    TEMPLATES[os.path.splitext(os.path.basename(_p))[0]] = load_template(_p)
TPL_SAVE_DIR = os.path.join(DATA_DIR, "templates") if FROZEN else os.path.join(HERE, "templates")
os.makedirs(TPL_SAVE_DIR, exist_ok=True)
if FROZEN:   # 打包后：允许在 exe 同目录放自定义模板覆盖/追加
    for _p in sorted(glob.glob(os.path.join(TPL_SAVE_DIR, "*.json"))):
        TEMPLATES[os.path.splitext(os.path.basename(_p))[0]] = load_template(_p)
SAMPLES_DIR = os.path.join(RES_DIR, "samples")
OUT_DIR = os.path.join(DATA_DIR, "outputs")
os.makedirs(OUT_DIR, exist_ok=True)


def get_template(tid):
    return TEMPLATES.get(tid) or TEMPLATES["demo"]


GENUS_NAMES = {"candida": "念珠菌属", "aspergillus": "曲霉属", "mould": "丝状真菌"}


def build_meta(sample_no, culture_info, genus, species, batch_no, operator, strain2=""):
    return {"sample_no": (sample_no or "").strip(),
            "culture_info": (culture_info or "").strip(),
            "genus_label": GENUS_NAMES.get(genus, genus or ""),
            "species": (species or "").strip(),
            "batch_no": (batch_no or "").strip(),
            "operator": (operator or "").strip(),
            "strain2": (strain2 or "").strip()}


QC_RULES = {}
_qc_path = os.path.join(RES_DIR, "qc_rules.json")
if os.path.isfile(_qc_path):
    with open(_qc_path, "r", encoding="utf-8") as _f:
        QC_RULES = json.load(_f)


def match_qc(sample_no):
    for key, rset in (QC_RULES.get("rule_sets") or {}).items():
        if key and key in (sample_no or ""):
            return rset
    return None


def enrich_qc(plates, sample_no):
    """按样本编号匹配质控范围，给每个药物结果标注 在控/超出。"""
    rset = match_qc(sample_no)
    if not rset:
        return None
    for p in plates:
        tp = p["sample"].get("time_point") or ""
        ranges = (rset.get("ranges") or {}).get(tp)
        if not ranges:
            continue
        for m in p["mic"]:
            rng = ranges.get(m["code"])
            if rng:
                m["qc_range"] = rng
                m["qc_ok"] = qc_in_range(m["mic"], rng)
    return rset

app = FastAPI(title="药敏板判读 Demo")
STORE = {}   # record_id -> xlsx bytes（演示用内存存储）


def preview_b64(path, max_w=900):
    """供批量结果页展示的缩略标注图。"""
    img = read_image(path)
    h, w = img.shape[:2]
    if w > max_w:
        img = cv2.resize(img, (max_w, int(round(h * max_w / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(buf.tobytes()).decode()


HTML_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>药敏板智能判读 Demo</title>
<style>
  body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f2f4f8;margin:0;padding:16px;color:#222}
  .card{max-width:1400px;margin:0 auto;background:#fff;border-radius:12px;padding:20px;box-shadow:0 2px 10px rgba(0,0,0,.06);margin-bottom:16px}
  h1{font-size:20px;margin:0 0 4px}
  h2{font-size:16px;margin:0 0 8px}
  h3{font-size:14px;margin:14px 0 2px}
  .sub{color:#888;font-size:13px;margin-bottom:12px}
  .btn{display:inline-block;padding:10px 16px;border:none;border-radius:8px;background:#2f6fed;color:#fff;font-size:15px;cursor:pointer;margin:4px 6px 4px 0}
  .btn.ghost{background:#eef2fb;color:#2f6fed}
  .btn.green{background:#2e8b57}
  input[type=text]{padding:10px;border:1px solid #ddd;border-radius:8px;font-size:15px;width:100%;box-sizing:border-box}
  .warn{background:#fff4e5;border-left:4px solid #ff9800;padding:8px 12px;border-radius:6px;margin:6px 0;font-size:14px}
  table{border-collapse:collapse;width:100%;font-size:14px;margin-top:8px}
  th,td{border:1px solid #e3e6ec;padding:6px 8px;text-align:center}
  th{background:#eef2fb}
  .gridwrap{overflow-x:auto}
  table.grid td{min-width:46px;height:42px;padding:2px 2px;font-size:9.5px;line-height:1.2;vertical-align:middle}
  .g{background:#f6b8c6}.n{background:#b8cdf6}.p{background:#d9c2f0}.u{background:#f1f2f4}.k{background:#fff}
  .mic{outline:3px solid #2e8b57;outline-offset:-2px}
  .ctrl{outline:2px dashed #ff9800;outline-offset:-2px}
  .revsel.changed{border-color:#c07000;background:#fff6e0;font-weight:600}
  .recomp{color:#b26a00;font-size:12px;white-space:nowrap}
  .mech{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:8px;background:#eef3fb;color:#3b6ab5;font-size:11px;white-space:nowrap}
  #boardImg{width:100%;border-radius:8px;margin-top:8px}
  .hint{color:#888;font-size:12px}
  .plate{border:1px solid #e3e6ec;border-radius:8px;padding:10px;margin:8px 0}
  .plate summary{cursor:pointer;font-weight:600;font-size:14px}
  tr.conflict td{color:#c0392b}
  .badge{display:inline-block;background:#eef2fb;color:#2f6fed;border-radius:6px;padding:1px 8px;font-size:12px;margin-left:6px}
  .board-idle{border:2px dashed #b9c6d8;border-radius:12px;padding:28px;display:grid;justify-items:center;gap:12px;text-align:center;background:#fafcff}
  .dragging .board-idle{border-color:#2f6fed;background:#f0f5ff}
  select{padding:9px;border:1px solid #ddd;border-radius:8px;background:#fff;font-size:14px;box-sizing:border-box}
  .cond{background:#f8fafc;border-left:3px solid #2f6fed;padding:8px 10px;border-radius:4px;font-size:12px;color:#445;margin-top:8px}
  .board-panes{display:grid;grid-template-columns:minmax(320px,1.3fr) minmax(300px,1fr);gap:14px;align-items:start}
  .plate .board-panes{grid-template-columns:minmax(240px,1.1fr) minmax(280px,1fr)}
  @media (max-width:900px){.board-panes{grid-template-columns:1fr}}
  svg.rings{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none}
  svg.rings text{font:700 15px/1 sans-serif;fill:#fff;paint-order:stroke;stroke:#1a7f37;stroke-width:3px}
  svg.rings text.ctrlLab{font-size:13px;stroke:#b26a00;fill:#fff}
  #gridArea{max-height:none}
  .imgwrap{position:relative;display:block;width:100%}
  .imgwrap img{display:block;width:100%;border-radius:8px}
  svg.hot{position:absolute;left:0;top:0;width:100%;height:100%}
  svg.hot circle{fill:rgba(255,255,255,0.01);stroke:transparent;stroke-width:2;cursor:pointer;pointer-events:all}
  svg.hot circle:hover{stroke:#2f6fed;stroke-width:3;fill:rgba(47,111,237,.14)}
  .cd{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:3px;border:1px solid rgba(0,0,0,.18);vertical-align:middle}
</style>
</head>
<body>
<div class="card">
  <h1>药敏板智能判读 Demo</h1>
  <div class="sub">96 孔微量肉汤稀释板 · 软件辅助判读，结果请人工复核</div>
  <div>
    <span class="hint">板型模板：</span>
    <select id="templateSel"></select>
    <button class="btn ghost" style="padding:6px 10px;font-size:13px" onclick="downloadTemplate()">下载模板 JSON</button>
    <label class="btn ghost" style="padding:6px 10px;font-size:13px">上传模板 JSON
      <input type="file" id="templateFile" accept=".json,application/json" style="display:none" onchange="uploadTemplate(this)">
    </label>
  </div>
  <div class="hint" style="margin-top:4px">（真实显色法板选「真菌药敏板（显色法）」，干板选对应 A板/B板；板面与判读条件可通过模板 JSON 修改后上传）</div>
  <div style="margin-top:10px">
    <label class="btn">上传药敏板照片（可多选 · 同一菌株）
      <input type="file" id="file" accept="image/*" multiple capture="environment" style="display:none" onchange="onPick(this)">
    </label>
    <input type="text" id="sampleNo" placeholder="样本编号/菌株（可选，如 克柔念珠菌6258）" style="margin-top:8px">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
      <select id="genusSel">
        <option value="candida">菌属：念珠菌属</option>
        <option value="aspergillus">菌属：曲霉属</option>
        <option value="mould">菌属：丝状真菌</option>
      </select>
      <input type="text" id="species" placeholder="菌种（如 克柔念珠菌）">
      <input type="text" id="cultureInfo" placeholder="培养信息（如 1640(Tris)）">
      <select id="timePoint">
        <option value="">时间点：未标注</option>
        <option value="24H">24H</option>
        <option value="48H">48H</option>
        <option value="72H">72H</option>
      </select>
      <input type="text" id="batchNo" placeholder="实验批次（可选）">
      <input type="text" id="operator" placeholder="操作人（可选）">
      <input type="text" id="strain2" placeholder="下半区菌号（B板上下接种两个菌时填，可选）" style="grid-column:1/3">
    </div>
    <div class="cond" id="ruleBox"></div>
  </div>
  <div id="status" class="hint"></div>
</div>
<div class="card" id="boardCard">
  <h2>板面</h2>
  <div class="hint" id="boardCap" style="margin-bottom:8px"></div>
  <div id="boardIdle" class="board-idle">
    <p style="margin:0">上传药敏板照片后，这里将显示 板面（照片 + 96 孔位标注）</p>
    <button class="btn" onclick="document.getElementById('file').click()">上传药敏板照片（可多选 · 同一菌株）</button>
    <p class="hint" style="margin:0">也可以把照片直接拖到这个区域</p>
  </div>
  <div id="boardView" style="display:none">
    <div class="board-panes">
      <div>
        <div class="imgwrap"><img id="boardImg" alt="板面（原图 + 孔位标注）"><svg id="boardRings" class="rings"></svg><svg id="boardHot" class="hot"></svg></div>
        <div class="hint" style="margin-top:6px">原图 + 孔位标注（绿圈 = MIC 判定孔）；<b>直接点击照片上的孔位可复核修改 MIC</b>　<a id="origLink" href="#" target="_blank" style="color:#2f6fed">查看原图 ↗</a></div>
      </div>
      <div>
        <div class="hint" id="gridHint" style="margin-bottom:4px"></div>
        <div class="gridwrap" id="gridArea"><table class="grid" id="grid"></table></div>
      </div>
    </div>
  </div>
</div>
<div class="card" id="result" style="display:none">
  <div id="singleBox" style="display:none">
    <h2>判读结果</h2>
    <div id="warns"></div>
    <div id="ctrl"></div>
    <h3>MIC 汇总 <span class="hint">（人工复核：下拉选择最终 MIC 值；软件判读值不会被覆盖）</span></h3>
    <table><thead><tr><th>药物</th><th>名称</th><th>软件判读</th><th>人工复核</th><th>质控范围</th><th>备注</th></tr></thead><tbody id="micRows"></tbody></table>
    <div style="margin-top:12px"><button class="btn green" onclick="downloadExcel()">导出 Excel（报告+登记表）</button>
      <button class="btn ghost" onclick="downloadReviewData()">导出复核数据（训练用 CSV）</button>
      <label style="margin-left:12px;font-size:14px"><input type="checkbox" class="revchk" id="reviewedChk"> 已人工复核</label>
    </div>
  </div>
  <div id="batchBox" style="display:none">
    <h2>多图判读结果 <span class="badge">同一菌株</span></h2>
    <div id="batchWarns"></div>
    <h3>各照片明细</h3>
    <div id="plateList"></div>
    <div style="margin-top:12px"><button class="btn green" onclick="downloadExcel()">导出 Excel（登记表 · 每张照片分别记录）</button>
      <button class="btn ghost" onclick="downloadReviewData()">导出复核数据（训练用 CSV）</button>
    </div>
  </div>
</div>
<div class="card" id="sessionCard">
  <h2>本次会话记录 <span class="hint">（点击记录可重新查看；刷新页面后清空）</span></h2>
  <div id="sessionList" class="hint">尚无记录。每次判读完成后会自动列到这里。</div>
</div>
<script>
const $=id=>document.getElementById(id);
let recordId=null;
let curWells=null,curMic=null,curMeta=null,recomputeTimer=null;
let curBatch=null,batchTimer=null;
let sessionRecords=[];
// 软件判读值（不可变，键 = 照片名||药物代码）与人工复核值分开存放：
// 人工修改下拉框只写入 REVIEWS，绝不覆盖 AUTO_MIC；逐孔重算的新值另列为“重算”。
let AUTO_MIC={},REVIEWS={};
function _key(f,c){return f+'||'+c;}
function resetReviewState(){AUTO_MIC={};REVIEWS={};}
function autoMicOf(file,m){const k=_key(file,m.code);return (k in AUTO_MIC)?AUTO_MIC[k]:m.mic;}
function rememberAuto(file,mic){(mic||[]).forEach(m=>{const k=_key(file,m.code);if(!(k in AUTO_MIC))AUTO_MIC[k]=m.mic;});}
function show(which){
  $('result').style.display='block';
  $('singleBox').style.display = which==='single'?'block':'none';
  $('batchBox').style.display = which==='batch'?'block':'none';
}
async function analyzeBlob(blob,fname){
  $('status').textContent='判读中…';
  $('result').style.display='none';
  resetReviewState();
  const fd=new FormData();
  fd.append('file',blob,fname);
  fd.append('sample_no',$('sampleNo').value);
  fd.append('template_id',$('templateSel').value||'demo');
  fd.append('culture_info',$('cultureInfo').value||'');
  fd.append('time_point',$('timePoint').value||'');
  fd.append('genus',$('genusSel').value||'candida');
  fd.append('species',$('species').value||'');
  fd.append('batch_no',$('batchNo').value||'');
  fd.append('operator',$('operator').value||'');
  fd.append('strain2',$('strain2').value||'');
  try{
    const r=await fetch('/api/analyze',{method:'POST',body:fd});
    const data=await r.json();
    if(!data.ok){$('status').textContent='判读失败：'+data.error;return;}
    $('status').textContent='判读完成（'+data.result.sample.file+'）';
    render(data); show('single');
    pushSession({label:data.result.sample.file,count:1,kind:'single',data:data,ts:new Date().toLocaleTimeString('zh-CN'),tp:data.result.sample.time_point||''});
  }catch(e){$('status').textContent='请求失败：'+e;}
}
async function loadSample(n,tpl){
  if(tpl&&$('templateSel')){$('templateSel').value=tpl;updateRuleBox();}
  $('status').textContent='加载示例照片…';
  const r=await fetch('/samples/'+n);
  if(!r.ok){$('status').textContent='示例照片 '+n+' 不在仓库中（真实药敏板照片未上传，请把自己的照片放入 samples/ 目录，按 real_01.jpg…命名即可用对应按钮）';return;}
  const b=await r.blob();
  await analyzeBlob(b,n);
}
function onPick(el){
  const fs=[...el.files]; el.value='';
  if(!fs.length) return;
  if(fs.length===1) analyzeBlob(fs[0],fs[0].name); else analyzeBatch(fs);
}
async function analyzeBatch(files){
  $('status').textContent='正在判读 '+files.length+' 张照片…';
  $('result').style.display='none';
  resetReviewState();
  const fd=new FormData();
  files.forEach(f=>fd.append('files',f,f.name));
  fd.append('sample_no',$('sampleNo').value);
  fd.append('template_id',$('templateSel').value||'demo');
  fd.append('culture_info',$('cultureInfo').value||'');
  fd.append('time_point',$('timePoint').value||'');
  fd.append('genus',$('genusSel').value||'candida');
  fd.append('species',$('species').value||'');
  fd.append('batch_no',$('batchNo').value||'');
  fd.append('operator',$('operator').value||'');
  fd.append('strain2',$('strain2').value||'');
  try{
    const r=await fetch('/api/analyze_batch',{method:'POST',body:fd});
    const data=await r.json();
    if(!data.ok){$('status').textContent='判读失败：'+data.error;return;}
    $('status').textContent='判读完成：'+data.batch.plates.length+' 张照片（同一菌株）';
    renderBatch(data); show('batch');
    pushSession({label:data.batch.plates.length+' 张（'+data.batch.plates[0].sample.file+' 等）',count:data.batch.plates.length,kind:'batch',data:data,ts:new Date().toLocaleTimeString('zh-CN'),tp:(data.batch.plates[0].sample.time_point||'')});
  }catch(e){$('status').textContent='请求失败：'+e;}
}
function revSelect(file,m){
  const k=_key(file,m.code);
  const opts=(m.options||[]).slice();
  [m.mic, autoMicOf(file,m)].forEach(v=>{if(v!==undefined&&opts.indexOf(v)<0)opts.push(v);});
  const orig=autoMicOf(file,m);
  const cur=(k in REVIEWS)?REVIEWS[k]:m.mic;
  return '<select class="revsel'+(String(cur)!==String(orig)?' changed':'')+'" data-file="'+esc(file)+'" data-code="'+esc(m.code)+'">'+
    opts.map(v=>'<option'+(String(v)===String(cur)?' selected':'')+'>'+esc(v)+'</option>').join('')+'</select>';
}
function qcBadge(m){
  if(!m.qc_range)return '';
  return m.qc_ok?'<span style="color:#2e8b57">在控 '+m.qc_range+'</span>'
                :'<span style="color:#c0392b;font-weight:bold">超出 '+m.qc_range+'</span>';
}
function micPosOf(mic){const p={};mic.forEach(m=>{if(m.mic_well)p[m.mic_well]=1;else if(m.mic_row)p[m.mic_row+m.col]=1;});return p;}
function concNum(v){const m=String(v==null?'':v).match(/-?[0-9]+(?:[.][0-9]+)?/);return m?parseFloat(m[0]):null;}
function reviewValue(file,code,fallback){const k=_key(file,code);return (k in REVIEWS)?REVIEWS[k]:fallback;}
function targetPosForValue(wmap,code,val){
  const ws=Object.values(wmap||{}).filter(w=>w&&w.drug===code&&w.conc!=null&&concNum(w.conc)!=null);
  if(!ws.length)return null;
  const hi=ws.reduce((a,b)=>concNum(a.conc)>=concNum(b.conc)?a:b);
  const lo=ws.reduce((a,b)=>concNum(a.conc)<=concNum(b.conc)?a:b);
  const t=String(val==null?'':val);
  if(/^[≥>]/.test(t))return null;   // 超过最高浓度：整列都长，没有“MIC 孔”
  if(/^[≤<]/.test(t))return lo.pos;
  const v=concNum(t); if(v==null)return null;
  let best=null,bd=Infinity;
  ws.forEach(w=>{const d=Math.abs(concNum(w.conc)-v);if(d<bd){bd=d;best=w.pos;}});
  return best;
}
function micListFor(file){
  if(curBatch&&curBatch.plates){
    const pl=curBatch.plates.find(x=>x.file===file);
    if(pl&&pl.mic)return pl.mic;
  }
  return curMic||[];
}
function micLabels(file,wmap){
  const p={};
  micListFor(file).forEach(m=>{
    const val=reviewValue(file,m.code,m.mic);
    if(/^[≥>]/.test(String(val==null?'':val)))return;   // 超上限：不画单孔 MIC 圈
    const t=wmap?targetPosForValue(wmap,m.code,val):null;
    if(t)p[t]=m.code+'='+val; else if(m.mic_well)p[m.mic_well]=m.code+'='+val;
  });
  return p;
}
// 超过最高浓度（≥xxx）的药物：整列孔都算生长，全部着红圈，文字标在最高浓度孔
function micAbove(file,wmap){
  const out={};
  micListFor(file).forEach(m=>{
    const val=reviewValue(file,m.code,m.mic);
    if(!/^[≥>]/.test(String(val==null?'':val)))return;
    let hi=null;
    Object.values(wmap||{}).forEach(w=>{
      if(!w||w.drug!==m.code||concNum(w.conc)==null)return;
      if(!hi||concNum(w.conc)>concNum(hi.conc))hi=w;
    });
    out[m.code]={val:val,hi:hi?hi.pos:null};
  });
  return out;
}
function reviewMicPos(file,wmap){
  const p={};
  Object.keys(micLabels(file,wmap)).forEach(k=>{p[k]=1;});
  return p;
}
const RING_COLOR={growth:'#ff3c6e',no_growth:'#2864e6',partial:'#9b5cc6',unknown:'#9aa3ad',unused:'#c8ccd2'};
function drawRings(file,wmap,size,r,svgId){
  const svg=$(svgId||'boardRings'); if(!svg)return;
  if(!wmap||!size){svg.innerHTML='';return;}
  svg.setAttribute('viewBox','0 0 '+size[0]+' '+size[1]);
  svg.removeAttribute('preserveAspectRatio');
  const rr=r||Math.max(10,(size[0]||1200)/12*0.42);
  const targets=micLabels(file,wmap);
  const above=micAbove(file,wmap);
  let out='';
  Object.values(wmap).forEach(w=>{
    if(!w.xy)return;
    const lab=targets[w.pos];
    const isT=!!lab;
    const ab=above[w.drug];
    const color=ab?RING_COLOR.growth:(isT?'#1a7f37':(RING_COLOR[w.status]||'#c8ccd2'));
    out+='<circle cx="'+w.xy[0]+'" cy="'+w.xy[1]+'" r="'+rr+'" fill="none" stroke="'+color+'" stroke-width="'+((isT||ab)?6:4)+'"></circle>';
    if(isT)out+='<text x="'+(w.xy[0]-rr)+'" y="'+(w.xy[1]-rr-6)+'" text-anchor="middle">'+esc(lab)+'</text>';
    if(ab&&ab.hi===w.pos)out+='<text x="'+(w.xy[0]-rr)+'" y="'+(w.xy[1]-rr-6)+'" text-anchor="middle">'+esc(w.drug+'='+ab.val)+'</text>';
    if(w.role==='positive_control'||w.role==='negative_control'){
      out+='<text x="'+(w.xy[0]-rr)+'" y="'+(w.xy[1]+rr+18)+'" text-anchor="middle" class="ctrlLab">'+(w.role==='positive_control'?'阳':'阴')+'</text>';
    }
  });
  svg.innerHTML=out;
}
function redrawRings(){
  if(curBatch&&curBatch.plates&&curBatch.plates.length){
    curBatch.plates.forEach((pl,i)=>{
      drawRings(pl.file,pl.wmap,pl.sample&&pl.sample.size,pl.sample&&pl.sample.well_r_orig,'rings_'+i);
    });
    const pl0=curBatch.plates[0];
    drawRings(pl0.file,pl0.wmap,pl0.sample&&pl0.sample.size,pl0.sample&&pl0.sample.well_r_orig);
  }else if(curMeta&&curMeta.file&&curWells){
    const si=curMeta.sampleInfo||{};
    drawRings(curMeta.file,curWells,si.size,si.well_r_orig);
  }else{
    const svg=$('boardRings'); if(svg)svg.innerHTML='';
    document.querySelectorAll('svg.rings[id^=rings_]').forEach(sv=>{sv.innerHTML='';});
  }
}
let REMARKS={};
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function autoNote(m){return (m.flags&&m.flags.length)?m.flags.join('；'):(m.note||'');}
function remarkValue(file,m){
  const key=file+'||'+m.code;
  return (key in REMARKS)?REMARKS[key]:autoNote(m);
}
function micTableHtml(file,mic){
  rememberAuto(file,mic);
  return mic.map(m=>{
    const orig=autoMicOf(file,m);
    const recomp=String(orig)!==String(m.mic);
    const sw='<b>'+esc(orig)+'</b>'+(recomp?'<div class="recomp" title="逐孔复核后重算值，原始软件判读不覆盖">重算 '+esc(m.mic)+'</div>':'');
    const meth=m.method?'<span class="mech">'+esc(m.method)+'</span>':'';
    return '<tr><td>'+esc(m.code)+'</td><td>'+esc(m.name)+meth+'</td><td>'+sw+'</td><td>'+revSelect(file,m)+'</td><td>'+qcBadge(m)+'</td>'+
    '<td><input type="text" class="remarkInp" data-file="'+esc(file)+'" data-code="'+esc(m.code)+'" value="'+esc(remarkValue(file,m))+'" placeholder="可填写备注" style="width:100%;min-width:180px;padding:4px 6px;border:1px solid #ddd;border-radius:6px;font-size:13px"></td></tr>';
  }).join('');
}
document.addEventListener('change',function(e){
  const t=e.target;
  if(t&&t.classList&&t.classList.contains('revsel')){
    const k=_key(t.dataset.file,t.dataset.code);
    REVIEWS[k]=t.value;
    const orig=AUTO_MIC[k];
    t.classList.toggle('changed', orig!==undefined && String(t.value)!==String(orig));
    if(curBatch&&curBatch.plates&&curBatch.plates.length){
      const i=curBatch.plates.findIndex(p=>p.file===t.dataset.file);
      refreshBatchGrids(i>=0?i:0);
      redrawRings();
    }
    else if(curWells){refreshGrid();}
    else{redrawRings();}
  }
});
document.addEventListener('input',function(e){
  const t=e.target;
  if(t&&t.classList&&t.classList.contains('remarkInp')){
    REMARKS[t.dataset.file+'||'+t.dataset.code]=t.value;
  }
});
function ctrlLine(controls,sample){
  const ck=Object.entries(controls).map(([p,v])=>p+' '+v.role+'：'+(v.ok?'正常 ✓':'异常 ✗')).join('　');
  return '对照孔：'+ck+(sample.time_point?'　时间点：'+sample.time_point:'')+(sample.culture_info?'　培养：'+sample.culture_info:'');
}
function wellTop(w){
  if(w.drug)return w.drug+'<br>'+(w.conc??'');
  if(w.role==='positive_control')return 'C+';
  if(w.role==='negative_control')return 'C-';
  return '';
}
function wellStTxt(s){return s==='growth'?'生长':(s==='no_growth'?'抑制':(s==='partial'?'临界':(s==='unknown'?'?':'')))}
const CDOT={blue:'#4a7fd4',purple:'#8e6fc4',pink:'#e56a94',pink_white:'#f0b6cc',white:'#e8e8e8'};
const DEF_LAYOUT={row_labels:'ABCDEFGH'.split(''),col_labels:Array.from({length:12},(_,i)=>String(i+1)),pos_order:'rowfirst'};
function buildGridHtml(wmap,micPos,idx,layout){
  const L=layout||DEF_LAYOUT;
  let html='<tr><th></th>'; L.col_labels.forEach(c=>{html+='<th>'+c+'</th>';}); html+='</tr>';
  const ia=(idx!==undefined&&idx!==null)?' data-idx="'+idx+'"':'';
  L.row_labels.forEach(rl=>{
    html+='<tr><th>'+rl+'</th>';
    L.col_labels.forEach(cl=>{
      const pos=(L.pos_order==='colfirst')?(cl+rl):(rl+cl);
      const w=wmap[pos]; if(!w){html+='<td></td>';return;}
      let cls=w.status==='growth'?'g':(w.status==='no_growth'?'n':(w.status==='partial'?'p':(w.status==='unused'?'u':'k')));
      if(micPos[pos])cls+=' mic';
      if(w.role==='positive_control'||w.role==='negative_control')cls+=' ctrl';
      const tip=pos+(w.drug?' '+w.drug+' '+(w.conc??''):'')+(w.color_label?' · '+w.color_label:'');
      const dot=w.color?'<span class="cd" style="background:'+(CDOT[w.color]||'#ccc')+'"></span>':'';
      html+='<td data-pos="'+pos+'"'+ia+' class="'+cls+'" title="'+tip+'"><div>'+wellTop(w)+'</div><div style="font-weight:700">'+dot+wellStTxt(w.status)+'</div></td>';
    });
    html+='</tr>';
  });
  return html;
}
function buildHotspots(wmap,size,shapeR){
  const ws=Object.values(wmap||{}).filter(w=>w&&w.xy);
  if(!ws.length||!size)return '';
  const r=shapeR||Math.max(12,(size[0]||1200)/12*0.45);
  return ws.map(w=>'<circle cx="'+w.xy[0]+'" cy="'+w.xy[1]+'" r="'+r+'" data-pos="'+w.pos+'"></circle>').join('');
}
function setHotspot(svgEl,wmap,size,shapeR,clickFn){
  if(!svgEl){return;}
  if(!size){svgEl.innerHTML='';return;}
  svgEl.setAttribute('viewBox','0 0 '+size[0]+' '+size[1]);
  svgEl.setAttribute('preserveAspectRatio','none');
  svgEl.innerHTML=buildHotspots(wmap,size,shapeR);
  svgEl.querySelectorAll('circle').forEach(c=>{
    c.addEventListener('click',()=>clickFn(c.dataset.pos));
  });
}
function refreshGrid(){if(!curWells)return;$('grid').innerHTML=buildGridHtml(curWells,reviewMicPos(curMeta&&curMeta.file,curWells),undefined,curMeta&&curMeta.layout);bindWellClicks();redrawRings();}
function bindWellClicks(){
  const inter=!!(curMeta&&curMeta.interactive);
  document.querySelectorAll('#grid td[data-pos]').forEach(td=>{
    td.style.cursor=inter?'pointer':'default';
    if(inter)td.onclick=()=>wellClick(td.dataset.pos);
  });
}
function wellClick(pos){
  if(!curMeta||!curMeta.interactive)return;
  const w=curWells[pos];if(!w)return;
  const seq=['growth','no_growth','partial'];
  w.status=seq[((seq.indexOf(w.status)+1)%3+3)%3];
  refreshGrid();
  redrawRings();
  clearTimeout(recomputeTimer);
  recomputeTimer=setTimeout(doRecompute,250);
}
async function doRecompute(){
  if(!curMeta||!curMeta.interactive)return;
  const fd=new FormData();
  fd.append('template_id',curMeta.template_id);
  fd.append('genus',curMeta.genus);
  fd.append('sample_no',curMeta.sample_no);
  fd.append('time_point',curMeta.time_point);
  const st={};Object.entries(curWells).forEach(([p,w])=>st[p]=w.status);
  fd.append('wells',JSON.stringify(st));
  try{
    const r=await fetch('/api/recompute',{method:'POST',body:fd});
    const d=await r.json();
    if(!d.ok)return;
    curMic=d.mic;
    $('micRows').innerHTML=micTableHtml(curMeta.file,d.mic);
    $('ctrl').innerHTML='<div class="hint">'+ctrlLine(d.controls,curMeta.sampleInfo)+'　（已按逐孔复核重算）</div>';
    $('warns').innerHTML=(d.warnings||[]).map(w=>'<div class="warn">⚠ '+w+'</div>').join('');
    refreshGrid();
    redrawRings();
  }catch(e){}
}
function render(data){
  const res=data.result;
  recordId=data.record_id;
  curBatch=null;
  $('warns').innerHTML=res.warnings.length?res.warnings.map(w=>'<div class="warn">⚠ '+w+'</div>').join(''):'';
  $('ctrl').innerHTML='<div class="hint">'+ctrlLine(res.controls,res.sample)+'</div>';
  const rchk=$('reviewedChk'); rchk.dataset.file=res.sample.file; rchk.checked=false;
  curMic=res.mic;
  $('micRows').innerHTML=micTableHtml(res.sample.file,res.mic);
  const wmap={}; res.wells.forEach(w=>{wmap[w.pos]=w;});
  curWells=wmap;
  curMeta={interactive:(res.mic||[]).some(m=>m.options),
           layout:res.layout,
           template_id:$('templateSel').value||'demo',
           genus:$('genusSel').value||'candida',
           sample_no:$('sampleNo').value||'',
           time_point:$('timePoint').value||'',
           file:res.sample.file,
           sampleInfo:res.sample};
  $('grid').innerHTML=buildGridHtml(curWells,reviewMicPos(res.sample.file,curWells),undefined,res.layout);
  bindWellClicks();
  $('gridHint').textContent=curMeta.interactive?'逐孔板面：点击任意孔位循环切换 生长→抑制→临界，MIC 与质控会自动重算':'逐孔板面（当前板型只读展示）';
  $('boardIdle').style.display='none';
  $('boardView').style.display='block';
  $('boardCap').textContent='板面：原图 + 96 孔位标注'+(res.sample.time_point?(' · '+res.sample.time_point):'')+(res.sample.culture_info?(' · '+res.sample.culture_info):'');
  $('boardImg').src='/api/orig/'+recordId+'/0';
  setHotspot($('boardHot'),curMeta.interactive?curWells:null,res.sample.size,res.sample.well_r_orig,wellClick);
  redrawRings();
  $('origLink').href='/api/orig/'+recordId+'/0';
}
function batchCtrlText(p){
  const ck=Object.entries(p.controls).map(([pos,v])=>pos+' '+v.role+'：'+(v.ok?'正常 ✓':'异常 ✗')).join('　');
  return '对照孔：'+ck+'　（已按逐孔复核重算）';
}
function batchGridHtml(i){
  const p=curBatch.plates[i];
  return buildGridHtml(p.wmap,reviewMicPos(p.file,p.wmap),i,curBatch&&curBatch.layout);
}
function refreshBatchGrids(i){
  if(i===0)$('grid').innerHTML=batchGridHtml(0);
  const g=document.getElementById('grid_'+i);
  if(g)g.innerHTML=batchGridHtml(i);
  bindBatchWellClicks();
}
function bindBatchWellClicks(){
  document.querySelectorAll('#grid td[data-idx], #plateList td[data-idx]').forEach(td=>{
    td.style.cursor='pointer';
    td.onclick=()=>batchWellClick(parseInt(td.dataset.idx,10), td.dataset.pos);
  });
}
function batchWellClick(i,pos){
  if(!curBatch)return;
  const p=curBatch.plates[i]; if(!p)return;
  const w=p.wmap[pos]; if(!w)return;
  const seq=['growth','no_growth','partial'];
  w.status=seq[((seq.indexOf(w.status)+1)%3+3)%3];
  refreshBatchGrids(i);
  clearTimeout(batchTimer);
  batchTimer=setTimeout(doRecomputeBatch,250);
}
async function doRecomputeBatch(){
  if(!curBatch)return;
  const items=curBatch.plates.map(p=>{
    const st={};Object.entries(p.wmap).forEach(([pos,w])=>st[pos]=w.status);
    return {file:p.file,states:st};
  });
  const fd=new FormData();
  fd.append('template_id',curBatch.template_id);
  fd.append('genus',curBatch.genus);
  fd.append('sample_no',curBatch.sample_no);
  fd.append('time_point',curBatch.time_point);
  fd.append('wells',JSON.stringify(items));
  try{
    const r=await fetch('/api/recompute_batch',{method:'POST',body:fd});
    const d=await r.json();
    if(!d.ok)return;
    d.plates.forEach((res,i)=>{
      const p=curBatch.plates[i]; if(!p)return;
      p.mic=res.mic; p.controls=res.controls; p.warnings=res.warnings||[];
      const tb=document.getElementById('micTb_'+i);
      if(tb)tb.innerHTML=micTableHtml(p.file,res.mic);
      const cl=document.getElementById('ctrl_'+i);
      if(cl)cl.innerHTML=batchCtrlText(p);
      const wn=document.getElementById('warns_'+i);
      if(wn)wn.innerHTML=p.warnings.map(w=>'<div class="warn">⚠ '+w+'</div>').join('');
      const dt=document.querySelector('#plateList details.plate:nth-of-type('+(i+1)+') summary .badge');
      if(dt)dt.textContent=p.warnings.length?p.warnings.length+' 条警告':'无警告';
      refreshBatchGrids(i);
    });
  }catch(e){}
}
function renderBatch(data){
  const b=data.batch;
  recordId=data.record_id;
  $('reviewedChk').dataset.file=''; $('reviewedChk').checked=false;
  curWells=null;curMic=null;curMeta={interactive:false};
  curBatch={template_id:$('templateSel').value||'demo',
            layout:(b.plates[0]&&b.plates[0].layout)||null,
            genus:$('genusSel').value||'candida',
            sample_no:$('sampleNo').value||'',
            time_point:$('timePoint').value||'',
            plates:[]};
  b.plates.forEach(p=>{
    const wmap={};
    (p.wells||[]).forEach(w=>{wmap[w.pos]=w;});
    curBatch.plates.push({file:p.sample.file,wmap:wmap,mic:p.mic,controls:p.controls,
                          warnings:p.warnings||[],sample:p.sample});
  });
  const interactive=!!(b.plates.length&&b.plates[0].mic&&b.plates[0].mic.some(m=>m.options));
  $('boardIdle').style.display='none';
  $('boardView').style.display='block';
  $('boardCap').textContent='板面：第 1 张照片（'+b.plates[0].sample.file+'）；各照片逐张展示在下方明细中';
  $('boardImg').src='/api/orig/'+recordId+'/0';
  if(interactive){
    setHotspot($('boardHot'),curBatch.plates[0].wmap,b.plates[0].sample.size,
               b.plates[0].sample.well_r_orig,pos=>batchWellClick(0,pos));
  }else{
    setHotspot($('boardHot'),null,null,null,null);
  }
  $('origLink').href='/api/orig/'+recordId+'/0';
  if(interactive){
    $('grid').innerHTML=batchGridHtml(0);
    redrawRings();
    $('gridHint').textContent='逐孔板面（第 1 张照片）：点击任意孔位循环切换 生长→抑制→临界，该照片 MIC 与下方合并结果自动重算';
  }else{
    const wmap0={}; (b.plates[0].wells||[]).forEach(w=>{wmap0[w.pos]=w;});
    $('grid').innerHTML=buildGridHtml(wmap0,{},undefined,curBatch.layout);
    $('gridHint').textContent='逐孔板面（多图模式：第 1 张只读展示；各照片复核在下方明细中进行）';
  }
  bindBatchWellClicks();
  $('batchWarns').innerHTML=(b.errors||[]).map(e=>'<div class="warn">✗ '+e.file+'：'+e.error+'</div>').join('');
  $('plateList').innerHTML=b.plates.map((p,i)=>{
    const ck=Object.entries(p.controls).map(([pos,v])=>pos+' '+v.role+'：'+(v.ok?'正常 ✓':'异常 ✗')).join('　');
    const warns=(p.warnings||[]).map(w=>'<div class="warn">⚠ '+w+'</div>').join('');
    const rows=micTableHtml(p.sample.file,p.mic);
    const photo='<div class="imgwrap"><img src="/api/orig/'+data.record_id+'/'+i+'" alt="板面照片"><svg class="rings" id="rings_'+i+'"></svg><svg class="hot" id="hot_'+i+'"></svg></div>'+
      '<div class="hint" style="margin-top:6px">原图 + 孔位标注（绿圈 = MIC 判定孔）；点击孔位即可复核　<a href="/api/orig/'+data.record_id+'/'+i+'" target="_blank" style="color:#2f6fed">查看原图 ↗</a></div>';
    const wmapP={};(p.wells||[]).forEach(w=>{wmapP[w.pos]=w;});
    const head='<div class="hint" style="margin-bottom:4px">逐孔板面（第 '+(i+1)+' 张）'+(interactive?'：点击孔位循环切换 生长→抑制→临界':'（只读展示）')+'</div>';
    const gridblock=head+'<div class="gridwrap"><table class="grid" id="grid_'+i+'">'+
      buildGridHtml(wmapP,interactive?reviewMicPos(p.file,wmapP):{},interactive?i:undefined,curBatch&&curBatch.layout)+
      '</table></div>';
    return '<details '+(i===0?'open':'')+' class="plate"><summary>照片 '+(i+1)+'：'+p.sample.file+
      ((p.warnings&&p.warnings.length)?'<span class="badge">'+p.warnings.length+' 条警告</span>':'<span class="badge">无警告</span>')+
      '</summary><div class="board-panes">'+
      '<div>'+photo+'</div>'+
      '<div>'+gridblock+'</div>'+
      '</div>'+
      '<div style="margin-top:10px"><div class="hint" id="ctrl_'+i+'">对照孔：'+ck+(p.sample.time_point?'　时间点：'+p.sample.time_point:'')+(p.sample.culture_info?'　培养：'+p.sample.culture_info:'')+'</div>'+
      '<div id="warns_'+i+'">'+warns+'</div>'+
      '<div style="margin:4px 0"><label style="font-size:13px"><input type="checkbox" class="revchk" data-file="'+p.sample.file+'"> 已人工复核</label></div>'+
      '<table><thead><tr><th>药物</th><th>名称</th><th>软件判读</th><th>人工复核</th><th>质控</th><th>备注</th></tr></thead><tbody id="micTb_'+i+'">'+rows+'</tbody></table></div>'+
      '</details>';
  }).join('');
  if(interactive){
    b.plates.forEach((p,i)=>{
      setHotspot(document.getElementById('hot_'+i),curBatch.plates[i].wmap,
                 p.sample.size,p.sample.well_r_orig,pos=>batchWellClick(i,pos));
    });
  }
  redrawRings();
}
function pushSession(rec){sessionRecords.unshift(rec);renderSession();}
function renderSession(){
  if(!sessionRecords.length){$('sessionList').innerHTML='尚无记录。每次判读完成后会自动列到这里。';return;}
  $('sessionList').innerHTML=sessionRecords.map((s,i)=>
    '<div style="border:1px solid #e3e6ec;border-radius:8px;padding:8px 10px;margin:6px 0;cursor:pointer" onclick="loadSessionRecord('+i+')"><b>'+s.label+'</b> <span class="badge">'+s.count+' 张</span> <span class="hint">'+s.ts+(s.tp?' · 时间点 '+s.tp:'')+'</span></div>').join('')+
    '<div style="margin-top:6px"><button class="btn ghost" style="padding:6px 10px;font-size:13px" onclick="sessionRecords=[];renderSession()">清空列表</button></div>';
}
function loadSessionRecord(i){
  const s=sessionRecords[i];if(!s)return;
  if(s.kind==='single'){render(s.data);show('single');}else{renderBatch(s.data);show('batch');}
}
function downloadTemplate(){
  const tid=$('templateSel').value||'demo';
  location.href='/api/template/'+tid+'/download';
}
async function uploadTemplate(el){
  const f=el.files&&el.files[0]; el.value='';
  if(!f)return;
  const fd=new FormData(); fd.append('file',f,f.name);
  const r=await fetch('/api/upload_template',{method:'POST',body:fd});
  const d=await r.json();
  if(!d.ok){alert('模板上传失败：'+d.error);return;}
  await initTemplates();
  $('templateSel').value=d.id;
  updateRuleBox();
  $('status').textContent='已载入模板：'+d.name+'（可直接上传照片判读）';
}
function updateRuleBox(){
  const tid=$('templateSel').value||'demo';
  const info=TPL_INFO[tid]||{};
  const g=$('genusSel').value||'candida';
  let t;
  if(info.mode==='growth'){
    t = (g==='mould'||g==='aspergillus')
      ? '干板·丝状真菌：按 WS/T 411 判读——两性霉素B、伊曲康唑、泊沙康唑、艾莎康唑、伏立康唑以 100% 抑制为终点；氟胞嘧啶、氟康唑以 50% 抑制为终点；阿尼芬净/卡泊芬净/米卡芬净判 MEC（菌落非融合生长的最低浓度）。阳性对照不生长时改用各药最低浓度孔作生长参照，并在结果中提示。'
      : '干板·念珠菌：孔内均匀浑浊或点状菌落＝生长，生长量 ≥ 阳性对照 50% 计为生长（CLSI M27）；MIC＝最低无生长孔浓度；跳孔 1 个忽略、≥2 个无效。';
    if(info.halves)t+='　B板上、下半区布局相同，可接种两个菌株。';
  }else if(info.mode==='color'){
    t = g==='mould'
      ? '显色板·丝状真菌：两性霉素B、伊曲康唑、泊沙康唑、伏立康唑、艾莎康唑仅蓝色＝无生长（紫/粉/白均算生长）；氟胞嘧啶、米卡芬净、卡泊芬净、氟康唑蓝或紫＝无生长（参考 DL-96Fungus 说明书）。'
      : (g==='aspergillus'
        ? '显色板·曲霉属：康唑类（FLU/ISA/VRC/POS/ITR）紫色或粉色＝阳性（生长）；蓝色＝阴性（抑制）；其他药物粉红＝阳性，蓝/紫＝阴性。'
        : '显色板·念珠菌属：粉红/红色＝阳性（生长），蓝色或紫色＝阴性（抑制）；两性霉素B 的紫色/粉色/红色均按阳性。');
  }else{
    t = info.reading || '按模板规则判读。';
  }
  $('ruleBox').textContent='当前判读条件：'+t;
}
$('genusSel').onchange=updateRuleBox;
$('templateSel').onchange=updateRuleBox;
async function downloadReviewData(){
  if(!recordId)return;
  let items=[];
  if(curBatch&&curBatch.plates&&curBatch.plates.length){
    items=curBatch.plates.map(p=>{
      const st={};Object.entries(p.wmap).forEach(([pos,w])=>st[pos]=w.status);
      return {file:p.file,states:st};
    });
  }else if(curWells){
    const st={};Object.entries(curWells).forEach(([pos,w])=>st[pos]=w.status);
    items=[{file:(curMeta&&curMeta.file)||'',states:st}];
  }
  if(!items.length)return;
  const fd=new FormData();
  fd.append('record_id',recordId);
  fd.append('wells',JSON.stringify(items));
  const r=await fetch('/api/review_data',{method:'POST',body:fd});
  if(!r.ok){alert('导出复核数据失败');return;}
  const b=await r.blob();
  const a=document.createElement('a');
  a.href=URL.createObjectURL(b);a.download='复核数据_训练用.csv';a.click();
}
async function downloadExcel(){
  if(!recordId)return;
  const reviews={},reviewed={};
  document.querySelectorAll('.revsel').forEach(s=>{
    const f=s.dataset.file,code=s.dataset.code;
    (reviews[f]=reviews[f]||{})[code]=s.value;
  });
  document.querySelectorAll('.revchk').forEach(chk=>{
    if(chk.dataset.file) reviewed[chk.dataset.file]=chk.checked;
  });
  const remarks={};
  document.querySelectorAll('.remarkInp').forEach(inp=>{
    const f=inp.dataset.file,code=inp.dataset.code;
    (remarks[f]=remarks[f]||{})[code]=inp.value;
  });
  const fd=new FormData();
  fd.append('record_id',recordId);
  fd.append('reviews',JSON.stringify(reviews));
  fd.append('reviewed',JSON.stringify(reviewed));
  fd.append('remarks',JSON.stringify(remarks));
  const r=await fetch('/api/export',{method:'POST',body:fd});
  const b=await r.blob();
  const a=document.createElement('a');
  a.href=URL.createObjectURL(b); a.download='药敏结果.xlsx'; a.click();
}
let TPL_INFO={};
async function initTemplates(){
  try{const r=await fetch('/api/templates');const d=await r.json();
    TPL_INFO={}; d.templates.forEach(t=>{TPL_INFO[t.id]=t;});
    $('templateSel').innerHTML=d.templates.map(t=>'<option value="'+t.id+'">'+t.name+'</option>').join('');
    updateRuleBox();
  }catch(e){}
}
initTemplates();
updateRuleBox();
(function(){
  const card=$('boardCard');
  card.addEventListener('dragover',e=>{e.preventDefault();card.classList.add('dragging')});
  card.addEventListener('dragleave',()=>card.classList.remove('dragging'));
  card.addEventListener('drop',e=>{
    e.preventDefault();card.classList.remove('dragging');
    const fs=[...e.dataTransfer.files].filter(f=>f.type.startsWith('image/')||/(jpe?g|png|webp|bmp)$/i.test(f.name));
    if(!fs.length){$('status').textContent='拖入的不是图片文件';return;}
    if(fs.length===1)analyzeBlob(fs[0],fs[0].name);else analyzeBatch(fs);
  });
})();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


@app.get("/api/templates")
def list_templates():
    return {"templates": [{"id": k, "name": v.get("name", k),
                           "mode": (v.get("detection") or {}).get("mode", "color"),
                           "reading": (v.get("detection") or {}).get("reading", ""),
                           "halves": bool(v.get("halves"))}
                          for k, v in TEMPLATES.items()]}


@app.get("/api/template/{tid}/download")
def download_template_file(tid: str):
    tpl = TEMPLATES.get(tid)
    if not tpl:
        return Response("模板不存在", status_code=404)
    from urllib.parse import quote
    fn = quote(f"{tid}.json")
    body = json.dumps(tpl, ensure_ascii=False, indent=2)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition":
                             f"attachment; filename={tid}.json; filename*=UTF-8''{fn}"})


@app.post("/api/upload_template")
async def upload_template(file: UploadFile = File(...)):
    """上传自定义板型/判读条件模板（JSON），保存后立即生效。"""
    try:
        data = json.loads((await file.read()).decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"JSON 解析失败：{e}"}
    if (not isinstance(data, dict) or not data.get("grid")
            or not (data.get("tracks") or data.get("drugs"))):
        return {"ok": False, "error": "模板缺少必要字段（需要 grid，以及 tracks 或 drugs）"}
    stem = os.path.splitext(os.path.basename(file.filename or "custom"))[0]
    safe = "".join(ch for ch in stem if ch.isalnum() or ch in "-_") or "custom"
    tid = "upload_" + safe
    try:
        with open(os.path.join(TPL_SAVE_DIR, f"{tid}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "error": f"保存失败：{e}"}
    TEMPLATES[tid] = data
    return {"ok": True, "id": tid, "name": data.get("name") or stem}


@app.get("/samples/{name}")
def get_sample(name: str):
    p = os.path.join(SAMPLES_DIR, name)
    if not os.path.isfile(p):
        return Response("not found", status_code=404)
    return FileResponse(p)


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...), sample_no: str = Form(""),
                  template_id: str = Form("demo"), culture_info: str = Form(""),
                  time_point: str = Form(""), genus: str = Form("candida"),
                  species: str = Form(""), batch_no: str = Form(""),
                  operator: str = Form(""), strain2: str = Form("")):
    rid = uuid.uuid4().hex
    orig = os.path.join(OUT_DIR, f"orig_{rid}.jpg")
    try:
        with open(orig, "wb") as f:
            f.write(await file.read())
        tpl = get_template(template_id)
        if tpl.get("panel_type") == "chart150":
            result = analyze_chart(orig, tpl, outdir=OUT_DIR,
                                   sample_no=sample_no.strip(), genus=genus)
        else:
            analyzer = PlateAnalyzer(tpl)
            result = analyzer.run(orig, outdir=OUT_DIR, sample_no=sample_no.strip(), genus=genus)
        result["orig"] = orig
        s = result["sample"]
        s["file"] = file.filename or s["file"]
        s["culture_info"] = culture_info.strip()
        s["time_point"] = time_point.strip()
        s["genus"] = genus
        s["species"] = species.strip()
        s["batch_no"] = batch_no.strip()
        s["operator"] = operator.strip()
        s["strain2"] = strain2.strip()
        rset = enrich_qc([result], sample_no.strip())
        with open(result["annotated"], "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        STORE[rid] = {"plates": [result], "template_id": template_id,
                      "meta": build_meta(sample_no, culture_info, genus, species,
                                         batch_no, operator, strain2),
                      "qc": rset}
        return {"ok": True, "result": result, "image_b64": img_b64, "record_id": rid}
    except Exception as e:
        if os.path.exists(orig):
            os.remove(orig)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/analyze_batch")
async def analyze_batch(files: list[UploadFile] = File(...), sample_no: str = Form(""),
                        template_id: str = Form("demo"), culture_info: str = Form(""),
                        time_point: str = Form(""), genus: str = Form("candida"),
                        species: str = Form(""), batch_no: str = Form(""),
                        operator: str = Form(""), strain2: str = Form("")):
    """同一菌株的多张照片：逐张判读，分别记录。"""
    rid = uuid.uuid4().hex
    plate_results, errors = [], []
    for f in files:
        orig = os.path.join(OUT_DIR, f"orig_{rid}_{len(plate_results)}.jpg")
        try:
            with open(orig, "wb") as fp:
                fp.write(await f.read())
            tpl = get_template(template_id)
            if tpl.get("panel_type") == "chart150":
                res = analyze_chart(orig, tpl, outdir=OUT_DIR,
                                    sample_no=sample_no.strip(), genus=genus)
            else:
                analyzer = PlateAnalyzer(tpl)
                res = analyzer.run(orig, outdir=OUT_DIR, sample_no=sample_no.strip(), genus=genus)
            res["orig"] = orig
            s = res["sample"]
            s["file"] = f.filename or s["file"]
            s["culture_info"] = culture_info.strip()
            s["time_point"] = time_point.strip()
            s["genus"] = genus
            s["species"] = species.strip()
            s["batch_no"] = batch_no.strip()
            s["operator"] = operator.strip()
            s["strain2"] = strain2.strip()
            res["image_b64"] = preview_b64(res["annotated"])
            plate_results.append(res)
        except Exception as e:
            if os.path.exists(orig):
                os.remove(orig)
            errors.append({"file": f.filename or "未命名",
                           "error": f"{type(e).__name__}: {e}"})

    if not plate_results:
        msg = "；".join(f"{e['file']}: {e['error']}" for e in errors)
        return {"ok": False, "error": "全部照片判读失败：" + msg}

    rset = enrich_qc(plate_results, sample_no.strip())
    store_plates = [{k: v for k, v in p.items() if k != "image_b64"}
                    for p in plate_results]
    STORE[rid] = {"plates": store_plates, "template_id": template_id,
                  "meta": build_meta(sample_no, culture_info, genus, species,
                                     batch_no, operator, strain2),
                  "qc": rset}
    plates = [{k: p[k] for k in ("sample", "controls", "warnings", "mic", "wells",
                                  "layout", "image_b64")}
              for p in plate_results]
    return {"ok": True, "record_id": rid,
            "batch": {"sample_no": sample_no.strip(),
                      "date": datetime.date.today().isoformat(),
                      "plates": plates,
                      "merged_mic": merge_mic(plate_results),
                      "errors": errors}}


@app.post("/api/recompute")
async def recompute(template_id: str = Form("demo"), genus: str = Form("candida"),
                    sample_no: str = Form(""), time_point: str = Form(""),
                    wells: str = Form("{}")):
    """逐孔板面人工复核后重算 MIC。"""
    tpl = get_template(template_id)
    an = PlateAnalyzer(tpl)
    if not an.new_style:
        return {"ok": False, "error": "该板型不支持逐孔复核"}
    try:
        states = json.loads(wells or "{}")
    except Exception:
        return {"ok": False, "error": "wells 数据格式错误"}
    built = {}
    for pos, st in states.items():
        role, code, _name, conc = an._well_meta(pos)
        rec = {"pos": pos, "role": role, "status": st, "growth": st == "growth",
               "score": None, "confidence": 1.0}
        if role == "drug":
            rec["drug"] = code
            rec["conc"] = conc
        built[pos] = rec
    an.genus = genus
    mic_list, controls, extra = an._derive_mic_tracks(built)
    rset = match_qc(sample_no)
    if rset:
        ranges = (rset.get("ranges") or {}).get(time_point or "")
        if ranges:
            for m in mic_list:
                rng = ranges.get(m["code"])
                if rng:
                    m["qc_range"] = rng
                    m["qc_ok"] = qc_in_range(m["mic"], rng)
    return {"ok": True, "mic": mic_list, "controls": controls, "warnings": extra}


@app.post("/api/recompute_batch")
async def recompute_batch(template_id: str = Form("demo"), genus: str = Form("candida"),
                          sample_no: str = Form(""), time_point: str = Form(""),
                          wells: str = Form("[]")):
    """多张照片：逐孔复核后重算各照片 MIC，并重新合并。"""
    tpl = get_template(template_id)
    an = PlateAnalyzer(tpl)
    if not an.new_style:
        return {"ok": False, "error": "该板型不支持逐孔复核"}
    try:
        items = json.loads(wells or "[]")
    except Exception:
        return {"ok": False, "error": "wells 数据格式错误"}
    rset = match_qc(sample_no.strip())
    rngs = (rset.get("ranges") or {}).get(time_point or "", {}) if rset else {}
    out, fake_plates = [], []
    an.genus = genus
    for it in items:
        fname = str(it.get("file") or "")
        built = {}
        for pos, st in (it.get("states") or {}).items():
            role, code, _name, conc = an._well_meta(pos)
            rec = {"pos": pos, "role": role, "status": st, "growth": st == "growth",
                   "score": None, "confidence": 1.0}
            if role == "drug":
                rec["drug"] = code
                rec["conc"] = conc
            built[pos] = rec
        mic_list, controls, extra = an._derive_mic_tracks(built)
        if rngs:
            for m in mic_list:
                rng = rngs.get(m["code"])
                if rng:
                    m["qc_range"] = rng
                    m["qc_ok"] = qc_in_range(m["mic"], rng)
        out.append({"file": fname, "mic": mic_list, "controls": controls,
                    "warnings": extra})
        fake_plates.append({"sample": {"file": fname}, "mic": mic_list})
    return {"ok": True, "plates": out, "merged_mic": merge_mic(fake_plates)}


@app.get("/api/orig/{rid}/{idx}")
def get_original(rid: str, idx: int):
    """查看原始照片（未标注）。"""
    rec = STORE.get(rid)
    if not rec or not (0 <= idx < len(rec.get("plates", []))):
        return Response("not found", status_code=404)
    p = rec["plates"][idx].get("orig")
    if not p or not os.path.isfile(p):
        return Response("not found", status_code=404)
    return FileResponse(p, media_type="image/jpeg")


@app.post("/api/review_data")
async def review_data(record_id: str = Form(...), wells: str = Form("[]")):
    """导出逐孔颜色特征 + 人工复核结果（训练/标定用 CSV）。"""
    import csv
    import io

    rec = STORE.get(record_id)
    if not rec:
        return Response("记录不存在或已过期", status_code=404)
    try:
        items = json.loads(wells or "[]")
    except Exception:
        items = []
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["照片", "孔位", "角色", "药物", "浓度", "色相H", "饱和度S", "明度V",
                 "颜色分级", "生长度量", "置信度", "软件判读", "复核判读"])
    role_cn = {"drug": "药物", "positive_control": "阳性对照",
               "negative_control": "阴性对照", "unused": "未使用"}
    for i, plate in enumerate(rec.get("plates", [])):
        states = (items[i].get("states") if i < len(items) else {}) or {}
        fname = plate.get("sample", {}).get("file", "")
        for wl in plate.get("wells", []):
            pos = wl.get("pos")
            hsv = wl.get("hsv") or ["", "", ""]
            wr.writerow([fname, pos, role_cn.get(wl.get("role"), wl.get("role")),
                         wl.get("drug", ""), wl.get("conc", ""),
                         hsv[0], hsv[1], hsv[2], wl.get("color_label", ""),
                         wl.get("score", ""), wl.get("confidence", ""),
                         wl.get("status", ""), states.get(pos) or wl.get("status", "")])
    from urllib.parse import quote
    fn = quote("复核数据_训练用.csv")
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=review_data.csv; filename*=UTF-8''{fn}"})


@app.post("/api/export")
async def export(record_id: str = Form(...), reviews: str = Form("{}"),
                 reviewed: str = Form("{}"), remarks: str = Form("{}")):
    rec = STORE.get(record_id)
    if not rec:
        return Response("记录不存在或已过期", status_code=404)
    try:
        reviews_map = json.loads(reviews or "{}")
        reviewed_map = json.loads(reviewed or "{}")
        remarks_map = json.loads(remarks or "{}")
    except Exception:
        reviews_map, reviewed_map, remarks_map = {}, {}, {}
    plates = rec["plates"]
    tpl = get_template(rec.get("template_id", "demo"))
    for p in plates:
        fname = p.get("sample", {}).get("file", "")
        p["sample"]["reviewed"] = bool(reviewed_map.get(fname, False))
        rm = remarks_map.get(fname) or {}
        for m in p.get("mic", []):
            rv = (reviews_map.get(fname) or {}).get(m.get("code"))
            if rv:
                m["final_mic"] = rv
            if m.get("code") in rm:
                m["remark"] = rm[m["code"]]
    meta = dict(rec.get("meta") or {})
    meta["date"] = datetime.date.today().isoformat()
    try:
        if tpl.get("tracks"):
            xlsx = export_record_sheet(plates, tpl, meta=meta, qc=rec.get("qc"))
        elif len(plates) == 1:
            xlsx = export_excel(plates[0])
        else:
            xlsx = export_excel_batch(plates, sample_no=meta.get("sample_no", ""))
    except Exception as e:
        return Response(f"导出失败: {type(e).__name__}: {e}", status_code=500)
    from urllib.parse import quote
    fn = quote("药敏结果.xlsx")
    return Response(
        xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f"attachment; filename=ast_results.xlsx; filename*=UTF-8''{fn}"})


if __name__ == "__main__":
    import uvicorn
    # 默认只允许本机访问；启动（局域网）脚本会把 AST_HOST 设为 0.0.0.0 开放给内网
    host = os.environ.get("AST_HOST", "127.0.0.1")
    port = int(os.environ.get("AST_PORT", "8765"))
    if FROZEN and os.environ.get("AST_NO_BROWSER") != "1":
        import threading
        import webbrowser
        threading.Timer(2.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(app, host=host, port=port)
