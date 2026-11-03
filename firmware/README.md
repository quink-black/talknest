# TalkNest ESP32-S3 语音终端

这个 ESP-IDF 工程适配 Waveshare ESP32-S3-Touch-AMOLED-2.06。设备连接 TalkNest 后会自动聆听，并由服务端的 VAD 判定每句话何时结束。播放回答时，轻触屏幕可以打断回答；PWR 短按进入浅睡眠，再按一次可以唤醒设备。长按 PWR 仍由 AXP2101 执行硬关机。

## 配置

首次构建前，将 `config/talknest.example.json` 复制为 `config/talknest.json`，然后填写本机配置。将服务端使用的 CA 或自签证书保存为 `config/ca.pem`。这两个文件都已被 Git 忽略，构建系统会把它们嵌入固件。

| 字段 | 含义 |
| --- | --- |
| `wifi_ssid`、`wifi_password` | 设备使用的 Wi-Fi 凭据 |
| `server_url` | 以 `wss://` 开头的完整 `/ws` 地址 |
| `mode`、`learner` | 连接后发送给 TalkNest 的模式和学习者 |
| `full_duplex` | 是否在播放期间继续上行音频；只有 AEC 通过真机验收后才能设为 `true` |
| `speaker_volume`、`mic_gain_db` | 播放音量和麦克风硬件增益 |
| `capture_gain` | 进入音频管线前的数字增益；若讲话无法触发 VAD，可逐步调高 |
| `aec_delay_ms` | 回声参考的校准延迟 |
| `playback_prebuffer_ms` | 开始播放前至少积累的音频时长，默认 500 ms；越大越抗网络抖动，越小首句延迟越低 |

固件会校验证书链和服务器主机名，因此证书中的 SAN 必须与 `server_url` 的主机名一致。设备还需要通过 SNTP 同步时间，才能正确检查证书有效期。修改配置后需要重新构建并烧录固件。

### TF 卡覆盖

设备支持在启动时从 FAT32 TF 卡读取 `/sdcard/talknest.json`，覆盖嵌入配置中出现的字段，其他字段保留嵌入值。文件可以只写需要修改的字段，例如：

```json
{
  "speaker_volume": 80
}
```

修改后重启设备生效；TF 卡不存在、文件缺失或 JSON 非法时，设备继续使用嵌入配置。TF 卡中的 Wi-Fi 密码是明文，设备转手或卡片丢失前应清除配置。

## 构建

安装并激活 ESP-IDF v6.1 后，在本目录执行：

```bash
idf.py set-target esp32s3
idf.py build
```

构建系统会从 ESP 组件仓库下载锁定版本的依赖。`partitions.csv` 包含应用分区和 ESP-SR 的 `model` 分区，因此首次烧录需要写完整的分区表和模型数据。

## 测试

主机测试使用系统 C++ 编译器检查 TDM 槽位混音和单声道到立体声的转换：

```bash
tests/run_host_tests.sh
```

## 烧录与日志

以下命令会覆盖设备上的现有固件。执行前应确认已备份需要保留的内容。

```bash
idf.py -p PORT flash
idf.py -p PORT monitor
```

将 `PORT` 替换为设备的串口路径。监视器需要交互式终端；如果只需要在脚本中采集日志，可以使用 pyserial 以设备实际配置的波特率读取串口。

## 交互与降级

`full_duplex=false` 时，固件在回答播放期间关闭上行门控，所以语音打断不可用，但触摸打断仍然有效。开启全双工前，应分别验证助手语音不会触发自我打断、真人插话能够停止播放，以及插话后的下一句话能被完整识别。
