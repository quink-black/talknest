# 架构

本文说明 Talknest 由哪些进程组成、每个进程使用什么运行时，以及搭建一台
机器需要准备什么。如果只想运行项目，阅读 README 即可。

## 链路

浏览器持续采集麦克风（带 AEC），并通过 WebSocket 把音频发送给
orchestrator。orchestrator 用流式 VAD 切分轮次，维护会话与模式，处理插话
打断，并把语音交给三个独立的推理服务。三个服务都支持流式输出，也可以逐个
替换。VAD 内嵌在 orchestrator 进程里，不单独占用端口。

## 四个服务

| 环节 | 模型 | 运行时 | 端口 |
|---|---|---|---|
| orchestrator | — | FastAPI + WebSocket | `TALKNEST_PORT`（8890） |
| ASR | Qwen3-ASR-1.7B | vLLM 的 `qwen-asr-serve`，或 llama.cpp | `ASR_PORT`（8891） |
| TTS | Qwen3-TTS-12Hz-0.6B-CustomVoice | qwen-tts 包，外面套一层 OpenAI 兼容 sidecar | `TTS_PORT`（8892） |
| LLM | Qwen3.5-9B MTP GGUF（Q4_K_M） | llama.cpp，可执行文件目录由 `TN9B_BIN_DIR` 指定 | `LLM_PORT`（8893） |
| VAD | Silero VAD（ONNX） | orchestrator 内嵌，CPU 流式 | — |

四个端口连成一段，起点是 orchestrator 自己的端口，三个后端依次占它后面
一个（`scripts/ports.sh` 是唯一的定义处，每个端口都能在 `local.env` 里
覆盖）。后端只监听 `127.0.0.1`，对外只需要放开 orchestrator 那一个。

浏览器到服务端的采样率是 24 kHz。Qwen3-ASR 在 16 kHz 输入下会转写失败，
而 24 kHz 与 Qwen3-Omni 的音频前端一致。轮次切分与打断的机制以及各参数的
调法见 [docs/vad-tuning.md](vad-tuning.md)。

## ASR 的两个运行时

`local.env` 里的 `ASR_BACKEND` 决定使用哪一个：

- `vllm`（默认）使用 vLLM 的 `qwen-asr-serve`，只能在 CUDA 设备上运行。
- `llamacpp` 使用 llama.cpp，不依赖 CUDA，已在 Apple Silicon 上验证。它
  读取 ggml-org 预转换的 `Qwen3-ASR-1.7B-GGUF`，而不是 Hugging Face 上的
  原始权重目录：llama.cpp 没有这个模型架构的转换脚本，因此只能使用官方
  发布的 GGUF。

两个运行时提供同一个 `/v1/audio/transcriptions` 接口，返回的
`language English<asr_text>` 信封格式也相同，因此切换后端时 orchestrator
不需要改动。两者唯一的差别在强制语言重试。当模型自报的语言不在
`asr.languages` 中时，`server/orchestrator/asr.py` 会带上 `to_language`
重新识别一次。llama.cpp 不支持该字段，它的 `language` 只是在提示词末尾
追加 `(language: en)`，而 Qwen3-ASR 不理会这个后缀，所以重试得到的仍是
同一个结果。这一条经过实测：在 macOS 设备（M4 Pro）上对中文语音强制
英文，返回的仍是中文。因此如果文本既不是拉丁字母也不是中日韩文字，这一轮会被丢弃，不会
改用英文或中文重新识别。

## TTS 的设备选择

sidecar 在启动时自动选择设备：有 CUDA 就用 CUDA，否则用 MPS，两者都没有
就用 CPU。`QWEN_TTS_DEVICE` 与 `QWEN_TTS_DTYPE` 可以覆盖自动选择的结果。
CUDA 与 MPS 都使用 bfloat16：在 MPS 上，float16 会使采样器溢出并拒绝自己
生成的概率张量，而 bfloat16 的指数范围更宽，不会出现这个问题。

## 换更大的 LLM

修改 `server/config.yaml` 的 `llm.active` 一行，可以切换到
Qwen3.8-27B-NVFP4（vLLM）。切换时要先停止 9B，再启动 27B，因为两者共用
同一块显存。27B 需要远大于 12 GB 的显存。

## 硬件与软件要求

### 平台

| 平台 | 硬件 | llama.cpp 构建选项 | ASR 后端 | TTS 设备 |
|---|---|---|---|---|
| Linux | NVIDIA GPU，显存 12 GB 以上 | `-DGGML_CUDA=ON` | `vllm`（默认） | CUDA |
| macOS | Apple Silicon | `-DGGML_METAL=ON`（Apple 平台默认开启） | `llamacpp` | MPS |

表中只列出已经测试的组合。Linux 上三个模型加载在同一张卡上，显存 12 GB
是实测下限；显存预算与各参数的取舍记录在
[bench/llm-vram-fit.md](bench/llm-vram-fit.md)。macOS 上无需做显存预算，
也无需为 `-fit` 保留启动顺序。

欢迎移植到其他平台。LLM、ASR、TTS 各自是独立的 HTTP 服务，移植时只需让
对应服务在目标平台上运行。llama.cpp 提供 Vulkan、HIP 等其他后端，TTS
sidecar 可以用 `QWEN_TTS_DEVICE` 指定 PyTorch 设备，因此 AMD GPU 或纯 CPU
环境都值得一试，但这些组合尚未验证。

### 软件

**Python 3.12 与 venv。** `venv/` 运行 orchestrator，安装
`server/requirements.txt`。`venv-tts/` 单独安装 qwen-tts 与 transformers，
目的是避免依赖降级。vLLM 路线的 ASR 还需要第三个 venv，其路径由
`ASR_VENV` 指定；使用 llama.cpp 运行 ASR 时不需要这个 venv。

**llama.cpp。** 需要自行构建，构建选项见上表。LLM 的可执行文件目录由
`TN9B_BIN_DIR` 指定，ASR 的由 `ASR_LLAMA_BIN` 指定。ASR 不复用
`ASR_BIN`，因为 `start_asr.sh` 已经把 `ASR_BIN` 当作 vLLM 的启动器来读取。

**浏览器。** 使用桌面版 Chrome 或 Edge。扬声器跟随麦克风切换依赖
`AudioContext.setSinkId()`，只有 Chrome 系浏览器提供该接口。在其他浏览器
里，扬声器下拉框被禁用，扬声器跟随系统输出。

## 模型权重

权重不随仓库分发，默认放在仓库同级的 `model/` 目录。根目录可以用
`TALKNEST_MODEL_ROOT` 修改；各服务另有单独的覆盖变量（`TN9B_MODEL_DIR`、
`ASR_MODEL`、`QWEN_TTS_DIR`）。

| 目录（相对 `model/`） | 用途 | 官方下载地址 |
|---|---|---|
| `Qwen3.5-9B-MTP-GGUF/` | LLM；默认文件 `Qwen3.5-9B-Q4_K_M.gguf`，量化后缀由 `TN9B_QUANT` 修改 | [Hugging Face: unsloth/Qwen3.5-9B-MTP-GGUF](https://huggingface.co/unsloth/Qwen3.5-9B-MTP-GGUF) |
| `Qwen3-ASR-1.7B/` | ASR，vLLM 路线 | [Hugging Face: Qwen/Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) |
| `Qwen3-ASR-1.7B-GGUF/` | ASR，llama.cpp 路线；需要 GGUF 与 mmproj 两个文件，路径见 `ASR_GGUF` 与 `ASR_MMPROJ` | [Hugging Face: ggml-org/Qwen3-ASR-1.7B-GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF) |
| `Qwen3-TTS-12Hz-0.6B-CustomVoice/` | TTS | [Hugging Face: Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice) |
| `Qwen3-TTS-Tokenizer-12Hz/` | TTS 分词器 | [Hugging Face: Qwen/Qwen3-TTS-Tokenizer-12Hz](https://huggingface.co/Qwen/Qwen3-TTS-Tokenizer-12Hz) |
| `silero-vad/silero_vad.onnx` | VAD | [GitHub: snakers4/silero-vad](https://github.com/snakers4/silero-vad)，文件位于 `src/silero_vad/data/` |
| `speaker/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` | 声纹，可选 | [GitHub: k2-fsa/sherpa-onnx 的 speaker-recongition-models 发布页](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models) |
| `dtln-aec/dtln_aec_128_{1,2}.tflite` | 终端客户端的回声取消，可选 | [GitHub: breizhn/DTLN-aec](https://github.com/breizhn/DTLN-aec)，文件位于 `pretrained_models/` |

上表中 Qwen 系列与 unsloth 的权重在 ModelScope 上有同名仓库，网络访问
Hugging Face 不便时，可以把下面命令中的 `hf download` 换成
`modelscope download --model`。

```bash
D="${TALKNEST_MODEL_ROOT:-../model}"    # 默认是与本仓库同级的 model/

hf download unsloth/Qwen3.5-9B-MTP-GGUF Qwen3.5-9B-Q4_K_M.gguf \
    --local-dir "$D/Qwen3.5-9B-MTP-GGUF"
hf download Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice \
    --local-dir "$D/Qwen3-TTS-12Hz-0.6B-CustomVoice"
hf download Qwen/Qwen3-TTS-Tokenizer-12Hz \
    --local-dir "$D/Qwen3-TTS-Tokenizer-12Hz"

# ASR 按 ASR_BACKEND 二选一
hf download Qwen/Qwen3-ASR-1.7B --local-dir "$D/Qwen3-ASR-1.7B"           # vllm
hf download ggml-org/Qwen3-ASR-1.7B-GGUF \
    Qwen3-ASR-1.7B-Q8_0.gguf mmproj-Qwen3-ASR-1.7B-Q8_0.gguf \
    --local-dir "$D/Qwen3-ASR-1.7B-GGUF"                                   # llamacpp

# 小文件，直接下载
mkdir -p "$D/silero-vad" "$D/speaker"
curl -L -o "$D/silero-vad/silero_vad.onnx" \
    https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx
curl -L -o "$D/speaker/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx" \
    https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx
```

两个 ASR 目录只会按 `ASR_BACKEND` 读取其中一个。

## 仓库目录

```
server/orchestrator/   会话编排与语音管线（FastAPI + WebSocket）
server/services/       各推理服务启动/停止脚本与 TTS sidecar
web/                   纯 JS 前端（getUserMedia 常驻采集 + 队列播放）
client/                终端客户端（headless）
scripts/               llama.cpp 启动器、tmux/systemd 编排、基准脚本
tests/                 单元测试 + 服务探针 + 全链路冒烟
docs/                  设计文档与实测报告
local.env              本机配置（不入库，见 local.env.example）
```
