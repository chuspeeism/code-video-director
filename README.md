# code-video-director · 代码视频导演

用代码做视频，好片子和废片的差别不在提示词，而在制作经验：先定风格和节拍，镜头怎么换，字停多久，交付前怎么自查。

这个 skill 把这些经验打包好了。换成其他模型（比如 Codex 里的 GPT-6），带上它，一样能做出像样的片子：动画短片、MV、绘本动画、科普讲解、历史地图纪录片、产品宣传片、三维组装说明、生图素材剪辑包装、Blender 三渲二动画。

## 它管什么

一句话需求进去，模型按导演的流程做到交付：

1. **写导演单**：把你点名的硬要求（歌、工具、风格、有没有旁白）逐条抄下，再补上风格元素清单、内容核心、声音方案。
2. **声音先行**：先测音乐的节拍和高潮点，或者先合成旁白、量好每句时长，再排镜头。卡点片直接用脚本按拍点出镜头时间表，高潮对准最重要的画面。
3. **按电影的画面语言做**：画面铺满，主体够大，景别轮换，大字砸进来，高潮给特写。不做成网页幻灯片。
4. **逐帧确定性渲染**：每一帧只由时间算出来，渲两次一模一样，返工只重渲那几秒。
5. **先静帧、后全片、再自检**：自检脚本查规格、黑帧、冻结、响度、切点落拍，还会查构图是否空、是否雷同、是否一直不动，没过就返工。

## 效果

拿同一句提示词，让 Codex（GPT-6）分别在不带 skill 和带 skill 的情况下做片，对照原片验收：

| 片子 | 不带 skill | 带 skill |
|---|---|---|
| 科学概念讲解 | 浅色网页仪表盘，主体很小，100% 的帧一半以上是空底 | 积木世界铺满画面，人物推成近景，空底帧 0% |
| 魔塔背景故事 MV | 切点落在拍上 23%，变身没对上副歌，看不到群战 | 28 刀全部落在拍上，变身砸在副歌第一拍，怪物群战、反转、笑着倒下全演出来 |
| 产品宣传片 | 75 秒，暗底小字，切点落拍 38% | 48 秒，大字砸出来，切点落拍 91% |
| AI 素材剪辑包装（生图 + 代码） | 同一个机位反复用，切了 8 刀没有一刀在拍上 | 大特写、低机位、腾空、碰杯近景轮换，18 刀全卡在拍上 |
| 二十四节气（Blender） | 30 秒一个正面固定机位，相邻两秒画面相似度 0.99，一刀不切 | 远景、桥洞仰拍、特写轮换，节气名做成竖排书法大字，23 刀全踩在鼓点上 |
| 奥德赛绘本（训练中没用过的考题） | 8 帧里 5 帧同一个构图，构图相似度 0.68 | 8 帧 8 个构图，构图相似度 0.08 |

## 安装

任选一种：

1. **用 git 装**（以后在这个文件夹里 `git pull` 就能更新）：

   ```
   git clone https://github.com/chuspeeism/code-video-director.git ~/.claude/skills/code-video-director   # Claude Code
   git clone https://github.com/chuspeeism/code-video-director.git ~/.codex/skills/code-video-director    # Codex
   ```

2. **让 Agent 帮你装**：把仓库链接 `https://github.com/chuspeeism/code-video-director` 发给 Claude Code 或 Codex，说「把这个 skill 装到我的 skills 目录」。
3. **用压缩包**：把 `code-video-director` 文件夹（压缩包解压出来就是它）整个放进 Codex 的 `~/.codex/skills/` 或 Claude Code 的 `~/.claude/skills/`。

需要：Node 18+、ffmpeg、Python 3 + numpy。网页渲染优先用系统里的 Chrome，不用另外下载浏览器。做三维动画要装 Blender。配音、生图用你自己的服务，没有就跳过或用系统语音。

第一次用之前，在做片的工作目录里准备两样东西（模型自己也会照 SKILL.md 去装）：

```
npm i playwright-core          # 渲染要用；有系统 Chrome 就不用再下载浏览器
python3 -m pip install numpy   # 测节拍、自检要用
```

`pip install` 被拦（Homebrew 等 Python 会提示 externally-managed-environment）时，建一个虚拟环境：

```
python3 -m venv .venv && .venv/bin/pip install numpy
```

之后用 `.venv/bin/python` 运行 skill 里的 Python 脚本。

## 用法

直接说你要什么片子，或者点名用它：

```
用 code-video-director 做一个 30 秒的儿童绘本动画，讲《奥德赛》。
风格：撕纸拼贴绘本风，彩色手工纸一层层贴上去，纸边带白色撕边。
配乐要轻快俏皮的复古电子小曲，画面踩着节拍切。不要旁白。
其他可以自由发挥。
```

提示词怎么写效果最好：
- 短句、口语，一行说一件事。
- 风格写一个有名字的风格，加上看得见的质感（撕纸、墨线、颗粒）。
- 点名做法（纯代码、Three.js、Blender、生图）和声音（配乐、卡点、旁白男声女声）。
- 用到本机软件就加一句「先检查电脑里有没有装 Blender，没有的话帮我装好」。
- 最后一句「其他可以自由发挥」。镜头、构图、动效交给模型。

## 目录

```
SKILL.md                 主流程和硬规则（模型先读这个）
references/              导演单模板、三条实现路线、声音、运动与镜头、风格卡、自检评分、翻车清单、写提示词与复刻
scripts/render.mjs       把 seek(t) 网页渲成视频（静帧拼图 / 全片 / 并行 / 断点续渲）
scripts/beats.py         测 BPM、拍点、小节重拍、高潮点
scripts/beat_timeline.py 卡点片的镜头时间表：高潮对准最重要的画面，按拍点排好每个镜头
scripts/qa.py            成片自检：规格、黑帧、冻结、构图（空底、雷同、变化）、静音、响度、切点落拍、联系表
templates/engine.html    seek(t) 网页模板
```

致谢见 `NOTICE.md`。
