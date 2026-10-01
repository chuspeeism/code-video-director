#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""beat_timeline.py —— 卡点片的镜头时间表：按歌的拍点切镜头，让高潮正好砸在片子里最重要的那一刻。

用法：
  python3 beat_timeline.py <歌.mp3> --film-dur 60 --key 33 [--drop 1] [--quiet-every 4] [--loud-every 1]
                           [--min-shot 0.4] [--out docs/timeline.json]
  --film-dur      成片时长（秒）
  --key           片子里最重要那一刻（反转、变身、亮相）在第几秒；歌的高潮会对准它
  --drop          用 beats.py 找到的第几个高潮候选（默认 1 = 最强的那个）
  --song-start    不给 --key 时，直接指定歌从第几秒开始放
  --quiet-every   安静段每几拍切一刀（默认 4 = 每小节一刀）
  --loud-every    响亮段每几拍切一刀（默认 1 = 每拍一刀；字多的地方改成 2）
  --min-shot      最短镜头秒数（默认 0.4，太碎会看不清）
输出：timeline.json（歌的起点、裁歌命令、每个镜头的起止和所在段落、片内拍点和重拍），终端打印镜头表。
依赖：同目录的 beats.py（numpy + ffmpeg）。
"""
import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from beats import analyze  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="按拍点生成卡点片的镜头时间表")
    ap.add_argument("song")
    ap.add_argument("--film-dur", type=float, required=True)
    ap.add_argument("--key", type=float)
    ap.add_argument("--drop", type=int, default=1)
    ap.add_argument("--song-start", type=float)
    ap.add_argument("--quiet-every", type=int, default=4)
    ap.add_argument("--loud-every", type=int, default=1)
    ap.add_argument("--min-shot", type=float, default=0.4)
    ap.add_argument("--out", default="timeline.json")
    a = ap.parse_args()

    an = analyze(a.song)
    D = a.film_dur
    drop = None
    if a.key is not None and an["drops"]:
        drop = an["drops"][min(len(an["drops"]), max(1, a.drop)) - 1]["time"]
        start = drop - a.key
    else:
        start = a.song_start or 0.0
    notes = []
    if start < 0:
        notes.append(f"高潮在歌里第 {drop:.2f} 秒，比片里的 {a.key:.2f} 秒还早：片头会有 {-start:.2f} 秒没有音乐。"
                     "要么把 --key 往后调，要么换一个更靠后的高潮候选（--drop 2）。")
    if an["duration"] - max(start, 0) < D:
        notes.append(f"从第 {start:.2f} 秒放到歌尾只有 {an['duration'] - max(start, 0):.1f} 秒，不够 {D:.1f} 秒的片长。")

    to_film = lambda t: round(t - start, 3)
    beats = [to_film(t) for t in an["beats"] if 0 <= t - start <= D]
    downs = [to_film(t) for t in an["downbeats"] if 0 <= t - start <= D]
    secs = []
    for s in an["sections"]:
        s0, s1 = max(0.0, s["start"] - start), min(D, s["end"] - start)
        if s1 > s0:
            secs.append({"start": round(s0, 3), "end": round(s1, 3), "level": s["level"]})
    if not secs:
        secs = [{"start": 0.0, "end": D, "level": "loud"}]

    def level_at(t):
        return next((s["level"] for s in secs if s["start"] <= t < s["end"]), secs[-1]["level"])

    cuts = {0.0}
    for s in secs:
        every = a.quiet_every if s["level"] == "quiet" else a.loud_every
        inside = [b for b in beats if s["start"] <= b < s["end"]]
        first_down = next((i for i, b in enumerate(inside) if b in downs), 0)
        cuts.update(inside[first_down::max(1, every)])
    key_film = round(a.key, 3) if a.key is not None else None
    if key_film is not None:
        cuts.add(key_film)
    cuts = sorted(c for c in cuts if 0 <= c < D)
    kept = []
    for c in cuts:                                         # 去掉太碎的镜头，但保住 0 秒和关键时刻
        if kept and c - kept[-1] < a.min_shot and c != key_film:
            continue
        if kept and c - kept[-1] < a.min_shot and c == key_film:
            kept.pop()
        kept.append(c)
    edges = kept + [D]
    shots = []
    for i in range(len(kept)):
        s0, s1 = edges[i], edges[i + 1]
        shots.append({"id": f"s{i + 1:02d}", "start": round(s0, 3), "end": round(s1, 3), "len": round(s1 - s0, 3),
                      "section": "安静" if level_at(s0) == "quiet" else "响亮",
                      "on_downbeat": any(abs(s0 - d) < 0.03 for d in downs),
                      "is_key": key_film is not None and abs(s0 - key_film) < 0.03})
    trim = (f'ffmpeg -y -ss {max(start, 0):.3f} -t {D:.3f} -i "{a.song}" '
            f'-af "afade=t=in:d=0.3,afade=t=out:st={max(0.0, D - 1.5):.3f}:d=1.5" music_cut.wav')
    out = {"song": os.path.abspath(a.song), "bpm": an["bpm"], "beat_period": an["beat_period"],
           "song_start": round(start, 3), "drop_in_song": drop, "key_in_film": key_film, "film_dur": D,
           "trim_cmd": trim, "sections": secs, "beats": beats, "downbeats": downs, "shots": shots, "notes": notes}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"== 卡点时间表：{os.path.basename(a.song)}  BPM≈{an['bpm']:.1f}  每拍 {an['beat_period']:.3f} 秒")
    if drop is not None:
        print(f"歌从第 {start:.2f} 秒开始放；歌里的高潮 {drop:.2f} 秒 → 落在片子第 {key_film:.2f} 秒")
    print("段落：" + "；".join(f"{s['start']:.1f}–{s['end']:.1f} 秒{'安静' if s['level'] == 'quiet' else '响亮'}" for s in secs))
    print(f"镜头 {len(shots)} 个（安静段每 {a.quiet_every} 拍一刀，响亮段每 {a.loud_every} 拍一刀）：")
    for s in shots:
        flag = "  ← 关键时刻" if s["is_key"] else ("  重拍" if s["on_downbeat"] else "")
        print(f"  {s['id']}  {s['start']:6.2f}–{s['end']:6.2f}  ({s['len']:.2f} 秒，{s['section']}){flag}")
    for n in notes:
        print("[注意] " + n)
    print(f"裁歌：{trim}")
    print(f"已写入：{os.path.abspath(a.out)}")


if __name__ == "__main__":
    main()
