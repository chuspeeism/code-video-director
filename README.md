# code-video-director · 代码视频导演

一个让 AI 编程助手（Codex / GPT-6、Claude Code 等）**用代码做出像样视频**的 skill。

一句话需求进去，一条拿得出手的片子出来：动画短片、MV、绘本动画、科普讲解、历史地图纪录片、产品宣传片、三维组装说明、生图素材剪辑包装、Blender 三渲二动画都适用。

它不靠更长的提示词，靠的是一套制作流程：
1. 先把一句话需求补成导演单：用户点名的硬要求、有名字的风格、内容核心、声音方案；
2. 声音先行：先测音乐的节拍和高潮点、先合成旁白，再排分镜时间轴；
3. 每一帧都由时间算出来，渲两次一模一样，返工只重渲那几秒；
4. 先出静帧自查，再全片渲染；
5. 成片自己看、打分，修掉最差的问题再交付。

## 安装

把整个 `code-video-director` 文件夹放进对应目录：

- Codex：`~/.codex/skills/code-video-director/`
- Claude Code：`~/.claude/skills/code-video-director/`

需要：Node 18+、ffmpeg、Python 3 + numpy。网页渲染优先用系统里的 Chrome；做三维动画需要 Blender（可选）；配音和生图用你自己的服务（可选，没有就自动跳过或用系统语音兜底）。

## 用法

直接说你要什么片子，或者点名用它：

> 用 code-video-director 做一个 30 秒的儿童绘本动画，讲《奥德赛》……

## 目录

```
SKILL.md                 主流程和硬规则（模型先读这个）
references/              导演单模板、三条实现路线、声音、运动与镜头、风格卡、自检评分、翻车清单
scripts/render.mjs       把 seek(t) 网页渲成视频（静帧拼图 / 全片 / 并行 / 断点续渲）
scripts/beats.py         测 BPM、拍点、小节重拍、高潮点
scripts/beat_timeline.py 卡点片的镜头时间表：高潮对准最重要的画面，按拍点排好每个镜头
scripts/qa.py            成片自检：规格、黑帧、冻结、构图（空底、雷同、变化）、静音、响度、切点落拍、联系表
templates/engine.html    seek(t) 网页模板
```

致谢见 `NOTICE.md`。
