# 致谢与授权

本 skill 的部分做法参考了下面的开源项目和公开文章，在此致谢：

- **howseen-ai/claude-motion-design**（Raphaël Aubry，MIT License）：seek(t) 确定性逐帧渲染、子帧混合做动态模糊、按能量找音乐高潮点、音效按峰值对齐、静帧先行和看帧打分的自检循环。`scripts/render.mjs`、`scripts/beats.py` 中借鉴其思路的地方已在文件头注明。
  原项目授权声明：

  > MIT License — Copyright (c) 2026 Howseen AI (Raphaël Aubry). Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions: The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

- **观默（@guanmo_ai）《用 Codex 自动生成顶尖动效视频》**：导演层 brief、主次与景深、错相、「预备 → 冲击 → 回稳」的运动语言、先建拍点网格再做动画。本 skill 用自己的话重写了这些原则，没有复制原文。
