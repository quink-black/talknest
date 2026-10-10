# Talknest

Talknest 是一个本地部署的英语口语对话练习应用，面向儿童零基础教学和成人
自练两种用途。打开页面、说一句话，对方就接话；全部推理都在家庭局域网内
完成，语音与文本都不离开本机。从说完到听见回复约 3 秒。

## 已验证的平台

| 平台 | 硬件 | ASR | llama.cpp 构建选项 | TTS 设备 | 常驻方式 |
|---|---|---|---|---|---|
| Linux | NVIDIA GPU，显存 12 GB 以上 | vLLM（默认） | `-DGGML_CUDA=ON` | CUDA | tmux 或 systemd |
| macOS | Apple Silicon | llama.cpp（`ASR_BACKEND=llamacpp`） | `-DGGML_METAL=ON` | MPS | tmux |

表中只列出已经测试过的组合。ASR、TTS 与 LLM 各自是独立的 HTTP 服务，因此
欢迎把项目移植到其他平台，例如 AMD GPU 或纯 CPU 环境。

## 快速开始

**1. 准备依赖。** 安装 Python 3.12，创建两个 venv，再按上表的构建选项自行
构建 llama.cpp。显存预算与各参数的取舍见
[docs/architecture.md](docs/architecture.md)：

```bash
python3.12 -m venv venv     && ./venv/bin/pip install -r server/requirements.txt
python3.12 -m venv venv-tts && ./venv-tts/bin/pip install "qwen-tts==0.1.1" uvicorn
```

**2. 填写本机配置。** 路径、权重根目录、构建目录这类只与这台机器有关的值
写在 `local.env`，该文件不进版本库：

```bash
cp local.env.example local.env
```

模板里对每一项都有说明。若某一项没有填写，脚本会报错并指出缺少哪一项。

**3. 下载模型权重。** 权重不随仓库分发，默认放在仓库同级的 `model/` 目录，
可以用 `TALKNEST_MODEL_ROOT` 修改。各模型的官方下载地址、下载命令和目标
子目录见 [docs/architecture.md](docs/architecture.md#模型权重)。

**4. 导入教学词库**（下载约 4 MB，只需执行一次）：

```bash
./venv/bin/python scripts/import_curriculum.py
```

跳过这一步应用仍然可以运行，但大纲里的词不再做拼写校验。

**5. 启动。** 下面的命令按依赖顺序启动四个服务，并等待每个服务就绪：

```bash
scripts/tmux_start.sh
```

**6. 打开界面。** 浏览器只在安全上下文中开放麦克风，因此通过局域网访问时
需要自签证书：

```bash
scripts/gen_dev_cert.sh     # 把输出的 TLS_CERT / TLS_KEY 写进 local.env
```

然后打开 `https://<本机地址>:8890`，并在浏览器的警告页面确认一次。如果只
在本机的浏览器里使用，可以直接打开 `http://127.0.0.1:8890`。

## 使用

### 对话

页面打开后自动连接麦克风。孩子直接说话，服务端检测到停顿就开始回复，
不需要按键。应用说话时，孩子可以随时插话打断。如果浏览器拦截了自动播放，
或者拒绝了麦克风权限，才需要点击一次按钮。

整条链路是流式的：语音一边说一边转写，回复生成完第一句就开始合成音频，
因此等待时间与整段回复的长度无关。

### 三档模式

| 模式 | 适合 | 对话语言 |
|---|---|---|
| `kid` | 零基础（约 7-10 岁） | 中文，英文只出现在教学点（“兔子的英文是 rabbit。”） |
| `kid_upper` | 小学高年级（约 10-12 岁） | 中文对话里自由夹杂短英文句 |
| `adult` | 成人自练 | 全英文，正常语速；明显卡住时才用中文 |

模式在页面上切换。三档的行为（系统提示词）以及音色、语言、语速都写在
`server/config.yaml` 的 `modes` 段；某一项没有在 `modes` 段中设置时，取
`tts` 段的全局值。因此更换音色或语言只需修改这一个文件。

### 自由聊天开关

`kid` 与 `kid_upper` 带“自由聊天”开关，切换时不会清空对话。打开开关后
应用以聊天为主：它不再推送大纲新词、复习词和跟读判定，但孩子说出的英文
仍然记为练习，老师在聊天中教的新词仍然记为话题词。开关状态按学习者保存在
数据库里，因此 iPad 和电脑上显示的是同一个状态。`adult` 模式本身就是自由
对话，没有这个开关。

### 我是谁

在页面侧栏选定学习者（“我是谁”）之后，练习记录、学习画像和自由聊天开关
都归属于这位学习者。切换学习者就切换到另一份档案。

### 声纹录入

家庭成员在侧栏的“声纹录入”里各录几段话，服务端就能分辨一句话来自孩子、
爸爸还是妈妈。家长的话不计入孩子的练习，对话气泡上也会标出说话的人。

录入方法：打开“声纹录入”，在孩子、爸爸、妈妈三行里点“录一段”，按提示
说话，约 8 秒后录音自动停止。每个人建议录两三段，累计 15 秒以上。录音期间
页面不会把声音发给对话，服务端也只保存声纹向量，不保存录音。

每一行下面列出已录的各段及其长度。如果某一段混入了别人的声音，点该段的
“删除”即可，其余录音不受影响。“清除”会删除该角色的全部录音，需要连续
点击两次确认。孩子和至少一位家长都录入之后，识别才会启用。识别启用后，
若判断不出说话的人，服务端按孩子处理，气泡标“我”。识别未启用时不显示
标签。

识别的阈值、模型与接口见
[docs/speaker-recognition.md](docs/speaker-recognition.md)。

### 音频设备

页面上的“麦克风 / 扬声器”下拉框显示当前正在使用的设备，也可以手动指定，
选择结果保存在浏览器本地。默认选项是“自动”：麦克风使用浏览器的默认设备，
扬声器使用麦克风所属的设备（例如蓝牙耳机的播放端）。因此蓝牙耳机重新连接
之后，麦克风和扬声器会一起回到耳机。

麦克风或扬声器离线时（例如蓝牙断开，或设备从列表中消失），页面自动暂停
并提示缺少哪个设备。设备恢复之后，页面自动恢复会话。

### iPad app

`ios/` 是一个原生 app，只提供外壳：启动后全屏加载 `https://<host>:8890`，
并自行处理麦克风授权、音频自动播放和自签证书。服务地址在 app 内填写。构建与
安装见 [ios/README.md](ios/README.md)。

### 终端客户端

没有浏览器时，可以使用 `client/` 下的终端客户端。它面向这样的设备：带音频
输入输出的 Linux 机器，用户从另一台设备通过 SSH 登录后使用，树莓派就是一种
典型设备。

```bash
pip install -r client/requirements.txt   # 需要 libportaudio2；Python >= 3.10
scripts/talknest-cli                     # 一条命令：自动 venv、自动 ws/wss
scripts/talknest-cli --list-devices      # 看设备
scripts/talknest-cli --input hw:2,0 --output hw:2,0   # 手动指定设备
```

回声取消（AEC）按设备类型自动选择（`--aec auto`）：蓝牙 HFP 设备使用
半双工，因为 HFP 的时延抖动会使依赖参考信号对齐的 AEC 失效；其余设备使用
进程内的 DTLN-AEC 神经网络。`--aec none` 强制使用半双工。当麦克风或扬声器
来自文件（`--input-file` / `--output-file`）时不需要声卡，可用于离线回归。
实测记录见 [docs/bench/cli-aec.md](docs/bench/cli-aec.md)。

## 配置

### local.env

venv 路径、构建目录、权重根目录这类值只与当前机器有关，因此不进版本库。
复制模板并填写一次之后，后续所有脚本都不需要再带参数。`scripts/local_env.sh`
负责加载该文件，因此其中的值对运行在 tmux 或 systemd 里的服务同样生效。

### 修改模式、提示词与音色

三档模式的行为与音色都在 `server/config.yaml`。模式的设置在 `modes` 段；
某一项没有在 `modes` 段中设置时，取 `tts` 段的全局值。修改后重启
orchestrator 即可生效。

### 浏览器访问

浏览器只在安全上下文中开放麦克风：通过纯 HTTP 访问局域网 IP 时，浏览器
不提供 `navigator.mediaDevices`，iPad 上的 WKWebView 也一样。
`scripts/gen_dev_cert.sh` 生成的自签证书覆盖 localhost 与本机的局域网地址，
把输出的 `TLS_CERT` / `TLS_KEY` 写进 `local.env` 即可。当 `certs/dev.pem`
存在时，`start_web.sh` 自动启用 TLS。

若要让孩子也不必确认自签证书的警告，可以给 Chrome 加上
`--autoplay-policy=no-user-gesture-required`，并在该 origin 上永久允许
麦克风。这样页面加载之后会自动开启麦克风。

## 运行与停止

四个服务：ASR（:8001）、TTS（:8002）、LLM（:9999）、界面（:8890）。

```bash
scripts/tmux_start.sh                 # 启动全部服务，按依赖顺序等待就绪
scripts/tmux_start.sh asr tts         # 只重启指定的服务
scripts/tmux_stop.sh                  # 关闭 tmux 窗口并释放端口
```

每个服务也可以在前台单独运行（`server/services/start_{asr,tts}.sh`、
`server/services/start_llm_llamacpp_9b.sh`、`scripts/start_web.sh`），
并用对应的 `stop_*.sh` 停止。Linux 上还可以用 systemd 用户单元实现无人值守
的常驻运行（`scripts/systemd/install.sh` 与 `scripts/systemd/services.sh`）。
macOS 没有 systemd，常驻运行使用 tmux。

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

`tests/ws_speaker_smoke.py` 测试声纹，需要先在另一个端口启动服务，用法见
脚本头部。`scripts/bench_llm.py` 测量 LLM 延迟。`scripts/eval_teaching.py`
用模拟的孩子进行若干轮对话，检查教学计划是否被执行，并测量计划块带来的
延迟，运行时 LLM 必须已经启动。

`scripts/hooks/pre-commit` 包含 gitleaks 密钥扫描，启用方法如下，只需执行
一次：

```bash
git config core.hooksPath scripts/hooks
```

若未安装 gitleaks，该 hook 只输出警告，不会阻止提交。

## 文档

- [docs/architecture.md](docs/architecture.md) — 进程与运行时、端口、模型
  权重、硬件与软件要求
- [docs/services.md](docs/services.md) — 启动顺序、tmux 与 systemd、停止
  与日志、本机配置怎么传到进程
- [docs/speaker-recognition.md](docs/speaker-recognition.md) — 声纹模型、
  阈值、接口
- [docs/vad-tuning.md](docs/vad-tuning.md) — 轮次切分与打断的机制、调参
- [docs/bench/](docs/bench/) — 延迟与显存实测记录
