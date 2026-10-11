# 不依赖 tmux 的一键启动

## 背景

`scripts/tmux_start.sh` 目前承担两件性质不同的事：

1. 按依赖顺序拉起四个服务，并在拉起下一个之前等待当前服务的健康检查
   通过。这是业务约束：llama.cpp 的 `-fit on` 按启动那一刻剩余的显存
   决定卸载层数，ASR 与 TTS 必须先占住各自的显存池。
2. 用 tmux 承载这些进程，每个服务一个 window。这只是观察手段。

问题在于这两件事目前只有 tmux 一条路径：想一次起全部服务就必须装
tmux，于是可选的观察手段变成了事实上的前置依赖。服务脚本本身
（`server/services/start_*.sh`、`scripts/start_web.sh`）都是纯前台的，
并不关心谁监督它们，缺的只是一个不依赖 tmux 的监督者。

## 目标

- 一条命令起全部服务，且不依赖 tmux。
- 保留「顺序拉起 + 就绪等待」这一约束，不引入并行启动。
- 停止方式与启动方式解耦：无论谁拉起的进程都能用同一条命令回收。

## 非目标

- 不做自动重启与故障恢复，那是 systemd 单元的职责。
- 不改动服务脚本本身，不新增配置变量。
- 不改动 systemd 单元与 `scripts/systemd/` 下的脚本。

## 方案

### scripts/lib/services.sh（新增）

把三个启动脚本共用的部分抽成一个只定义函数、不自己执行的库，由调用方
source：

- 服务名清单与默认的依赖顺序。
- 每个服务的启动脚本与停止脚本路径。
- 每个服务的健康检查 URL，含 web 的 scheme 判定（配置了 TLS 或存在
  `certs/dev.pem` 时用 https）。
- `wait_ready`：轮询健康 URL 直到 `TALKNEST_READY_TIMEOUT`；同时检查进程
  是否还活着，启动即崩的情况据此快速失败。
- 日志路径：新建 `logs/<服务>_<时间戳>.log`，并把
  `logs/<服务>.current.log` 指向它，沿用现有约定。
- 启动前停掉正在运行的 systemd 单元，避免端口争夺把单元拖进
  `Restart=on-failure` 循环。

约束：macOS 的 `/bin/bash` 是 3.2，没有关联数组，因此这些表继续用 case
语句表达。

### scripts/start.sh（新增）

```
scripts/start.sh [-d|--daemon] [asr|tts|llm|web ...]
```

不带服务名时按依赖顺序启动全部四个；带服务名时只处理指定的几个。两种
模式共用同一套拉起与等待逻辑，差别只在拉起之后做什么。

**默认模式（attached）。** 服务依次进入后台，各自的输出重定向到自己的日
志文件；全部就绪后，终端留在前台合并跟随这些日志。Ctrl-C 或收到 SIGTERM
时，trap 停止本次启动的服务并退出（退出码 130 / 143）。这一模式替代
tmux 承担调试入口：一条命令看到全部输出，不需要先装 tmux。

**守护模式（`-d`）。** 服务用 `nohup` 拉起，标准输入接 `/dev/null`、输出
追加到各自的日志；脚本等全部就绪后打印每个服务的 PID、日志路径与健康
URL，然后退出 0。进程脱离终端继续运行，关闭终端不影响它们。

任一服务在超时前未就绪，或进程提前退出：打印错误与日志路径后退出 1，
保留已经起来的服务，便于就地排查，并在提示里给出收尾命令。

### scripts/stop.sh（新增，取代 scripts/tmux_stop.sh）

```
scripts/stop.sh [asr|tts|llm|web ...]
```

默认停止全部。三步依次执行，每一步对不存在的对象都是空操作：停止处于
active 的 systemd 单元、关闭对应的 tmux window（若该 session 存在）、按
端口回收进程（复用 `scripts/free_port.sh`）。单元放在第一位，因为先杀掉
端口上的进程只会触发单元的 `Restart=on-failure`，并与重启争夺同一个端
口。停止不关心进程是谁拉起的，因此这一条命令覆盖全部三种运行方式。
`scripts/tmux_stop.sh` 删除，`scripts/systemd/services.sh` 里对它的调用改
为 `scripts/stop.sh`。

### scripts/tmux_start.sh（改造）

改为 source 上述库，只保留 tmux 特有的部分：创建 session 与 window、用
`-e` 传递 `TALKNEST_` 等前缀的环境变量（tmux window 继承的是 tmux
server 的环境，不是调用者的）。对外行为保持不变。tmux 由此退回「想分窗
口观察时用」的可选项。

## 关键决策

**前台模式不是真正的前台。** 四个进程无法共用一个终端的前台，所以默认
模式的实现是「后台运行 + 日志跟随 + Ctrl-C 停止」。只启动单个服务时才有
真正的单进程前台，那个场景今天直接执行对应的 `start_*.sh` 即可。

**不开启作业控制（不用 `set -m`）。** 作业控制关闭时，bash 让异步命令忽略
SIGINT，于是 Ctrl-C 只到达脚本本身与前台的日志跟随，服务进程收不到；停止
由 trap 按端口回收，行为可预测。反过来，`set -m` 会把终端交给前台命令，
Ctrl-C 只到达那个命令，脚本的 trap 反而永远不执行：这一点在 bash 5.2 上实
测过，带 INT trap 的脚本在 `set -m` 下按 Ctrl-C 直接退出，trap 没有运行。

**不引入 PID 文件。** 停止已经按端口回收，与启动方式无关，PID 只在启动
后的存活检查和守护模式的打印信息里用到。写文件会多出一份需要清理的状
态，僵尸 PID 文件还会误导后续操作。

**用 `nohup` 而不是 `setsid`。** macOS 没有 `setsid`；`nohup` 加输出重定向
加 `/dev/null` 输入在两个系统上都能让进程在终端关闭后继续运行。

## 需要改动的文件

| 文件 | 改动 |
|---|---|
| `scripts/lib/services.sh` | 新增，共享的服务表与就绪等待 |
| `scripts/start.sh` | 新增，默认前台跟随，`-d` 守护 |
| `scripts/stop.sh` | 新增，通用的停止入口 |
| `scripts/tmux_start.sh` | 改为 source 库，只留 tmux 部分 |
| `scripts/tmux_stop.sh` | 删除 |
| `scripts/systemd/services.sh` | 对 `tmux_stop.sh` 的调用改为 `stop.sh` |
| `README.md` | 「运行与停止」一节以 `start.sh` / `stop.sh` 为主 |
| `docs/services.md` | 监督方式、停止与日志三节按新定位重写 |

## 验收

```bash
# 未安装 tmux 的机器上（可用 PATH 剔除 tmux 模拟）
scripts/start.sh asr tts          # 顺序拉起，日志跟随，Ctrl-C 停全部
scripts/start.sh -d               # 打印 PID 与 URL 后退出
curl -fsS http://127.0.0.1:8891/health    # 关闭终端后仍然应答
scripts/stop.sh                   # 四个端口都释放
scripts/tmux_start.sh             # 行为与改造前一致
scripts/tmux_start.sh llm         # 单独替换一个 window 仍可用
shellcheck scripts/*.sh scripts/lib/*.sh
```

`scripts/start.sh` 与 `scripts/tmux_start.sh` 都需要保持 bash 3.2 语法，
shellcheck 无告警。
