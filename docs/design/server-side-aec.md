# 服务端 AEC 与双通道上行设计

状态：ESP32 终端目前不使用本方案。真机上助手误听自己、误识别用户发音，终端改为单工加触摸打断（见 `docs/design/esp32-s3-voice-client.md`）。服务端 AEC 与 `channels: 2` 的 `start` 处理仍保留，供 CLI 客户端和以后的评估使用；下文描述的是该方案的原始设计。

## 背景与动机

ESP32-S3 终端的全双工语音打断此前的实现路径是设备端 ESP-SR AFE
（24 kHz 采集 → 2:3 抽取到 16 kHz → AEC → 3:2 插值回 24 kHz）。
真机测量表明该路径不可行：

- AFE 单任务产出 121344 样本 / 5 秒，而 24 kHz 单声道实时需要
  120000 样本 / 5 秒——单个核接近满载，与播放任务（同核更高优先级）
  互相挤占，表现为"输出断续"与"无法打断"之间反复摆动。
- TalkNest 协议上行固定 24 kHz，而 ESP-SR AFE 工作在 16 kHz，双向
  重采样是设备上的纯开销；这部分计算在服务端是免费的。
- 调试中 AFE 输出全幅噪声（输入正常），输入格式问题未定位。

设备硬件（ES7210 ADC、ES8311 DAC）不含回声消除，但 ES8311 播放
回采已进入 TDM 采集流的 slot 1，与麦克风信号同源同钟，提供样本级
对齐的参考信号。本设计把回声消除的计算移到服务端，设备只上传
麦克风加参考两路信号。

## 方案总览

- 设备端删除 AFE、双向重采样、feed/fetch 任务和参考延迟缓冲，
  上行改为双通道交错 PCM：slot 0（MIC1）与 slot 1（ES8311 回采）。
- 服务端为每个声明双通道的会话运行两级回声消除（复用 CLI 客户端
  `client/talknest_cli/aec.py` 的实现，模型沿用仓库根
  `dtln-aec/dtln_aec_128_{1,2}.tflite`），消除回声后把干净的
  单声道交给现有 VAD 与打断判定（`barge_in_due`）。
- 第一级是线性维纳路径估计（`server/orchestrator/wiener_aec.py`）：
  终端的扬声器-麦克风路径是固定几何，但高音量下明显非线性，线性
  滤波最多消约 13 dB；这一级每秒在滑动窗口上解 Wiener-Hopf 方程
  （FFT 相关 + 共轭梯度，带对角加载与验证门限），估计跨停顿保持，
  因此每个远端突发一开始就能消掉线性分量。第二级 DTLN 处理残余与
  非线性分量。只用 DTLN 时，每次播放开始后的 0.1–0.7 秒内其掩码
  重新收敛，残余足以让 VAD 以 0.6+ 概率持续触发并造成自打断；
  级联后在纯回声录音上不再出现可触发 VAD 的残余。
- 打断时服务端发送已有的 `stop_playback`，设备停止播放；设备端
  播放期间不再门控上行。

两路信号来自同一条 I2S DMA 流，对齐按样本数成立；播放静音期间
回采自然为静音，满足 DTLN 参考流必须连续的要求（见 aec.py 模块
注释），因此设备端无需任何参考流簿记。

## 协议扩展

`start` 消息增加可选字段 `channels`：

```json
{"type": "start", "channels": 2}
```

- 省略或为 1：上行二进制帧仍是 24 kHz little-endian int16 单声道，
  web 与 CLI 客户端行为不变。
- 为 2：上行二进制帧为 24 kHz little-endian int16 双通道交错，
  帧内顺序为 [MIC1, 回采参考]。帧边界不要求对齐声道对，服务端
  按会话维护流式缓冲。

下行不变。`stop_playback` / `playback ended` 语义不变。

## 设备端改动

`audio_pipeline`：

- 删除 `afe_processor` 组件、`afe_loop`/`afe_task`、24k↔16k 转换
  数组、`reference_delay` 环形缓冲及其全部调用点。
- `full_duplex=true` 时：采集循环直接从 TDM 帧提取 slot 0 与
  slot 1 交错写入上行缓冲；`full_duplex=false` 时维持现状
  （提取 slot 0 单声道，播放期间门控上行）。
- 采集、播放任务恢复原始优先级布局（AEC 任务消失后不再需要
  为其腾挪）。

`talknest_client`：`full_duplex=true` 时 `start` 消息携带
`"channels": 2`。

`firmware/README.md`：更新 `full_duplex` 的语义说明
（由"设备端 AEC"改为"双通道上行 + 服务端 AEC"）。

## 服务端改动

- `start` 处理：读取 `channels`，为 2 且配置启用 AEC 时创建会话级
  AEC 处理器；未启用或模型缺失时拒绝该 `start`（发送错误状态并
  关闭连接），避免回声直通 VAD 造成自打断。
- 处理线程：每个双通道会话一个独立线程（DTLN 推理不进 asyncio
  事件循环）。WS 接收循环把原始帧放入该线程的输入队列；线程完成
  拆声道、24k→16k 重采样、DTLN、16k→24k 重采样后，把干净单声道
  写入会话现有的 `sess.q`，VAD 及后续链路无感。
- DTLN 移植：将 `client/talknest_cli/aec.py` 的 `DtlnAec` 提为
  服务端与 CLI 共用的模块（放 `server/orchestrator/` 下，CLI 改为
  引用），避免两份拷贝。重采样复用 CLI `audio.py` 已有实现。
- 配置：`config.yaml` 增加 `aec` 段（`enabled`、`model_prefix`），
  默认指向仓库根 `dtln-aec/dtln_aec_128`。
- 依赖：服务端 venv 增加 `ai-edge-litert`（CLI 已验证同一模型
  与推理路径）。

### 资源与延迟

- 上行带宽约 768 kbps（24 kHz × 16 bit × 2 ch），2.4 GHz 局域网
  可承受；TLS 加密走硬件加速。
- DTLN 每会话每 8 ms 两次 TFLite 推理，参照 CLI 基准
  （`docs/bench/cli-aec.md`）单会话 CPU 占用远低于一核，服务端
  同时服务的设备数量在本项目范围内不成问题。
- 打断判定增加一个网络往返（估计 100–300 ms），远小于
  `BARGE_IN_MS` 的持续时长要求。

## 验收

1. 单元测试：用含回声的录音（麦克风 + 参考双通道）回放，断言
   ERLE 显著为正且语音段不被抹除。
2. 真机三项场景：纯助手语音播放不自打断；真人插话在既有
   `BARGE_IN_MS` 时限内可靠触发 `stop_playback` 并停止输出；
   打断后第二句完整识别。
3. 单声道上行的 web/CLI 客户端回归：行为与改动前一致。
4. 服务端关闭 AEC（`enabled: false`）时，双通道 `start` 被明确
   拒绝且日志说明原因。

## 同步修订

`docs/design/esp32-s3-voice-client.md` 的"音频与打断策略"一节
改指向本设计；其非目标中"不修改 TalkNest 协议"修订为"下行协议
不变，上行允许通过 `start.channels` 声明双通道扩展"。
`docs/architecture.md` 的 DTLN 条目更新为服务端与 CLI 共用。
