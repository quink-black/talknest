# Talknest

Talknest 是一个本地部署的英语口语对话练习应用，面向儿童零基础教学和成人
自练两种用途。全部推理都在家庭局域网内完成，语音与文本都不离开本机。
三个模型同时加载在一张消费级显卡上（实测 RTX 3060 12G），从说完到听见
回复约 3 秒，显存与延迟的实测数据见
[docs/bench/llm-vram-fit.md](docs/bench/llm-vram-fit.md)。

## 特性

- **免按键自由对话**：页面打开后自动连接麦克风，孩子直接说话，检测到
  停顿之后服务端就接话。对方说话时孩子可以直接插话打断。只有当浏览器
  拦截自动播放或者拒绝麦克风权限时，才需要点击一次按钮。
- **全链路流式**：ASR 边接收边转写，LLM 输出满第一句就把文本交给 TTS，
  TTS 边合成边推送音频（`tts.stream`）。因此首音频延迟不按整段回复的
  长度计价。
- **三档模式**：`kid`（儿童教学：中文对话加英文教学点）、`kid_upper`
  （小学高年级：中文对话里夹杂短英文句）、`adult`（全英文对话）。三档
  模式的提示词与音色全部集中在 `server/config.yaml`。
- **声纹识别**：家庭成员在页面的“声纹录入”面板里各录几段话，服务端
  就能分辨一句话来自孩子、爸爸还是妈妈。家长的话不会计入孩子的练习，
  对话气泡上也会标出说话的人。详见“声纹识别”一节。
- **音频设备热插拔**：蓝牙耳机断连时页面自动暂停并提示缺少哪个设备，
  设备回来之后自动恢复会话；麦克风与扬声器可以手动指定，扬声器默认
  跟随麦克风所属的设备。

## 架构

链路从浏览器 Web UI 开始（麦克风常驻采集，带 AEC），进入 orchestrator
（流式 VAD 切分轮次、会话、模式与打断，FastAPI + WebSocket，:8890），
再由三个独立的推理服务（ASR、LLM、TTS）完成推理。三个推理服务全部支持
流式，并且可以逐个替换；VAD 内嵌在 orchestrator 进程内：

| 环节 | 模型 | 运行时 | 端口 |
|---|---|---|---|
| LLM | Qwen3.5-9B MTP GGUF (Q4_K_M) | llama.cpp（`TN9B_BIN_DIR` 指定 build/bin） | 9999 |
| ASR | Qwen3-ASR-1.7B（**24 kHz 输入**） | vLLM `qwen-asr-serve`（`ASR_VENV` 指定 venv） | 8001 |
| TTS | Qwen3-TTS-12Hz-0.6B-CustomVoice | qwen-tts 包（专用 `venv-tts/`）+ OpenAI 兼容 sidecar | 8002 |
| VAD | Silero VAD (ONNX) | orchestrator 内嵌（CPU，流式） | — |

轮次切分与打断的机制和调参方法见 [docs/vad-tuning.md](docs/vad-tuning.md)。

LLM 可以切换到 Qwen3.8-27B-NVFP4（vLLM）：修改 `server/config.yaml` 的
`llm.active` 一行即可。切换时要先停止 9B 再启动 27B，因为两者的显存
互斥。27B 需要远大于 12G 的显存，或者需要统一内存。

## 硬件与软件要求

- **NVIDIA GPU**：三个模型加载在同一张 GPU 上，12G 是实测下限。显存
  预算与各参数的取舍记录在
  [docs/bench/llm-vram-fit.md](docs/bench/llm-vram-fit.md)。
- **Python 3.12 与三个 venv**：`venv/` 运行 orchestrator，安装
  `server/requirements.txt`；`venv-tts/` 由 qwen-tts 与 transformers
  独用一套依赖，目的是避免依赖降级；还有一个安装 vLLM 或
  `qwen-asr-serve` 的 venv，路径由 `ASR_VENV` 指定。
- **llama.cpp**：需要自行构建，并带上 `-DGGML_CUDA=ON`。可执行文件目录
  由 `TN9B_BIN_DIR` 指定。
- **浏览器**：桌面 Chrome 或 Edge。扬声器跟随麦克风切换依赖
  `AudioContext.setSinkId()`，该接口只有 Chrome 系浏览器提供。在其他
  浏览器中扬声器下拉框禁用，扬声器跟随系统输出。

## 模型权重

模型权重不随仓库分发。权重默认放在仓库同级的 `model/` 目录，根目录可以
用 `TALKNEST_MODEL_ROOT` 覆盖；各服务另有单独的覆盖变量
（`TN9B_MODEL_DIR`、`ASR_MODEL`、`QWEN_TTS_DIR`）：

```
model/
  Qwen3.5-9B-MTP-GGUF/Qwen3.5-9B-Q4_K_M.gguf   LLM（量化后缀 TN9B_QUANT 可换）
  Qwen3-ASR-1.7B/                               ASR
  Qwen3-TTS-12Hz-0.6B-CustomVoice/              TTS
  Qwen3-TTS-Tokenizer-12Hz/                     TTS 分词器
  silero-vad/silero_vad.onnx                    VAD
  speaker/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx   声纹（可选）
```

## 教学词库

教学大纲用到的中文释义与拼音不随仓库分发，需要导入一次（下载约 4 MB，
只需执行一次）：

```bash
./venv/bin/python scripts/import_curriculum.py
```

释义写在 `server/curriculum/glosses.yaml`（该文件入库，可以直接修改），
导入脚本从 CC-CEDICT 补充拼音，并生成 `data/curriculum/lexicon.jsonl`。
跳过这一步也能运行，但大纲里的词不做拼写校验，因此拼错不会被发现。

## 本机配置（local.env）

venv 路径、构建目录、权重根目录、显存分配这类值只与当前机器有关，因此
不进入版本库。复制模板填写一次之后，后续所有脚本都不需要再带参数：

```bash
cp local.env.example local.env   # local.env 已被 .gitignore 忽略
```

`scripts/local_env.sh` 负责加载 local.env。启动脚本、停止脚本、
`scripts/run_server.sh` 与 `scripts/llama_qwen35_9b.sh` 都会 source 该
文件，因此其中的值对运行在 tmux 或 systemd 里的服务同样生效。当某一项
缺失时，脚本会直接报错并提示需要填写哪一项，而不会猜测路径。

权重根目录 `TALKNEST_MODEL_ROOT` 是唯一有默认值的变量：未填写时回退到
与仓库同级的 `model/` 目录，`server/orchestrator/config.py` 的
`model_root()` 使用同一规则。

## 运行

共四个服务，启动顺序有意义：先启动 ASR 与 TTS，因为两者各自预留显存；
LLM 最后启动，llama.cpp 的 `-fit on` 会按剩余显存自动选择卸载层数（想
固定层数就用 `TN9B_NGL`）。每个服务脚本只负责在前台运行，不管理监督
方式，因此三种方式任选：

```bash
# 单独前台跑
server/services/start_asr.sh               # ASR   :8001
server/services/start_tts.sh               # TTS   :8002
server/services/start_llm_llamacpp_9b.sh   # LLM   :9999
scripts/start_web.sh                       # 界面  :8890
```

开发观察使用 tmux（一个 session、每服务一个 window、输出 tee 到
`logs/<service>.current.log`）：

```bash
scripts/tmux_start.sh                 # 全部，按依赖顺序（LLM 最后）
scripts/tmux_start.sh asr tts         # 只重启某几个
scripts/tmux_stop.sh                  # 停窗口并释放端口
```

无人值守常驻使用 systemd 用户单元（崩溃后自动拉起、可开机自启）：

```bash
scripts/systemd/install.sh             # 渲染单元到 ~/.config/systemd/user/
scripts/systemd/services.sh start      # 停掉 tmux 实例后按依赖顺序拉起
scripts/systemd/services.sh status     # 或 restart / stop，可带 asr tts llm web
journalctl --user -u talknest-asr -f   # 日志走 journald
scripts/systemd/install.sh --enable    # 开机自启（含 enable-linger）
```

两种模式可以随时互换，因为两者都会先停掉另一模式的实例，但不要同时运行。
注意：

- 单元依赖：LLM 单元用 `Requires=` 依赖 ASR 与 TTS，因此 ASR 或 TTS
  未就绪时 LLM 不会启动，这条依赖保护了 `-fit` 所要求的显存分配顺序。
  显式停止 ASR 或 TTS 会连带停止 LLM，但不会停止 orchestrator。
- 停止单个服务时使用对应的 `stop_*.sh`
  （`server/services/stop_{asr,tts,llm}.sh`、`scripts/stop_web.sh`），
  脚本按端口回收进程，与该服务在哪里启动无关。
- 修改 TLS 配置、修改端口或移动 checkout 之后要重新运行 `install.sh`，
  因为 web 健康检查的 URL 在渲染单元时已经固定。

### 浏览器访问（安全上下文）

麦克风要求安全上下文：当局域网 IP 通过纯 HTTP 访问时，浏览器不会暴露
`navigator.mediaDevices`。推荐自签 TLS：

```bash
scripts/gen_dev_cert.sh                    # 生成自签证书（SAN 覆盖本机 IP）
# 把输出的 TLS_CERT / TLS_KEY 两行写进 local.env；certs/dev.pem 存在时
# start_web.sh 自动启用 TLS。浏览器打开 https://<host>:8890，接受一次
# 自签警告。
```

临时替代方案：在 `chrome://flags/#unsafely-treat-insecure-origin-as-secure`
中加入 `http://<host>:8890`，然后重启浏览器。

如果想让孩子把接受自签警告这一次点击也省掉，给 Chrome 加上
`--autoplay-policy=no-user-gesture-required` 参数，并在该 origin 上永久
允许麦克风，页面加载之后就会自己开麦。

### iPad app

`ios/` 是一个只做壳的原生 app：启动即全屏加载 `https://<host>:8890`，
自己处理麦克风授权、音频自动播放和自签证书，地址在 app 内填写。构建与
安装见 [ios/README.md](ios/README.md)。同样需要上面那一步自签 TLS：
iPad 上的 WKWebView 只在安全上下文里暴露麦克风。

## 模式

- `kid`：零基础（约 7-10 岁）。对话用中文，英文只出现在教学点
  （“兔子的英文是 rabbit。”），目标是培养兴趣和日常表达能力。
- `kid_upper`：小学高年级（约 10-12 岁）。在中文对话里自由夹杂短英文句。
- `adult`：全英文对话，用正常语速；只有当用户明显卡住时才用中文。

三种模式的行为（系统提示词）与音色、语言、语速都写在
`server/config.yaml` 的 `modes` 段，未被覆盖的项取 `tts` 段的全局值，
因此换音色、换语言只改这一个文件。Qwen3-TTS 0.6B 没有速率参数，可选的
放慢由 orchestrator 端 WSOLA 变速实现（`server/orchestrator/tts.py`），
而变速会在语音上产生可闻的伪影，因此默认不启用。

## CLI 客户端（headless）

没有浏览器时可以使用 `client/` 下的终端客户端。它的目标形态是“任何带
音频输入输出的 Linux 机器，从另一台设备 SSH 进来使用”，树莓派是目标
形态之一。

```bash
pip install -r client/requirements.txt   # 需要 libportaudio2；Python >= 3.10
scripts/talknest-cli                     # 一条命令：自动 venv、自动 ws/wss
scripts/talknest-cli --list-devices      # 看设备
scripts/talknest-cli --input hw:2,0 --output hw:2,0   # 手动指定设备
```

回声取消方式按设备类型自动选择（`--aec auto`，默认值）。当设备是蓝牙
HFP 设备时，客户端降级为半双工，因为 HFP 的时延抖动实测会让任何参考
对齐型 AEC 失效；其余设备使用进程内的 DTLN-AEC 神经网络（非 WebRTC
路线，TFLite 模型，见
[docs/bench/cli-aec.md](docs/bench/cli-aec.md)）。`--aec none` 强制
半双工。当麦克风或扬声器来自文件（`--input-file`/`--output-file`）时，
客户端不需要声卡，因此可以用于离线回归。实测记录见
[docs/bench/cli-aec.md](docs/bench/cli-aec.md)。

## 声纹识别

声纹识别用 3D-Speaker 的 CAM++ 模型（ONNX，约 27 MB）把一段语音转成
192 维的声纹向量，再与已录入的家庭成员比较。模型在 CPU 上运行，不占用
显存。权重放在 `model/speaker/`，来源是 sherpa-onnx 发布的
`speaker-recongition-models`，文件名见上面的目录树。权重缺失时，
`GET /learners/{learner}/speakers` 返回 `ready: false`，录入面板置灰，
对话流程保持原样。

**录入。** 打开页面侧栏的“声纹录入”，在孩子、爸爸、妈妈三行里点“录一段”，
按提示说话，约 8 秒后自动停止。每个人建议录两三段，累计 15 秒以上。录音
期间页面不把声音发给对话，服务端也不保存录音，只保存声纹向量，位置是
`data/learners/<学习者>/speakers.json`。声纹跟着“我是谁”走，换一个学习者
需要重新录入。

每一行下面列出已录的各段及其长度。某一段录进了别人的声音时，点那一段的
“删除”即可，其余录音不受影响；“清除”删除该角色的全部录音，需要连点两次
确认。

服务端会检查新录的一段是否像这个角色：它与该角色已录各段中最接近的一段
相比，相似度低于 `enroll_min_score` 时直接拒收，并在服务日志里记一条
WARNING。比较对象取最接近的一段，因此已经录进去的一段错误录音不会连累
之后正确的录音。新的一段若更像另一个已录入的人，服务端照常保存，同时在
页面上提示可能录错了人。

**启用条件。** 孩子和至少一位家长都录入之后，识别才会启用。识别结果有
四种：`child`、`dad`、`mom`，以及无法判断（`null`）。无法判断的情况包括
有效语音不足 1 秒、与最接近的人相似度低于 `min_score`，以及最接近的两个
人分数相差不到 `margin`。无法判断的话按孩子处理，因为孩子是主要使用者。

**对教学的影响。** 爸爸和妈妈在教学逻辑里同属“家长”：

- 家长的话不记入孩子的练习词，不推进孩子的轮次计数，也不会被当作对“跟我读”
  的回答。
- 发给 LLM 的消息带有 `【家长】` 前缀，不附教学计划块；系统提示词会说明
  这个前缀的含义，让老师简短回应家长，再把话题交还给孩子。
- 孩子的学习画像只取孩子本人的轮次。

**接口。** 录入面板使用下面三个 REST 接口，`role` 取 `child`、`dad`、`mom`：

| 方法与路径 | 作用 |
|---|---|
| `GET /learners/{learner}/speakers` | 各角色已录的段数与秒数，以及 `ready`、`enabled` |
| `POST /learners/{learner}/speakers/{role}` | 请求体是 24 kHz 单声道 int16 原始 PCM，追加一段声纹 |
| `DELETE /learners/{learner}/speakers/{role}` | 清除该角色的全部声纹 |
| `DELETE /learners/{learner}/speakers/{role}/{clip_id}` | 删除一段录音，`clip_id` 取自 `GET` 返回的 `clips` |

WebSocket 的 `user_text` 事件新增 `speaker` 字段，取值同上。阈值和开关在
`server/config.yaml` 的 `speaker_id` 一节。识别准确率取决于麦克风和房间，
也取决于孩子的声音与家长的声音有多接近，因此首次使用时建议先用日常对话
观察气泡上的标签，再按需要调整 `min_score` 与 `margin`。

## 音频设备

页面上的“麦克风 / 扬声器”下拉框显示当前实际在用的设备，也可以直接指定
设备，选择结果记录在 localStorage。默认值是“自动”：麦克风跟随浏览器
默认设备，扬声器跟随麦克风所属的设备（同一个 `groupId`，也就是蓝牙耳机
的另一半），因此蓝牙耳机重连之后声音会一起回到耳机，而不会留在 HDMI
外放上。

当麦克风或扬声器离线时（蓝牙断连、track 结束、设备从列表消失），页面会
自动暂停并提示缺少哪个设备；设备回来之后，页面会自动恢复会话。播放切换
输出设备走 `AudioContext.setSinkId()`，播放和采集始终共用同一个
AudioContext，因此 AEC 的参考信号不受影响。

## 目录

```
server/orchestrator/   会话编排与语音管线（FastAPI + WebSocket）
server/services/       各推理服务启动/停止脚本与 TTS sidecar
web/                   纯 JS 前端（getUserMedia 常驻采集 + 队列播放）
client/                终端客户端（headless，见上文“CLI 客户端”）
scripts/               llama.cpp 启动器、tmux/systemd 编排、基准脚本
tests/                 单元测试 + 服务探针 + 全链路冒烟
docs/                  设计文档与实测报告
local.env              本机配置（不入库，见 local.env.example）
```

## 测试

```bash
./venv/bin/python -m pytest tests/ -v      # 单元测试（无需服务）
./venv/bin/python tests/api_probe.py       # 服务健康探针
./venv/bin/python tests/ws_smoke.py        # 全链路：合成用户语音 → 流式推流 → 转写/回复/音频
./venv/bin/python tests/ws_smoke.py --barge   # 断言插话能打断
./venv/bin/python tests/ws_smoke.py --reask   # 断言追问时已有回答不丢
./venv/bin/python tests/ws_speaker_smoke.py   # 声纹：录入三个合成音色，断言识别结果（需先在另一端口起服务，见脚本头部）
./venv/bin/python scripts/bench_llm.py     # LLM 延迟基准
./venv/bin/python scripts/eval_teaching.py # 教学计划：跟不跟计划、计划块带来多少延迟
```

`eval_teaching.py` 需要 LLM 服务在运行。脚本用一个模拟的孩子跑若干轮
对话，输出四组数字：是否教了计划里的词、有没有把已会的词当成新词教、
回复中英文占比的中位数，以及加计划块前后首 token 延迟的差值。学习者
状态写入临时目录，因此不会修改真实档案。加上
`--write docs/bench/teaching-eval.md` 可以把结果存成报告。

## 提交前密钥扫描

仓库带有 gitleaks pre-commit hook（`scripts/hooks/pre-commit`），当暂存
内容包含密钥特征时，该 hook 会直接拒绝提交。一次性启用：

```bash
git config core.hooksPath scripts/hooks
gitleaks version || echo "先安装: https://github.com/gitleaks/gitleaks/releases"
```

未安装 gitleaks 时，该 hook 只警告而不拦截。

## 文档

- [docs/vad-tuning.md](docs/vad-tuning.md) — 轮次切分与打断的机制、调参
- [docs/bench/](docs/bench/) — 延迟与显存实测记录（LLM 显存预算、ASR
  fp8 量化、M1 基线）
