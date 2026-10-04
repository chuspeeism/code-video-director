#!/usr/bin/env python3
"""锁层：分步实现的第一遍（主动作）、第二遍（辅层）、第三遍（冲击）各自量过就锁住，防止后面返工时把前面做好的挤掉（跷跷板）。

锁住一层 = 记下这一层代码文件的指纹 + 这一遍量到的数字（写进 qa/锁.json）。
画面只由时间算出来，所以文件指纹没变，这一遍的画面和数字就一定没变；指纹变了，就得重量这一遍再锁，
数字比锁住时退步一成以上，说明这次改动把这一层挤掉了，拒绝重新上锁。

用法：
  锁住 / 重新锁：python3 lock.py lock --layer 1 --files main.js --motion qa_第1遍/motion.json
  查有没有动过：python3 lock.py check            （selfcheck.py 每轮也会自动查）
  --state 锁文件位置，默认 qa/锁.json；--force 退步了也硬锁（理由写进 --why，会记进锁文件和返工单）
退出码：0 = 没问题；4 = 有锁住的文件被改动或数字退步。"""
import argparse, hashlib, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

NAMES = {1: "主动作层", 2: "辅层", 3: "冲击层"}
# 每一遍锁哪些数字：(键, 越大越好?)
METRICS = {
    1: [("动作最快速度_中位px每秒", True), ("动作用时_中位秒", False), ("慢起慢停_占比%", False), ("画面重播_占比%", False),
        ("动作长短变化", True), ("一帧冲到全速_占比%", False)],
    2: [("辅层偏薄的镜头_占比%", False), ("成段全空的镜头_占比%", False), ("原地晃动_面积%", False), ("镜头在动_占比%", True)],
    3: [("明显动作_面积%", True), ("动作速度_快的那10%", True), ("没有明显动作_占比%", False), ("最长没动作秒", False)],
}


# 每一遍的过线标准（GATES 是 MV 档；--style calm / promo / none 换成讲述铺陈、产品快剪、只查通用几项）：没过线不许锁，除非 --force 写明理由
GATES = {
    1: {"动作最快速度_中位px每秒": (">=", 650), "动作用时_中位秒": ("<=", 0.5), "慢起慢停_占比%": ("<=", 60), "画面重播_占比%": ("<=", 2),
        "动作长短变化": (">=", 0.6), "一帧冲到全速_占比%": ("<=", 35)},
    2: {"辅层偏薄的镜头_占比%": ("<=", 20), "成段全空的镜头_占比%": ("<=", 10), "原地晃动_面积%": ("<=", 15), "镜头在动_占比%": (">=", 35)},
    3: {"明显动作_面积%": (">=", 16), "动作速度_快的那10%": (">=", 170), "没有明显动作_占比%": ("<=", 15), "最长没动作秒": ("<=", 0.5)},
}
GATES[1]["动作长短变化"] = (">=", 0.4); GATES[2]["原地晃动_面积%"] = ("<=", 20)     # MV 档校准：高潮多是短促重击、辅层要给原地小晃留空间
GENERAL = {1: {"动作最快速度_中位px每秒": (">=", 350), "动作用时_中位秒": ("<=", 0.8)}, 2: {"原地晃动_面积%": ("<=", 15)}, 3: {}}
CALM = {1: {"动作最快速度_中位px每秒": (">=", 250), "动作用时_中位秒": ("<=", 1.0), "画面重播_占比%": ("<=", 2), "动作长短变化": (">=", 0.4), "一帧冲到全速_占比%": ("<=", 45)},
        2: {"原地晃动_面积%": ("<=", 15)}, 3: {"没有明显动作_占比%": ("between", (15, 65)), "动作速度_快的那10%": ("between", (120, 400))}}
PROMO = {1: {"动作最快速度_中位px每秒": (">=", 650), "动作用时_中位秒": ("<=", 0.45), "画面重播_占比%": ("<=", 2), "动作长短变化": (">=", 0.4), "一帧冲到全速_占比%": ("<=", 45)},
         2: {"原地晃动_面积%": ("<=", 15)}, 3: {"没有明显动作_占比%": ("between", (15, 35)), "动作速度_快的那10%": ("between", (170, 650))}}


TIER_GATES = {"mv": GATES, "calm": CALM, "promo": PROMO, "none": GENERAL}


def misses(vals, gates):
    ok = lambda v, op, thr: thr[0] <= v <= thr[1] if op == "between" else (v >= thr if op == ">=" else v <= thr)
    return [(k, vals.get(k), op, thr) for k, (op, thr) in gates.items() if vals.get(k) is not None and not ok(vals[k], op, thr)]


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:16]


def measure(motion_json):
    """从 qa.py / motion.py 量出的 motion.json 取数字（缺的按镜头辅层两项从逐帧数据补算）。"""
    import motion
    j = json.load(open(motion_json, encoding="utf-8"))
    s = {**j["summary"], **motion.action_shape(j["segments"])}
    if "画面重播_占比%" not in s and "repeats" in j:
        s["画面重播_占比%"] = j["repeats"]["画面重播_占比%"]
    if "辅层偏薄的镜头_占比%" not in s:
        sl = motion.shot_layers(j["series"])
        if sl:
            s["辅层偏薄的镜头_占比%"] = round(100.0 * sum(a < motion.AUX_THIN for _, _, a, _ in sl) / len(sl), 1)
            s["成段全空的镜头_占比%"] = round(100.0 * sum(e > motion.EMPTY_SHOT for _, _, _, e in sl) / len(sl), 1)
    return s


def worse(key, up, old, new):
    """新值比锁住时退步一成以上？百分比项锁住时很小（<10）就按 2 个点算，秒数项至少 0.03 秒。"""
    if old is None or new is None:
        return False
    tol = abs(old) * 0.10
    if key.endswith("%"):
        tol = max(tol, 2.0)
    elif key.endswith("秒"):
        tol = max(tol, 0.03)
    return (old - new > tol) if up else (new - old > tol)


def load(state):
    return json.load(open(state, encoding="utf-8")) if os.path.exists(state) else {}


def check(state):
    """查锁住的文件有没有被改动。返回 [(层, 文件)]。"""
    out = []
    for k, rec in sorted(load(state).items()):
        for f, h in rec["files"].items():
            if not os.path.exists(f) or sha(f) != h:
                out.append((int(k), os.path.relpath(f) if f.startswith(os.getcwd()) else f))
    return out


def do_lock(a):
    st = load(a.state)
    new = measure(a.motion)
    nums = {k: new.get(k) for k, _ in METRICS[a.layer]}
    old = st.get(str(a.layer))
    secs = json.load(open(a.motion, encoding="utf-8")).get("sections") or []      # qa.py 带 --sections 量的：每段按自己的档过线
    if secs:
        miss = [(f"[{x['起秒']:g}–{x['止秒']:g} 秒·{x['档']}] {k}", v, op, thr) for x in secs
                for k, v, op, thr in misses({**x["summary"], "画面重播_占比%": new.get("画面重播_占比%")}, TIER_GATES.get(x["档"], GATES)[a.layer])]
    else:
        miss = misses(new, TIER_GATES.get(a.style, GATES)[a.layer])
    if miss and not a.force:
        print(f"[不能上锁] 第 {a.layer} 层（{NAMES[a.layer]}）还没过这一遍的线：")
        for k, v, op, thr in miss:
            print(f"  · {k.split('_')[0]} = {v}（要 {f'{thr[0]}–{thr[1]}' if op == 'between' else f'{op} {thr}'}）")
        print("先在这一遍里改到过线再锁；确实过不了，加 --force --why \"理由\"（会写进锁文件和交付说明）")
        sys.exit(4)
    bad = []
    if old:
        bad = [(k, old["metrics"].get(k), nums.get(k)) for k, up in METRICS[a.layer] if worse(k, up, old["metrics"].get(k), nums.get(k))]
    if bad and not a.force:
        print(f"[拒绝重新上锁] 第 {a.layer} 层（{NAMES[a.layer]}）被挤掉了——这次改动让下面这些数字比锁住时退步一成以上：")
        for k, o, n in bad:
            print(f"  · {k.split('_')[0]}：{o} → {n}")
        print("把这次改动退回去，或者换一种不伤这一层的改法；确实要接受，加 --force --why \"理由\"（会写进锁文件和返工单）")
        sys.exit(4)
    st[str(a.layer)] = {"name": NAMES[a.layer], "files": {os.path.abspath(f): sha(f) for f in a.files}, "metrics": nums, "motion": a.motion,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"), **({"forced": a.why or "（没写理由）"} if (bad or miss) else {})}
    os.makedirs(os.path.dirname(os.path.abspath(a.state)), exist_ok=True)
    json.dump(st, open(a.state, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"第 {a.layer} 层（{NAMES[a.layer]}）已锁：{', '.join(a.files)}")
    for k, _ in METRICS[a.layer]:
        print(f"  · {k.split('_')[0]} = {nums.get(k)}")


def main():
    ap = argparse.ArgumentParser(description="锁层：分步实现每一遍量过就锁住，防止返工时把前面做好的挤掉")
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("lock", help="锁住 / 重新锁一层")
    l.add_argument("--layer", type=int, choices=[1, 2, 3], required=True)
    l.add_argument("--files", nargs="+", required=True, help="这一层的代码文件")
    l.add_argument("--motion", required=True, help="这一遍渲染（render.mjs --layers 1..N）用 qa.py 量出的 motion.json")
    l.add_argument("--force", action="store_true"); l.add_argument("--why", default="")
    l.add_argument("--style", choices=["mv", "calm", "promo", "none"], default="mv", help="节奏档：mv（默认）MV 打斗；calm 讲述铺陈；promo 快剪；none 只查通用几项（和 qa.py 用同一档）")
    c = sub.add_parser("check", help="查锁住的文件有没有被改动")
    for p in (l, c):
        p.add_argument("--state", default=os.path.join("qa", "锁.json"))
    a = ap.parse_args()
    if a.cmd == "lock":
        do_lock(a)
    else:
        bad = check(a.state)
        if not load(a.state):
            print("还没有锁住任何一层"); return
        if not bad:
            print("锁住的层都没被改动"); return
        for k, f in bad:
            print(f"第 {k} 层（{NAMES[k]}）被改动：{f}——先重渲这一遍（render.mjs --layers 1..{k}）、qa.py 重量，再 lock.py lock --layer {k} 重新锁")
        sys.exit(4)


if __name__ == "__main__":
    main()
