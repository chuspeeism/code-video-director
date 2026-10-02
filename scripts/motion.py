#!/usr/bin/env python3
"""动态测量：逐帧算光流（每个位置往哪动、动多快），把画面里的运动分成三类分开量——
1. 镜头：推拉摇移的时间占比；是不是匀速推拉（一个镜头从头到尾同一个速度，机械感）。
2. 明显动作：扣掉镜头和整层视差漂移后，局部速度超过每秒 150 像素（1920 宽）、真的从一处到了另一处的运动——
   人物在演、东西飞进来、数字跳、方块落下。量同屏有几块、每秒发生几个动作、动作有多快，
   以及每个动作怎么起、怎么停（缓入、硬起、缓出、硬停、匀速）。
3. 原地晃动：局部速度每秒 30–150 像素的小幅运动（漂浮、呼吸、上下浮动）。有它画面不死，但只有它就是「在晃、没在演」。
另外量运动量怎么随时间分布：没有明显动作的时间占比、最长一段没动作、转场前后在不在动。
只依赖 numpy（有 scipy 会快一点）和 ffmpeg。速度单位统一换算成「1920 宽画面上每秒多少像素」。
用法：python3 motion.py <视频> [--out 目录] [--start 秒] [--dur 秒] [--width 320]
输出 <out>/motion.json：summary（汇总数字）+ series（逐帧序列）+ segments（每个动作的速度曲线）"""
import argparse, json, pathlib, subprocess
import numpy as np

try:
    from scipy import ndimage as ndi
except Exception:  # 没有 scipy 就用 numpy 版
    ndi = None

REF_W = 1920.0           # 速度统一换算到 1920 宽
CELL = 8                 # 按 8×8 像素的格子统计（320 宽时 40×22 格）
EPS_TEX = 2e-4           # 纹理太弱（纯色块内部）的地方光流不可信，不参与
ACT = 150.0              # 局部速度超过这个（px/s，1920 宽）算明显动作
WOBBLE = 30.0            # 30–150 算原地晃动 / 慢漂；30 以下当作静止（光流本身的误差在 15 左右）
CAM_MOVE = 12.0          # 镜头整体移动超过这个算镜头在动（约每秒移动画面宽度的 0.6%）
DIFF_CELL = 0.04         # 光流对齐后格子平均亮度变化还超过这个，算「出现、消失、闪变」，归入明显动作
SEG_LOW = 30.0           # 动作分段：低于这个算停
NET_MIN = 0.015          # 一个动作至少挪动画面宽度的 1.5%（约 29 像素）才算「从一处到了另一处」
RAMP_S = 0.10            # 起势/收势用时不少于 0.1 秒算「缓」
LAYER_TOL = 0.12         # 像素的光流和某一层整体运动差不到这个（分析尺寸下每帧像素），算这一层带着走的
LAYER_MIN = 0.12         # 一层至少占可信像素的 12%
LAYER_SPREAD = 0.6       # 第二层起，覆盖范围要横跨画面宽或高的 60% 以上才算「层」，紧凑的大物体不算
# 画面在重播（t % 周期 写出来的循环动作）：扣掉镜头运动之后，同一个镜头里隔固定时间画面又回到原样
REP_W = 128              # 用 128 宽的灰度小图看
REP_WIN = 3.2            # 长镜头按 3.2 秒一窗滑动看（4 秒以内的镜头整段看），步长 0.8 秒
REP_LAG = (0.3, 1.6)     # 找 0.3–1.6 秒的重播周期
REP_GRID = (8, 5)        # 画面分 8×5 格，每格单独看
REP_CHANGE = 0.04        # 格子里的画面要明显变过（灰度平均差 ≥ 0.04）才算「在演」
REP_SCORE = 0.6          # 隔一个周期，格子里的画面差比之前最大时小六成以上，算「回到原样」
REP_AREA = 0.15          # 一窗里 15% 以上的格子在重播，这一窗算「画面在重播」


# ---------- 读帧 ----------
def probe(path):
    j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                   "stream=width,height,avg_frame_rate,r_frame_rate:format=duration", "-of", "json", str(path)],
                                  capture_output=True, text=True).stdout)
    s = j["streams"][0]
    n, d = (s.get("avg_frame_rate") or s["r_frame_rate"]).split("/")
    fps = float(n) / float(d) if float(d) else 30.0
    if fps > 61 or fps < 10:
        n, d = s["r_frame_rate"].split("/"); fps = float(n) / float(d)
    return int(s["width"]), int(s["height"]), fps, float(j["format"]["duration"])


def read_frames(path, width, start=None, dur=None):
    W, H, fps, D = probe(path)
    h = int(round(H * width / W / 2) * 2)
    cmd = ["ffmpeg", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if dur:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vf", f"scale={width}:{h}:flags=area,format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    a = np.frombuffer(raw, np.uint8)
    n = a.size // (width * h)
    return a[: n * width * h].reshape(n, h, width), fps, (W, H, D)


# ---------- 光流（金字塔 Lucas–Kanade，numpy 实现） ----------
def box(a, r):
    k = 2 * r + 1
    p = np.pad(a, r, mode="edge").astype(np.float64)
    c = np.zeros((p.shape[0] + 1, p.shape[1] + 1))
    c[1:, 1:] = p.cumsum(0).cumsum(1)
    return ((c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)).astype(np.float32)


_GRID = {}


def grid(shape):
    if shape not in _GRID:
        yy, xx = np.mgrid[0:shape[0], 0:shape[1]].astype(np.float32)
        _GRID[shape] = (yy, xx)
    return _GRID[shape]


def sample(img, x, y):
    H, W = img.shape
    x = np.clip(x, 0, W - 1.001); y = np.clip(y, 0, H - 1.001)
    x0 = x.astype(np.int32); y0 = y.astype(np.int32)
    fx = x - x0; fy = y - y0
    return ((img[y0, x0] * (1 - fx) + img[y0, x0 + 1] * fx) * (1 - fy)
            + (img[y0 + 1, x0] * (1 - fx) + img[y0 + 1, x0 + 1] * fx) * fy)


def warp(img, u, v):
    yy, xx = grid(img.shape)
    return sample(img, xx + u, yy + v)


def down(a):
    H, W = a.shape
    a = a[: H // 2 * 2, : W // 2 * 2]
    return 0.25 * (a[0::2, 0::2] + a[1::2, 0::2] + a[0::2, 1::2] + a[1::2, 1::2])


def up(f, shape):
    g = np.repeat(np.repeat(f, 2, 0), 2, 1) * 2.0
    out = np.zeros(shape, np.float32)
    h, w = min(shape[0], g.shape[0]), min(shape[1], g.shape[1])
    out[:h, :w] = g[:h, :w]
    if h < shape[0]:
        out[h:, :w] = out[h - 1:h, :w]
    if w < shape[1]:
        out[:, w:] = out[:, w - 1:w]
    return out


def flow(I0, I1, levels=4, r=3):
    p0, p1 = [I0], [I1]
    for _ in range(levels - 1):
        p0.append(down(p0[-1])); p1.append(down(p1[-1]))
    u = v = lam = None
    for L in range(levels - 1, -1, -1):
        a, b = p0[L], p1[L]
        if u is None:
            u = np.zeros_like(a); v = np.zeros_like(a)
        else:
            u = up(u, a.shape); v = up(v, a.shape)
        Iy, Ix = np.gradient(a)
        Sxx, Syy, Sxy = box(Ix * Ix, r), box(Iy * Iy, r), box(Ix * Iy, r)
        det = Sxx * Syy - Sxy * Sxy
        tr = (Sxx + Syy) / 2
        lam = tr - np.sqrt(np.maximum(tr * tr - det, 0))
        ok = lam > EPS_TEX
        dd = np.where(ok, det, 1.0)
        for _ in range(3 if L == levels - 1 else 2):
            It = warp(b, u, v) - a
            Sxt, Syt = box(Ix * It, r), box(Iy * It, r)
            u = u + np.clip(np.where(ok, (-Syy * Sxt + Sxy * Syt) / dd, 0), -2, 2)
            v = v + np.clip(np.where(ok, (Sxy * Sxt - Sxx * Syt) / dd, 0), -2, 2)
    return u, v, lam > EPS_TEX


def affine(u, v, valid):
    """对可信像素做稳健的仿射拟合（平移 + 缩放 + 旋转），返回每个像素上的整体运动。"""
    H, W = u.shape
    ys, xs = np.mgrid[0:H:3, 0:W:3]
    m = valid[::3, ::3]
    if m.sum() < 40:
        return np.zeros_like(u), np.zeros_like(v), 0.0
    x = (xs[m] - W / 2) / W; y = (ys[m] - H / 2) / W
    U = u[::3, ::3][m]; V = v[::3, ::3][m]
    A = np.stack([np.ones_like(x), x, y], 1)
    keep = np.ones(len(U), bool)
    pu = pv = np.zeros(3)
    for _ in range(4):
        pu = np.linalg.lstsq(A[keep], U[keep], rcond=None)[0]
        pv = np.linalg.lstsq(A[keep], V[keep], rcond=None)[0]
        res = np.hypot(A @ pu - U, A @ pv - V)
        thr = max(float(np.median(res[keep])) * 2.5, 0.05)
        nk = res < thr
        if nk.sum() < 40:
            break
        keep = nk
    yy, xx = grid((H, W))
    X = (xx - W / 2) / W; Y = (yy - H / 2) / W
    return (pu[0] + pu[1] * X + pu[2] * Y).astype(np.float32), (pv[0] + pv[1] * X + pv[2] * Y).astype(np.float32), float(keep.mean())


def layers(u, v, ok, max_layers=3):
    """画面分成几层整体运动：第 0 层是镜头（主导运动），后面是视差层。
    返回 (镜头运动 cu, cv, 局部残差 res = 每个像素扣掉最贴合那一层之后还剩的运动, 层数)。"""
    H, W = u.shape
    nok = max(1, int(ok.sum()))
    remaining = ok.copy()
    best = np.full(u.shape, np.inf, np.float32)
    cam = None; nl = 0
    for L in range(max_layers):
        if remaining.sum() < LAYER_MIN * nok:
            break
        cu, cv, _ = affine(u, v, remaining)
        r = np.hypot(u - cu, v - cv)
        fit = remaining & (r < LAYER_TOL)
        if L == 0:
            cam = (cu, cv)
        if fit.sum() < LAYER_MIN * nok:
            if L == 0:
                best = np.minimum(best, r)
            break
        if L > 0:
            ys, xs = np.nonzero(fit)
            if max((xs.max() - xs.min()) / W, (ys.max() - ys.min()) / H) < LAYER_SPREAD:
                break
        best = np.minimum(best, r); remaining &= ~fit; nl += 1
    if cam is None:
        cam = (np.zeros_like(u), np.zeros_like(v)); best = np.hypot(u, v)
    best[~np.isfinite(best)] = 0
    return cam[0], cam[1], best, nl


def label(mask):
    if ndi is not None:
        return ndi.label(mask, structure=np.ones((3, 3)))
    H, W = mask.shape
    big = H * W + 10
    lab = np.where(mask, np.arange(1, H * W + 1).reshape(H, W), 0)
    while True:
        p = np.pad(np.where(mask, lab, big), 1, constant_values=big)
        n = np.minimum.reduce([p[1 + dy:1 + dy + H, 1 + dx:1 + dx + W] for dy in (-1, 0, 1) for dx in (-1, 0, 1)])
        n = np.where(mask, n, 0)
        if np.array_equal(n, lab):
            break
        lab = n
    ids = np.unique(lab[lab > 0])
    remap = np.zeros(lab.max() + 1, np.int64); remap[ids] = np.arange(1, len(ids) + 1)
    return remap[lab], len(ids)


def cells(a, H, W):
    h, w = H // CELL, W // CELL
    return a[: h * CELL, : w * CELL].reshape(h, CELL, w, CELL).mean(axis=(1, 3))


def act_cells(loc, ok, change, H, W, prev=None):
    """明显动作的格子：格子里可信像素至少 6 个、其中三成以上局部速度超过 ACT；或光流解释不了的闪变。
    去掉最外一圈格子（边缘光流不可靠），而且要上一帧附近也在动（连续两帧），零星噪点不算。"""
    nv = cells(ok.astype(np.float32), H, W) * CELL * CELL
    na = cells(((loc > ACT) & ok).astype(np.float32), H, W) * CELL * CELL
    a = ((nv >= 6) & (na >= 4) & (na >= 0.3 * nv)) | (cells(change, H, W) > DIFF_CELL)
    a[0, :] = a[-1, :] = False; a[:, 0] = a[:, -1] = False
    if prev is not None:
        p = np.pad(prev, 1)
        near = np.zeros_like(prev)
        for dy in (0, 1, 2):
            for dx in (0, 1, 2):
                near |= p[dy:dy + prev.shape[0], dx:dx + prev.shape[1]]
        return a & near, a
    return a & False, a


def cell_vec(u, v, ok, H, W):
    """每个格子里可信像素的平均运动向量（方向乱的噪声会互相抵消），可信像素不足 6 个的格子记 0。"""
    w = ok.astype(np.float32)
    nv = cells(w, H, W) * CELL * CELL
    su = cells(u * w, H, W) * CELL * CELL; sv = cells(v * w, H, W) * CELL * CELL
    good = nv >= 6
    return np.where(good, su / np.maximum(nv, 1), 0), np.where(good, sv / np.maximum(nv, 1), 0), good


def blobs(mask):
    lab, n = label(mask)
    if not n:
        return 0
    return int((np.bincount(lab.ravel())[1:] >= 2).sum())


def frame_motion(I0, I1, k):
    """一对相邻帧的运动：镜头速度、局部速度场（px/s）、可信像素、光流解释不了的变化。
    先稳镜头再量：第一遍光流估出镜头运动，用它把下一帧对齐，再在对齐后的画面上量剩下的运动。
    镜头推拉时细线、网格上的光流误差就不会被当成「东西在动」。"""
    u0, v0, ok0 = flow(I0, I1)
    cu, cv, _, _ = layers(u0, v0, ok0, max_layers=1)
    cam = float(np.sqrt(np.mean(cu * cu + cv * cv))) * k
    I1s = warp(I1, cu, cv)
    u2, v2, ok = flow(I0, I1s, levels=3)
    _, _, res, nl = layers(u2, v2, ok)                    # 再扣掉视差层（第 0 层这时接近不动）
    loc = np.where(ok, res * k, 0)
    change = np.abs(warp(I1s, u2, v2) - I0)
    return cam, loc, ok, change, nl, (cu + u2, cv + v2, cu, cv, u2, v2)


# ---------- 画面在重播 ----------
def cam_params(u, v, ok):
    """镜头整体运动拟合成仿射参数：u = a0 + a1·x + a2·y（x、y 以画面中心为原点、按画宽归一）。"""
    H, W = u.shape
    ys, xs = np.mgrid[0:H:2, 0:W:2]
    m = ok[::2, ::2]
    if m.sum() < 40:
        return np.zeros(3), np.zeros(3)
    x = (xs[m] - W / 2) / W; y = (ys[m] - H / 2) / W
    U = u[::2, ::2][m]; V = v[::2, ::2][m]
    A = np.stack([np.ones_like(x), x, y], 1)
    keep = np.ones(len(U), bool)
    pu = pv = np.zeros(3)
    for _ in range(4):
        pu = np.linalg.lstsq(A[keep], U[keep], rcond=None)[0]
        pv = np.linalg.lstsq(A[keep], V[keep], rcond=None)[0]
        res = np.hypot(A @ pu - U, A @ pv - V)
        thr = max(float(np.median(res[keep])) * 2.5, 0.05)
        nk = res < thr
        if nk.sum() < 40:
            break
        keep = nk
    return pu, pv


def stabilize(F, P, s0, e0):
    """把 s0..e0-1 帧都对齐到 s0 帧的镜头位置（顺着每对相邻帧的镜头运动一路累加）。返回（对齐后的帧, 有效像素）。"""
    H, W = F.shape[1:]
    yy, xx = grid((H, W))
    X, Y = xx.astype(np.float32).copy(), yy.astype(np.float32).copy()
    out = np.empty((e0 - s0, H, W), np.float32); valid = np.empty((e0 - s0, H, W), bool)
    for k in range(s0, e0):
        out[k - s0] = sample(F[k], X, Y)
        valid[k - s0] = (X >= 0) & (X <= W - 1) & (Y >= 0) & (Y <= H - 1)
        if k < e0 - 1:
            pu, pv = P[k]
            Xn = (X - W / 2) / W; Yn = (Y - H / 2) / W
            X = X + (pu[0] + pu[1] * Xn + pu[2] * Yn); Y = Y + (pv[0] + pv[1] * Xn + pv[2] * Yn)
    return out, valid


def rep_window(S, V, fps):
    """一窗里每一格：画面隔 P 秒回到原样、中间又明显变过 = 这一格在重播。返回（重播格占比, 周期中位秒）。"""
    m, h, w = S.shape
    gx, gy = REP_GRID
    lo = int(round(REP_LAG[0] * fps)); hi = min(int(round(REP_LAG[1] * fps)), m // 2)
    if hi < lo + 1:
        return None
    hh, ww = h // gy, w // gx
    C = S[:, :hh * gy, :ww * gx].reshape(m, gy, hh, gx, ww)
    okc = (V[:, :hh * gy, :ww * gx].reshape(m, gy, hh, gx, ww).all(axis=0).mean(axis=(1, 3)) > 0.7).reshape(-1)
    if okc.sum() < gx * gy * 0.5:
        return None
    D = np.array([np.abs(C[L:] - C[:-L]).mean(axis=(0, 2, 4)) for L in range(1, hi + 2)]).reshape(hi + 1, -1)
    per = []
    for c in np.nonzero(okc)[0]:
        d = D[:, c]; best = None
        for L in range(lo, hi + 1):
            if d[L - 1] > d[L - 2] or d[L - 1] > d[L]:
                continue
            peak = float(d[:L].max())
            if peak < REP_CHANGE:
                continue
            sc = 1 - d[L - 1] / peak
            if best is None or sc > best[1]:
                best = (L / fps, sc)
        if best and best[1] >= REP_SCORE:
            per.append(best[0])
    return len(per) / int(okc.sum()), (float(np.median(per)) if per else None)


def repeats(path, start=None, dur=None):
    """画面在重播：`t % 周期` 写出来的主动作（每拍同一刀、同一下顿推）会让同一个镜头里隔固定时间画面回到原样。
    先扣掉镜头运动（推拉摇移、顿推、震屏），再按格子找 0.3–1.6 秒的重播周期。
    返回 {"画面重播_占比%": 重播的时间占全片多少, "窗口": [(起秒, 止秒, 重播格占比, 周期秒), ...]}。"""
    fr, fps, _ = read_frames(path, REP_W, start, dur)
    F = fr.astype(np.float32) / 255
    n, H, W = F.shape
    if n < 3:
        return {"画面重播_占比%": 0.0, "窗口": []}
    small = F[:, ::2, ::2]
    d = np.abs(np.diff(small, axis=0)).mean(axis=(1, 2))
    cut = np.zeros(len(d), bool)
    for i in range(len(d)):
        nb = np.concatenate([d[max(0, i - 4):i], d[i + 1:min(len(d), i + 5)]])
        cut[i] = d[i] > max(3 * (float(np.median(nb)) if len(nb) else 0.0), 0.06)
    P = []
    for i in range(n - 1):
        if cut[i]:
            P.append((np.zeros(3), np.zeros(3))); continue
        u, v, ok = flow(F[i], F[i + 1], levels=3)
        P.append(cam_params(u, v, ok))
    my, mx = int(H * 0.06), int(W * 0.06)          # 四边各去 6%：撕纸边框、字幕条这类每帧在抖的边不算
    bounds, a = [], 0
    for i, c in enumerate(cut):
        if c:
            bounds.append((a, i)); a = i + 1
    bounds.append((a, n - 1))
    flagged = np.zeros(n, bool); rows = []
    win, step = int(round(REP_WIN * fps)), int(round(0.8 * fps))
    for a, b in bounds:
        m = b - a + 1
        if m / fps < 2.0:                             # 短于 2 秒的镜头看不出重播
            continue
        whole = m <= int(4 * fps)
        for s0 in ([a] if whole else range(a, b - win + 2, step)):
            e0 = b + 1 if whole else s0 + win
            S, V = stabilize(F, P, s0, e0)
            r = rep_window(S[:, my:H - my, mx:W - mx], V[:, my:H - my, mx:W - mx], fps)
            if r and r[0] >= REP_AREA:
                flagged[s0:e0] = True
                rows.append((round((start or 0) + s0 / fps, 2), round((start or 0) + e0 / fps, 2), round(r[0], 2), round(r[1], 2)))
    return {"画面重播_占比%": round(100.0 * float(flagged.mean()), 1), "窗口": rows}


# ---------- 主流程 ----------
def analyze(path, width=320, start=None, dur=None):
    fr, fps, (W0, H0, D) = read_frames(path, width, start, dur)
    n, H, W = fr.shape
    k = REF_W / W * fps                      # 每帧像素（分析尺寸）→ 每秒像素（1920 宽）
    small = fr[:, ::4, ::4].astype(np.float32) / 255
    d = np.abs(np.diff(small, axis=0)).mean(axis=(1, 2)) if n > 1 else np.zeros(0)
    cut = np.zeros(len(d), bool)
    for i in range(len(d)):
        nb = np.concatenate([d[max(0, i - 4):i], d[i + 1:min(len(d), i + 5)]])
        cut[i] = d[i] > max(3 * (float(np.median(nb)) if len(nb) else 0.0), 0.06)
    keys = ("cam", "act_blobs", "act_area", "wob_area", "p90", "layers")
    ser = {"t": [], "cut": [], **{x: [] for x in keys}}
    alive, done = [], []
    nxt_seed = 0
    hist = []
    for i in range(n - 1):
        ser["t"].append(round((start or 0) + (i + 0.5) / fps, 3)); ser["cut"].append(bool(cut[i]))
        if cut[i]:
            done += [(tr[2], tr[3], True, tr[4], tr[5], tr[6], tr[7], tr[8]) for tr in alive]
            alive = []; nxt_seed = i + 1
            for x in keys:
                ser[x].append(None)
            continue
        I0 = fr[i].astype(np.float32) / 255; I1 = fr[i + 1].astype(np.float32) / 255
        cam, loc, ok, change, nl, (u, v, cu, cv, u2, v2) = frame_motion(I0, I1, k)
        mu, mv, good = cell_vec(u2, v2, ok, H, W)
        if i == 0 or cut[i - 1]:
            hist = []
        hist = (hist + [(mu, mv)])[-4:]
        inst = np.hypot(mu, mv) * k                                       # 这一帧格子的速度
        avg2 = np.hypot(np.mean([h[0] for h in hist[-2:]], 0), np.mean([h[1] for h in hist[-2:]], 0)) * k
        avg4 = np.hypot(np.mean([h[0] for h in hist], 0), np.mean([h[1] for h in hist], 0)) * k
        act_c = ((inst > ACT) & (avg2 > ACT * 0.6) & good) | (cells(change, H, W) > DIFF_CELL)
        act_c[0, :] = act_c[-1, :] = False; act_c[:, 0] = act_c[:, -1] = False
        wob_c = (avg4 > WOBBLE) & (avg4 <= ACT) & good & ~act_c
        wob_c[0, :] = wob_c[-1, :] = False; wob_c[:, 0] = wob_c[:, -1] = False
        moving = avg2[avg2 > WOBBLE]
        ser["cam"].append(round(cam, 1)); ser["act_blobs"].append(blobs(act_c))
        ser["act_area"].append(round(float(act_c.mean()), 4)); ser["wob_area"].append(round(float(wob_c.mean()), 4))
        ser["p90"].append(round(float(np.percentile(moving, 90)), 0) if moving.size > 3 else 0.0)
        ser["layers"].append(nl)
        # 点跟踪：记录每个点的局部速度（扣掉镜头和视差层），点本身跟着完整光流走
        keep = []
        if alive:
            P = np.array([[tr[0], tr[1]] for tr in alive], np.float32)
            su = sample(u, P[:, 0], P[:, 1]); sv = sample(v, P[:, 0], P[:, 1])
            sp = sample(loc, P[:, 0], P[:, 1])
            lu = su - sample(cu, P[:, 0], P[:, 1]); lv = sv - sample(cv, P[:, 0], P[:, 1])
            nx, ny = P[:, 0] + su, P[:, 1] + sv
            inside = (nx > 2) & (nx < W - 3) & (ny > 2) & (ny < H - 3)
            okp = ok[np.clip(ny.astype(int), 0, H - 1), np.clip(nx.astype(int), 0, W - 1)]
            for j, tr in enumerate(alive):
                tr[2].append(float(sp[j])); tr[5].append(float(lu[j])); tr[6].append(float(lv[j]))
                tr[7].append(float(P[j, 0])); tr[8].append(float(P[j, 1]))
                if inside[j] and (okp[j] or len(tr[2]) < 3) and len(tr[2]) < 240:
                    tr[0], tr[1] = float(nx[j]), float(ny[j]); keep.append(tr)
                else:   # 跟丢或出画：看不到它怎么停的，这一头不参与判断
                    done.append((tr[2], tr[3], True, tr[4], tr[5], tr[6], tr[7], tr[8]))
        alive = keep
        if i >= nxt_seed:   # 每 6 帧在有纹理、附近没点的地方补种；新点的起点不参与缓入判断
            occ = np.zeros((H // 6 + 1, W // 6 + 1), bool)
            for tr in alive:
                occ[int(tr[1]) // 6, int(tr[0]) // 6] = True
            ys, xs = np.mgrid[4:H - 4:6, 4:W - 4:6]
            m = ok[ys, xs] & ~occ[ys // 6, xs // 6]
            alive += [[float(x), float(y), [], True, i, [], [], [], []] for x, y in zip(xs[m][:1500], ys[m][:1500])]
            nxt_seed = i + 6
    done += [(tr[2], tr[3], True, tr[4], tr[5], tr[6], tr[7], tr[8]) for tr in alive]
    segs = segments(done, fps, W)
    return summarize(ser, segs, fps, n, D), ser, segs


def segments(done, fps, W):
    """把每条轨迹的速度曲线切成一个个动作，只留明显动作：峰值超过 ACT、挪动超过画面宽 1.5%。"""
    out = []
    for sp, cut_head, cut_tail, f0, lus, lvs, pxs, pys in done:
        s = np.array(sp, np.float32)
        if len(s) < 6:
            continue
        s = np.maximum(s, np.concatenate([[0], s[:-1]]))   # 一拍二（每两帧动一次）的动画不至于被切碎
        mv = s > SEG_LOW
        i = 0
        while i < len(s):
            if not mv[i]:
                i += 1; continue
            j = i
            while j + 1 < len(s) and mv[j + 1]:
                j += 1
            seg = s[i:j + 1]; pk = float(seg.max())
            net = float(np.hypot(sum(lus[i:j + 1]), sum(lvs[i:j + 1]))) / W   # 扣掉镜头之后真正挪了多远
            if pk >= ACT and len(seg) >= 4 and net >= NET_MIN:
                head_known = i > 0 or not cut_head
                tail_known = j < len(s) - 1 or not cut_tail
                a = int(np.argmax(seg >= 0.8 * pk)); b = len(seg) - 1 - int(np.argmax(seg[::-1] >= 0.8 * pk))
                lo, hi = int(len(seg) * 0.2), max(int(len(seg) * 0.8), int(len(seg) * 0.2) + 1)
                mid = seg[lo:hi]
                out.append({
                    "f": f0 + i, "x": round(pxs[i], 1), "y": round(pys[i], 1), "net": round(net, 3),
                    "len_s": round(len(seg) / fps, 3), "peak": round(pk, 1),
                    "head": None if not head_known else ("硬起" if seg[0] >= 0.5 * pk else ("缓入" if a / fps >= RAMP_S else "快起")),
                    "tail": None if not tail_known else ("硬停" if seg[-1] >= 0.5 * pk else ("缓出" if (len(seg) - 1 - b) / fps >= RAMP_S else "快停")),
                    "uniform": bool(len(seg) >= 8 and float(mid.std() / (mid.mean() + 1e-6)) < 0.12),
                    "curve": [round(float(x / pk), 3) for x in seg[:: max(1, len(seg) // 24)]],
                })
            i = j + 1
    return out


def action_shape(segs):
    """完整动作（看得到起和停）的形状：用时、最快时多快、快起慢停还是对称慢进慢出、最快的一刻在动作的哪里。"""
    full = [s for s in segs if s["head"] and s["tail"]]
    if not full:
        return {}
    return {
        "动作用时_中位秒": round(float(np.median([s["len_s"] for s in full])), 2),
        "动作最快速度_中位px每秒": round(float(np.median([s["peak"] for s in full])), 0),
        "快起慢停_占比%": round(100 * float(np.mean([s["head"] in ("硬起", "快起") and s["tail"] == "缓出" for s in full])), 1),
        "慢起慢停_占比%": round(100 * float(np.mean([s["head"] == "缓入" and s["tail"] == "缓出" for s in full])), 1),
        "最快时刻位置_中位%": round(100 * float(np.median([int(np.argmax(s["curve"])) / max(1, len(s["curve"]) - 1) for s in full])), 0),
        "动作长短变化": round(float((np.percentile([s["len_s"] for s in full], 75) - np.percentile([s["len_s"] for s in full], 25)) / max(1e-6, np.median([s["len_s"] for s in full]))), 2),
        "一帧冲到全速_占比%": round(100 * float(np.mean([s["head"] == "硬起" for s in full])), 1),
    }


def summarize(ser, segs, fps, n, D):
    valid = [i for i, c in enumerate(ser["cut"]) if not c and ser["act_blobs"][i] is not None]
    g = lambda key: np.array([ser[key][i] for i in valid], float)
    blobs_, area, wob, cam, p90 = g("act_blobs"), g("act_area"), g("wob_area"), g("cam"), g("p90")
    dur = len(valid) / fps if valid else 1
    pct = lambda a: round(100.0 * float(a), 1)
    noact = area < 0.01
    runs, cur = [], 0
    for x in noact:
        if x:
            cur += 1
        elif cur:
            runs.append(cur); cur = 0
    if cur:
        runs.append(cur)
    # 镜头：每段连续的镜头运动，速度变化很小就算匀速推拉
    camm = cam > CAM_MOVE
    uni_t = mov_t = 0
    i = 0
    while i < len(cam):
        if not camm[i]:
            i += 1; continue
        j = i
        while j + 1 < len(cam) and camm[j + 1]:
            j += 1
        seg = cam[i:j + 1]
        mov_t += len(seg)
        if len(seg) >= fps * 0.8 and seg.std() / (seg.mean() + 1e-6) < 0.25:
            uni_t += len(seg)
        i = j + 1
    # 动作事件：同一时刻（±3 帧）、同一位置附近（30 像素内，分析尺寸）开始的多条轨迹算同一个动作
    ev = []
    for sgm in sorted(segs, key=lambda q: q["f"]):
        if not any(abs(sgm["f"] - e[0]) <= 3 and abs(sgm["x"] - e[1]) <= 30 and abs(sgm["y"] - e[2]) <= 30 for e in ev[-60:]):
            ev.append((sgm["f"], sgm["x"], sgm["y"]))
    hk = [s["head"] for s in segs if s["head"]]; tk = [s["tail"] for s in segs if s["tail"]]
    both = [s for s in segs if s["head"] and s["tail"]]
    cuts = [i for i, c in enumerate(ser["cut"]) if c]
    w = max(1, int(fps * 0.3))

    def moving_at(lo, hi):
        idx = [j for j in range(max(0, lo), min(len(ser["cut"]), hi)) if ser["act_area"][j] is not None]
        return bool(idx) and (np.mean([ser["act_area"][j] for j in idx]) >= 0.01 or np.mean([ser["cam"][j] for j in idx]) > CAM_MOVE)
    m2m = sum(1 for c in cuts if moving_at(c - w, c) and moving_at(c + 1, c + 1 + w))
    P = lambda x: pct(x) if x is not None else None
    return {
        "时长秒": round(D, 1), "帧率": round(fps, 2), "分析帧数": int(n), "硬切": len(cuts),
        "明显动作_同屏块数": round(float(blobs_.mean()), 2) if len(blobs_) else 0,
        "明显动作_至少2块_占比%": pct((blobs_ >= 2).mean()) if len(blobs_) else 0,
        "明显动作_面积%": pct(area.mean()) if len(area) else 0,
        "动作_每秒几个": round(len(ev) / dur, 2),
        "动作速度_快的那10%": round(float(np.median(p90[p90 > 0])), 0) if (p90 > 0).any() else 0,
        "原地晃动_面积%": pct(wob.mean()) if len(wob) else 0,
        "没有明显动作_占比%": pct(noact.mean()) if len(noact) else 0,
        "最长没动作秒": round(max(runs) / fps, 2) if runs else 0,
        "镜头在动_占比%": pct(camm.mean()) if len(cam) else 0,
        "镜头匀速推拉_占镜头运动%": P(uni_t / mov_t) if mov_t else None,
        "动作数_有起停": len(both),
        "缓入_占比%": P(hk.count("缓入") / len(hk)) if hk else None,
        "硬起_占比%": P(hk.count("硬起") / len(hk)) if hk else None,
        "缓出_占比%": P(tk.count("缓出") / len(tk)) if tk else None,
        "硬停_占比%": P(tk.count("硬停") / len(tk)) if tk else None,
        "两头都缓_占比%": P(sum(1 for s in both if s["head"] == "缓入" and s["tail"] == "缓出") / len(both)) if both else None,
        "匀速动作_占比%": P(sum(1 for s in segs if s["uniform"]) / len(segs)) if segs else None,
        "转场动接动_占比%": P(m2m / len(cuts)) if cuts else None,
        **action_shape(segs),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video"); ap.add_argument("--out", default=None)
    ap.add_argument("--start", type=float); ap.add_argument("--dur", type=float)
    ap.add_argument("--width", type=int, default=320)
    a = ap.parse_args()
    out = pathlib.Path(a.out or (pathlib.Path(a.video).parent / "motion"))
    out.mkdir(parents=True, exist_ok=True)
    summ, ser, segs = analyze(a.video, a.width, a.start, a.dur)
    json.dump({"video": str(a.video), "summary": summ, "series": ser, "segments": segs},
              open(out / "motion.json", "w", encoding="utf-8"), ensure_ascii=False)
    print(json.dumps(summ, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
