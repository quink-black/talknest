# 服务的启动、停止与日志

本文讲四个服务是怎么被拉起和回收的。只想把项目跑起来的话，看 README
的「运行与停止」一节就够了。

## 启动脚本

每个启动脚本只负责在前台运行一个服务，不关心自己被谁监督，所以前台单跑、
`scripts/start.sh`、tmux、systemd 四种方式任选：

| 服务 | 启动脚本 | 端口 |
|---|---|---|
| 界面 | `scripts/start_web.sh` | `TALKNEST_PORT`（默认 8890） |
| ASR | `server/services/start_asr.sh` | `ASR_PORT`（默认 8891） |
| TTS | `server/services/start_tts.sh` | `TTS_PORT`（默认 8892） |
| LLM | `server/services/start_llm_llamacpp_9b.sh` | `LLM_PORT`（默认 8893） |

四个端口是一段连续的块，起点就是界面的端口：三个后端各占它后面一个端口
（`scripts/ports.sh` 是唯一的定义处，每个端口都能在 `local.env` 里单独覆
盖）。改 `TALKNEST_PORT` 整块跟着移动，改其中某一个只钉住那个服务。三个
后端只监听 `127.0.0.1`，所以对外只需要放开界面的那一个端口。

在 CUDA 卡上启动顺序有意义：ASR 与 TTS 各自预留显存，LLM 最后启动，
llama.cpp 的 `-fit on` 会按启动那一刻剩余的显存选择卸载层数（想固定层数
就用 `TN9B_NGL`）。统一内存没有这个约束，macOS 上顺序只是习惯，每层都
直接上 GPU。

## 四种监督方式

**前台。** 直接执行上表的脚本，适合临时调试单个服务。

**一条命令（`scripts/start.sh`，默认方式）。** 按依赖顺序拉起并等各自的健
康检查通过再起下一个；某个服务起不来就停下，不会继续把后面的服务带到没有
显存的卡上。默认模式把每个服务的输出写入 `logs/<service>.current.log`，
并在当前终端合并跟随，Ctrl-C 停止本次启动的全部服务；`-d` 在就绪后打印每
个服务的 PID、健康地址与日志路径并退出，进程脱离终端继续运行，关闭终端不
影响它们。这一条路径不依赖 tmux。

**tmux。** `scripts/tmux_start.sh` 起一个 session，每服务一个 window，顺
序与健康检查同上，输出 tee 到 `logs/<service>.current.log`。想分开观察每
个服务的输出时用；没有装 tmux 也不影响上面两种方式。

**systemd（仅 Linux）。** `scripts/systemd/install.sh` 把四个单元渲染到
`~/.config/systemd/user/`，`scripts/systemd/services.sh` 做
start / stop / restart / status。日志走 journald。macOS 没有 systemd，
常驻这一步用 `scripts/start.sh -d`。

`services.sh` 只用到服务名表，但它与两个启动脚本共用
`scripts/lib/services.sh`，因此也会读 `local.env` 与 `scripts/ports.sh`：
这两处写错时连 `status` 都会退出，而不仅仅是启动失败。所有启动脚本同样
依赖它们，所以这只把 systemd 这条路径拉到与其它路径一致的水平。

三种方式可以互换：两种启动脚本都会先停掉正在运行的 systemd 单元，
`scripts/stop.sh` 也覆盖三种方式收尾。不要同时运行，端口只有一个。

两条 systemd 细节值得知道：

- LLM 单元用 `Requires=` 依赖 ASR 与 TTS，因此 ASR 或 TTS 未就绪时 LLM
  不会启动，这条依赖保护的是 `-fit` 所要求的显存分配顺序。显式停止 ASR
  或 TTS 会连带停止 LLM，但不会停止 orchestrator。
- 修改 TLS 配置、修改端口或移动 checkout 之后要重新运行 `install.sh`，
  因为各单元的健康检查 URL 在渲染时就写死了端口和协议。

## 停止

`scripts/stop.sh [服务...]`（默认全部）依次做三件事，每一步对不存在的对象
都是空操作：停止处于 active 的 systemd 单元、关掉对应的 tmux window、按端
口回收进程。单元放在第一步，是因为先杀端口上的进程只会触发单元的
`Restart=on-failure`，与重启争夺同一个端口。因此停止与启动方式无关，一条
命令覆盖上面三种方式。

`server/services/stop_{asr,tts,llm}.sh` 与 `scripts/stop_web.sh` 按端口
回收单个服务，也可以单独执行。端口取自 `scripts/ports.sh`，因此与
`local.env` 里的覆盖一致。四者共用 `scripts/free_port.sh`：它用
`lsof -nP -iTCP:<port> -sTCP:LISTEN -t` 找出监听者并 SIGKILL，之后复查
端口是否真的空了，没空就报错退出。

不用 Linux 的 `fuser -k PORT/tcp` 有两个原因：macOS 也带一个 `fuser`，
但那是个 Perl 脚本，既不接受 `-k` 也不接受 `PORT/tcp`，而
`command -v fuser` 照样成功，于是脚本会杀不掉进程却报告已释放；
`lsof` 在两种系统上都答得对。`free_port.sh` 找不到 lsof 时直接报错，
而不是当作已经释放。

## 日志

- `scripts/start.sh` 与 tmux 模式下每个服务写 `logs/<service>.current.log`
  （带时间戳的那一份的符号链接）。
- LLM 的 llama.cpp 输出另外 tee 一份到 `logs/llama_qwen35_9b_<quant>_<时间戳>.log`，
  `logs/llama.current.log` 指向最新一份。
- systemd 模式下用 `journalctl --user -u talknest-<服务> -f`。

## 本机配置怎么传到进程

`scripts/local_env.sh` 负责加载仓库根目录的 `local.env`（`set -a` 导出），
启动脚本、停止脚本、`scripts/run_server.sh` 与
`scripts/llama_qwen35_9b.sh` 都会 source 它，所以其中的值对 tmux 与
systemd 里的服务同样生效。某一项缺失时脚本直接报错并提示要填哪一项，
不会猜测路径。

tmux 是个例外：window 继承的是 tmux server 的环境，不是调用者的，因此
`tmux_start.sh` 另外把 `TALKNEST_`、`ASR_`、`TTS_`、`LLM_`、`TN9B_`、
`QWEN38_`、`TLS_` 开头的变量，以及 `RUN_QWEN38`、`HF_HOME`、
`PYTORCH_CUDA_ALLOC_CONF` 这几个不带前缀的变量，用 `-e` 逐个传进去。漏掉
一个，命令行上给的对应覆盖就会被静默丢掉：服务仍按 `local.env` 或默认值
启动，而 supervisor 这边的健康检查可能已经在等另一个端口。不在这个清单里
的变量要写进 `local.env`。

`TALKNEST_MODEL_ROOT` 是唯一有默认值的变量：未填写时回退到与仓库同级的
`model/` 目录，`server/orchestrator/config.py` 的 `model_root()` 用同一
规则。

## 浏览器访问需要安全上下文

麦克风要求安全上下文：局域网 IP 通过纯 HTTP 访问时，浏览器不暴露
`navigator.mediaDevices`，iPad 上的 WKWebView 同样如此。
`scripts/gen_dev_cert.sh` 生成自签证书，SAN 覆盖 localhost 与本机 LAN
IPv4（Linux 用 `hostname -I`，macOS 用 `ifconfig`；虚拟接口的地址也会
被收进来，多一个 SAN 无害），把输出的 `TLS_CERT` / `TLS_KEY` 写进
`local.env` 即可，`certs/dev.pem` 存在时 `start_web.sh` 自动启用 TLS。

临时替代方案是把 `http://<host>:8890` 加进
`chrome://flags/#unsafely-treat-insecure-origin-as-secure` 再重启浏览器。

想让页面加载后自己开麦，省掉接受自签警告的那一次点击，给 Chrome 加
`--autoplay-policy=no-user-gesture-required`，并在该 origin 上永久允许
麦克风。
