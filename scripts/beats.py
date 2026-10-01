#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""beats.py —— 分析音乐给卡点用：BPM、拍点、重拍、「高潮砸下来」的位置、安静/响亮段。
部分思路来自 howseen-ai/claude-motion-design（MIT）

用法：python3 beats.py <音频> [--json out.json]
  不给 --json 时，JSON 存在音频旁边：<音频名>.beats.json
依赖：numpy；PATH 里有 ffmpeg（或用环境变量 FFMPEG 指定路径）。
其它脚本可以 `from beats import analyze` 直接拿结果（qa.py 就这么用）。
提醒：自动拍点网格可能整体偏一两拍，最终以能量跳升点（drops）为准。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

try:
    import numpy as np
except ImportError:  # 新装的 Python 常常没有 numpy，给一句能照做的提示
    sys.exit("[错误] 缺 numpy。先运行：python3 -m pip install numpy\n"
             "如果被拦（Homebrew 等 Python 会提示 externally-managed-environment），就建一个虚拟环境：\n"
             "  python3 -m venv .venv && .venv/bin/pip install numpy\n"
             "然后用 .venv/bin/python 运行本 skill 的脚本。")

SR, HOP, NFFT = 22050, 256, 1024
FPS = SR / HOP            # 起音包络的帧率，约 86 帧/秒
FRAME_T0 = NFFT / 2 / SR  # 第 0 帧对应的时刻（窗口中心）


def ffmpeg_bin():
    ff = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not ff:
        sys.exit("[错误] 找不到 ffmpeg：请先安装并加入 PATH（或用环境变量 FFMPEG 指定）")
    return ff


def load_audio(path, sr=SR):
    """用 ffmpeg 解码成单声道 float64。"""
    r = subprocess.run([ffmpeg_bin(), "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("ffmpeg 解码失败（文件没有音轨或格式不支持）：" + r.stderr.decode("utf-8", "ignore")[-300:])
    return np.frombuffer(r.stdout, np.float32).astype(np.float64)


def boxcar(x, n):
    """长度 n 的居中滑动平均（累加和实现，O(N)）。"""
    n = max(1, min(int(n), len(x)))
    c = np.concatenate([[0.0], np.cumsum(x)])
    y = (c[n:] - c[:-n]) / n
    left = (len(x) - len(y)) // 2
    return np.concatenate([np.zeros(left), y, np.zeros(len(x) - len(y) - left)])


def lowband(x, sr=SR, fc=150.0):
    """两次滑动平均 ≈ 150 Hz 低通：时间上只抹开几毫秒，适合找鼓点起音。"""
    n = max(2, int(round(sr / fc)))
    return boxcar(boxcar(x, n), n)


def onset_envelope(x):
    """频谱通量起音包络：对数幅度谱逐帧的正向增量求和，再减掉 0.5 秒局部均值。"""
    if len(x) < NFFT:
        x = np.pad(x, (0, NFFT - len(x)))
    frames = np.lib.stride_tricks.sliding_window_view(x, NFFT)[::HOP]
    win, out, last = np.hanning(NFFT), np.zeros(len(frames)), None
    for s in range(0, len(frames), 2048):
        mag = np.log1p(100 * np.abs(np.fft.rfft(frames[s:s + 2048] * win, axis=1)))
        ref = np.vstack([mag[:1] if last is None else last[None], mag[:-1]])
        out[s:s + len(mag)] = np.maximum(0.0, mag - ref).sum(axis=1)
        last = mag[-1]
    out -= boxcar(out, int(FPS * 0.5))
    return np.maximum(out, 0.0)


def estimate_period(env):
    """起音包络自相关，在 60–200 BPM 里找峰；乘以 120 BPM 为中心的对数高斯先验防止倍频/半频错。返回一拍的帧数。"""
    e = env - env.mean()
    n = len(e)
    lo, hi = int(np.floor(60 * FPS / 200)), int(np.ceil(60 * FPS / 60))
    if n < hi + 3:
        return 60 * FPS / 120.0
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    ac = np.fft.irfft(np.abs(np.fft.rfft(e, nfft)) ** 2)[:n]
    ac /= ac[0] + 1e-12
    lags = np.arange(lo, hi + 1)
    prior = np.exp(-0.5 * np.log2((60 * FPS / lags) / 120.0) ** 2)
    k = lo + int(np.argmax(ac[lo:hi + 1] * prior))
    a, b, c = ac[k - 1], ac[k], ac[k + 1]
    den = a - 2 * b + c
    return k + (float(np.clip(0.5 * (a - c) / den, -0.5, 0.5)) if den != 0 else 0.0)


def fit_grid(env, period):
    """在 ±1.5% 周期和全部相位里，找让拍点最多落在起音峰上的网格。返回 (周期, 相位)，单位：帧。"""
    env = boxcar(env, 3)
    n, best = len(env), (-1.0, period, 0.0)
    for p in np.linspace(period * 0.985, period * 1.015, 61):
        k = np.arange(int((n - 1) / p) + 1)
        phases = np.arange(0, p, 0.25)
        pos = phases[:, None] + k[None, :] * p
        valid = pos <= n - 1
        score = (np.interp(pos, np.arange(n), env) * valid).sum(1) / np.maximum(valid.sum(1), 1)
        i = int(np.argmax(score))
        if score[i] > best[0]:
            best = (float(score[i]), float(p), float(phases[i]))
    return best[1], best[2]


def db(e):
    return 10 * np.log10(np.asarray(e) + 1e-12)


class Energy:
    """用平方累加和，O(1) 求任意时间窗的平均能量。"""
    def __init__(self, x, sr=SR):
        self.c, self.sr = np.concatenate([[0.0], np.cumsum(x * x)]), sr

    def __call__(self, a, b):
        i = int(np.clip(round(a * self.sr), 0, len(self.c) - 2))
        j = int(np.clip(round(b * self.sr), i + 1, len(self.c) - 1))
        return (self.c[j] - self.c[i]) / (j - i)


def find_drops(xl, xf, beats, beat_s, dur, top=3):
    """「高潮砸下来」：以每一拍为起点，比较后 4 拍和前 4 拍的低频能量（dB），跳升最大的拍；
    再在 ±0.3 秒里用 20 毫秒窗口（步长 5 毫秒）找跳升最陡的那一刻。"""
    el, ef, bar = Energy(xl), Energy(xf), 4 * beat_s
    cands = []
    for b in beats:
        if b - bar < -beat_s * 0.5 or b + bar > dur + beat_s * 0.5:
            continue
        jump = db(el(b, b + bar)) - db(el(max(0.0, b - bar), b))
        cands.append([float(b), float(jump), float(db(ef(b, b + bar)))])
    if not cands:
        return []
    loudest = max(c[2] for c in cands)
    cands = [c for c in cands if c[2] > loudest - 15]   # 砸下来之后要够响，排除安静段里的小起伏
    cands.sort(key=lambda c: -c[1])
    picked = []
    for c in cands:
        if c[1] <= 1.0 or any(abs(c[0] - p[0]) < bar - 1e-6 for p in picked):
            continue
        picked.append(c)
        if len(picked) == top:
            break
    out = []
    for b, jump, _ in picked:
        ts = np.arange(max(0.02, b - 0.3), min(dur - 0.02, b + 0.3), 0.005)
        d = [db(el(t, t + 0.02)) - db(el(max(0.0, t - 0.25), t)) for t in ts]
        t = float(ts[int(np.argmax(d))]) if len(ts) else b
        out.append({"time": round(t, 3), "grid_beat": round(b, 3), "jump_db": round(jump, 1)})
    return out


def find_sections(xf, downbeats, dur):
    """按小节算全频能量，用 20/80 分位数的中点当门槛分「安静 / 响亮」，3 小节中值滤波去抖后合并。"""
    edges = sorted(set([0.0] + [float(d) for d in downbeats if 0 < d < dur] + [float(dur)]))
    ef = Energy(xf)
    lv = np.array([db(ef(a, b)) for a, b in zip(edges[:-1], edges[1:])])
    if len(lv) == 0:
        return []
    p20, p80 = np.percentile(lv, 20), np.percentile(lv, 80)
    if p80 - p20 < 6:
        return [{"start": 0.0, "end": round(dur, 3), "level": "even", "db": round(float(lv.mean()), 1)}]
    loud = (lv > (p20 + p80) / 2).astype(int)
    if len(loud) >= 3:
        loud = np.array([int(np.median(loud[max(0, i - 1):i + 2])) for i in range(len(loud))])
    secs = []
    for i, flag in enumerate(loud):
        if secs and secs[-1]["_f"] == flag:
            secs[-1]["end"], secs[-1]["_l"] = edges[i + 1], secs[-1]["_l"] + [lv[i]]
        else:
            secs.append({"start": edges[i], "end": edges[i + 1], "_f": flag, "_l": [lv[i]]})
    return [{"start": round(s["start"], 3), "end": round(s["end"], 3), "level": "loud" if s["_f"] else "quiet",
             "db": round(float(np.mean(s["_l"])), 1)} for s in secs]


def analyze(path):
    """分析一段音乐，返回 dict：bpm、beats、downbeats、drops、sections、duration（时间单位都是秒）。"""
    x = load_audio(path)
    dur = len(x) / SR
    env = onset_envelope(x)
    period, phase = fit_grid(env, estimate_period(env))
    beat_s = period / FPS
    beats = [FRAME_T0 + (phase + k * period) / FPS for k in range(int((len(env) - 1 - phase) / period) + 1)]
    while beats and beats[0] - beat_s > -0.03:          # 往前补满到 0 秒（容差 30 毫秒，负值记成 0）
        beats.insert(0, beats[0] - beat_s)
    beats = [max(0.0, b) for b in beats if -0.03 <= b <= dur]
    xl = lowband(x)
    # 重拍：4 种相位里，哪一种拍点之后 120 毫秒的低频能量最强（底鼓多半落在第 1 拍），就当每小节第 1 拍
    el = Energy(xl)
    hits = [el(max(0.0, b - 0.02), b + 0.12) for b in beats]
    off = int(np.argmax([np.mean(hits[m::4]) if hits[m::4] else 0 for m in range(4)])) if beats else 0
    downbeats = beats[off::4]
    drops = find_drops(xl, x, beats, beat_s, dur)
    sections = find_sections(x, downbeats, dur)
    for i in range(1, len(sections)):                   # 「安静→响亮」的分界如果离某个高潮点不到一小节，就对齐到它
        a, b = sections[i - 1], sections[i]
        near = [d["time"] for d in drops if abs(d["time"] - b["start"]) < 4 * beat_s and a["start"] < d["time"] < b["end"]]
        if a["level"] == "quiet" and b["level"] == "loud" and near:
            a["end"] = b["start"] = near[0]
    r = lambda v: [round(float(t), 3) for t in v]
    return {"file": os.path.abspath(path), "duration": round(dur, 3), "bpm": round(60.0 / beat_s, 2),
            "beat_period": round(beat_s, 4), "beats": r(beats), "downbeats": r(downbeats),
            "drops": drops, "sections": sections}


def summary(a):
    L = [f"== 音乐分析：{os.path.basename(a['file'])}（时长 {a['duration']:.2f} 秒）",
         f"BPM ≈ {a['bpm']:.1f}（一拍 {a['beat_period']:.3f} 秒，一小节 4 拍 = {4 * a['beat_period']:.3f} 秒）",
         f"拍点 {len(a['beats'])} 个，前 16 个：" + " ".join(f"{t:.2f}" for t in a["beats"][:16]),
         f"重拍（每小节第 1 拍）{len(a['downbeats'])} 个，前 12 个：" + " ".join(f"{t:.2f}" for t in a["downbeats"][:12]),
         "高潮砸下来的候选（低频能量跳升最大处，已精确到 20 毫秒窗口）："]
    for i, d in enumerate(a["drops"], 1):
        L.append(f"  {i}. {d['time']:.3f} 秒   低频 +{d['jump_db']:.1f} dB   （网格上最近的拍：{d['grid_beat']:.3f} 秒）")
    if not a["drops"]:
        L.append("  （没找到明显的低频跳升：这段音乐可能没有「砸下来」的段落）")
    name = {"quiet": "安静", "loud": "响亮", "even": "整体均匀"}
    L.append("安静/响亮分段：" + " | ".join(f"{s['start']:.2f}–{s['end']:.2f} 秒 {name[s['level']]}（{s['db']:.0f} dB）"
                                     for s in a["sections"]))
    L.append("提醒：自动拍点网格可能整体偏一两拍，最终以能量跳升点为准——画面最重的那一下对到候选 1。")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="分析音乐：BPM、拍点、重拍、高潮位置、安静/响亮段")
    ap.add_argument("audio")
    ap.add_argument("--json", help="JSON 输出路径（默认存在音频旁边：<音频名>.beats.json）")
    args = ap.parse_args()
    if not os.path.exists(args.audio):
        sys.exit(f"[错误] 找不到音频文件：{args.audio}")
    try:
        a = analyze(args.audio)
    except RuntimeError as e:
        sys.exit(f"[错误] {e}")
    out = args.json or os.path.splitext(args.audio)[0] + ".beats.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(a, f, ensure_ascii=False, indent=1)
    print(summary(a))
    print(f"JSON：{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
