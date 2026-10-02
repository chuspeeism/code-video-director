#!/usr/bin/env python3
"""自检循环：成片（或小样）量完动态，逐个镜头找毛病，写一张「返工单」——哪一段、什么问题、改哪个参数。
改完只重渲有问题的镜头，再跑一次，直到返工单清空（最多 3 轮，每轮结果写进交付说明）。
有参考片（用户给的原片、要复刻的片子）就加 --ref，目标改成「不比参考片差太多」；没有就用硬门槛（qa.py 里的 MOTION_RULES）。

用法：
  python3 <skill>/scripts/selfcheck.py 成片.mp4 --out qa [--ref 参考.mp4] [--round 1]
退出码：0 = 全部达标；3 = 还要返工（看 qa/返工单.md）。
依赖：numpy、ffmpeg；动态测量用同目录的 motion.py（qa.py 跑过会留下 qa/motion.json，这里直接复用，不重算）。"""
import argparse, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import numpy as np  # noqa: F401
except Exception:
    sys.exit("[错误] 缺 numpy。先运行：python3 -m pip install numpy（被拦就用 python3 -m venv .venv && .venv/bin/pip install numpy）")
import motion
from qa import MOTION_RULES, STYLE_RULES

SHOT_MIN = 1.0      # 短于 1 秒的镜头不单独判（快切段落整体看）

# 分步实现（references/11）：每个问题属于哪一层——只许改当前这一遍的层，问题出在已锁的层就明说「解锁第 X 层」从那一遍重走
LAYER_OF = {"动作慢": "第 1 层", "动作拖": "第 1 层", "慢进慢出": "第 1 层", "动作一个样": "第 1 层", "起步太猛": "第 1 层", "画面在重播": "第 1 层",
            "分镜动作复制": "第 1 层", "空档太长": "第 1/2 层", "停得太久": "第 1/2 层", "几乎没东西在动": "第 1/2 层",
            "一直在晃": "第 2 层", "辅层太薄": "第 2 层", "镜头里有空白": "第 2 层", "镜头从头推到尾": "第 2 层", "镜头太静": "第 2 层",
            "动作最快速度": "第 1 层", "动作用时": "第 1 层", "快起慢停": "第 1 层", "慢起慢停": "第 1 层", "动作长短变化": "第 1 层", "一帧冲到全速": "第 1 层", "画面重播": "第 1 层",
            "原地晃动": "第 2 层", "镜头在动": "第 2 层", "辅层偏薄的镜头": "第 2 层", "成段全空的镜头": "第 2 层",
            "明显动作": "第 3 层", "动作速度": "第 3 层", "没有明显动作": "第 1/2 层", "最长没动作秒": "第 1/2 层"}


def load_or_measure(video, out_dir, name, rep=True):
    """动态测量（qa.py 跑过就直接读 qa/motion.json）。rep=True 时连「画面在重播」一起量。返回 (summary, series, segments, repeats)。"""
    p = os.path.join(out_dir, name)
    j = None
    if os.path.exists(p) and os.path.getmtime(p) >= os.path.getmtime(video):
        j = json.load(open(p, encoding="utf-8"))
    if j is None:
        summ, ser, segs = motion.analyze(video)
        j = {"summary": summ, "series": ser, "segments": segs}
    if rep and "repeats" not in j:
        j["repeats"] = motion.repeats(video)
        j["summary"]["画面重播_占比%"] = j["repeats"]["画面重播_占比%"]
    if "辅层偏薄的镜头_占比%" not in j["summary"]:      # 旧版 qa.py 量的，没有按镜头的辅层两项：从逐帧数据补算
        sl = motion.shot_layers(j["series"])
        if sl:
            j["summary"]["辅层偏薄的镜头_占比%"] = round(100.0 * sum(a < motion.AUX_THIN for _, _, a, _ in sl) / len(sl), 1)
            j["summary"]["成段全空的镜头_占比%"] = round(100.0 * sum(e > motion.EMPTY_SHOT for _, _, _, e in sl) / len(sl), 1)
    json.dump(j, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    return j["summary"], j["series"], j["segments"], j.get("repeats")


LOOP_RE = re.compile(r"\(([^()]{0,40}\b(?:t|time|now|lt|local|q|tt|ts|sec|u)\b[^()]{0,40})\)\s*%\s*\(?\s*[\d.]+"
                     r"|\b(?:t|time|now|lt|local|q|tt|ts|sec|u)\b\s*%\s*\(?\s*[\d.]+")
SKIP_DIRS = {"node_modules", "out", "qa", "tmp", "渲染", "frames", "素材", ".git"}


def code_loops(root):
    """项目代码里用「时间 % 周期」写的循环（同一个动作每隔一段时间重播）。返回 [(文件, 行号, 片段)]。
    数量本身说明不了问题：Opus 5.5 魔塔原片 1.4 万行代码里有十来处，全是眨眼、冒汗、火星、气泡、速度线闪动这类背景小效果；
    GPT-6 的魔塔把挥剑、刀光、镜头顿推也写成了这种循环。所以只列出来让人逐行核对，不算不合格。"""
    out = []
    if not root or not os.path.isdir(root):
        return out
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".noindex") and os.path.relpath(os.path.join(dirpath, d), root).count(os.sep) < 2]
        for f in files:
            if not f.endswith((".js", ".mjs", ".html")):
                continue
            fp = os.path.join(dirpath, f)
            try:
                lines = open(fp, encoding="utf-8", errors="ignore").read().splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                if line.lstrip().startswith("//"):
                    continue
                for m in LOOP_RE.finditer(line):
                    out.append((os.path.relpath(fp, root), i, m.group(0)[:60]))
    return out


def board_copies(path):
    """分镜表里「动作 / 事件 / 相机」这类栏，是不是一整列复制同一句话（每个镜头套同一套动作，做出来就是每个镜头同一个循环）。
    返回 [(栏名, 镜头数, 同一句的个数, 那句话)]。"""
    if not path or not os.path.exists(path):
        return []
    out, rows, head = [], [], None

    def flush():
        if head and len(rows) >= 8:
            for k, h in enumerate(head):
                if not any(w in h for w in ("动作", "事件", "动态", "运动", "进场", "出场", "相机")):
                    continue
                vals = [r[k] for r in rows if k < len(r) and len(r[k]) >= 8]
                if len(vals) < 8:
                    continue
                top = max(set(vals), key=vals.count)
                if vals.count(top) >= max(3, 0.3 * len(vals)):
                    out.append((h, len(vals), vals.count(top), top))
    for line in open(path, encoding="utf-8").read().splitlines() + [""]:
        if line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if head is None:
                head = cells
            else:
                rows.append(cells)
        else:
            flush(); rows, head = [], None
    return out


def shots(ser):
    """按硬切把时间轴分成镜头：[(起秒, 止秒, 帧序号列表)]"""
    t, cut = ser["t"], ser["cut"]
    out, cur = [], []
    for i, c in enumerate(cut):
        if c:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(i)
    if cur:
        out.append(cur)
    return [(t[s[0]], t[s[-1]], s) for s in out if s]


def shot_check(ser, segs, fps, frames, style=None):
    idx = set(frames)
    g = lambda key: [ser[key][i] for i in frames if ser[key][i] is not None]
    wob, cam, act = g("wob_area"), g("cam"), g("act_area")
    mine = [s for s in segs if s["f"] in idx and s["head"] and s["tail"]]
    probs = []
    if len(mine) >= 5:
        dur = float(np.median([s["len_s"] for s in mine])); pk = float(np.median([s["peak"] for s in mine]))
        soft = float(np.mean([s["head"] == "缓入" and s["tail"] == "缓出" for s in mine])) * 100
        slow, drag = (650, 0.5) if style == "mv" else (350, 0.8)      # MV 按两条 MV 原片（最快 952/973、用时 0.30/0.47 秒）收紧
        if pk < slow:
            probs.append(("动作慢", f"动作最快时只有每秒 {pk:.0f} 像素（要 ≥ {slow}）", "缩短动作时长到 0.2–0.35 秒、拉大位移（至少画面宽 15%），用 drop()/beat()/travel()"))
        if dur > drag:
            probs.append(("动作拖", f"一个动作要 {dur:.2f} 秒（要 ≤ {drag}）", "dur 改成 0.2–0.35（MV）或 0.3–0.5；弹簧频率提到 2.5–4；做完立刻接下一件事"))
        if soft > 60:
            probs.append(("慢进慢出", f"{soft:.0f}% 的动作两头对称地慢", "物体动作的 ease 从 inOutCubic / sine 换成 beat()、drop()、pop() 或 spring(f≥2.5)"))
        lens = [s["len_s"] for s in mine]
        spread = (np.percentile(lens, 75) - np.percentile(lens, 25)) / max(1e-6, np.median(lens))
        hard = float(np.mean([s["head"] == "硬起" for s in mine])) * 100
        if len(mine) >= 8 and spread < 0.4:
            probs.append(("动作一个样", f"动作时长几乎一样（长短变化 {spread:.2f}）", "按动作类型混用：砸下 drop 0.12 秒、入场 beat 0.3 秒、小东西 pop 0.2 秒、大东西 0.5–0.7 秒"))
        if hard > 50:
            probs.append(("起步太猛", f"{hard:.0f}% 的动作一帧冲到全速", "入场改 beat()（预备 0.1 秒再冲），砸下改 drop()（先慢后快、到点急停）"))
    if wob and np.mean(wob) * 100 > 15:
        probs.append(("一直在晃", f"原地小晃占画面 {np.mean(wob) * 100:.0f}%", "把 Math.sin 浮动的幅度压到画面高 1% 以内，换成一次从 A 到 B 的动作"))
    dur_shot = len(frames) / fps
    if cam and dur_shot >= 2 and np.mean(np.array(cam) > motion.CAM_MOVE) >= 0.9:
        probs.append(("镜头从头推到尾", f"这个镜头 {dur_shot:.1f} 秒里镜头一直在动", "镜头关键帧改成：先停 → 在关键一拍 0.4–0.6 秒快推（E.outCubic）→ 停住"))
    if style == "mv" and dur_shot >= 1.5:
        if cam and np.mean(np.array(cam) > motion.CAM_MOVE) < 0.2:
            probs.append(("镜头太静", f"{dur_shot:.1f} 秒的镜头里镜头几乎不动", "两拍之间 push() 匀速慢推，重拍上 punch() 顿推，打斗跟着动作摇，或在这里切一刀"))
        if act and np.mean(np.array(act) < 0.01) >= 0.5:
            probs.append(("空档太长", f"一半以上时间没有明显动作", "补一次性的伴奏事件（怪物从画外冲上来、碎片炸开、背景层滑过），别用循环晃动填空档；动作之间最多停 0.2 秒"))
        fl = [x for x in (motion.frame_layers(ser, i) for i in frames) if x]
        if fl:
            aux = float(np.mean([a for m, a in fl])); empty = float(np.mean([(not m) and a == 0 for m, a in fl]))
            if aux < motion.AUX_THIN:
                probs.append(("辅层太薄", f"每帧平均只有 {aux:.1f} 层辅动作", "加辅层：镜头匀速缓推、一层真的在走的粒子（drift()）、一两样小东西小摆（sway()）；辅层可以循环，要小、要错开"))
            if empty > motion.EMPTY_SHOT:
                probs.append(("镜头里有空白", f"{empty * 100:.0f}% 的帧什么都没在动", "安静镜头也要有辅层托着：镜头缓推 + 飘落的粒子 + 小摆，别让画面死掉"))
        run = best = 0
        for x in act:
            run = run + 1 if x < 0.01 else 0
            best = max(best, run)
        if best / fps > 0.5:
            probs.append(("停得太久", f"有一段 {best / fps:.2f} 秒什么都没在动", "对峙、角力这类定格压到 4–6 帧（0.13–0.2 秒），或在这里补一个事件、切一刀"))
    if act and dur_shot >= 1.5 and np.mean(np.array(act) < 0.01) >= 0.8 and (not cam or np.mean(np.array(cam) > motion.CAM_MOVE) < 0.5):
        probs.append(("几乎没东西在动", f"{dur_shot:.1f} 秒里八成时间没有明显动作", "加主动作 + 跟随动作 + 一串依次飞入 / 拼装的小物件（after() 错开 0.04–0.1 秒）"))
    return probs


def main():
    ap = argparse.ArgumentParser(description="自检循环：逐个镜头找动态毛病，写返工单")
    ap.add_argument("video"); ap.add_argument("--out", default=None); ap.add_argument("--ref", default=None)
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--style", choices=["mv"], help="片子路子：mv = MV、卡点、打斗，加动作速度、动作用时、镜头、空档、在动面积、最长停顿、辅层八条")
    ap.add_argument("--board", default=None, help="分镜表（默认找成片旁边的 docs/分镜.md）：查动作栏是不是整列复制同一句")
    ap.add_argument("--src", default=None, help="项目代码目录（默认是成片所在的目录）：查主动作是不是用「时间 %% 周期」写成了循环")
    a = ap.parse_args()
    v = os.path.abspath(a.video)
    out = os.path.abspath(a.out or os.path.splitext(v)[0] + "_qa"); os.makedirs(out, exist_ok=True)
    summ, ser, segs, rep = load_or_measure(v, out, "motion.json")
    summ = {**summ, **motion.action_shape(segs)}
    fps = summ.get("帧率") or 30
    # 1. 全片目标：有参考片就跟参考片比，没有就用硬门槛
    rules = {**MOTION_RULES, **STYLE_RULES.get(a.style or "", {})}
    ref = None
    if a.ref:
        rs, _, rsegs, _ = load_or_measure(os.path.abspath(a.ref), out, "motion_参考.json", rep=False)
        ref = {**rs, **motion.action_shape(rsegs)}
        tol = {"动作最快速度_中位px每秒": 0.7, "动作速度_快的那10%": 0.7, "快起慢停_占比%": 0.6, "动作用时_中位秒": 1.3}
        for k, (op, thr, fix) in MOTION_RULES.items():
            rv = ref.get(k)
            if rv is None:
                continue
            if k in tol:
                thr = round(rv * tol[k], 2)
            elif k == "慢起慢停_占比%":
                thr = round(min(rv + 15, 75), 1)
            else:
                continue
            if k in rules and rules[k][0] == op:      # 和现有门槛（含 --style mv）取更严的那个
                thr = max(thr, rules[k][1]) if op == ">=" else min(thr, rules[k][1])
            rules[k] = (op, thr, fix + f"（参考片 {rv}）")
    glob_fail = []
    for k, (op, thr, fix) in rules.items():
        val = summ.get(k)
        if val is not None and not (val >= thr if op == ">=" else val <= thr):
            glob_fail.append((k, val, op, thr, fix))
    # 2. 逐个镜头找毛病
    rows = []
    for t0, t1, frames in shots(ser):
        if (t1 - t0) < SHOT_MIN:
            continue
        for kind, what, fix in shot_check(ser, segs, fps, frames, a.style):
            rows.append((t0, t1, kind, what, fix))
        # 画面在重播：这个镜头里有一窗被判重播（重叠 1 秒以上）
        hit = [w for w in (rep or {}).get("窗口", []) if min(t1, w[1]) - max(t0, w[0]) >= 1.0]
        if hit:
            area = max(w[2] for w in hit); per = sorted(w[3] for w in hit)[len(hit) // 2]
            rows.append((t0, t1, "画面在重播", f"每 {per:.2f} 秒画面回到原样，{area * 100:.0f}% 的画面在重播",
                         "主动作别用 t % 周期：按拍点写成一串只发生一次的事件（ev()），相邻两拍不演同一个动作，同类冲击逐级加码（ramp()）"))
    # 3. 分镜表：动作栏整列复制同一句 = 每个镜头套同一套动作
    board = a.board or os.path.join(os.path.dirname(v), "docs", "分镜.md")
    for h, n, k, top in board_copies(board):
        rows.append((0.0, summ.get("时长秒") or 0.0, "分镜动作复制", f"分镜表「{h}」一栏 {n} 个镜头里 {k} 个是同一句：{top[:40]}…",
                     "每个镜头写它自己的事件表：哪一拍发生什么、和上一个镜头不一样；高潮每 2.4 秒至少 5 件不同的事"))
    # 4. 代码里「时间 % 周期」的循环：列在返工单末尾逐行核对（不算不合格）
    loops = code_loops(a.src or os.path.dirname(v))
    # 5. 锁层（分步实现）：锁住的文件被改动 → 返工单第一行就报
    lock_state = os.path.join(out, "锁.json")
    if os.path.exists(lock_state):
        import lock
        for k, f in lock.check(lock_state):
            rows.insert(0, (0.0, summ.get("时长秒") or 0.0, "锁层被改动", f"第 {k} 层（{lock.NAMES[k]}）锁住后被改动：{f}",
                            f"先重渲这一遍（render.mjs --layers 1..{k} --out out/第{k}遍）、qa.py 重量，再 lock.py lock --layer {k} 重新锁；数字退步一成以上会被拒，说明这次改动挤掉了这一层，退回去"))
    ok = not glob_fail and not rows
    lines = [f"# 返工单（第 {a.round} 轮）", "", f"成片：`{v}`" + (f"；参考片：`{os.path.abspath(a.ref)}`" if a.ref else "；没有参考片，用硬门槛"), ""]
    if ok:
        lines += ["**全部达标。** 把这一轮的数字写进交付说明的「自检」一节。"]
    else:
        lines += ["## 全片还差的", "", "| 指标 | 属于哪层 | 现在 | 要求 | 怎么改 |", "|---|---|---|---|---|"]
        lines += [f"| {k.split('_')[0]} | {LAYER_OF.get(k.split('_')[0], '—')} | {val} | {op} {thr} | {fix} |" for k, val, op, thr, fix in glob_fail] or ["| — | — | — | — | 全片指标已达标 |"]
        lines += ["", "## 逐个镜头（只重渲这些时间段）", "", "| 时间段 | 问题 | 属于哪层 | 量到的 | 改哪里 |", "|---|---|---|---|---|"]
        lines += [f"| {t0:.1f}–{t1:.1f} 秒 | {kind} | {LAYER_OF.get(kind, '锁' if kind == '锁层被改动' else '—')} | {what} | {fix} |" for t0, t1, kind, what, fix in rows] or ["| — | — | — | — | 没有单独出问题的镜头，按全片还差的改 |"]
        lines += ["", "分步实现时：只改当前这一遍的层；问题属于已经锁住的层，就明说「解锁第 X 层」，从那一遍重新渲、重新量、重新锁（见 references/11-分步实现与锁层.md）。"]
        cmd = f"python3 {HERE}/selfcheck.py 成片.mp4 --out qa --round {a.round + 1}" + (f" --ref {a.ref}" if a.ref else "") + (f" --style {a.style}" if a.style else "")
        lines += ["", f"改完只删掉这些时间段的帧重渲，合成后再跑：`{cmd}`",
                  "最多 3 轮；第 3 轮还没清空，就把剩下的问题写进交付说明，别假装没问题。"]
    if ref:
        lines += ["", "## 跟参考片比", "", "| 指标 | 成片 | 参考片 |", "|---|---|---|"]
        lines += [f"| {k.split('_')[0]} | {summ.get(k)} | {ref.get(k, '—')} |" for k in MOTION_RULES]
    if loops:
        lines += ["", f"## 代码里的周期循环（共 {len(loops)} 处，逐行核对）", "",
                  "眨眼、冒汗、火星、气泡、速度线闪动这类背景小效果可以留；主角动作、刀光、镜头顿推、震屏用了就改成 ev() 钉在拍点上的一次性事件。", ""]
        lines += [f"- `{f}:{l}`　`{snip}`" for f, l, snip in loops[:12]] + ([f"- ……另有 {len(loops) - 12} 处"] if len(loops) > 12 else [])
    p = os.path.join(out, "返工单.md")
    open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n返工单：{p}")
    sys.exit(0 if ok else 3)


if __name__ == "__main__":
    main()
