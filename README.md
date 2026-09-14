# Talknest

本地部署的英语口语对话练习应用（儿童零基础教学 + 成人自练）。全部推理在
家庭局域网内完成，语音与文本不出本机；三个模型共驻一张消费级显卡
（实测 RTX 3060 12G），说完到听见回复约 3 秒（见
[docs/bench/llm-vram-fit.md](docs/bench/llm-vram-fit.md)）。

## 特性

- **免按键自由对话**：页面打开即连麦，孩子直接说话，停顿后服务端接话；
  对方说话时可以直接插话打断。只有浏览器拦截自动播放或拒绝麦克风时才
  需要点一次按钮。
- **全链路流式**：ASR 边收边转写、LLM 出满第一句就送 TTS、TTS 边合成边
  推音频（`tts.stream`），首音频延迟不以整段回复长度计价。
- **三档模式**：`kid`（儿童教学：中文对话 + 英文教学点）、`kid_upper`
  （小学高年级：中文对话里夹短英文句）、`adult`（全英文对话），提示词
  与音色全部集中在 `server/config.yaml`。
- **音频设备热插拔**：蓝牙耳机断连自动暂停并提示，设备回来自动恢复；
  麦克风/扬声器可手动指定，输出默认跟随麦克风所属设备。

## 架构

浏览器 Web UI（麦克风常驻采集，AEC）→ Orchestrator（流式 VAD 切分轮次/
会话/模式/打断，FastAPI + WebSocket，:8890）→ 三个独立推理服务（ASR、
LLM、TTS），全部流式、可逐个替换；VAD 内嵌在 orchestrator 里：

| 环节 | 模型 | 运行时 | 端口 |
|---|---|---|---|
| LLM | Qwen3.5-9B MTP GGUF (Q4_K_M) | llama.cpp（`TN9B_BIN_DIR` 指定 build/bin） | 9999 |
| ASR | Qwen3-ASR-1.7B（**24 kHz 输入**） | vLLM `qwen-asr-serve`（`ASR_VENV` 指定 venv） | 8001 |
| TTS | Qwen3-TTS-12Hz-0.6B-CustomVoice | qwen-tts 包（专用 `venv-tts/`）+ OpenAI 兼容 sidecar | 8002 |
| VAD | Silero VAD (ONNX) | orchestrator 内嵌（CPU，流式） | — |

轮次切分与打断的机制、调参见 [docs/vad-tuning.md](docs/vad-tuning.md)。

LLM 可切换到 Qwen3.8-27B-NVFP4（vLLM）：改 `server/config.yaml` 的
`llm.active` 一行，先停 9B 再起 27B（显存互斥）。27B 需要远大于 12G
的显存或统一内存。

## 硬件与软件要求

- **NVIDIA GPU**：三个模型共驻同一张卡，12G 是实测下限；显存预算与
  各参数的取舍记录在 [docs/bench/llm-vram-fit.md](docs/bench/llm-vram-fit.md)。
- **Python 3.12**，三个 venv：`venv/`（orchestrator，装
  `server/requirements.txt`）、`venv-tts/`（qwen-tts 与 transformers
  独用一套，避免依赖降级）、以及一个装 vLLM 或 `qwen-asr-serve` 的
  venv（`ASR_VENV` 指定）。
- **llama.cpp**：自建，带 `-DGGML_CUDA=ON`，可执行文件目录由
  `TN9B_BIN_DIR` 指定。
- **浏览器**：桌面 Chrome/Edge。扬声器跟随麦克风切换依赖
  `AudioContext.setSinkId()`（Chrome 系独有），其他浏览器扬声器下拉
  禁用、跟随系统输出。

## 模型权重

权重不入库。默认放在仓库同级的 `model/` 目录（`TALKNEST_MODEL_ROOT`
可覆盖），各服务另有单独覆盖变量（`TN9B_MODEL_DIR`、`ASR_MODEL`、
`QWEN_TTS_DIR`）：

```
model/
  Qwen3.5-9B-MTP-GGUF/Qwen3.5-9B-Q4_K_M.gguf   LLM（量化后缀 TN9B_QUANT 可换）
  Qwen3-ASR-1.7B/                               ASR
  Qwen3-TTS-12Hz-0.6B-CustomVoice/              TTS
  Qwen3-TTS-Tokenizer-12Hz/                     TTS 分词器
  silero-vad/silero_vad.onnx                    VAD
```

## 教学词库

大纲用到的中文释义和拼音不入库，需要导一次（下载约 4 MB，只下一次）：

```bash
./venv/bin/python scripts/import_curriculum.py
```

释义写在 `server/curriculum/glosses.yaml`（入库，可以直接改），脚本从
CC-CEDICT 补拼音，生成 `data/curriculum/lexicon.jsonl`。不导也能跑，只是
大纲里的词不做校验 —— 拼错不会被发现。

## 本机配置（local.env）

venv 路径、构建目录、权重根目录、显存分配这些只跟机器有关的东西不进
版本库：复制模板填一次，之后所有脚本都不用再带参数。

```bash
cp local.env.example local.env   # local.env 已被 .gitignore 忽略
```

`scripts/local_env.sh` 负责加载它，启动脚本、停止脚本、
`scripts/run_server.sh` 和 `scripts/llama_qwen35_9b.sh` 都会 source，
所以里面的值对跑在 tmux 或 systemd 里的服务同样生效。缺了某个值时脚本
会直接报错并提示该填哪一项，而不是猜一个路径。

权重根目录 `TALKNEST_MODEL_ROOT` 是唯一的例外：不填时回退到与仓库同级
的 `model/` 目录（`server/orchestrator/config.py` 的 `model_root()` 同
规则）。

## 运行

四个服务，**启动顺序有意义**：ASR 与 TTS 先起（各自圈定显存），LLM 最后
（llama.cpp `-fit on` 按剩余显存自动挑卸载层数；想固定层数用 `TN9B_NGL`）。
每个服务脚本只负责前台运行，不管监督方式，所以三种方式任选：

```bash
# 单独前台跑
server/services/start_asr.sh               # ASR   :8001
server/services/start_tts.sh               # TTS   :8002
server/services/start_llm_llamacpp_9b.sh   # LLM   :9999
scripts/start_web.sh                       # 界面  :8890
```

开发观察用 tmux（一个 session、每服务一个 window、输出 tee 到
`logs/<service>.current.log`）：

```bash
scripts/tmux_start.sh                 # 全部，按依赖顺序（LLM 最后）
scripts/tmux_start.sh asr tts         # 只重启某几个
scripts/tmux_stop.sh                  # 停窗口并释放端口
```

无人值守常驻用 systemd 用户单元（崩溃自动拉起、可开机自启）：

```bash
scripts/systemd/install.sh             # 渲染单元到 ~/.config/systemd/user/
scripts/systemd/services.sh start      # 停掉 tmux 实例后按依赖顺序拉起
scripts/systemd/services.sh status     # 或 restart / stop，可带 asr tts llm web
journalctl --user -u talknest-asr -f   # 日志走 journald
scripts/systemd/install.sh --enable    # 开机自启（含 enable-linger）
```

两种模式可随时互换（都会先停掉另一模式的实例），但不要同时跑。注意：

- 单元依赖：LLM `Requires=` ASR+TTS，ASR/TTS 没就绪时 LLM 不启动（护住
  `-fit` 的显存顺序）；显式停 ASR/TTS 会连带停 LLM，但不会停 orchestrator。
- 停止单个服务用对应的 `stop_*.sh`（`server/services/stop_{asr,tts,llm}.sh`、
  `scripts/stop_web.sh`），按端口回收，不管服务在哪里启动。
- 改了 TLS 配置、端口或移动了 checkout 后要重跑 `install.sh`（web 健康
  检查的 URL 是渲染时定死的）。

### 浏览器访问（安全上下文）

麦克风要求安全上下文：局域网 IP 走纯 HTTP 时浏览器不暴露
`navigator.mediaDevices`。推荐自签 TLS：

```bash
scripts/gen_dev_cert.sh                    # 生成自签证书（SAN 覆盖本机 IP）
# 把输出的 TLS_CERT / TLS_KEY 两行写进 local.env；certs/dev.pem 存在时
# start_web.sh 自动启用 TLS。浏览器打开 https://<host>:8890，接受一次
# 自签警告。
```

临时替代：`chrome://flags/#unsafely-treat-insecure-origin-as-secure` 加入
`http://<host>:8890` 后重启浏览器。

想让孩子连接受自签警告这一次点击都省掉，给 Chrome 加
`--autoplay-policy=no-user-gesture-required`（并在该 origin 上永久允许
麦克风），页面加载后就会自己开麦。

## 模式

- `kid`：零基础（约 7-10 岁）——对话用中文，英文只出现在教学点
  （“兔子的英文是 rabbit。”），目标是培养兴趣和日常表达。
- `kid_upper`：小学高年级（约 10-12 岁）——中文对话里自由夹短英文句。
- `adult`：全英文对话，正常语速；用户明显卡住时才用中文。

模式的行为（系统提示词）与音色、语言、语速都写在 `server/config.yaml`
的 `modes` 段，未覆盖的项取 `tts` 段的全局值——换音色、换语言只改这
一个文件。Qwen3-TTS 0.6B 无速率参数，可选的放慢由 orchestrator 端
WSOLA 变速实现（`server/orchestrator/tts.py`），在语音上有可闻伪影，
默认不启用。

## CLI 客户端（headless）

没有浏览器也能用：`client/` 下有一个终端客户端，面向"任何带音频输入输出
的 Linux 机器，从另一台设备 SSH 进来用"的形态（树莓派是目标形态之一）。

```bash
pip install -r client/requirements.txt   # 需要 libportaudio2；Python >= 3.10
scripts/talknest-cli                     # 一条命令：自动 venv、自动 ws/wss
scripts/talknest-cli --list-devices      # 看设备
scripts/talknest-cli --input hw:2,0 --output hw:2,0   # 手动指定设备
```

回声取消按设备类型自动选择（`--aec auto`，默认）：蓝牙 HFP 设备自动降级
为半双工（HFP 时延抖动实测会让任何参考对齐型 AEC 失效），其余设备用
进程内 DTLN-AEC 神经网络（非 WebRTC 路线，TFLite，模型见
[client/docs/bench/cli-aec.md](docs/bench/cli-aec.md)）；`--aec none`
强制半双工。麦克风/扬声器来自文件（`--input-file`/`--output-file`）时
不需要声卡，可做离线回归。实测记录见
[docs/bench/cli-aec.md](docs/bench/cli-aec.md)。

## 音频设备

页面上的“麦克风 / 扬声器”下拉显示当前实际在用的设备，也可以直接指定，
选择记在 localStorage。默认“自动”：麦克风跟浏览器默认，扬声器跟麦克风
所属的设备（同一个 `groupId`，即蓝牙耳机的另一半），所以蓝牙耳机重连后
声音会一起回到耳机，不会留在 HDMI 外放上。

麦克风或扬声器离线（蓝牙断连、track 结束、设备从列表消失）时页面自动
暂停并提示缺了什么；设备回来后自动恢复会话。播放切输出走
`AudioContext.setSinkId()`，播放和采集始终共用同一个 AudioContext，
AEC 的参考信号不受影响。

## 目录

```
server/orchestrator/   会话编排与语音管线（FastAPI + WebSocket）
server/services/       各推理服务启动/停止脚本与 TTS sidecar
web/                   纯 JS 前端（getUserMedia 常驻采集 + 队列播放）
client/                终端客户端（headless，见上文"CLI 客户端"）
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

## 文档

- [docs/vad-tuning.md](docs/vad-tuning.md) — 轮次切分与打断的机制、调参
- [docs/bench/](docs/bench/) — 延迟与显存实测记录（LLM 显存预算、ASR
  fp8 量化、M1 基线）
