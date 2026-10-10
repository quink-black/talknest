# 架构

本文讲 Talknest 由哪些进程组成、各自跑在什么运行时上，以及搭建一台机器
需要准备什么。只想把项目跑起来的话，看 README 就够了。

## 链路

浏览器常驻采集麦克风（带 AEC），通过 WebSocket 送到 orchestrator；
orchestrator 用流式 VAD 切分轮次、维护会话与模式、处理插话打断，并把
语音交给三个独立的推理服务。三个服务都支持流式，也可以逐个替换。VAD
内嵌在 orchestrator 进程里，不单独占端口。

## 四个服务

| 环节 | 模型 | 运行时 | 端口 |
|---|---|---|---|
| orchestrator | — | FastAPI + WebSocket | 8890 |
| LLM | Qwen3.5-9B MTP GGUF（Q4_K_M） | llama.cpp，可执行文件目录由 `TN9B_BIN_DIR` 指定 | 9999 |
| ASR | Qwen3-ASR-1.7B | vLLM 的 `qwen-asr-serve`，或 llama.cpp | 8001 |
| TTS | Qwen3-TTS-12Hz-0.6B-CustomVoice | qwen-tts 包，外面套一层 OpenAI 兼容 sidecar | 8002 |
| VAD | Silero VAD（ONNX） | orchestrator 内嵌，CPU 流式 | — |

浏览器到服务端是 24 kHz：16 kHz 输入会让 Qwen3-ASR 失败，24 kHz 与
Qwen3-Omni 的音频前端一致。轮次切分与打断的机制、以及各参数的调法见
[docs/vad-tuning.md](vad-tuning.md)。

## ASR 的两个运行时

`local.env` 里的 `ASR_BACKEND` 决定用哪个：

- `vllm`（默认）走 vLLM 的 `qwen-asr-serve`，只能在 CUDA 卡上跑。
- `llamacpp` 走 llama.cpp，覆盖 Apple Silicon。它读的是 ggml-org 预转换
  的 `Qwen3-ASR-1.7B-GGUF`，不是 HF 目录 —— llama.cpp 没有这个架构的
  转换脚本，所以只能用官方发布的 GGUF。

两者回答同一个 `/v1/audio/transcriptions` 接口，连
`language English<asr_text>` 这个信封都一样，因此 orchestrator 不需要
随后端改动。唯一的差别在强制语言重试：模型自报的语言不在
`asr.languages` 里时，`server/orchestrator/asr.py` 会带 `to_language`
再读一遍；llama.cpp 不认这个字段，它的 `language` 只是往提示词尾部追加
`(language: en)`，而 Qwen3-ASR 不理会这个后缀，于是重试拿到的是同一个
结果。若文本仍然既不是拉丁字母也不是中日韩文字，这一轮就会被丢弃，而
不是改用英文或中文重读。

## TTS 的设备选择

sidecar 在启动时自己挑设备：有 CUDA 用 CUDA，否则用 MPS，都没有就用
CPU。`QWEN_TTS_DEVICE` 与 `QWEN_TTS_DTYPE` 可以覆盖。两个加速器都用
bfloat16：MPS 上 float16 会让采样器溢出并拒绝自己的概率张量，而
bfloat16 的指数范围更宽，不会触发。

## 换更大的 LLM

`server/config.yaml` 的 `llm.active` 一行可以切到 Qwen3.8-27B-NVFP4
（vLLM）。切换时要先停止 9B 再启动 27B，两者的显存互斥；27B 需要远大于
12G 的显存，或者需要统一内存。

## 硬件与软件要求

**GPU。** Linux 上是一张 NVIDIA 卡，三个模型加载在同一张卡上，12G 是实测
下限；显存预算与各参数的取舍记录在
[bench/llm-vram-fit.md](bench/llm-vram-fit.md)。macOS 上是 Apple Silicon
的统一内存，容量比 12G 宽裕得多，因此既不必按显存做预算，也不必为了
`-fit` 保留启动顺序。

**Python 3.12 与 venv。** `venv/` 运行 orchestrator，装
`server/requirements.txt`；`venv-tts/` 由 qwen-tts 与 transformers 独用
一套依赖，目的是避免依赖降级。vLLM 路线的 ASR 还需要第三个 venv，路径由
`ASR_VENV` 指定；用 llama.cpp 跑 ASR 时不需要它。

**llama.cpp。** 需要自行构建：Linux 上带 `-DGGML_CUDA=ON`，macOS 上带
`-DGGML_METAL=ON`（Apple 平台默认开启）。LLM 的可执行文件目录由
`TN9B_BIN_DIR` 指定，ASR 的由 `ASR_LLAMA_BIN` 指定 —— 后者不复用
`ASR_BIN`，因为 `start_asr.sh` 已经把 `ASR_BIN` 当作 vLLM 的启动器读了。

**浏览器。** 桌面 Chrome 或 Edge。扬声器跟随麦克风切换依赖
`AudioContext.setSinkId()`，该接口只有 Chrome 系浏览器提供；在其他
浏览器里扬声器下拉框禁用，扬声器跟随系统输出。

## 模型权重

权重不随仓库分发，默认放在仓库同级的 `model/` 目录，根目录可以用
`TALKNEST_MODEL_ROOT` 覆盖；各服务另有单独的覆盖变量（`TN9B_MODEL_DIR`、
`ASR_MODEL`、`QWEN_TTS_DIR`）。

```
model/
  Qwen3.5-9B-MTP-GGUF/Qwen3.5-9B-Q4_K_M.gguf   LLM（量化后缀 TN9B_QUANT 可换）
  Qwen3-ASR-1.7B/                               ASR（vLLM 路线）
  Qwen3-ASR-1.7B-GGUF/                          ASR（llama.cpp 路线：
                                                ggml-org 预转换的 GGUF 与
                                                mmproj，见 ASR_GGUF）
  Qwen3-TTS-12Hz-0.6B-CustomVoice/              TTS
  Qwen3-TTS-Tokenizer-12Hz/                     TTS 分词器
  silero-vad/silero_vad.onnx                    VAD
  speaker/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx   声纹（可选）
```

两个 ASR 目录只按 `ASR_BACKEND` 取一个。

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
