# -*- coding: utf-8 -*-
"""96 孔药敏板图像判读管线（演示版）。

流程: 读图 → 板子检测/透视校正 → 孔位定位 → 颜色提取 → 对照孔校准判读 → MIC 推导
      → 标注图 + JSON 结果 + Excel 导出

命令行用法:
    uv run --with opencv-python --with numpy --with openpyxl --with pillow python ast_reader.py <照片路径>
"""
import argparse
import datetime
import io
import json
import os

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROW_LETTERS = "ABCDEFGH"

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def load_template(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_image(path):
    """兼容中文/特殊路径的图片读取。"""
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"无法读取图片: {path}")
    return img


def write_image(path, img):
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        raise ValueError(f"无法写入图片: {path}")
    buf.tofile(path)


def order_corners(pts):
    """四点按 左上/右上/右下/左下 排序。"""
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.float32([pts[np.argmin(s)], pts[np.argmin(d)],
                       pts[np.argmax(s)], pts[np.argmax(d)]])


class PlateAnalyzer:
    def __init__(self, template):
        self.t = template
        self.grid = template["grid"]
        self.rows, self.cols = self.grid["rows"], self.grid["cols"]
        self.tracks = template.get("tracks")          # 行式板型（如显色法真菌药敏板）
        self.new_style = bool(self.tracks)
        self.genus = None                             # 菌属（影响颜色判读规则）
        self.pw_mm, self.ph_mm = template["plate_mm"]
        self.well_r_px = 30
        self.warnings = []

    # ---------- 1. 板子检测与透视校正 ----------
    def find_plate(self, img):
        """在照片中找板子四边形（亮板 + 暗背景）。"""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            return None
        h, w = gray.shape[:2]
        area_min = 0.08 * h * w
        for c in sorted(cnts, key=cv2.contourArea, reverse=True):
            if cv2.contourArea(c) < area_min:
                continue
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.02 * peri, True)
            if len(approx) == 4:
                return order_corners(approx.reshape(4, 2).astype(np.float32))
        return None

    def warp(self, img, corners):
        ratio = self.pw_mm / self.ph_mm
        W = 1400
        H = int(round(W / ratio))
        dst = np.float32([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]])
        M = cv2.getPerspectiveTransform(corners, dst)
        warped = cv2.warpPerspective(img, M, (W, H))
        self.well_r_px = int(round(self.t["well_diameter_mm"] / 2 * W / self.pw_mm))
        return warped, M, (W, H)

    # ---------- 2. 孔位定位 ----------
    def template_centers(self, W, H):
        sx, sy = W / self.pw_mm, H / self.ph_mm
        ox, oy = self.grid.get("origin_mm", [14.38, 11.24])
        pitch = self.grid.get("pitch_mm", 9.0)
        pts = {}
        for i in range(self.rows):
            for j in range(self.cols):
                pts[f"{ROW_LETTERS[i]}{j + 1}"] = (
                    int(round((ox + j * pitch) * sx)), int(round((oy + i * pitch) * sy)))
        return pts

    def refine_centers(self, warped, pts):
        """用孔壁暗环的质心修正每个孔的模板中心；
        仅在修正量小且孔环对比确实提升时才接受（防止被孔间结构带偏）。
        Hough 圆直接定位的高可信网格已足够准确，跳过精修。"""
        if getattr(self, "grid_trusted", False):
            return dict(pts)
        gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (7, 7), 0)
        H, W = gray.shape
        hw = int(round(self.well_r_px * 1.1))
        r = max(8, int(self.well_r_px * 0.9))

        def ring_score(cx, cy):
            if cx - r < 2 or cy - r < 2 or cx + r + 3 > W or cy + r + 3 > H:
                return None
            c0 = float(gray[cy - 2:cy + 3, cx - 2:cx + 3].mean())
            rims = [float(gray[cy - 2:cy + 3, cx + r - 2:cx + r + 3].mean()),
                    float(gray[cy - 2:cy + 3, cx - r - 2:cx - r + 3].mean()),
                    float(gray[cy + r - 2:cy + r + 3, cx - 2:cx + 3].mean()),
                    float(gray[cy - r - 2:cy - r + 3, cx - 2:cx + 3].mean())]
            return float(np.mean(rims)) - c0

        refined = {}
        for pos, (cx, cy) in pts.items():
            win = gray[max(0, cy - hw):cy + hw + 1, max(0, cx - hw):cx + hw + 1]
            if win.size == 0:
                refined[pos] = (cx, cy)
                continue
            rim = win < (np.median(win) - 10)
            ys, xs = np.nonzero(rim)
            if len(xs) < 20:
                refined[pos] = (cx, cy)
                continue
            nx, ny = int(round(cx - hw + xs.mean())), int(round(cy - hw + ys.mean()))
            if abs(nx - cx) > 0.7 * self.well_r_px or abs(ny - cy) > 0.7 * self.well_r_px:
                refined[pos] = (cx, cy)          # 修正量过大：视为误吸，保留原中心
                continue
            s_old, s_new = ring_score(cx, cy), ring_score(nx, ny)
            if s_new is not None and (s_old is None or s_new > s_old + 2):
                refined[pos] = (nx, ny)
            else:
                refined[pos] = (cx, cy)
        return refined

    # ---------- 2b. 孔位网格自动校准 ----------
    def _fit_axis(self, prof, size):
        """单轴拟合：自相关求孔距 → 相位扫描选取整排孔位。"""
        pitch = self._pitch_of(prof)
        if pitch <= 0:
            return None, None
        sm = self._smooth(prof)
        lo, hi = size * 0.02, size * 0.95
        best_phi, best_score = 0, -1e18
        for phi in range(pitch):
            xs = np.arange(phi, size, pitch)
            xs = xs[(xs >= lo) & (xs <= hi)]
            if len(xs) == 0:
                continue
            sc = float(sm[xs].sum())
            if sc > best_score:
                best_phi, best_score = phi, sc
        pos = np.arange(best_phi, size, pitch)
        pos = pos[(pos >= lo) & (pos <= hi)]
        return pos, sm

    def _fit_from_profiles(self, prof_x, prof_y, W, H):
        px_, smx = self._fit_axis(prof_x, W)
        py_, smy = self._fit_axis(prof_y, H)
        if px_ is None or py_ is None:
            return None, 0.0

        def best_window(pos, sm, count):
            if len(pos) < count:
                return None
            best, bi = -1e18, 0
            for i in range(len(pos) - count + 1):
                sc = float(sm[pos[i:i + count]].sum())
                if sc > best:
                    best, bi = sc, i
            return pos[bi:bi + count]

        cols = best_window(px_, smx, self.cols)
        rows = best_window(py_, smy, self.rows)
        if cols is None or rows is None:
            return None, 0.0
        centers = {}
        for i in range(self.rows):
            for j in range(self.cols):
                centers[f"{ROW_LETTERS[i]}{j + 1}"] = (int(cols[j]), int(rows[i]))
        pitch_avg = (float(np.mean(np.diff(cols))) + float(np.mean(np.diff(rows)))) / 2
        return centers, pitch_avg

    @staticmethod
    def _pitch_of(p):
        q = p - p.mean()
        ac = np.correlate(q, q, "full")[len(q) - 1:]
        ac = ac / ac[0]
        lo, hi = 40, min(240, len(p) // 3)
        if hi <= lo:
            return 0
        return int(max(range(lo, hi), key=lambda L: ac[L]))

    @staticmethod
    def _smooth(v, k=5):
        return np.convolve(v.astype(np.float32), np.ones(k, np.float32) / k, mode="same")

    def hough_grid(self, warped):
        """Hough 圆检测直接定位孔位（孔环清晰的板最可靠），
        按行/列聚类并用匀距栅格补齐缺失行列。
        返回 (centers, pitch_avg, 圆点集) 或 (None, 0, None)。"""
        gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
        g = cv2.medianBlur(gray, 3)
        circles = cv2.HoughCircles(g, cv2.HOUGH_GRADIENT, dp=1.4, minDist=55,
                                   param1=120, param2=38, minRadius=26, maxRadius=62)
        if circles is None:
            return None, 0.0, None
        pts = np.round(circles[0]).astype(int)
        if len(pts) < self.rows * self.cols * 0.5:
            return None, 0.0, None

        def cluster(vals, tol):
            vals = np.sort(vals)
            groups, cur = [], [vals[0]]
            for v in vals[1:]:
                if v - cur[-1] <= tol:
                    cur.append(v)
                else:
                    groups.append(cur)
                    cur = [v]
            groups.append(cur)
            return [int(np.mean(g)) for g in groups if len(g) >= 2]

        col_cs = cluster(pts[:, 0], 28)
        row_cs = cluster(pts[:, 1], 28)
        if len(col_cs) < self.cols * 0.6 or len(row_cs) < self.rows * 0.6:
            return None, 0.0, None

        def fill(cs, count, size):
            if len(cs) == count:
                return cs
            diffs = np.diff(cs)
            p = int(round(np.median(diffs))) if len(diffs) else 0
            if p <= 0:
                return None
            best, bs = None, -1e18
            for st in range(cs[0] - 4 * p, cs[0] + 3 * p + 1):
                grid = [st + k * p for k in range(count)]
                hit_d = [min(abs(c - v) for v in grid) for c in cs]
                m = sum(1 for d in hit_d if d <= 12)
                outside = sum(1 for v in grid if v < 0 or v >= size)  # 越界格必有误
                sc = m - 0.02 * outside - 0.001 * sum(min(d, 12) for d in hit_d)
                if sc > bs:
                    bs, best = sc, grid
            return best

        cols = fill(col_cs, self.cols, gray.shape[1])
        rows = fill(row_cs, self.rows, gray.shape[0])
        if cols is None or rows is None:
            return None, 0.0, None
        centers = {}
        for i in range(self.rows):
            for j in range(self.cols):
                centers[f"{ROW_LETTERS[i]}{j + 1}"] = (int(cols[j]), int(rows[i]))
        pitch_avg = (float(np.mean(np.diff(cols))) + float(np.mean(np.diff(rows)))) / 2
        return centers, pitch_avg, pts

    def fit_grid(self, warped):
        """多来源拟合：原始暗度 / 高通滤波暗度（低对比度干板）/ Hough 圆检测。
        有 Hough 圆时按“孔位命中率”评判各候选；否则按孔对比度/孔环对比度评分。
        返回 (centers 或 None, 平均孔距px, 是否高可信)。"""
        gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY).astype(np.float32)
        H, W = gray.shape
        chans = (255.0 - gray, cv2.GaussianBlur(gray, (0, 0), 25) - gray)
        cands = []
        for dark in chans:
            fx = dark[int(H * 0.12):int(H * 0.88), :].mean(axis=0)
            fy = dark[:, int(W * 0.08):int(W * 0.92)].mean(axis=1)
            c, pitch = self._fit_from_profiles(fx, fy, W, H)
            if c is not None:
                cands.append((c, pitch))
        hc, hp_, pts = self.hough_grid(warped)
        if hc is not None:
            cands.append((hc, hp_))
        if not cands:
            return None, 0.0, False
        if len(cands) == 1:
            return cands[0][0], cands[0][1], bool(pts is not None and hc is not None)

        saved = self.well_r_px
        nominal = self.grid.get("pitch_mm", 9.0)
        best, best_sc, trusted = None, -1e18, False
        for c, pitch in cands:
            if pitch > 0:
                self.well_r_px = max(12, int(round(
                    pitch * (self.t["well_diameter_mm"] / nominal) / 2)))
            if pts is not None:                      # 匹配质量：Hough 圆与孔位中心的距离（越近越好）
                tol = 0.6 * max(20.0, pitch)
                cxs = np.array([v[0] for v in c.values()], np.float32)
                cys = np.array([v[1] for v in c.values()], np.float32)
                qsum = 0.0
                for (px_, py_) in pts[:, :2]:
                    dd = float(np.hypot(cxs - px_, cys - py_).min())
                    qsum += max(0.0, 1.0 - dd / tol)
                sc = qsum / len(pts)
            else:
                sc = max(self.grid_score(warped, c), self.grid_rim_score(warped, c))
            if sc > best_sc:
                best_sc = sc
                best = (c, pitch)
                trusted = bool(pts is not None and sc >= 0.8)
        self.well_r_px = saved
        return best[0], best[1], trusted

    def grid_score(self, warped, centers):
        """孔中心应比孔间缝隙暗——对比度越高，网格对位越可信。"""
        gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY).astype(np.float32)
        H, W = gray.shape
        xs = sorted({c[0] for c in centers.values()})
        ys = sorted({c[1] for c in centers.values()})
        hx = max(2, int(np.median(np.diff(xs)) / 2)) if len(xs) > 1 else 20
        hy = max(2, int(np.median(np.diff(ys)) / 2)) if len(ys) > 1 else 20

        def patch(px, py, r=3):
            if px - r < 0 or py - r < 0 or px + r + 1 > W or py + r + 1 > H:
                return None
            return float(gray[py - r:py + r + 1, px - r:px + r + 1].mean())

        vals = []
        for (cx, cy) in centers.values():
            ci = patch(cx, cy)
            gaps = [patch(cx + hx, cy), patch(cx - hx, cy),
                    patch(cx, cy + hy), patch(cx, cy - hy)]
            gaps = [g for g in gaps if g is not None]
            if ci is None or not gaps:
                continue
            vals.append(float(np.mean(gaps) - ci))
        return float(np.mean(vals)) if vals else -1e9

    def align_grid_offset(self, warped, centers):
        """对低对比度板（孔内与板面亮度接近）：用孔环(rim)暗于孔心的特性，
        全局搜索最佳偏移使每个孔的 环-心 亮度差最大。返回 (centers, 得分)。"""
        gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY).astype(np.float32)
        H, W = gray.shape
        xs = sorted({c[0] for c in centers.values()})
        ys = sorted({c[1] for c in centers.values()})
        if len(xs) < 2 or len(ys) < 2:
            return centers, -1e9
        px_ = max(8, int(np.median(np.diff(xs))))
        py_ = max(8, int(np.median(np.diff(ys))))
        r = max(8, int(self.well_r_px * 0.9))

        def patch(px, py, rad=3):
            if px - rad < 0 or py - rad < 0 or px + rad + 1 > W or py + rad + 1 > H:
                return None
            return float(gray[py - rad:py + rad + 1, px - rad:px + rad + 1].mean())

        def score_at(dx, dy):
            vals = []
            for (cx, cy) in centers.values():
                c0 = patch(cx + dx, cy + dy)
                rims = [patch(cx + dx + r, cy + dy), patch(cx + dx - r, cy + dy),
                        patch(cx + dx, cy + dy + r), patch(cx + dx, cy + dy - r)]
                rims = [v for v in rims if v is not None]
                if c0 is None or not rims:
                    continue
                vals.append(float(np.mean(rims) - c0))
            return float(np.mean(vals)) if vals else -1e9

        best = (0, 0)
        best_score = -1e9
        for dy in range(-py_ // 2, py_ // 2 + 1, 4):
            for dx in range(-px_ // 2, px_ // 2 + 1, 4):
                v = score_at(dx, dy)
                if v > best_score:
                    best_score, best = v, (dx, dy)
        # 粗定位后再微调 ±4px
        bx, by = best
        for dy in range(by - 4, by + 5, 2):
            for dx in range(bx - 4, bx + 5, 2):
                v = score_at(dx, dy)
                if v > best_score:
                    best_score, best = v, (dx, dy)
        out = {p: (c[0] + best[0], c[1] + best[1]) for p, c in centers.items()}
        return out, best_score

    def grid_rim_score(self, warped, centers):
        """孔环应比孔心暗——低对比度板（干板）的对位信号。"""
        gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY).astype(np.float32)
        H, W = gray.shape
        r = max(8, int(self.well_r_px * 0.9))

        def patch(px, py, rad=3):
            if px - rad < 0 or py - rad < 0 or px + rad + 1 > W or py + rad + 1 > H:
                return None
            return float(gray[py - rad:py + rad + 1, px - rad:px + rad + 1].mean())

        vals = []
        for (cx, cy) in centers.values():
            c0 = patch(cx, cy)
            rims = [patch(cx + r, cy), patch(cx - r, cy), patch(cx, cy + r), patch(cx, cy - r)]
            rims = [v for v in rims if v is not None]
            if c0 is None or not rims:
                continue
            vals.append(float(np.mean(rims) - c0))
        return float(np.mean(vals)) if vals else -1e9

    def locate_wells(self, warped):
        """优先用图像自动拟合网格（Hough 命中率或暗度/孔环信号达标）；
        否则孔环对齐模板；再退到纯模板。"""
        auto_centers, pitch_avg, trusted = self.fit_grid(warped)
        tmpl_centers = self.template_centers(*warped.shape[:2][::-1])
        nominal = self.grid.get("pitch_mm", 9.0)
        d_t = self.grid_score(warped, tmpl_centers)
        r_t = self.grid_rim_score(warped, tmpl_centers)
        self.grid_trusted = False
        if auto_centers is not None:
            r_save = self.well_r_px
            if pitch_avg > 0:
                self.well_r_px = max(12, int(round(
                    pitch_avg * (self.t["well_diameter_mm"] / nominal) / 2)))
            d_a = self.grid_score(warped, auto_centers)
            r_a = self.grid_rim_score(warped, auto_centers)
            if trusted or (d_a > 5 and d_a >= d_t) or (r_a > 5 and r_a >= r_t):
                self.grid_trusted = trusted
                return auto_centers, "auto", round(max(d_a, r_a), 1)
            self.well_r_px = r_save
        aligned, rim_score = self.align_grid_offset(warped, tmpl_centers)
        if rim_score > 8:
            return aligned, "aligned", round(rim_score, 1)
        return tmpl_centers, "template", round(d_t, 1)

    def _well_meta(self, pos):
        """返回 (role, 药物代码, 药物名, 浓度)。兼容两种模板格式。"""
        if self.new_style:
            for code, tr in self.tracks.items():
                if pos in tr["wells"]:
                    return "drug", code, tr["name"], tr["wells"][pos]
            ctrls = self.t.get("controls", {})
            pc = ctrls.get("positive")
            nc = ctrls.get("negative")
            pcs = pc if isinstance(pc, list) else ([pc] if pc else [])
            ncs = nc if isinstance(nc, list) else ([nc] if nc else [])
            if pos in pcs:
                return "positive_control", None, None, None
            if pos in ncs:
                return "negative_control", None, None, None
            return "unused", None, None, None
        det = self.t["detection"]
        if pos == det["positive_control"]:
            return "positive_control", None, None, None
        if pos == det["negative_control"]:
            return "negative_control", None, None, None
        drug = self.t["drugs"].get(str(int(pos[1:])))
        if not drug:
            return "unused", None, None, None
        return "drug", drug["code"], drug["name"], drug["conc"][ROW_LETTERS.index(pos[0])]

    # ---------- 3. 颜色提取与生长判定 ----------
    def classify(self, warped, centers):
        hsv = cv2.cvtColor(warped, cv2.COLOR_BGR2HSV)
        H, W = hsv.shape[:2]
        yy, xx = np.mgrid[0:H, 0:W]
        rr = int(round(self.well_r_px * 0.42))          # 取孔中心小圆区域，避开孔壁

        def well_stats(pos):
            cx, cy = centers[pos]
            mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= rr * rr
            px = hsv[mask]
            return None if px.shape[0] < 50 else np.median(px, axis=0)

        if self.new_style and self.t.get("detection", {}).get("mode") == "growth":
            # 无显色剂微量肉汤板：按孔内浊度（孔比周围板面暗的程度）判定生长
            gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY).astype(np.float32)
            Hg, Wg = gray.shape
            yy, xx = np.mgrid[0:Hg, 0:Wg]
            rg = int(round(self.well_r_px * 0.55))
            gap_half = int(round(self.well_r_px * 1.3))          # 跟随实际孔距：半孔距 ≈ 1.3R

            def gap_patch(px, py, rad=3):
                if px - rad < 0 or py - rad < 0 or px + rad + 1 > Wg or py + rad + 1 > Hg:
                    return None
                return float(gray[py - rad:py + rad + 1, px - rad:px + rad + 1].mean())

            raw, spots = {}, {}
            sq = int(round(self.well_r_px * 2.6))
            mrad = max(6, int(round(self.well_r_px * 0.72)))
            yy2, xx2 = np.mgrid[0:sq, 0:sq]
            cmask = (xx2 - sq / 2) ** 2 + (yy2 - sq / 2) ** 2 <= mrad * mrad
            for pos in centers:
                cx, cy = centers[pos]
                mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= rg * rg
                px = gray[mask]
                if px.size < 50:
                    raw[pos] = None
                    spots[pos] = None
                    continue
                bg = float(np.median(px))
                col, rowL = int(pos[1:]), pos[0]
                gaps = []
                for dx, dy, d in ((gap_half, 0, "r"), (-gap_half, 0, "l"),
                                  (0, gap_half, "d"), (0, -gap_half, "u")):
                    if d == "l" and col == 1:      # 边缘孔：不向板外方向采样（避免板壁/阴影干扰）
                        continue
                    if d == "r" and col == 12:
                        continue
                    if d == "u" and rowL == "A":
                        continue
                    if d == "d" and rowL == "H":
                        continue
                    v = gap_patch(cx + dx, cy + dy)
                    if v is not None and v > bg - 60:   # 剔除落入背景/阴影的采样点
                        gaps.append(v)
                raw[pos] = (float(np.mean(gaps)) - bg) if gaps else 0.0  # 浊度：孔比周围暗的程度
                x0, y0 = cx - sq // 2, cy - sq // 2
                if x0 >= 0 and y0 >= 0 and x0 + sq <= Wg and y0 + sq <= Hg:
                    p = gray[y0:y0 + sq, x0:x0 + sq]
                    det = p - cv2.GaussianBlur(p, (0, 0), max(6.0, self.well_r_px * 0.6))
                    spots[pos] = float((det[cmask] < -8).mean())   # 暗点占比：孔内点状菌落
                else:
                    spots[pos] = None
            ctrls2 = self.t.get("controls", {})
            pc2 = ctrls2.get("positive")
            pcs2 = pc2 if isinstance(pc2, list) else ([pc2] if pc2 else [])

            def _median_of(dd):
                vals = [dd.get(p_) for p_ in pcs2 if dd.get(p_) is not None]
                return float(np.median(vals)) if vals else None

            ref = _median_of(raw)
            ref_sp = _median_of(spots)
            wells = {}
            for pos in centers:
                role, code, _, conc = self._well_meta(pos)
                rec = {"pos": pos, "role": role}
                if role == "drug":
                    rec["drug"] = code
                    rec["conc"] = conc
                if role == "unused":
                    rec.update(status="unused", growth=None, score=None, confidence=None)
                    wells[pos] = rec
                    continue
                sc, sp = raw.get(pos), spots.get(pos)
                if sc is None:
                    rec.update(status="unknown", growth=None, score=None, confidence=0.0)
                elif ref and ref > 1e-6:
                    r2 = max(0.0, sc) / ref                # 浊度：相对阳性对照
                    if sp is not None and ref_sp and ref_sp > 1e-6:
                        ratio = max(r2, sp / ref_sp)       # 暗点：任一形态达标即生长（CLSI M27：≥50% 为生长）
                    else:
                        ratio = r2
                    if ratio >= 0.5:
                        cls = "growth"
                        conf = min(0.95, 0.55 + (ratio - 0.5) * 0.6)
                    elif ratio <= 0.3:
                        cls = "no_growth"
                        conf = min(0.95, 0.55 + (0.3 - ratio) * 0.6)
                    else:
                        cls, conf = "partial", 0.35
                    rec.update(status=cls, growth=(cls == "growth"),
                               score=round(float(ratio), 2), confidence=round(float(conf), 2))
                else:
                    cls = "growth" if sc >= 10 else ("no_growth" if sc <= 3 else "partial")
                    rec.update(status=cls, growth=(cls == "growth"),
                               score=round(float(sc), 1), confidence=0.5)
                wells[pos] = rec
            return wells

        if self.new_style:
            # 行式板型（显色法等板型）：颜色三分类，规则由模板给出
            wells = {}
            for pos in centers:
                role, code, _, conc = self._well_meta(pos)
                rec = {"pos": pos, "role": role}
                if role == "drug":
                    rec["drug"] = code
                    rec["conc"] = conc
                if role == "unused":
                    rec.update(status="unused", growth=None, score=None, confidence=None)
                    wells[pos] = rec
                    continue
                stat = well_stats(pos)
                if stat is None:
                    rec.update(status="unknown", growth=None, score=None, confidence=0.0)
                else:
                    h, s = float(stat[0]), float(stat[1])
                    if (h >= 150 or h <= 15) and s > 55:         # 粉红/红色：生长
                        cls = "growth"
                        margin = (h - 150) if h >= 150 else (15 - h)
                        conf = min(0.95, 0.55 + margin * 0.02)
                    elif h <= 138 and s > 40:                    # 蓝：无生长
                        cls = "no_growth"
                        conf = min(0.95, 0.55 + (138 - h) * 0.02)
                    else:                                        # 紫/临界：按模板规则判定
                        cls, conf = "partial", 0.35
                    rec.update(status=cls, growth=(cls == "growth"),
                               score=None, confidence=round(float(conf), 2))
                wells[pos] = rec
            return wells

        det = self.t["detection"]
        pos_ctrl, neg_ctrl = det["positive_control"], det["negative_control"]
        pos_hsv, neg_hsv = well_stats(pos_ctrl), well_stats(neg_ctrl)

        def cdist(a, b):
            dh = min(abs(a[0] - b[0]), 180 - abs(a[0] - b[0])) / 180.0
            ds = abs(a[1] - b[1]) / 255.0
            dv = abs(a[2] - b[2]) / 255.0
            return float(np.sqrt(dh * dh + 0.3 * ds * ds + 0.2 * dv * dv))

        use_fallback = False
        if pos_hsv is None or neg_hsv is None:
            self.warnings.append("对照孔定位失败，改用绝对颜色阈值判读，结果仅供参考")
            use_fallback = True
        elif cdist(pos_hsv, neg_hsv) < 0.25:
            self.warnings.append("阳性/阴性对照孔颜色过于接近，改用绝对颜色阈值判读，结果仅供参考")
            use_fallback = True
        elif (90 <= pos_hsv[0] <= 140 and pos_hsv[1] > 50
              and neg_hsv[0] >= 150 and neg_hsv[1] > 50):
            self.warnings.append("对照孔颜色与模板设定的极性相反，已改用绝对颜色阈值判读（请核对模板中对照孔位置）")
            use_fallback = True

        drug_cols = {int(k): v for k, v in self.t["drugs"].items()}
        wells = {}
        for pos in centers:
            row_i = ROW_LETTERS.index(pos[0])
            col_j = int(pos[1:])
            if pos == pos_ctrl:
                role = "positive_control"
            elif pos == neg_ctrl:
                role = "negative_control"
            elif col_j in drug_cols:
                role = "drug"
            else:
                role = "unused"
            rec = {"pos": pos, "role": role}
            if role == "drug":
                rec["drug"] = drug_cols[col_j]["code"]
                rec["conc"] = drug_cols[col_j]["conc"][row_i]
            if role == "unused":
                rec.update(status="unused", growth=None, score=None, confidence=None)
                wells[pos] = rec
                continue
            stat = well_stats(pos)
            if stat is None:
                rec.update(status="unknown", growth=None, score=None, confidence=0.0)
            elif use_fallback:
                h, s = float(stat[0]), float(stat[1])
                is_pink = ((h >= 150.0) or (h <= 15.0)) and (s > 60.0)   # 刃天青还原后的粉红/红
                rec.update(status="growth" if is_pink else "no_growth",
                           growth=is_pink, score=round(1.0 if is_pink else 0.0, 2),
                           confidence=0.5)
            else:
                dp, dn = cdist(stat, pos_hsv), cdist(stat, neg_hsv)
                score = dn / (dp + dn + 1e-6)                  # 距阴性对照越近 → 越像"无生长"
                growth = score > 0.5
                rec.update(status="growth" if growth else "no_growth",
                           growth=bool(growth), score=round(score, 2),
                           confidence=round(min(1.0, abs(score - 0.5) * 2.0), 2))
            wells[pos] = rec
        return wells

    # ---------- 4. MIC 推导 ----------
    def derive_mic(self, wells):
        if self.new_style:
            return self._derive_mic_tracks(wells)
        extra = []
        det = self.t["detection"]
        pc, nc = wells[det["positive_control"]], wells[det["negative_control"]]
        controls = {
            det["positive_control"]: {"role": "阳性对照", "growth": pc.get("growth"),
                                      "ok": pc.get("growth") is True},
            det["negative_control"]: {"role": "阴性对照", "growth": nc.get("growth"),
                                      "ok": nc.get("growth") is False},
        }
        if not controls[det["positive_control"]]["ok"]:
            extra.append("阳性对照孔未判为生长：菌株可能未生长或接种失败，本次结果不可信")
        if not controls[det["negative_control"]]["ok"]:
            extra.append("阴性对照孔判为生长：可能存在污染，本次结果不可信")

        mic_list = []
        for col_s, drug in sorted(self.t["drugs"].items(), key=lambda kv: int(kv[0])):
            col = int(col_s)
            concs = drug["conc"]
            states = [bool(wells[f"{ROW_LETTERS[i]}{col}"].get("growth"))
                      for i in range(self.rows)]
            first = next((i for i, g in enumerate(states) if not g), None)
            if first is None:
                mic_disp, row, note = f">{concs[0]}", None, "全部孔均有生长，超出检测上限"
            elif first == 0:
                mic_disp, row, note = f"≤{concs[-1]}", 0, "全部孔均无生长，低于检测下限"
            else:
                mic_disp, row, note = str(concs[first]), first, ""
            flags = []
            if any(states[i] and not states[i - 1] for i in range(1, len(states))):
                flags.append("跳跃孔（低浓度无生长、高浓度有生长），建议人工复核")
            confs = [wells[f"{ROW_LETTERS[i]}{col}"].get("confidence") or 0
                     for i in range(self.rows)]
            mic_list.append({
                "col": col, "code": drug["code"], "name": drug["name"],
                "mic": mic_disp, "mic_row": ROW_LETTERS[row] if row is not None else None,
                "confidence": round(sum(confs) / len(confs), 2),
                "flags": flags, "note": note,
            })
        return mic_list, controls, extra

    def _derive_mic_tracks(self, wells):
        """行式板型（显色法等板型）：按药物序列 + 说明书规则判读 MIC。"""
        extra = []
        ctrls = self.t.get("controls", {})
        controls = {}
        pc = ctrls.get("positive")
        nc = ctrls.get("negative")
        for p_ in (pc if isinstance(pc, list) else ([pc] if pc else [])):
            if p_ not in wells:
                continue
            g = wells[p_].get("status") == "growth"
            controls[p_] = {"role": "阳性对照", "growth": g, "ok": g}
            if not g:
                extra.append(f"阳性对照孔 {p_} 未显示生长（应生长）：应延长培养；仍不生长则终止试验并查找原因")
        for p_ in (nc if isinstance(nc, list) else ([nc] if nc else [])):
            if p_ not in wells:
                continue
            g = wells[p_].get("status") == "growth"
            controls[p_] = {"role": "阴性对照", "growth": g, "ok": not g}
            if g:
                extra.append(f"阴性对照孔 {p_} 出现生长迹象（应无生长）：可能存在污染，结果不可信")

        genus_rule = (self.t.get("genus_rules") or {}).get(self.genus or "", {})
        purple_growth = set(genus_rule.get("purple_as_growth")
                            or self.t.get("detection", {}).get("purple_as_growth_for", []))
        mic_list = []
        partial_count = 0
        for code, tr in self.tracks.items():
            positions = list(tr["wells"].keys())
            concs = [tr["wells"][p] for p in positions]
            n = len(positions)
            states = []
            for pos in positions:
                st = wells.get(pos, {}).get("status")
                if st == "growth":
                    states.append(True)
                elif st == "no_growth":
                    states.append(False)
                elif st == "partial":
                    partial_count += 1
                    states.append(code in purple_growth)     # 紫色：仅指定药物按生长计
                else:
                    states.append(None)

            def wrongs(k):
                out = []
                for i in range(k):
                    if states[i] is True:
                        out.append((positions[i], "生长"))
                for i in range(k, n):
                    if states[i] is False:
                        out.append((positions[i], "无生长"))
                return out

            best_k, best_w = 0, None
            for k in range(n + 1):
                w = wrongs(k)
                if best_w is None or len(w) < len(best_w) or (len(w) == len(best_w) and k > best_k):
                    best_k, best_w = k, w

            flags = []
            unknown = [positions[i] for i in range(n) if states[i] is None]
            if unknown:
                flags.append("存在无法判读的孔：" + "、".join(unknown))
            if len(best_w) >= 2:
                desc = "、".join(f"{p}({t_})" for p, t_ in best_w)
                flags.append(f"不连续孔 {len(best_w)} 个（{desc}），按说明书结果无效，需重新试验")
                mic, mic_well, note = "无效", None, "跳孔超过 1 个"
            else:
                if len(best_w) == 1:
                    p, t_ = best_w[0]
                    flags.append(f"存在 1 个不连续孔（{p}={t_}），已按说明书忽略")
                if best_k == 0:
                    mic, mic_well, note = ">" + concs[0], None, "全部孔均有生长，超出检测上限"
                elif best_k == n:
                    mic, mic_well, note = "≤" + concs[-1], positions[-1], "全部孔均无生长，低于检测下限"
                else:
                    mic, mic_well, note = concs[best_k - 1], positions[best_k - 1], ""
            confs = [wells.get(p, {}).get("confidence") or 0.5 for p in positions]
            mic_list.append({
                "code": code, "name": tr["name"], "mic": mic, "mic_well": mic_well,
                "confidence": round(sum(confs) / len(confs), 2),
                "flags": flags, "note": note,
                "options": [">" + concs[0]] + concs + ["≤" + concs[-1], "无效"],
            })

        if partial_count >= 10:
            if self.new_style and self.t.get("detection", {}).get("mode") == "growth":
                extra.append(f"共 {partial_count} 个孔生长状态介于生长/未生长之间（临界）：可能培养时间不足、接种量偏低或光照不均，请人工复核")
            else:
                extra.append(f"共 {partial_count} 个孔呈临界色（紫色或颜色不明显）：可能板型不匹配、培养时间不足或光照异常，请人工复核")
        return mic_list, controls, extra

    # ---------- 5. 标注图 ----------
    @staticmethod
    def _find_font():
        for p in (r"C:/Windows/Fonts/msyh.ttc", r"C:/Windows/Fonts/simhei.ttf",
                  r"C:/Windows/Fonts/simsun.ttc"):
            if os.path.exists(p):
                return p
        return None

    def _annotate(self, img, mapper, centers, wells, mic_list):
        if not HAS_PIL:
            return img

        def radius_at(c):
            cx, cy = c
            p0 = mapper((cx, cy))
            p1 = mapper((cx + self.well_r_px * 0.8, cy))
            return float(np.hypot(p1[0] - p0[0], p1[1] - p0[1])) * 1.25

        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        base = Image.fromarray(rgb).convert("RGBA")
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        font_path = self._find_font()
        try:
            font = ImageFont.truetype(font_path, 26) if font_path else ImageFont.load_default()
            font_s = ImageFont.truetype(font_path, 20) if font_path else ImageFont.load_default()
        except Exception:
            font = font_s = ImageFont.load_default()

        for pos, w_ in wells.items():
            c = mapper(centers[pos])
            r = radius_at(centers[pos])
            bbox = [c[0] - r, c[1] - r, c[0] + r, c[1] + r]
            st = w_.get("status")
            if st == "growth":
                d.ellipse(bbox, fill=(255, 110, 150, 70), outline=(255, 60, 110, 220), width=3)
            elif st == "no_growth":
                d.ellipse(bbox, fill=(80, 140, 255, 70), outline=(40, 100, 230, 220), width=3)
            elif st == "unknown":
                d.ellipse(bbox, outline=(150, 150, 150, 200), width=3)
            else:
                d.ellipse(bbox, outline=(120, 120, 120, 160), width=1)
            if w_.get("role") in ("positive_control", "negative_control"):
                label = "阳" if w_["role"] == "positive_control" else "阴"
                d.text((c[0] - r, c[1] - r - 28), label, font=font_s,
                       fill=(255, 200, 0, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))

        if self.new_style:
            for m in mic_list:
                wp = m.get("mic_well")
                if wp and wp in centers:
                    cx, cy = mapper(centers[wp])
                    rr = radius_at(centers[wp])
                    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                              outline=(46, 139, 87, 255), width=5)
                    d.text((cx + rr * 1.1, cy - rr * 1.2), f"{m['code']}={m['mic']}", font=font_s,
                           fill=(46, 139, 87, 255), stroke_width=2, stroke_fill=(255, 255, 255, 255))
        else:
            for m in mic_list:
                col = m["col"]
                bx, by = mapper(centers[f"H{col}"])
                br = radius_at(centers[f"H{col}"])
                d.text((bx - br * 0.9, by + br * 1.1), m["code"], font=font_s,
                       fill=(255, 255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0, 255))
                if m["mic_row"]:
                    wp = f"{m['mic_row']}{col}"
                    cx, cy = mapper(centers[wp])
                    rr = radius_at(centers[wp])
                    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                              outline=(46, 139, 87, 255), width=5)
                    d.text((cx + rr * 1.1, cy - rr * 1.2), "MIC=" + m["mic"], font=font_s,
                           fill=(46, 139, 87, 255), stroke_width=2, stroke_fill=(255, 255, 255, 255))
                else:
                    d.text((bx - br * 0.9, by + br * 2.6), m["mic"], font=font_s,
                           fill=(255, 140, 0, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))

        if self.warnings:
            h = base.size[1]
            band_h = 34 * len(self.warnings) + 12
            d.rectangle([0, h - band_h, base.size[0], h], fill=(0, 0, 0, 150))
            for i, wmsg in enumerate(self.warnings):
                d.text((16, h - band_h + 8 + 34 * i), "⚠ " + wmsg, font=font_s,
                       fill=(255, 210, 120, 255))

        out = Image.alpha_composite(base, overlay).convert("RGB")
        return cv2.cvtColor(np.array(out), cv2.COLOR_RGB2BGR)

    # ---------- 6. 主流程 ----------
    def run(self, img_path, outdir=None, sample_no="", genus=None):
        self.genus = genus
        self.warnings = []
        outdir = outdir or os.path.join(HERE, "outputs")
        os.makedirs(outdir, exist_ok=True)

        img = read_image(img_path)
        corners = self.find_plate(img)
        if corners is None:
            raise ValueError("未检测到药敏板：请让板子占满画面、背景与板子反差明显后重新拍摄")

        warped, M, _ = self.warp(img, corners)
        gray_w = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
        blur_score = float(cv2.Laplacian(gray_w, cv2.CV_64F).var())
        if blur_score < 40:
            self.warnings.append(f"照片可能模糊（清晰度 {blur_score:.0f}），建议重新拍摄")
        centers, grid_source, grid_score = self.locate_wells(warped)
        if grid_source == "template" and grid_score < 5:
            self.warnings.append("孔位自动校准未生效（按模板假定边距定位），结果可能不准")
        centers = self.refine_centers(warped, centers)
        wells = self.classify(warped, centers)
        mic_list, controls, extra = self.derive_mic(wells)
        self.warnings.extend(extra)

        stem = os.path.splitext(os.path.basename(img_path))[0]
        M_inv = cv2.invert(M)[1]

        def back(pt):
            q = cv2.perspectiveTransform(np.array([[[pt[0], pt[1]]]], np.float32), M_inv)[0][0]
            return float(q[0]), float(q[1])

        annotated = self._annotate(img, back, centers, wells, mic_list)
        ann_path = os.path.join(outdir, f"{stem}_annotated.png")
        write_image(ann_path, annotated)
        warped_view = self._annotate(warped, lambda p: (float(p[0]), float(p[1])),
                                     centers, wells, mic_list)
        grid_path = os.path.join(outdir, f"{stem}_warped.png")
        write_image(grid_path, warped_view)

        well_list = sorted(wells.values(),
                           key=lambda r: (int(r["pos"][1:]), r["pos"][0]))
        return {
            "sample": {"file": os.path.basename(img_path), "sample_no": sample_no,
                       "template": self.t["name"], "date": datetime.date.today().isoformat(),
                       "blur_score": round(blur_score, 1),
                       "plate_detected": True, "grid_source": grid_source},
            "warnings": self.warnings,
            "controls": controls,
            "mic": mic_list,
            "wells": well_list,
            "annotated": ann_path,
            "warped": grid_path,
        }


# ---------- Excel 导出 ----------
def export_excel(result, out_path=None):
    if not HAS_OPENPYXL:
        raise RuntimeError("缺少 openpyxl，无法导出 Excel")
    wb = Workbook()
    thin = Side(style="thin", color="BBBBBB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    center = Alignment(horizontal="center", vertical="center")

    # Sheet1: MIC 汇总
    ws = wb.active
    ws.title = "MIC汇总"
    ws.merge_cells("A1:G1")
    ws["A1"] = f"药敏板判读结果 — {result['sample']['template']}"
    ws["A1"].font = Font(bold=True, size=14)
    info = [
        ("样本编号", result["sample"].get("sample_no") or "待填写"),
        ("检测日期", result["sample"]["date"]),
        ("照片文件", result["sample"]["file"]),
        ("判读方式", "软件辅助判读（结果请人工复核）"),
        ("对照孔", " / ".join(f"{p} {v['role']}: {'正常' if v['ok'] else '异常'}"
                             for p, v in result["controls"].items())),
    ]
    for r, (k, v) in enumerate(info, start=3):
        ws.cell(row=r, column=1, value=k).font = Font(bold=True)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
        ws.cell(row=r, column=2, value=v)

    head_row = len(info) + 4
    headers = ["序号", "药物代码", "药物名称", "MIC (µg/mL)", "置信度", "备注"]
    for c, htxt in enumerate(headers, start=1):
        cell = ws.cell(row=head_row, column=c, value=htxt)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = center
    for i, m in enumerate(result["mic"], start=1):
        note = "；".join(m["flags"]) or m["note"]
        row_vals = [i, m["code"], m["name"], m["mic"], f"{m['confidence'] * 100:.0f}%", note]
        for c, v in enumerate(row_vals, start=1):
            cell = ws.cell(row=head_row + i, column=c, value=v)
            cell.border = border
            cell.alignment = center if c != 6 else Alignment(horizontal="left", vertical="center")
    for i, wmsg in enumerate(result["warnings"], start=1):
        cell = ws.cell(row=head_row + len(result["mic"]) + 1 + i, column=1,
                       value=f"警告: {wmsg}")
        cell.font = Font(color="CC5500")
        ws.merge_cells(start_row=cell.row, start_column=1, end_row=cell.row, end_column=6)
    for col_letter, width in zip("ABCDEFG", [6, 10, 18, 14, 10, 46]):
        ws.column_dimensions[col_letter].width = width

    # Sheet2: 逐孔明细
    ws2 = wb.create_sheet("逐孔明细")
    headers2 = ["孔位", "角色", "药物", "浓度 (µg/mL)", "判读", "生长分数", "置信度"]
    role_names = {"drug": "药物", "positive_control": "阳性对照",
                  "negative_control": "阴性对照", "unused": "未使用"}
    for c, htxt in enumerate(headers2, start=1):
        cell = ws2.cell(row=1, column=c, value=htxt)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = center
    for i, w_ in enumerate(result["wells"], start=2):
        status_names = {"growth": "生长", "no_growth": "无生长",
                        "unused": "未使用", "unknown": "未知", "partial": "紫色(临界)"}
        row_vals = [w_["pos"], role_names.get(w_["role"], w_["role"]),
                    w_.get("drug", ""), w_.get("conc", ""),
                    status_names.get(w_.get("status"), ""),
                    w_.get("score"), w_.get("confidence")]
        for c, v in enumerate(row_vals, start=1):
            cell = ws2.cell(row=i, column=c, value=v)
            cell.border = border
            cell.alignment = center
    for col_letter, width in zip("ABCDEFG", [8, 12, 10, 14, 10, 10, 10]):
        ws2.column_dimensions[col_letter].width = width

    if out_path:
        wb.save(out_path)
        return out_path
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def merge_mic(plate_results):
    """同一菌株多张照片的 MIC 合并。

    同一药物代码：各图结果一致 → 合并为一行并保留全部来源照片；
    不一致 → 每张图各占一行并标记「多图结果不一致」。
    """
    groups = {}
    for res in plate_results:
        fname = res["sample"]["file"]
        for m in res["mic"]:
            groups.setdefault(m["code"], []).append((m, fname))
    merged = []
    for code in sorted(groups):
        entries = groups[code]
        name = entries[0][0]["name"]
        vals = sorted({m["mic"] for m, _ in entries})
        if len(vals) == 1:
            m0 = entries[0][0]
            note = "；".join(m0["flags"]) or m0["note"]
            merged.append({
                "code": code, "name": name, "mic": m0["mic"],
                "sources": [f for _, f in entries],
                "confidence": round(sum(m["confidence"] for m, _ in entries) / len(entries), 2),
                "note": note, "conflict": False})
        else:
            for m, f in entries:
                extra = "；".join(m["flags"]) or m["note"]
                note = "多图结果不一致，请人工复核"
                if extra:
                    note += "；" + extra
                merged.append({
                    "code": code, "name": name, "mic": m["mic"],
                    "sources": [f], "confidence": m["confidence"],
                    "note": note, "conflict": True})
    return merged


def export_excel_batch(plate_results, sample_no="", out_path=None):
    """同一菌株多张照片导出：合并 MIC 汇总 + 全部逐孔明细。"""
    if not HAS_OPENPYXL:
        raise RuntimeError("缺少 openpyxl，无法导出 Excel")
    wb = Workbook()
    thin = Side(style="thin", color="BBBBBB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    center = Alignment(horizontal="center", vertical="center")

    ws = wb.active
    ws.title = "MIC汇总(合并)"
    ws.merge_cells("A1:G1")
    ws["A1"] = "药敏板判读结果（同一菌株 · 多张照片合并）"
    ws["A1"].font = Font(bold=True, size=14)
    files_txt = "、".join(r["sample"]["file"] for r in plate_results)
    info = [
        ("样本编号", sample_no or "待填写"),
        ("检测日期", datetime.date.today().isoformat()),
        ("照片数量", f"{len(plate_results)} 张：{files_txt}"),
        ("判读方式", "软件辅助判读（结果请人工复核）"),
    ]
    for row_i, (k, v) in enumerate(info, start=3):
        ws.cell(row=row_i, column=1, value=k).font = Font(bold=True)
        ws.merge_cells(start_row=row_i, start_column=2, end_row=row_i, end_column=6)
        ws.cell(row=row_i, column=2, value=v)

    head_row = len(info) + 4
    headers = ["序号", "药物代码", "药物名称", "MIC (µg/mL)", "置信度", "来源照片", "备注"]
    for col_i, htxt in enumerate(headers, start=1):
        cell = ws.cell(row=head_row, column=col_i, value=htxt)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = center

    merged = merge_mic(plate_results)
    for i, m in enumerate(merged, start=1):
        row_vals = [i, m["code"], m["name"], m["mic"], f"{m['confidence'] * 100:.0f}%",
                    "、".join(m["sources"]), m["note"]]
        for col_i, v in enumerate(row_vals, start=1):
            cell = ws.cell(row=head_row + i, column=col_i, value=v)
            cell.border = border
            cell.alignment = center if col_i not in (6, 7) else Alignment(
                horizontal="left", vertical="center")
        if m["conflict"]:
            for col_i in (4, 7):
                ws.cell(row=head_row + i, column=col_i).font = Font(color="CC0000", bold=True)

    row_i = head_row + len(merged) + 2
    for p in plate_results:
        for wmsg in p["warnings"]:
            cell = ws.cell(row=row_i, column=1,
                           value=f"警告（{p['sample']['file']}）: {wmsg}")
            cell.font = Font(color="CC5500")
            ws.merge_cells(start_row=row_i, start_column=1, end_row=row_i, end_column=7)
            row_i += 1
    for col_letter, width in zip("ABCDEFG", [6, 10, 18, 14, 10, 30, 46]):
        ws.column_dimensions[col_letter].width = width

    ws2 = wb.create_sheet("逐孔明细")
    headers2 = ["来源照片", "孔位", "角色", "药物", "浓度 (µg/mL)", "判读", "生长分数", "置信度"]
    role_names = {"drug": "药物", "positive_control": "阳性对照",
                  "negative_control": "阴性对照", "unused": "未使用"}
    status_names = {"growth": "生长", "no_growth": "无生长",
                    "unused": "未使用", "unknown": "未知", "partial": "紫色(临界)"}
    for col_i, htxt in enumerate(headers2, start=1):
        cell = ws2.cell(row=1, column=col_i, value=htxt)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = center
    row_i = 2
    for p in plate_results:
        for w_ in p["wells"]:
            row_vals = [p["sample"]["file"], w_["pos"], role_names.get(w_["role"], w_["role"]),
                        w_.get("drug", ""), w_.get("conc", ""),
                        status_names.get(w_.get("status"), ""),
                        w_.get("score"), w_.get("confidence")]
            for col_i, v in enumerate(row_vals, start=1):
                cell = ws2.cell(row=row_i, column=col_i, value=v)
                cell.border = border
                cell.alignment = center
            row_i += 1
    for col_letter, width in zip("ABCDEFGH", [26, 8, 12, 10, 14, 10, 10, 10]):
        ws2.column_dimensions[col_letter].width = width

    if out_path:
        wb.save(out_path)
        return out_path
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def qc_in_range(value, range_str):
    """判断 MIC 值是否在质控范围（'lo-hi'）内。支持 '>x' / '≤x' 形式。返回 True/False/None。"""
    try:
        lo, hi = [float(x) for x in str(range_str).replace("～", "-").split("-")]
    except Exception:
        return None
    s = str(value).strip()
    try:
        if s.startswith(">"):
            return float(s[1:]) < hi
        if s.startswith("≤") or s.startswith("<"):
            return float(s.lstrip("≤<")) >= lo
        return lo <= float(s) <= hi
    except Exception:
        return None


def export_record_sheet(plates, template, meta=None, qc=None, out_path=None):
    """按实验室登记表格式导出：质控范围行 + 每张照片一行（含时间点与复核状态）。"""
    if not HAS_OPENPYXL:
        raise RuntimeError("缺少 openpyxl，无法导出 Excel")
    meta = meta or {}
    tracks = template.get("tracks", {})
    codes = list(tracks.keys())
    lower_codes = set((template.get("halves") or {}).get("lower") or [])
    thin = Side(style="thin", color="BBBBBB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    qc_fill = PatternFill("solid", fgColor="FFF2CC")
    center = Alignment(horizontal="center", vertical="center")
    red_font = Font(color="CC0000", bold=True)

    wb = Workbook()
    ws = wb.create_sheet("MIC登记表")
    wsr = wb.active
    wsr.title = "MIC报告"
    ncols = 5 + len(codes) + 2

    # ---- Sheet：MIC 报告（简洁长表：菌株/菌属/菌种/药物/MIC）----
    headers_r = ["菌株编号", "菌属", "菌种", "培养信息", "时间点", "实验批次", "操作人",
                 "照片", "药物", "药物名称", "MIC (µg/mL)", "复核", "质控", "备注"]
    for i, htxt in enumerate(headers_r, start=1):
        cell = wsr.cell(1, i, htxt)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = center
    rr = 2
    for p in plates:
        s = p.get("sample", {})
        tp = s.get("time_point") or ""
        ranges = (qc.get("ranges") or {}).get(tp, {}) if qc else {}
        for m in p.get("mic", []):
            final = m.get("final_mic", m["mic"])
            rng = (ranges or {}).get(m["code"], "")
            qc_mark = ""
            if rng:
                ok_ = qc_in_range(final, rng)
                qc_mark = "在控" if ok_ else ("超出" if ok_ is False else "")
            note = "；".join(m.get("flags") or []) or m.get("note", "")
            sid = meta.get("sample_no") or s.get("sample_no") or ""
            if m["code"] in lower_codes and meta.get("strain2"):
                sid = meta["strain2"]                     # B板下半区（第二个菌株）用下半区菌号
                note = ("下半区；" + note) if note else "下半区（B板）"
            row_vals = [sid,
                        meta.get("genus_label") or "",
                        meta.get("species") or "",
                        meta.get("culture_info") or "",
                        tp or "未标注",
                        meta.get("batch_no") or "",
                        meta.get("operator") or "",
                        s.get("file", ""),
                        m["code"], m["name"], final,
                        "已复核" if s.get("reviewed") else "未复核",
                        qc_mark, note]
            for i, v in enumerate(row_vals, start=1):
                cell = wsr.cell(rr, i, v)
                cell.border = border
                cell.alignment = center if i not in (1, 8, 14) else Alignment(
                    horizontal="left", vertical="center")
            if qc_mark == "超出":
                wsr.cell(rr, 13).font = red_font
            rr += 1
    for col_letter, width in zip("ABCDEFGHIJKLMN",
                                 [18, 10, 12, 14, 8, 12, 10, 22, 7, 14, 12, 8, 8, 40]):
        wsr.column_dimensions[col_letter].width = width
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    ws.cell(1, 1, "真菌药敏板判读记录表（MIC，µg/mL）").font = Font(bold=True, size=14)
    ws.cell(2, 1, f"菌株/样本：{meta.get('sample_no') or '—'}").font = Font(bold=True)
    ws.cell(2, 3, f"菌属/菌种：{(meta.get('genus_label') or '—')} {(meta.get('species') or '')}")
    ws.cell(2, 5, f"培养信息：{meta.get('culture_info') or '—'}")
    ws.cell(3, 1, f"实验批次：{meta.get('batch_no') or '—'}")
    ws.cell(3, 3, f"操作人：{meta.get('operator') or '—'}")
    ws.cell(3, 5, f"日期：{meta.get('date') or datetime.date.today().isoformat()}")
    if meta.get("strain2"):
        ws.cell(2, max(7, ncols - 2), f"下半区菌号：{meta['strain2']}").font = Font(bold=True)
    ws.cell(3, max(7, ncols - 2), "软件辅助判读，以人工复核值为准").font = Font(color="888888")

    header = ["序号", "菌株/样本", "培养信息", "时间点", "照片"] + \
             [c for c in codes] + ["复核", "备注"]
    hr = 5
    for i, htxt in enumerate(header, start=1):
        cell = ws.cell(hr, i, htxt)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = center
    next_row = hr + 1
    if qc:
        for row_i, tp_key, lab in ((next_row, "24H", "24h-CLSI质控范围"),
                                   (next_row + 1, "48H", "48h-CLSI质控范围")):
            ws.merge_cells(start_row=row_i, start_column=1, end_row=row_i, end_column=5)
            c0 = ws.cell(row_i, 1, lab)
            c0.font = Font(bold=True)
            c0.fill = qc_fill
            c0.alignment = center
            rng_map = (qc.get("ranges") or {}).get(tp_key, {})
            for i, code in enumerate(codes):
                cell = ws.cell(row_i, 6 + i, rng_map.get(code, "—"))
                cell.fill = qc_fill
                cell.border = border
                cell.alignment = center
        next_row += 2

    r = next_row
    for idx, p in enumerate(plates):
        s = p.get("sample", {})
        m_by_code = {m["code"]: m for m in p.get("mic", [])}
        tp = s.get("time_point") or ""
        ranges = (qc.get("ranges") or {}).get(tp) if qc else None
        notes = []
        for j, code in enumerate(codes):
            m = m_by_code.get(code)
            if not m:
                continue
            val = m.get("final_mic", m["mic"])
            cell = ws.cell(r, 6 + j, val)
            cell.border = border
            cell.alignment = center
            if ranges and code in ranges:
                ok = qc_in_range(val, ranges[code])
                if ok is False:
                    cell.font = red_font
                    notes.append(f"{code} 超出质控范围 {ranges[code]}")
            for fl in m.get("flags", []):
                notes.append(fl)
        ws.cell(r, 1, idx + 1).alignment = center
        ws.cell(r, 2, s.get("sample_no") or meta.get("sample_no") or "")
        ws.cell(r, 3, s.get("culture_info") or meta.get("culture_info") or "")
        ws.cell(r, 4, tp or "未标注")
        ws.cell(r, 5, s.get("file", ""))
        ws.cell(r, 6 + len(codes), "已复核" if s.get("reviewed") else "未复核")
        ws.cell(r, 7 + len(codes), "；".join(notes)[:150])
        for i in range(1, ncols + 1):
            ws.cell(r, i).border = border
        r += 1
    for col_letter, width in zip("ABCDE", [6, 18, 14, 9, 22]):
        ws.column_dimensions[col_letter].width = width
    for i in range(len(codes)):
        ws.column_dimensions[chr(ord("F") + i)].width = 12
    ws.column_dimensions[chr(ord("F") + len(codes))].width = 9
    ws.column_dimensions[chr(ord("F") + len(codes) + 1)].width = 42

    role_names = {"drug": "药物", "positive_control": "阳性对照",
                  "negative_control": "阴性对照", "unused": "未使用"}
    status_names = {"growth": "生长", "no_growth": "无生长",
                    "unused": "未使用", "unknown": "未知", "partial": "紫色(临界)"}

    ws2 = wb.create_sheet("复核明细")
    headers2 = ["照片", "时间点", "药物", "名称", "软件建议", "复核值", "是否修改",
                "质控范围", "质控", "置信度", "判读孔位", "警告与备注"]
    for i, htxt in enumerate(headers2, start=1):
        cell = ws2.cell(1, i, htxt)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = center
    r = 2
    for p in plates:
        s = p.get("sample", {})
        tp = s.get("time_point") or ""
        ranges = (qc.get("ranges") or {}).get(tp, {}) if qc else {}
        for m in p.get("mic", []):
            final = m.get("final_mic", m["mic"])
            rng = (ranges or {}).get(m["code"], "")
            qc_mark = ""
            if rng:
                ok = qc_in_range(final, rng)
                qc_mark = "在控" if ok else ("超出" if ok is False else "")
            row_vals = [s.get("file", ""), tp or "未标注", m["code"], m["name"],
                        m["mic"], final, ("是" if final != m["mic"] else ""),
                        rng, qc_mark, m.get("confidence"), m.get("mic_well") or "",
                        "；".join(m.get("flags") or []) or m.get("note", "")]
            for i, v in enumerate(row_vals, start=1):
                cell = ws2.cell(r, i, v)
                cell.border = border
                cell.alignment = center if i not in (1, 12) else Alignment(
                    horizontal="left", vertical="center")
            if qc_mark == "超出":
                ws2.cell(r, 9).font = red_font
            r += 1
    for col_letter, width in zip("ABCDEFGHIJKL",
                                 [22, 8, 7, 14, 10, 10, 8, 10, 8, 8, 9, 46]):
        ws2.column_dimensions[col_letter].width = width

    ws3 = wb.create_sheet("逐孔明细")
    headers3 = ["照片", "时间点", "孔位", "角色", "药物", "浓度 (µg/mL)", "判读", "置信度"]
    for i, htxt in enumerate(headers3, start=1):
        cell = ws3.cell(1, i, htxt)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = center
    r = 2
    for p in plates:
        s = p.get("sample", {})
        for w_ in p.get("wells", []):
            row_vals = [s.get("file", ""), s.get("time_point") or "未标注", w_["pos"],
                        role_names.get(w_["role"], w_["role"]), w_.get("drug", ""),
                        w_.get("conc", ""), status_names.get(w_.get("status"), ""),
                        w_.get("confidence")]
            for i, v in enumerate(row_vals, start=1):
                cell = ws3.cell(r, i, v)
                cell.border = border
                cell.alignment = center if i != 1 else Alignment(
                    horizontal="left", vertical="center")
            r += 1
    for col_letter, width in zip("ABCDEFGH", [22, 8, 8, 12, 10, 12, 12, 10]):
        ws3.column_dimensions[col_letter].width = width

    if out_path:
        wb.save(out_path)
        return out_path
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description="96 孔药敏板图像判读（演示版）")
    ap.add_argument("image", help="药敏板照片路径")
    ap.add_argument("--template", default=os.path.join(HERE, "template.json"))
    ap.add_argument("--outdir", default=os.path.join(HERE, "outputs"))
    ap.add_argument("--sample-no", default="")
    args = ap.parse_args()

    analyzer = PlateAnalyzer(load_template(args.template))
    result = analyzer.run(args.image, outdir=args.outdir, sample_no=args.sample_no)
    xlsx = os.path.join(args.outdir,
                        os.path.splitext(os.path.basename(args.image))[0] + "_结果.xlsx")
    export_excel(result, xlsx)
    result["excel"] = xlsx
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
