# 声纹识别

本文讲声纹是怎么算出来的、有哪些接口、以及阈值该怎么调。怎么在页面里
录入声纹，看 README 的「声纹录入」一节。

## 模型

声纹用 3D-Speaker 的 CAM++ 模型（ONNX，约 27 MB）把一段语音转成 192 维
向量，再与已录入的家庭成员比较。模型在 CPU 上跑，不占显存。权重放在
`model/speaker/`，来自 sherpa-onnx 发布的 `speaker-recongition-models`。
权重缺失时 `GET /learners/{learner}/speakers` 返回 `ready: false`，页面上
录入面板置灰，对话流程保持原样。

声纹跟着“我是谁”走，换一个学习者需要重新录入。向量存在
`data/learners/<学习者>/speakers.json`，服务端不保存录音本身。

## 录入时的校验

服务端会检查新录的一段是否像这个角色：它与该角色已录各段中**最接近的
一段**相比，相似度低于 `enroll_min_score` 时直接拒收，并在服务日志里记
一条 WARNING。比较对象取最接近的一段，因此已经录进去的一段错误录音不会
连累之后正确的录音。

新的一段若更像另一个已录入的人，服务端照常保存，同时在页面上提示可能
录错了人。

## 识别的判定

孩子和至少一位家长都录入之后，识别才会启用。结果有四种：`child`、
`dad`、`mom`，以及无法判断（`null`）。无法判断的情况包括：

- 有效语音不足 1 秒；
- 与最接近的人相似度低于 `min_score`；
- 最接近的两个人分数相差不到 `margin`。

无法判断时按孩子处理，因为孩子是主要使用者；对话气泡上也就标“我”。

## 接口

录入面板用下面四个 REST 接口，`role` 取 `child`、`dad`、`mom`：

| 方法与路径 | 作用 |
|---|---|
| `GET /learners/{learner}/speakers` | 各角色已录的段数与秒数，以及 `ready`、`enabled` |
| `POST /learners/{learner}/speakers/{role}` | 请求体是 24 kHz 单声道 int16 原始 PCM，追加一段声纹 |
| `DELETE /learners/{learner}/speakers/{role}` | 清除该角色的全部声纹 |
| `DELETE /learners/{learner}/speakers/{role}/{clip_id}` | 删除一段录音，`clip_id` 取自 `GET` 返回的 `clips` |

WebSocket 的 `user_text` 事件带 `speaker` 字段，取值同上。选定学习者时
服务端回报的 `learner` 事件带 `enabled` 字段；该学习者的声纹发生变化时
服务端发 `{"type": "voiceprint", "learner": ..., "enabled": ...}`，页面
据此决定是否显示气泡标签。

## 对教学的影响

爸爸和妈妈在教学逻辑里同属“家长”：

- 家长的话不记入孩子的练习词，不推进孩子的轮次计数，也不会被当作对
  “跟我读”的回答。
- 发给 LLM 的消息带 `【家长】` 前缀，不附教学计划块；系统提示词会说明
  这个前缀的含义，让老师简短回应家长，再把话题交还给孩子。
- 孩子的学习画像只取孩子本人的轮次。

## 调参

阈值与开关在 `server/config.yaml` 的 `speaker_id` 一节。识别准确率取决于
麦克风和房间，也取决于孩子的声音与家长的声音有多接近，因此首次使用时
建议先用日常对话观察气泡上的标签，再按需要调整 `min_score` 与 `margin`。
