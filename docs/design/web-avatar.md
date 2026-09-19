# 前端虚拟形象：英语老师

## 目标

在对话页上加一个英语老师形象，可用开关打开/关闭。说话时嘴要动，
其余时候要有轻微的“活着”的感觉（眨眼、呼吸起伏）。

## 约束

- 前端没有构建步骤、没有依赖：`index.html` 里内联 `<style>`，`app.js`
  是纯 ES5 风格的脚本，靠 FastAPI 的静态挂载直接发出去（main.py:766）。
  任何需要 npm、打包、或引入库的方案都不合适。
- 要动的是形象的局部（嘴、眼睛），所以 SVG 必须**内联**在 HTML 里：
  用 `<img src="x.svg">` 引外部 SVG 的话，页面 CSS 碰不到它内部节点。
- 服务端不需要改：形象是纯前端的，改完刷新页面即可，无需重启。

## 形象怎么来

**手写 inline SVG，扁平矢量风格**。用圆、椭圆、圆角矩形拼一个干净的卡通
老师：丸子头/短发 + 圆框眼镜 + 开衫，一只手抱着写有 ABC 的书。全部是基本
图形，不需要美术资产，任意 DPI 不糊，改颜色改比例就是改数字。

为什么不用生成的 PNG：不可控（每次生成不一致）、要往仓库里放二进制、没法
按状态改嘴和眼睛、还要考虑授权。

SVG 放在 `index.html` 里，约 60-100 行。嫌 `index.html` 太长的话可以拆成
`web/avatar.svg` 再 `fetch` 回来 `innerHTML`——代价是多一个异步加载和一次
首帧空白，所以默认还是内联。

## 布局与开关

- 位置：**和 transcript 并排**。新增一个 `#stage` 容器包住形象和
  `#transcript`，宽屏 `flex-direction: row`（形象固定 150-180px 宽），
  窄屏（<520px）自动换成上下堆叠。这样形象在孩子说话时一直在视线里，
  又不会像浮动挂件那样挡住内容。
- 开关：控制栏里加一个 checkbox，跟现有的 `学习单元` 一个样式：

  ```html
  <label class="dev"><input type="checkbox" id="avatarToggle"> 老师形象</label>
  ```

  记忆方式沿用现有约定：`PREF_KEY`（app.js:34）加一项 `avatar`，用
  `loadPref` / `savePref`（app.js:206/209）存 `talknest.avatar`。
- 默认关：你要求的是“可以通过开关打开”，所以不动现有用户的页面。想默认
  开的话就是改一个默认值。
- 关掉时 `display: none`，并且**不创建 analyser、不跑 rAF**，避免白烧 CPU。

## 状态驱动

复用现成的状态，不新增协议：

| 触发 | 来源 | 形象动作 |
|---|---|---|
| `listening` / `hearing` | `setStatus`（app.js:41） | 眼睛睁开，轻微上下呼吸，随机眨眼 |
| `thinking` / `preparing` | 同上 | 眼睛看上方，眉毛抬起，头顶冒 `…` |
| `speaking`（有音频在播） | `playbackActive`（app.js:101） | 嘴随音量开合 |
| `idle` / `disconnected` | 同上 | 眼微闭，静止 |

**嘴用音频驱动，不是定时摆动**：在 `playChunk`（app.js:614）里把
`src.connect(audioCtx.destination)` 改成 `src.connect(analyser)`，
`analyser.connect(audioCtx.destination)`（analyser 懒创建、全局一个）。
AnalyserNode 是直通节点、不改增益、不引入延迟，所以既不影响播放也不影响
浏览器用播放信号做的回声消除。

每帧 `getByteTimeDomainData` 算 RMS，映射到嘴的 `scaleY`，只在
`playbackActive` 为真时跑 `requestAnimationFrame`，播完就停。

CSS 定时摆动是更省事的做法（一个 `@keyframes` 加 class 就行），但嘴的节奏
和真正的音节对不上，一眼假。既然 analyser 只有几行，就走音频驱动。

`prefers-reduced-motion: reduce` 时关掉呼吸和眨眼，只保留静态形象。

## 代码落点

| 文件 | 改动 |
|---|---|
| `web/index.html` | `#stage` 容器包裹形象与 transcript；控制栏加 checkbox；内联 SVG；`#avatar` 相关 CSS（尺寸、呼吸、眨眼、嘴的 transform-origin） |
| `web/app.js` | `PREF_KEY.avatar`；`setAvatarState(kind)`；在 `setStatus` 和 `audio_start`/`audio_end`/`stop_playback`/`reply_done` 分支里调用；`playChunk` 接 analyser；`syncAvatar()` 处理开关与 rAF 生命周期 |

`setStatus` 是唯一的状态入口，所以在里面挂一行就够了，其余分支只需要管
播放开关。

## 风险

- 改播放链路是唯一有实际风险的地方（接错会导致没声音）。缓解：analyser 是
  直通节点；改完跑一次 `tests/ws_smoke.py` 确认音频仍然正常。
- SVG 里各部件的 `id` 要加前缀（如 `av-mouth`），避免和页面其它 id 撞车。
- 形象是装饰性的，SVG 上加 `aria-hidden="true"`，别让读屏软件念它。

## 验证

按约定只做冒烟和代码正确性检查，不做视觉校验：`node --check app.js`
过、页面能取到、开关能持久化、状态类名切换正确。观感这一步需要你自己开着
页面看——我这边没有浏览器。

## 这次不做

- 形象随回复内容变表情（夸奖时笑）——需要语义判断，先不做。
- 在旁边的小黑板上显示当前正在教的词——计划块目前只在服务端，没有通过
  WebSocket 下发，要做得先加协议。
- 多个形象/换装。

## 更新：形象换成 whale.webp

手画的 SVG 卡通不够可爱，换成了 `whale.webp` 角色设定图的正面视图：
裁出后用泛洪法去掉白底（裙子、头巾这类被深色轮廓包住的白色得以
保留），再向内腐蚀 1px 去白边，存成带透明通道的 `web/whale.webp`。

位图不能动嘴和眼睛，动画全部改为全身级：说话时原来是嘴型开合，
现在是随音量上下轻弹（scaleY 1→1.05，同样由 analyser 的 RMS 驱动）；
呼吸、听讲摇摆、思考倾斜保留；眨眼和手臂摆动删掉了（位图做不到）。
层次也相应改成 静态姿势(av-pose) → 关键帧动画(av-bob) → JS 每帧
(img) 三层，避免关键帧动画和静态 transform 互相覆盖。
