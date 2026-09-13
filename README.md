# Talknest

本地部署的英语口语对话练习应用（成人自练 + 儿童练习双模式）。全部推理在家庭局域网内完成，语音与文本不出本机。

## 文档

- [docs/bench/](docs/bench/) — 延迟与性能实测记录

## 架构一句话

浏览器 Web UI（麦克风常驻采集，AEC）→ Orchestrator（流式 VAD 切分轮次/会话/教学策略/kid mode/打断）→ ASR / LLM / TTS / VAD 四个独立推理服务，全部流式，可逐个替换。

## 自由对话与打断

页面打开即自动连上并开麦，之后不需要任何操作：孩子直接对着麦克风说话，说完停顿
一下服务端就接话；对方说话时也可以直接插话打断。只有在浏览器拦截了自动播放
（AudioContext 需要用户手势）或麦克风被拒绝时，才需要点一次按钮。

- **轮次切分**：`server/orchestrator/vad.py` 的 `StreamVAD` 把 24 kHz 流降采样到
  16 kHz 逐帧喂 Silero，连续语音 `start_ms` 开口、尾部静音 `silence_ms` 收口，
  起点前保留 `pre_roll_ms` 以免吃掉第一个字。
- **打断（barge-in）**：助手播放期间仍检测语音，但要求持续 `barge_in_ms` 且概率
  超过 `barge_in_threshold` 才判定为孩子插话，播放开始后的 `playback_grace_ms`
  内忽略——这是对抗扬声器回声被麦克风拾取导致自我打断的代价。命中后服务端取消
  LLM/TTS 并通知前端立即停播。
- 参数都在 `server/config.yaml` 的 `vad` 段。家里回声大、助手老是被自己打断时，
  优先调大 `barge_in_ms`；孩子说话停顿长、一句话被切成两句时，调大 `silence_ms`。

## 模型角色（M1）

| 环节 | 模型 | 运行时 | 端口 |
|---|---|---|---|
| LLM | Qwen3.5-9B MTP GGUF (Q4_K_M) | llama.cpp（`TN9B_BIN_DIR` 指定 build/bin） | 9999 |
| ASR | Qwen3-ASR-1.7B（**24 kHz 输入**） | vLLM `qwen-asr-serve`（`ASR_VENV` 指定 venv） | 8001 |
| TTS | Qwen3-TTS-0.6B-CustomVoice | qwen-tts 包（专用 `venv-tts/`）+ OpenAI 兼容 sidecar | 8002 |
| VAD | Silero VAD (ONNX) | orchestrator 内嵌（CPU，流式） | — |

权重目录由 `TALKNEST_MODEL_ROOT` 决定，默认是仓库同级的 `model/`（即 `../model`）。
各服务也保留各自的覆盖变量：`TN9B_MODEL_DIR`、`ASR_MODEL`、`QWEN_TTS_DIR`。

LLM 可切换到 Qwen3.8-27B-NVFP4（vLLM）：改 `server/config.yaml` 的 `llm.active` 一行，先停 9B 再起 27B（统一内存互斥）。采样率与 TTS 运行时细节见 [model/README.md](../model/README.md)。

## 模式与语速

- `adult`：全英文对话，正常语速。
- `kid`：零基础教学——对话用中文，英文只出现在教学点（"兔子的英文是
  rabbit。"），目标是培养兴趣和日常表达。

模式的行为（系统提示词）与音色、语言、语速都写在 `server/config.yaml` 的
`modes` 段，未覆盖的项取 `tts` 段的全局值——换音色、换语言只改这一个文件。
Qwen3-TTS 0.6B 无速率参数，可选的放慢由 orchestrator 端 WSOLA 变速实现
（`server/orchestrator/tts.py`），但在语音上有可闻伪影，默认不启用。

## 目录

```
server/orchestrator/   会话编排与语音管线（FastAPI + WebSocket）
server/services/       各推理服务启动/停止脚本与 TTS sidecar
web/                   纯 JS 前端（getUserMedia 常驻采集 + 队列播放）
scripts/               llama.cpp 启动器、orchestrator 启停、基准脚本
local.env              本机配置（不入库，见 local.env.example）
tests/                 单元测试 + 服务探针 + 全链路冒烟
docs/                  设计文档与基准报告
```

## 依赖

- Python 3.12 虚拟环境 `venv/`（orchestrator）与 `venv-tts/`（qwen-tts 与
  transformers 独用一套，避免降级）。
- NVIDIA GPU + CUDA；llama.cpp 需带 `-DGGML_CUDA=ON` 构建，可执行文件目录
  由 `TN9B_BIN_DIR` 指定。
- vLLM（`qwen-asr-serve`，ASR）。

## 本机配置（local.env）

venv、构建目录、权重根目录、显存分配这些只跟机器有关的东西不进版本库：
复制模板填一次，之后所有脚本都不用再带参数。

```bash
cp local.env.example local.env   # local.env 已被 .gitignore 忽略
```

`scripts/local_env.sh` 负责加载它，启动脚本、停止脚本、
`scripts/run_server.sh` 和 `scripts/llama_qwen35_9b.sh` 都会 source，
所以里面的值对跑在 tmux 里的服务同样生效。缺了某个值时脚本会直接报错并
提示该填哪一项，而不是猜一个路径。

权重根目录 `TALKNEST_MODEL_ROOT` 是唯一的例外：不填时回退到与仓库同级的
`model/` 目录（`server/orchestrator/config.py` 的 `model_root()` 同规则）。

## 运行

每个服务脚本只负责把它自己的服务跑在前台，不关心谁在管它——可以直接在
shell 里跑、用 systemd 或容器入口脚本跑，也可以交给 tmux：

```bash
server/services/start_asr.sh               # ASR   :8001
server/services/start_tts.sh               # TTS   :8002
server/services/start_llm_llamacpp_9b.sh   # LLM   :9999
scripts/start_web.sh                       # 界面  :8890
```

想一处看全部输出时用 tmux 辅助脚本，它建一个 session、每个服务一个
window，并把输出 tee 到 `logs/<service>.current.log`：

```bash
scripts/tmux_start.sh                 # 全部，按依赖顺序（LLM 最后）
scripts/tmux_start.sh asr tts         # 只重启某几个
tmux attach -t talknest
scripts/tmux_stop.sh                  # 停窗口并释放端口
```

LLM 放最后起：llama.cpp 默认 `-fit on`，让它看到已被 ASR/TTS 占掉的显存。
停止单个服务用对应的 `stop_*.sh`（`server/services/stop_{asr,tts,llm}.sh`、
`scripts/stop_web.sh`），它们按端口回收，不管服务是在哪里启动的。

要无人值守常驻（崩溃自动拉起、开机自启）时用 systemd 用户单元模式：

```bash
scripts/systemd/install.sh             # 渲染单元到 ~/.config/systemd/user/
scripts/systemd/services.sh start      # 停掉 tmux 实例后按依赖顺序拉起
scripts/systemd/services.sh status     # 或 restart / stop，可带 asr tts llm web
journalctl --user -u talknest-asr -f   # 日志走 journald
scripts/systemd/install.sh --enable    # 开机自启（含 enable-linger）
```

两种模式可随时互换（`services.sh start` 与 `tmux_start.sh` 都会先停掉
另一模式的实例），但不要同时跑。单元依赖：LLM `Requires=` ASR+TTS，
所以 ASR/TTS 没就绪时 LLM 不会启动（护住 `-fit` 的显存顺序）；显式停
ASR/TTS 会连带停 LLM，但不会停 orchestrator。改了 TLS 配置、端口或移动
了 checkout 后要重跑 `install.sh`（web 健康检查的 URL 是渲染时定死的）。

三个服务共驻同一张卡。LLM 用 llama.cpp `-fit on` 按剩余显存自动挑卸载层数，
所以 ASR 与 TTS 要先起；ASR 的 vLLM 池大小用 `ASR_GPU_UTIL` 调，想固定
LLM 的卸载层数用 `TN9B_NGL`。

麦克风要求安全上下文：局域网 IP 走纯 HTTP 时浏览器不暴露 `navigator.mediaDevices`。
推荐直接用 TLS：

```bash
scripts/gen_dev_cert.sh                    # 生成自签证书（SAN 覆盖本机 IP）
# 把输出的 TLS_CERT / TLS_KEY 两行写进 local.env，之后 start_web.sh 自动带上
# 浏览器打开 https://<talknest-host>:8890，接受一次自签警告
```

`certs/dev.pem` 与 `certs/dev.key` 存在时 `start_web.sh` 会自动启用 TLS，
不必每次显式传这两个变量。

或临时用 Chrome flag：`chrome://flags/#unsafely-treat-insecure-origin-as-secure`
加入 `http://<talknest-host>:8890` 后重启浏览器。

想做到孩子连这一次点击都不用，给 Chrome 加
`--autoplay-policy=no-user-gesture-required`（并在这次 origin 上永久允许麦克风），
页面加载后就会自己开麦。

## 音频设备

页面上的“麦克风 / 扬声器”下拉显示当前实际在用的设备，也可以直接指定，选择
记在 localStorage。默认“自动”：麦克风跟浏览器默认，扬声器跟麦克风所属的
设备（同一个 `groupId`，即蓝牙耳机的另一半），所以蓝牙耳机重连后声音会一起
回到耳机，不会留在 HDMI 外放上。判断设备是否还在，除了看 track 状态，也看
`deviceId` 是否还在 `enumerateDevices()` 的列表里。

切换输出走 `AudioContext.setSinkId()`，播放和采集始终共用同一个
AudioContext，AEC 的参考信号不受影响。麦克风和扬声器不属于同一个设备时页面
会提示回声消除变弱，这时 `server/config.yaml` 的 `barge_in_ms` 可能要调大。
非 Chrome（没有 `setSinkId`）下扬声器下拉会禁用，声音跟随系统默认输出。

## 测试

```bash
./venv/bin/python -m pytest tests/ -v      # 单元测试（无需服务）
./venv/bin/python tests/api_probe.py       # 服务健康探针
./venv/bin/python tests/ws_smoke.py        # 全链路：合成用户语音 → 流式推流 → 转写/回复/音频
./venv/bin/python tests/ws_smoke.py --barge   # 同上，并断言插话能打断
./venv/bin/python scripts/bench_llm.py     # LLM 延迟基准
```

## 提交前密钥扫描

仓库带 gitleaks pre-commit hook（`scripts/hooks/pre-commit`），提交暂存
内容含密钥特征时直接拒绝。一次性启用：

```bash
git config core.hooksPath scripts/hooks
gitleaks version || echo "先安装: https://github.com/gitleaks/gitleaks/releases"
```

未安装 gitleaks 时 hook 只警告不拦截。
