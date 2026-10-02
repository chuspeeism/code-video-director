#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qa.py —— 成片自检：规格、黑帧、冻帧、构图（空底、雷同、变化）、静音、响度、硬切点卡拍，外加联系表和手机尺寸预览图。
部分思路来自 howseen-ai/claude-motion-design（MIT）

用法：python3 qa.py <成片.mp4> [--expect-size 1920x1080] [--expect-dur 60]
                  [--music | --no-music] [--voice | --no-voice] [--out 目录]
                  [--allow-solid-bg] [--fixed-layout]
  --allow-solid-bg       只给「纯色大底的动态排版」用：不查画面空不空
  --fixed-layout         只给「固定界面的交互网页录屏、说明书」用：不查构图变化和每秒变化
  --music / --no-music   声明成片有没有背景音乐（用来判断静音比例是否合理、要不要查卡拍）
  --voice / --no-voice   声明有没有人声/配音
  --out                  报告和预览图放哪（默认：成片旁边的 <成片名>_qa/）
输出：qa_report.json、联系表.jpg（每 2 秒一帧）、手机尺寸.jpg（每秒一帧，360 宽），终端打印中文摘要。
依赖：numpy；PATH 里有 ffmpeg 和 ffprobe。卡拍检查会调用同目录的 beats.py。
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
try:
    import numpy  # noqa: F401  构图、卡拍检查要用
except ImportError:
    sys.exit("[错误] 缺 numpy。先运行：python3 -m pip install numpy\n"
             "如果被拦（Homebrew 等 Python 会提示 externally-managed-environment），就建一个虚拟环境：\n"
             "  python3 -m venv .venv && .venv/bin/pip install numpy\n"
             "然后用 .venv/bin/python 运行本 skill 的脚本。")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, WARN, FAIL = "通过", "警告", "不通过"


def tool(name):
    p = os.environ.get(name.upper()) or shutil.which(name)
    if not p:
        sys.exit(f"[错误] 找不到 {name}：请先安装 ffmpeg 并加入 PATH")
    return p


def run(args):
    return subprocess.run(args, capture_output=True, text=True, errors="ignore")


def probe(path):
    r = run([tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path])
    if r.returncode != 0:
        sys.exit(f"[错误] ffprobe 读不了这个文件：{r.stderr.strip()[-300:]}")
    j = json.loads(r.stdout)
    v = next((s for s in j["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in j["streams"] if s.get("codec_type") == "audio"), None)
    if not v:
        sys.exit("[错误] 文件里没有视频流")
    num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    dur = float(j["format"].get("duration") or v.get("duration") or 0)
    return {"width": int(v["width"]), "height": int(v["height"]), "fps": round(fps, 3), "duration": round(dur, 3),
            "video_codec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"), "has_audio": a is not None,
            "audio_codec": a.get("codec_name") if a else None}


def video_scan(path, dur):
    """一次解码同时跑：黑帧（blackdetect）、冻帧（freezedetect）、硬切点（scene > 0.3）。"""
    vf = "scale=640:-2,blackdetect=d=0.3:pix_th=0.10,freezedetect=n=0.003:d=1.5,select='gt(scene\\,0.3)',showinfo"
    err = run([tool("ffmpeg"), "-hide_banner", "-nostats", "-i", path, "-map", "0:v:0", "-vf", vf, "-an", "-f", "null", "-"]).stderr
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", err)]
    fs = [float(x) for x in re.findall(r"freeze_start:\s*([\d.]+)", err)]
    fe = [float(x) for x in re.findall(r"freeze_end:\s*([\d.]+)", err)]
    freezes = [(s, fe[i] if i < len(fe) else dur) for i, s in enumerate(fs)]
    cuts = [float(x) for x in re.findall(r"Parsed_showinfo.*?pts_time:\s*([\d.]+)", err)]
    return blacks, freezes, cuts


def audio_scan(path, dur):
    """一次解码同时跑：静音（silencedetect -50 dB，0.3 秒以上）和响度（ebur128 积分响度 I、真峰值）。"""
    af = "silencedetect=n=-50dB:d=0.3,ebur128=peak=true:framelog=quiet"
    err = run([tool("ffmpeg"), "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0", "-af", af, "-f", "null", "-"]).stderr
    ss = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", err)]
    se = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", err)]
    spans = [(max(0.0, s), se[i] if i < len(se) else dur) for i, s in enumerate(ss)]
    silent = sum(b - a for a, b in spans)
    m = re.search(r"Integrated loudness:\s*I:\s*(-?[\d.]+|-inf)\s*LUFS", err)
    p = re.search(r"True peak:\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS", err)
    lufs = float(m.group(1)) if m and m.group(1) != "-inf" else None
    peak = float(p.group(1)) if p and p.group(1) != "-inf" else None
    return {"silence_ratio": round(silent / dur, 3) if dur else 0.0, "silences": spans, "lufs": lufs, "true_peak": peak}


def sheet(path, out, every, cols, width, dur):
    """每 every 秒取一帧，缩到 width 宽，cols 列拼成一张图。"""
    n = max(1, int(math.ceil(dur / every)))
    rows = int(math.ceil(n / cols))
    vf = f"fps=1/{every},scale={width}:-2,tile={min(cols, n)}x{rows}:padding=4:margin=4:color=0x202020"
    r = run([tool("ffmpeg"), "-hide_banner", "-v", "error", "-y", "-i", path, "-vf", vf, "-frames:v", "1", "-q:v", "3", out])
    return out if r.returncode == 0 and os.path.exists(out) else None


def beat_check(path, cuts):
    """用 beats.py 分析成片音轨，算切点落在拍点 ±80 毫秒内的比例，和「随便切」的期望比例对比。"""
    from beats import analyze
    a = analyze(path)
    beats, period = a["beats"], a["beat_period"]
    on = [c for c in cuts if beats and min(abs(c - b) for b in beats) <= 0.08]
    expect = min(1.0, 2 * 0.08 / period) if period else 0.0
    drop = a["drops"][0]["time"] if a["drops"] else None
    return {"bpm": a["bpm"], "beat_period": period, "drops": a["drops"], "on_beat": len(on), "cuts": len(cuts),
            "ratio": round(len(on) / len(cuts), 3) if cuts else None, "random_expect": round(expect, 3),
            "cut_at_drop": bool(drop is not None and any(abs(c - drop) <= 0.1 for c in cuts))}


def spike_cuts(path, fps):
    """补漏的切点检测：逐帧缩成 64×36 灰度，算相邻帧差；某一帧的差值明显高于前后几帧
    （超过邻近 8 帧中位数的 3 倍，且平均差 > 8），就算一个硬切。
    专门补 scene 阈值漏掉的「同一场景里的切换」：暗色调、同一个房间、叠着同一层界面的镜头之间。"""
    import numpy as np
    raw = run_bytes([tool("ffmpeg"), "-v", "error", "-i", path, "-an", "-vf", "scale=64:36,format=gray", "-f", "rawvideo", "-"])
    n = len(raw) // (64 * 36)
    if n < 3 or not fps:
        return []
    fr = np.frombuffer(raw[: n * 64 * 36], np.uint8).reshape(n, 36, 64).astype(np.float32)
    d = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))
    out = []
    for i in range(len(d)):
        nb = np.concatenate([d[max(0, i - 4):i], d[i + 1:i + 5]])
        base = float(np.median(nb)) if len(nb) else 0.0
        if d[i] > max(3.0 * base, 8.0):
            t = round((i + 1) / fps, 3)
            if not out or t - out[-1] > 0.15:
                out.append(t)
    return out


def compose_scan(path, dur):
    """构图检查：每秒取一帧（160×90），算三样——
    空底：一帧里最多的那种颜色占多大面积（>50% 说明大片留白或空底摆卡片）；
    构图雷同：相隔 3 秒以上的两帧，边缘分布有多像（平均 >0.45 说明版式重复、机位不变）；
    每秒变化：相邻两秒的帧有多像（平均 >0.82 说明画面像幻灯片翻页或一直同一个机位）。
    阈值用原片和翻车片校准过：动画、实拍、地图类原片都在阈值内。"""
    import numpy as np
    w, h = 160, 90
    raw = run_bytes([tool("ffmpeg"), "-v", "error", "-i", path, "-vf", f"fps=1,scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    n = len(raw) // (w * h * 3)
    if n < 6:
        return None
    F = np.frombuffer(raw[: n * w * h * 3], np.uint8).reshape(n, h, w, 3).astype(np.float32)

    def bgfrac(f):
        q = (f // 32).astype(int)
        k = q[..., 0] * 64 + q[..., 1] * 8 + q[..., 2]
        return np.bincount(k.ravel(), minlength=512).max() / k.size

    def layout(f):
        g = f.mean(2)
        e = np.abs(np.diff(g, axis=1))[:-1, :] + np.abs(np.diff(g, axis=0))[:, :-1]
        H, W = e.shape
        e = e[: H // 10 * 10, : W // 10 * 10].reshape(H // 10, 10, W // 10, 10).mean((1, 3)).ravel()
        e = e - e.mean()
        nn = np.linalg.norm(e)
        return e / nn if nn else e

    bf = np.array([bgfrac(f) for f in F])
    L = np.array([layout(f) for f in F])
    far = [float(L[i] @ L[j]) for i in range(n) for j in range(i + 3, n)]
    adj = [float(L[i] @ L[i + 1]) for i in range(n - 1)]
    return {"empty_share": round(float((bf > 0.5).mean()), 3), "layout_sim": round(float(np.mean(far)), 3) if far else None,
            "adjacent_sim": round(float(np.mean(adj)), 3), "samples": n}


# ---------- 动态检查（逐帧光流，scripts/motion.py） ----------
# 门槛按 Opus 5.5 原片实测定的：科学讲解、AI 素材包装、魔塔 MV、四渡赤水四条原片全部能过（见 references/08-自检与评分.md「动态检查」）
MOTION_RULES = {
    "动作最快速度_中位px每秒": (">=", 350, "动作太慢：主要动作最快时至少每秒 350 像素（1920 宽），0.25–0.5 秒做完、位移拉大"),
    "动作速度_快的那10%": (">=", 170, "画面里最快的那些东西也不够快：加从画外飞入、冲刺、砸下这类快动作，别全是慢慢挪"),
    "快起慢停_占比%": (">=", 15, "快起慢停的动作太少：入场用 beat()、小东西用 pop()、大东西用 travel()，一下冲出去再软着陆"),
    "慢起慢停_占比%": ("<=", 60, "两头对称的慢进慢出太多，显得软、飘：inOutCubic 只给镜头用，物体动作改用 beat()、drop()、pop()"),
    "动作用时_中位秒": ("<=", 0.8, "动作拖：一个动作 0.25–0.5 秒做完（入场、走位、弹出、落下），超过 0.6 秒的只留给镜头运动"),
    "原地晃动_面积%": ("<=", 15, "一直在晃：漂浮、上下浮动这类原地小晃面积太大，它只能当底噪（幅度 ≤ 画面高 1%），换成从 A 到 B 的动作"),
    "镜头在动_占比%": ("<=", 90, "镜头一直在动：推完要停，让动作在静止的镜头里发生；不要每个镜头从头推到尾"),
    "动作长短变化": (">=", 0.6, "每个动作一个样（时长都差不多）：按动作类型挑写法——砸下 drop 0.1–0.13 秒、入场 beat 0.3 秒、小东西 pop 0.2 秒、大东西走位 0.5–0.7 秒"),
    "一帧冲到全速_占比%": ("<=", 35, "起步太猛、没有加速过程：入场用 beat()（先预备一下再冲），砸下用 drop()（先慢后快、到点急停），别所有动作都从全速开始"),
    "画面重播_占比%": ("<=", 2, "画面在重播：同一个动作每拍播一遍（t % 周期 写的主动作、顿推）。按拍点写成一串只发生一次的事件（ev()），高潮每 2.4 秒至少 5 件不同的事，同类冲击逐级加码（ramp()）；t % 周期 只给眨眼、粒子、滚动背景"),
}


# 片子路子：MV、卡点、打斗这类一直热闹的片子，再加四条下限（按魔塔 MV、「眩晕」两条原片定，两条都能过：镜头 74%/39%、空档 11%/3%、在动面积 22%/18%、最长停顿 0.2 秒）
STYLE_RULES = {
    "mv": {
        "镜头在动_占比%": (">=", 35, "MV 镜头太静：两拍之间匀速慢推 push()，重拍上 punch() 顿推、甩镜头 whip() 转场、打斗跟着动作摇，镜头在动的时间不低于三成半"),
        "没有明显动作_占比%": ("<=", 20, "空档太多：MV 里任何时刻都要有东西在演，动作之间最多停 0.2–0.5 秒；加一次性的伴奏事件（怪物从画外冲上来、碎片炸开、背景层滑过），别用循环晃动填空档"),
        "明显动作_面积%": (">=", 12, "在动的面积太小：动作要大，冲刺、飞入、砸地要占画面一大块，特效层（速度线、闪光、冲击波）铺开"),
        "最长没动作秒": ("<=", 1.0, "有一段超过 1 秒什么都没在动：补动作或切镜头"),
    },
}


def motion_check(v, out_dir, add, style=None):
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import motion
    summ, ser, segs = motion.analyze(v)
    rep = motion.repeats(v)                       # 画面在重播：扣掉镜头之后，隔固定时间画面又回到原样
    summ = {**summ, "画面重播_占比%": rep["画面重播_占比%"]}
    with open(os.path.join(out_dir, "motion.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": summ, "series": ser, "segments": segs, "repeats": rep}, f, ensure_ascii=False)
    rules = {**MOTION_RULES, **STYLE_RULES.get(style or "", {})}
    for key, (op, thr, fix) in rules.items():
        val = summ.get(key)
        if val is None:
            add(key.split("_")[0], PASS, "量不出（完整的动作太少）")
            continue
        good = val >= thr if op == ">=" else val <= thr
        unit = " 像素/秒" if (key.endswith("px每秒") or key == "动作速度_快的那10%") else ("%" if key.endswith("%") else ("秒" if key.endswith("秒") else ""))
        name = {"动作最快速度": "动作快不快", "动作速度": "快动作够不够", "动作长短变化": "动作有长有短", "一帧冲到全速": "起步有加速",
                "没有明显动作": "空档", "明显动作": "在动的面积", "最长没动作秒": "最长停顿", "画面重播": "画面在重播"}.get(key.split("_")[0] if "_" in key else key, key.split("_")[0])
        add(name, PASS if good else WARN,
            (f"{key.split('_')[0]}（{key.split('_')[1].replace('px每秒', '')}）" if "_" in key else key) + f"= {val}{unit}（要求 {op} {thr}{unit}）" + ("" if good else "：" + fix))
    # 逐帧条：开头 2.4 秒、最热闹的 2.4 秒，每格 0.2 秒——自己数同屏有几样东西在做「从 A 到 B」的动作，少于 3 样就返工
    t = ser["t"]; a = ser["act_area"]
    best, bt = -1, 0.0
    for i in range(len(t)):
        win = [x for x, tt in zip(a[i:], t[i:]) if tt < t[i] + 2.4 and x is not None]
        if win and sum(win) > best:
            best, bt = sum(win), t[i]
    strips = {}
    for name, st in (("逐帧条_开头", 0.0), ("逐帧条_最热闹", bt)):
        p = os.path.join(out_dir, name + ".jpg")
        run([tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{st:.2f}", "-t", "2.4", "-i", v, "-vf",
             "fps=5,scale=480:-2,tile=4x3:padding=4:color=white", "-frames:v", "1", p])
        strips[name] = p if os.path.exists(p) else None
    add("同屏几样在动", PASS, "看「逐帧条_开头.jpg」「逐帧条_最热闹.jpg」（每格 0.2 秒）：数一数同时在做「从 A 到 B」动作的东西，少于 3 样就返工")
    return summ, strips


def run_bytes(args):
    return subprocess.run(args, capture_output=True).stdout


def main():
    ap = argparse.ArgumentParser(description="成片自检：规格、黑帧、冻帧、构图、动态（动作快慢、起停、原地晃、镜头）、静音、响度、卡拍、联系表")
    ap.add_argument("video")
    ap.add_argument("--expect-size", help="期望尺寸，如 1920x1080")
    ap.add_argument("--expect-dur", type=float, help="期望时长（秒）")
    g1, g2 = ap.add_mutually_exclusive_group(), ap.add_mutually_exclusive_group()
    g1.add_argument("--music", action="store_true", help="成片有背景音乐")
    g1.add_argument("--no-music", action="store_true", help="成片没有背景音乐")
    g2.add_argument("--voice", action="store_true", help="成片有人声/配音")
    g2.add_argument("--no-voice", action="store_true", help="成片没有人声")
    ap.add_argument("--out", help="报告和预览图目录（默认：成片旁边的 <成片名>_qa/）")
    ap.add_argument("--allow-solid-bg", action="store_true", help="风格本身就是纯色大底（动态排版快切），不查「画面空不空」")
    ap.add_argument("--fixed-layout", action="store_true", help="风格本身就是固定界面（交互网页录屏、说明书），不查「构图变化」「每秒变化」")
    ap.add_argument("--style", choices=["mv"], help="片子路子：mv = MV、卡点、打斗这类一直热闹的片子，动态检查再加镜头、空档、在动面积、最长停顿四条下限")
    ap.add_argument("--skip-motion", action="store_true", help="跳过动态检查（逐帧光流，60 秒成片要两三分钟）。只在反复调同一处时用，交付前必须跑")
    args = ap.parse_args()
    v = os.path.abspath(args.video)
    if not os.path.exists(v):
        sys.exit(f"[错误] 找不到成片：{v}")
    out_dir = os.path.abspath(args.out or os.path.splitext(v)[0] + "_qa")
    os.makedirs(out_dir, exist_ok=True)
    items = []
    add = lambda name, st, detail: items.append({"item": name, "status": st, "detail": detail})

    info = probe(v)
    w, h, fps, dur = info["width"], info["height"], info["fps"], info["duration"]
    add("规格", PASS, f"{w}×{h}，{fps:g} fps，{dur:.2f} 秒，{'有' if info['has_audio'] else '没有'}音轨"
                      f"（{info['video_codec']} / {info['pix_fmt']}{' / ' + info['audio_codec'] if info['audio_codec'] else ''}）")
    if args.expect_size:
        m = re.match(r"^\s*(\d+)\s*[x×X*]\s*(\d+)\s*$", args.expect_size)
        if not m:
            sys.exit("[错误] --expect-size 要写成 1920x1080")
        ew, eh = int(m.group(1)), int(m.group(2))
        add("尺寸", PASS if (w, h) == (ew, eh) else FAIL, f"实际 {w}×{h}，期望 {ew}×{eh}")
    if args.expect_dur is not None:
        diff, tol = dur - args.expect_dur, max(0.1, 1.5 / fps if fps else 0.1)
        add("时长", PASS if abs(diff) <= tol else (WARN if abs(diff) <= 1.0 else FAIL),
            f"实际 {dur:.2f} 秒，期望 {args.expect_dur:.2f} 秒（差 {diff:+.2f} 秒）")
    if info["pix_fmt"] not in ("yuv420p", "yuvj420p"):
        add("像素格式", WARN, f"{info['pix_fmt']}：很多播放器和平台只认 yuv420p")
    if (args.music or args.voice) and not info["has_audio"]:
        add("音轨", FAIL, "声明了有音乐/人声，但成片里没有音轨")
    if args.no_music and args.no_voice and info["has_audio"]:
        add("音轨", WARN, "声明了既没音乐也没人声，但成片里有音轨")

    blacks, freezes, cuts = video_scan(v, dur)
    extra = [c for c in spike_cuts(v, fps) if all(abs(c - k) > 0.15 for k in cuts)]
    cuts = sorted(cuts + extra)
    add("黑帧", WARN if blacks else PASS, ("超过 0.3 秒的黑场：" + "，".join(f"{a:.2f}–{b:.2f} 秒" for a, b in blacks))
        if blacks else "没有超过 0.3 秒的黑场")
    tail = [f for f in freezes if f[1] >= dur - 0.15]
    mid = [f for f in freezes if f not in tail]
    note = f"；提示：片尾定格 {dur - tail[0][0]:.1f} 秒（有意的结尾停留可以忽略）" if tail else ""
    add("冻帧", WARN if mid else PASS, ("画面停住超过 1.5 秒：" + "，".join(f"{a:.2f}–{b:.2f} 秒" for a, b in mid) + note)
        if mid else "没有中途停住超过 1.5 秒的画面" + note)

    cs = compose_scan(v, dur)
    if cs:
        if args.allow_solid_bg:
            cs["empty_share_raw"], cs["empty_share"] = cs["empty_share"], 0.0
        if args.fixed_layout:
            cs["layout_sim_raw"], cs["adjacent_sim_raw"] = cs["layout_sim"], cs["adjacent_sim"]
            cs["layout_sim"], cs["adjacent_sim"] = None, 0.0
        add("画面空不空", WARN if cs["empty_share"] >= 0.5 else PASS,
            ("按风格放过（纯色大底）" if args.allow_solid_bg else f"{cs['empty_share']:.0%} 的帧有一半以上面积是同一种底色") + ("：大片留白或空底上摆卡片，把主体放大、画面铺满场景" if cs["empty_share"] >= 0.5 else ""))
        if cs["layout_sim"] is not None:
            add("构图变化", WARN if cs["layout_sim"] > 0.45 else PASS,
                f"相隔 3 秒以上的画面平均相似度 {cs['layout_sim']:.2f}（>0.45 算雷同）" + ("：各镜头版式重复、机位不变，换景别和角度" if cs["layout_sim"] > 0.45 else ""))
        add("每秒变化", WARN if cs["adjacent_sim"] > 0.82 else PASS,
            ("按风格放过（固定界面）" if args.fixed_layout else f"相邻两秒画面平均相似度 {cs['adjacent_sim']:.2f}（>0.82 算太少）") + ("：像幻灯片翻页或一直同一个机位，加相机运动和镜头切换" if cs["adjacent_sim"] > 0.82 else ""))

    au, bc = None, None
    if info["has_audio"]:
        au = audio_scan(v, dur)
        sr = au["silence_ratio"]
        st, d = PASS, f"静音（低于 -50 dB 且持续 0.3 秒以上）占 {sr:.0%}"
        if args.music and sr > 0.8:
            st, d = WARN, d + "：声明有音乐，却几乎全是静音——音乐没混进去，或音量太小"
        elif args.no_music and sr < 0.03:
            st, d = WARN, d + "：声明没有音乐，但句间几乎没有静音——可能垫了音乐或有持续底噪"
        elif sr > 0.95 and not (args.no_music and args.no_voice):
            st, d = WARN, d + "：音轨几乎全是静音"
        add("静音", st, d)
        L, pk = au["lufs"], au["true_peak"]
        if L is None:
            add("响度", WARN, "测不出整体响度（音轨可能全是静音）")
        else:
            hint = "，偏小：可用 ffmpeg 的 loudnorm=I=-14:TP=-1 拉上来" if L < -16 else ("，偏大：上平台可能被压" if L > -12 else "")
            ok = -16 <= L <= -12 and (pk is None or pk <= -1.0)
            pk_s = f"，真峰值 {pk:.1f} dBTP" + ("（超过 -1，转码后可能破音）" if pk > -1.0 else "") if pk is not None else ""
            add("响度", PASS if ok else WARN, f"整体 {L:.1f} LUFS（建议 -16 到 -12）{hint}{pk_s}")
    elif not (args.music or args.voice):
        add("音频", PASS, "没有音轨，跳过静音/响度/卡拍检查")

    cut_s = f"硬切 {len(cuts)} 处（场景变化 > 0.3，加上相邻帧差突变 {len(extra)} 处）" + ("：" + " ".join(f"{c:.2f}" for c in cuts[:30]) if cuts else "")
    add("硬切点", PASS, cut_s + (" …" if len(cuts) > 30 else ""))
    if info["has_audio"] and not args.no_music:
        try:
            bc = beat_check(v, cuts)
        except Exception as e:  # 音乐分析失败不影响其它检查
            add("卡拍", WARN, f"音乐分析失败：{e}")
        else:
            drop = f"；高潮砸下来在 {bc['drops'][0]['time']:.2f} 秒" + ("，那里有切点" if bc["cut_at_drop"] else "，那里没有切点") if bc["drops"] else ""
            if not cuts:
                add("卡拍", PASS, f"BPM≈{bc['bpm']:.1f}；没有硬切，不查卡拍{drop}")
            else:
                good = len(cuts) < 4 or bc["ratio"] >= min(0.9, 1.5 * bc["random_expect"])
                add("卡拍", PASS if good else WARN, f"BPM≈{bc['bpm']:.1f}；{bc['on_beat']}/{len(cuts)} 个切点落在拍点 ±80 毫秒内"
                    f"（{bc['ratio']:.0%}），随便切的期望约 {bc['random_expect']:.0%}{drop}")

    ms, strips = None, {}
    if not args.skip_motion:
        try:
            ms, strips = motion_check(v, out_dir, add, args.style)
        except Exception as e:  # 动态分析失败不影响其它检查
            add("动态检查", WARN, f"没跑成：{e}")
    imgs = {"联系表": sheet(v, os.path.join(out_dir, "联系表.jpg"), 2, 6, 320, dur),
            "手机尺寸": sheet(v, os.path.join(out_dir, "手机尺寸.jpg"), 1, 5, 360, dur), **strips}
    nf, nw = sum(i["status"] == FAIL for i in items), sum(i["status"] == WARN for i in items)
    verdict = f"不通过（{nf} 项不通过，{nw} 项警告）" if nf else (f"有 {nw} 项警告" if nw else "全部通过")
    report = {"file": v, "spec": info, "verdict": verdict, "items": items, "blacks": blacks, "freezes": freezes,
              "cuts": cuts, "compose": cs, "audio": au, "beats": bc, "motion": ms, "images": imgs}
    rp = os.path.join(out_dir, "qa_report.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"== 成片自检：{os.path.basename(v)}")
    for i in items:
        print(f"[{i['status']}] {i['item']}：{i['detail']}")
    print(f"总体：{verdict}")
    for k, p in imgs.items():
        print(f"{k}：{p or '生成失败'}")
    print(f"报告：{rp}")
    sys.exit(2 if nf else 0)


if __name__ == "__main__":
    main()
