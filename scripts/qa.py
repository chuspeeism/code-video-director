#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qa.py —— 成片自检：规格、黑帧、冻帧、构图（空底、雷同、变化）、静音、响度、硬切点卡拍，外加联系表和手机尺寸预览图。
部分思路来自 howseen-ai/claude-motion-design（MIT）

用法：python3 qa.py <成片.mp4> [--expect-size 1920x1080] [--expect-dur 60]
                  [--music | --no-music] [--voice | --no-voice] [--out 目录]
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


def run_bytes(args):
    return subprocess.run(args, capture_output=True).stdout


def main():
    ap = argparse.ArgumentParser(description="成片自检：规格、黑帧、冻帧、静音、响度、卡拍、联系表")
    ap.add_argument("video")
    ap.add_argument("--expect-size", help="期望尺寸，如 1920x1080")
    ap.add_argument("--expect-dur", type=float, help="期望时长（秒）")
    g1, g2 = ap.add_mutually_exclusive_group(), ap.add_mutually_exclusive_group()
    g1.add_argument("--music", action="store_true", help="成片有背景音乐")
    g1.add_argument("--no-music", action="store_true", help="成片没有背景音乐")
    g2.add_argument("--voice", action="store_true", help="成片有人声/配音")
    g2.add_argument("--no-voice", action="store_true", help="成片没有人声")
    ap.add_argument("--out", help="报告和预览图目录（默认：成片旁边的 <成片名>_qa/）")
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
    add("黑帧", WARN if blacks else PASS, ("超过 0.3 秒的黑场：" + "，".join(f"{a:.2f}–{b:.2f} 秒" for a, b in blacks))
        if blacks else "没有超过 0.3 秒的黑场")
    tail = [f for f in freezes if f[1] >= dur - 0.15]
    mid = [f for f in freezes if f not in tail]
    note = f"；提示：片尾定格 {dur - tail[0][0]:.1f} 秒（有意的结尾停留可以忽略）" if tail else ""
    add("冻帧", WARN if mid else PASS, ("画面停住超过 1.5 秒：" + "，".join(f"{a:.2f}–{b:.2f} 秒" for a, b in mid) + note)
        if mid else "没有中途停住超过 1.5 秒的画面" + note)

    cs = compose_scan(v, dur)
    if cs:
        add("画面空不空", WARN if cs["empty_share"] >= 0.5 else PASS,
            f"{cs['empty_share']:.0%} 的帧有一半以上面积是同一种底色" + ("：大片留白或空底上摆卡片，把主体放大、画面铺满场景" if cs["empty_share"] >= 0.5 else ""))
        if cs["layout_sim"] is not None:
            add("构图变化", WARN if cs["layout_sim"] > 0.45 else PASS,
                f"相隔 3 秒以上的画面平均相似度 {cs['layout_sim']:.2f}（>0.45 算雷同）" + ("：各镜头版式重复、机位不变，换景别和角度" if cs["layout_sim"] > 0.45 else ""))
        add("每秒变化", WARN if cs["adjacent_sim"] > 0.82 else PASS,
            f"相邻两秒画面平均相似度 {cs['adjacent_sim']:.2f}（>0.82 算太少）" + ("：像幻灯片翻页或一直同一个机位，加相机运动和镜头切换" if cs["adjacent_sim"] > 0.82 else ""))

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

    cut_s = f"硬切 {len(cuts)} 处（场景变化 > 0.3）" + ("：" + " ".join(f"{c:.2f}" for c in cuts[:30]) if cuts else "")
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

    imgs = {"联系表": sheet(v, os.path.join(out_dir, "联系表.jpg"), 2, 6, 320, dur),
            "手机尺寸": sheet(v, os.path.join(out_dir, "手机尺寸.jpg"), 1, 5, 360, dur)}
    nf, nw = sum(i["status"] == FAIL for i in items), sum(i["status"] == WARN for i in items)
    verdict = f"不通过（{nf} 项不通过，{nw} 项警告）" if nf else (f"有 {nw} 项警告" if nw else "全部通过")
    report = {"file": v, "spec": info, "verdict": verdict, "items": items, "blacks": blacks, "freezes": freezes,
              "cuts": cuts, "compose": cs, "audio": au, "beats": bc, "images": imgs}
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
