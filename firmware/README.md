# TalkNest ESP32-S3 语音终端

这个 ESP-IDF 工程适配 Waveshare ESP32-S3-Touch-AMOLED-2.06。设备连接 TalkNest 后会自动聆听，并由服务端的 VAD 判定每句话何时结束。屏幕的常驻主角是一个互动形象，随对话状态与播放音频连续变化；播放回答时轻触屏幕可以打断回答，PWR 短按进入浅睡眠，再按一次可以唤醒设备。长按 PWR 仍由 AXP2101 执行硬关机。

### 形象

屏幕以保留颈部和上半身的互动形象为常驻主体，并通过深蓝渐变背景、水下舞台和少量环境光点构成完整的海底场景。形象随对话状态与播放音频连续变化：四帧表情（睁/闭眼 × 闭/张嘴）、呼吸起伏、侧头、说话弹跳与眨眼。顶部状态胶囊显示当前对话状态，底部显示与状态对应的行动提示；说话短柱由真实播放音量驱动，聆听与思考使用三点活动指示。连接与配置细节不上屏，中文回复正文仍只写入串口日志。

表情帧由 `scripts/gen_avatar_frames.py` 从 `web/whale_front.webp` 离线生成，输出 `components/ui_avatar/assets/avatar_frames.bin`（RGB565A8，含 alpha 通道），构建时通过 `EMBED_FILES` 编入固件。修改形象后，用装有 Pillow、numpy 与 scipy 的 Python 环境重新生成并构建：

```bash
python scripts/gen_avatar_frames.py --preview /tmp/avatar.png
python scripts/gen_avatar_frames.py --layout-preview /tmp/screen.png
```

第二条命令生成 410×502 的整屏近似预览，用于检查背景层次、头像轮廓、顶部状态和底部提示的布局。

## 配置

首次构建前，将 `config/talknest.example.json` 复制为 `config/talknest.json`，然后填写本机配置。将服务端使用的 CA 或自签证书保存为 `config/ca.pem`。这些文件都已被 Git 忽略，构建系统会把它们嵌入固件。

`config/` 下所有匹配 `talknest*.json` 的文件（示例文件除外）都是一份独立的环境配置，构建时全部嵌入：`talknest.json` 排在最前作为默认回退，其余按文件名排序，上限 8 份，超过上限会在构建期报错。文件名去掉 `talknest.` 前缀后是环境标识，`talknest.json` 的标识为 `default`，例如 `talknest.office.json` 对应 `office`。每份配置都是完整配置，字段与下表一致。增删配置文件后需执行一次 `idf.py reconfigure` 再构建，因为 IDF 在脚本模式下无法用 `CONFIGURE_DEPENDS` 自动检测新增文件。

| 字段 | 含义 |
| --- | --- |
| `wifi_ssid`、`wifi_password` | 设备使用的 Wi-Fi 凭据 |
| `server_url` | 以 `wss://` 开头的完整 `/ws` 地址 |
| `mode`、`learner` | 连接后发送给 TalkNest 的模式和学习者 |
| `speaker_volume`、`mic_gain_db` | 播放音量和麦克风硬件增益 |
| `capture_gain` | 进入音频管线前的数字增益；若讲话无法触发 VAD，可逐步调高 |
| `playback_prebuffer_ms` | 开始播放前至少积累的音频时长，默认 500 ms；越大越抗网络抖动，越小首句延迟越低 |

固件会校验证书链和服务器主机名，因此证书中的 SAN 必须与 `server_url` 的主机名一致。设备还需要通过 SNTP 同步时间，才能正确检查证书有效期。修改嵌入配置后需要重新构建并烧录固件。

### 多环境自动选择

开机时设备启动 Wi-Fi 并扫描可见网络，把扫描到的 SSID 与各配置的
`wifi_ssid` 精确比较，选中信号最强的那一份；命中多份且 RSSI 相同时按
文件名顺序。扫描失败或没有命中的网络时回退到默认配置并留下日志；启动日志会列出候选环境的次序，便于确认选择结果。
因此同一份固件可以在不同场所自动使用对应的 Wi-Fi 与服务器地址，无需
重新烧录。唤醒后沿用开机时选定的配置，不重新扫描。

隐藏 SSID 的网络不会出现在扫描结果里，可以在 TF 卡配置中写 `profile`
字段强制指定环境标识，例如：

```json
{
  "profile": "office"
}
```

强制指定存在时设备跳过扫描；标识不存在时记录警告并继续扫描。

### mDNS 服务器地址

`server_url` 可以使用以 `.local` 结尾的主机名，例如
`wss://<主机名>.local:8890/ws`。固件通过 lwip 的组播 mDNS 查询解析这类
主机名，服务器 IP 变化后不需要改配置。需要满足三个条件：设备与发布该
名字的 mDNS 应答者处于同一广播域，应答者把名字广播到设备可达的 IPv4
地址，且服务器证书的 SAN 包含该 `.local` 名称。

mDNS 组播不会跨网段转发。例如设备连在热点网段、服务器接在有线网段时，
即使 IP 可以互相路由，设备也收不到另一个网段的组播应答，此时
`getaddrinfo` 会报无法解析主机名。要让这种拓扑使用 `.local`，需在网关上
启用 mDNS 反射，或让服务器接入设备所在的同一广播域；否则配置里仍应写
IP。生成开发证书时可用 `scripts/gen_dev_cert.sh` 的
`TALKNEST_CERT_DNS`、`TALKNEST_CERT_IPS` 环境变量补充名称与地址，这两个
变量应写在被 Git 忽略的 `local.env` 中。

### TF 卡覆盖

设备支持在启动时从 FAT32 TF 卡读取 `/sdcard/talknest.json`，覆盖选中配置中出现的字段，其他字段保留嵌入值。文件可以只写需要修改的字段，例如：

```json
{
  "speaker_volume": 80
}
```

修改后重启设备生效；TF 卡不存在、文件缺失或 JSON 非法时，设备继续使用嵌入配置。TF 卡中的 Wi-Fi 密码是明文，设备转手或卡片丢失前应清除配置。

### 隐私

所有真实配置文件与 CA 文件都被 Git 忽略；示例文件只含占位值。固件日志只打印环境标识、选择原因与音频数值，不打印 SSID、Wi-Fi 密码或配置全文。本机排障需要查看配置时使用：

```bash
scripts/show_device_config.sh
```

该脚本把 `wifi_password` 替换为 `<redacted>` 后再输出，其余字段仍会显示，请勿把输出粘贴到日志、提交或外部系统。配置以明文存在于设备 Flash 中，设备未启用 Flash 加密，若设备可能丢失应另行评估该风险。

## 构建

安装并激活 ESP-IDF v6.1 后，在本目录执行：

```bash
idf.py set-target esp32s3
idf.py build
```

构建系统会从 ESP 组件仓库下载锁定版本的依赖。`partitions.csv` 只包含应用分区，首次烧录会写入完整的分区表。

## 测试

主机测试使用系统 C++ 编译器检查 TDM 槽位混音、单声道到立体声的转换、环境配置的 SSID 精确匹配，以及互动形象的状态/包络/选帧映射：

```bash
tests/run_host_tests.sh
```

## 烧录与日志

在仓库任意目录执行下面这条命令，即可完成增量构建与烧录：

```bash
scripts/flash_firmware.sh
```

脚本会自动激活 ESP-IDF 环境，串口路径与 IDF 环境入口由 `local.env` 提供，详见 `local.env.example`。也可以把串口路径作为第一个参数传入。烧录会覆盖设备上的现有固件，脚本不会先备份 Flash。

监视器需要交互式终端：

```bash
idf.py -p PORT monitor
```

将 `PORT` 替换为设备的串口路径。如果只需要在脚本中采集日志，可以使用 pyserial 以设备实际配置的波特率读取串口。

## 交互方式

终端是单工的：回答播放期间以及播放结束后的约 600 ms 内，固件不向服务器发送麦克风音频，因此助手自己的声音不会被当作用户发言。回答期间若要打断，按住屏幕约 600 ms，详见 `docs/design/esp32-s3-voice-client.md`。
