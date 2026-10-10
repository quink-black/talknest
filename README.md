# Talknest

Talknest 是一个本地部署的英语口语对话练习应用，面向儿童零基础教学和成人
自练两种用途。打开页面、说一句话，对方就接话；全部推理都在家庭局域网内
完成，语音与文本都不离开本机。从说完到听见回复约 3 秒。

Linux 与 macOS 都能跑，前者需要一张 NVIDIA 卡，后者用 Apple Silicon 的
统一内存。

## 快速开始

**1. 准备依赖。** 需要 Python 3.12 与两个 venv，llama.cpp 需要自行构建
（Linux 带 `-DGGML_CUDA=ON`，macOS 带 `-DGGML_METAL=ON`）。显卡、显存与
各参数的取舍见 [docs/architecture.md](docs/architecture.md)：

```bash
python3.12 -m venv venv     && ./venv/bin/pip install -r server/requirements.txt
python3.12 -m venv venv-tts && ./venv-tts/bin/pip install "qwen-tts==0.1.1" uvicorn
```

**2. 填本机配置。** 路径、权重根目录、构建目录这类只与这台机器有关的值
写在 `local.env`，不进版本库：

```bash
cp local.env.example local.env
```

每一项在模板里都有说明。脚本缺哪一步会直接报错并提示要填哪一项，不会
猜测路径。

**3. 放模型权重。** 权重不随仓库分发，默认放在仓库同级的 `model/` 目录，
可以用 `TALKNEST_MODEL_ROOT` 改到别处。各模型该放哪个子目录见
[docs/architecture.md](docs/architecture.md#模型权重)。

**4. 导入教学词库**（下载约 4 MB，只需执行一次）：

```bash
./venv/bin/python scripts/import_curriculum.py
```

跳过也能跑，但大纲里的词不做拼写校验，拼错不会被发现。

**5. 启动。** 一条命令拉起四个服务，按依赖顺序等各自就绪：

```bash
scripts/tmux_start.sh
```

**6. 打开界面。** 麦克风要求安全上下文，局域网访问时用自签证书：

```bash
scripts/gen_dev_cert.sh     # 把输出的 TLS_CERT / TLS_KEY 写进 local.env
```

然后打开 `https://<本机地址>:8890`，接受一次自签警告。只在本机浏览器里
用的话，`http://127.0.0.1:8890` 直接可用。

## 使用

### 对话

页面打开后自动连接麦克风，孩子直接说话，检测到停顿之后服务端就接话，
不需要按键。对方说话时可以直接插话打断。只有当浏览器拦截自动播放或者
拒绝麦克风权限时，才需要点击一次按钮。

整条链路是流式的：语音边说边转写，回复攒满第一句就开始合成音频，因此
等待时间不按整段回复的长度计价。

### 三档模式

| 模式 | 适合 | 对话语言 |
|---|---|---|
| `kid` | 零基础（约 7-10 岁） | 中文，英文只出现在教学点（“兔子的英文是 rabbit。”） |
| `kid_upper` | 小学高年级（约 10-12 岁） | 中文对话里自由夹杂短英文句 |
| `adult` | 成人自练 | 全英文，正常语速；明显卡住时才用中文 |

模式在页面上切换。三档的行为（系统提示词）与音色、语言、语速都写在
`server/config.yaml` 的 `modes` 段，未被覆盖的项取 `tts` 段的全局值，
因此换音色、换语言只改这一个文件。

### 自由聊天开关

`kid` 与 `kid_upper` 带“自由聊天”开关，切换时不清空对话。打开后以聊天
为主：不再推送大纲新词、复习词和跟读判定，但孩子说出的英文照旧记为
练习，老师顺口教的新词照旧记为话题词。开关状态按学习者记在数据库里，
因此 iPad 和电脑上看到的是同一个状态。`adult` 本来就是自由对话，没有
这个开关。

### 我是谁

页面侧栏选定学习者（“我是谁”）之后，练习记录、学习画像和自由聊天开关
都跟着这个人走。换一个人就换一份档案。

### 声纹录入

家庭成员在侧栏的“声纹录入”里各录几段话，服务端就能分辨一句话来自孩子、
爸爸还是妈妈。家长的话不计入孩子的练习，对话气泡上也会标出说话的人。

打开“声纹录入”，在孩子、爸爸、妈妈三行里点“录一段”，按提示说话，约 8 秒
后自动停止。每个人建议录两三段，累计 15 秒以上。录音期间页面不把声音
发给对话，服务端也只保存声纹向量，不保存录音。

每一行下面列出已录的各段及其长度。某一段录进了别人的声音时，点那一段
的“删除”即可，其余录音不受影响；“清除”删除该角色的全部录音，需要连点
两次确认。孩子和至少一位家长都录入之后，识别才会启用；判断不出是谁时
按孩子处理，气泡标“我”。识别未启用时不显示标签。

识别的阈值、模型与接口见
[docs/speaker-recognition.md](docs/speaker-recognition.md)。

### 音频设备

页面上的“麦克风 / 扬声器”下拉框显示当前实际在用的设备，也可以直接指定，
选择结果记在浏览器本地。默认是“自动”：麦克风跟随浏览器默认设备，扬声器
跟随麦克风所属的设备（也就是蓝牙耳机的另一半），因此蓝牙耳机重连之后
声音会一起回到耳机。

麦克风或扬声器离线时（蓝牙断连、设备从列表消失），页面自动暂停并提示
缺少哪个设备；设备回来之后自动恢复会话。

### iPad app

`ios/` 是一个只做壳的原生 app：启动即全屏加载
`https://<host>:8890`，自己处理麦克风授权、音频自动播放和自签证书，地址
在 app 内填写。构建与安装见 [ios/README.md](ios/README.md)。

### 终端客户端

没有浏览器时可以用 `client/` 下的终端客户端，目标形态是“一台带音频输入
输出的 Linux 机器，从另一台设备 SSH 进来使用”，树莓派是形态之一。

```bash
pip install -r client/requirements.txt   # 需要 libportaudio2；Python >= 3.10
scripts/talknest-cli                     # 一条命令：自动 venv、自动 ws/wss
scripts/talknest-cli --list-devices      # 看设备
scripts/talknest-cli --input hw:2,0 --output hw:2,0   # 手动指定设备
```

回声取消按设备类型自动选择（`--aec auto`）：蓝牙 HFP 设备降级为半双工，
因为 HFP 的时延抖动会让参考对齐型 AEC 失效；其余设备用进程内的 DTLN-AEC
神经网络。`--aec none` 强制半双工。麦克风或扬声器来自文件时
（`--input-file`/`--output-file`）不需要声卡，可用于离线回归。实测记录见
[docs/bench/cli-aec.md](docs/bench/cli-aec.md)。

## 配置

### local.env

venv 路径、构建目录、权重根目录这类值只与当前机器有关，因此不进版本库。
复制模板填写一次之后，后续所有脚本都不需要再带参数；`scripts/local_env.sh`
负责加载它，因此其中的值对运行在 tmux 或 systemd 里的服务同样生效。

### 改模式、提示词与音色

三档模式的行为与音色都在 `server/config.yaml`：模式在 `modes` 段，未覆盖
的项取 `tts` 段的全局值。改完重启 orchestrator 生效。

### 浏览器访问

麦克风要求安全上下文：局域网 IP 通过纯 HTTP 访问时浏览器不暴露
`navigator.mediaDevices`，iPad 上的 WKWebView 同样如此。
`scripts/gen_dev_cert.sh` 生成的自签证书覆盖 localhost 与本机 LAN 地址，
把输出的 `TLS_CERT` / `TLS_KEY` 写进 `local.env` 即可，`certs/dev.pem`
存在时 `start_web.sh` 自动启用 TLS。

想让孩子连接受自签警告这一次点击也省掉，给 Chrome 加
`--autoplay-policy=no-user-gesture-required`，并在该 origin 上永久允许
麦克风，页面加载之后就会自己开麦。

## 运行与停止

四个服务：ASR（:8001）、TTS（:8002）、LLM（:9999）、界面（:8890）。

```bash
scripts/tmux_start.sh                 # 全部拉起，按依赖顺序等就绪
scripts/tmux_start.sh asr tts         # 只重启某几个
scripts/tmux_stop.sh                  # 停窗口并释放端口
```

每个服务也可以单独前台跑（`server/services/start_{asr,tts}.sh`、
`server/services/start_llm_llamacpp_9b.sh`、`scripts/start_web.sh`），
停止用对应的 `stop_*.sh`。Linux 上还能用 systemd 用户单元做无人值守常驻
（`scripts/systemd/install.sh` 与 `scripts/systemd/services.sh`）；
macOS 没有 systemd，常驻用 tmux。

启动顺序、日志位置、健康检查与 systemd 单元的依赖关系见
[docs/services.md](docs/services.md)。

## 开发

```bash
./venv/bin/python -m pytest tests/ -v      # 单元测试（无需服务）
./venv/bin/python tests/api_probe.py       # 四个服务的健康探针
./venv/bin/python tests/ws_smoke.py        # 全链路：合成语音 → 转写/回复/音频
./venv/bin/python tests/ws_smoke.py --barge   # 断言插话能打断
./venv/bin/python tests/ws_smoke.py --reask   # 断言追问时已有回答不丢
```

`tests/ws_speaker_smoke.py` 测声纹，需要先在另一个端口起服务，见脚本头部。
`scripts/bench_llm.py` 测 LLM 延迟，`scripts/eval_teaching.py` 用模拟的
孩子跑若干轮对话，看教学计划跟不跟得上、计划块带来多少延迟（需要 LLM 在
运行）。

`scripts/hooks/pre-commit` 带 gitleaks 密钥扫描，启用一次：

```bash
git config core.hooksPath scripts/hooks
```

未安装 gitleaks 时该 hook 只警告而不拦截。

## 文档

- [docs/architecture.md](docs/architecture.md) — 进程与运行时、端口、模型
  权重、硬件与软件要求
- [docs/services.md](docs/services.md) — 启动顺序、tmux 与 systemd、停止
  与日志、本机配置怎么传到进程
- [docs/speaker-recognition.md](docs/speaker-recognition.md) — 声纹模型、
  阈值、接口
- [docs/vad-tuning.md](docs/vad-tuning.md) — 轮次切分与打断的机制、调参
- [docs/bench/](docs/bench/) — 延迟与显存实测记录
