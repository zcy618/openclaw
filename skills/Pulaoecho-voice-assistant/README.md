# Pulaoecho 远程音频设备语音助手

## 概述

基于 WebSocket 的远程音频设备语音助手服务。远程设备（如 Pulaoecho 硬件）通过 WebSocket 持续发送 PCM 音频流，服务器使用 VAD 实时检测语音片段，触发唤醒词识别和命令识别，查询 OpenClaw，并将 TTS 音频回传给设备。

## 架构

```
远程音频设备
    │  PCM音频流 (80ms/chunk, base64)
    │  控制信号 (wakeup/process/feedback/sleep)
    ▼
WebSocketAudioServer          ← asyncio 事件循环，纯转发
    │
AudioBridge                   ← 线程安全缓冲层
    ├─ AudioReceiveQueue       ← 设备音频 → VA
    └─ AudioSendQueue          ← TTS音频 → 设备
    │
VoiceAssistantRemote
    ├─ recording_thread        ← VAD切割 + 异步ASR，永不阻塞
    │    └─ SpeechRecognizer   ← 每句独立线程识别，callback回传
    ├─ execution_thread        ← 查询 OpenClaw gateway
    └─ tts_thread              ← gTTS生成PCM，等队列清空发sleep信号
```

**关键设计原则**：录音线程永不阻塞——VAD 切割出语音段后立即提交给 `SpeechRecognizer`，后者在独立线程中调用 Google ASR，结果通过 `_asr_result_q` 异步回传录音循环。用户随时可再次唤醒打断正在进行的识别/TTS。

## WebSocket通信协议

### 服务端点

```
ws://{server_ip}:18181/v1/audio/stream
```

默认端口：`18181`

### 消息格式

#### 1. 音频数据上行（设备 → 服务器）

**单声道模式**：

```json
{
  "proto": 1,
  "seq": 123456,
  "type": "audio",
  "format": "pcm",
  "sampleRate": 16000,
  "channels": 1,
  "bits": 16,
  "txdata": "base64编码的PCM音频数据",
  "ts": 1739876543210
}
```

**双声道模式**：

```json
{
  "proto": 1,
  "seq": 123456,
  "type": "audio",
  "format": "pcm",
  "sampleRate": 16000,
  "channels": 2,
  "bits": 16,
  "txdata": "base64编码的PCM音频数据(TX)",
  "rxdata": "base64编码的PCM音频数据(RX)",
  "ts": 1739876543210
}
```

**字段说明**：

| 字段名     | 类型   | 固定值 | 说明                     |
| ---------- | ------ | ------ | ------------------------ |
| proto      | int    | 1      | 协议版本，永久固定       |
| seq        | long   | 自增   | 消息序号，从0开始递增    |
| type       | string | audio  | 固定为音频流             |
| format     | string | pcm    | 音频格式固定             |
| sampleRate | int    | 16000  | 采样率                   |
| channels   | int    | 1/2    | 声道数                   |
| bits       | int    | 16     | 位深                     |
| txdata     | string | BASE64 | 80ms PCM音频Base64编码   |
| rxdata     | string | BASE64 | (可选)接收音频Base64编码 |
| ts         | long   | 时间戳 | 毫秒级Unix时间戳         |

#### 2. 音频数据下行（服务器 → 设备）

```json
{
  "proto": 1,
  "type": "audio",
  "format": "pcm",
  "sampleRate": 16000,
  "channels": 1,
  "bits": 16,
  "data": "base64编码的PCM音频",
  "ts": 1739876543999
}
```

#### 3. 心跳包

**上行（设备 → 服务器）**：

```json
{
  "proto": 1,
  "type": "ping",
  "ts": 1739876543210
}
```

**下行（服务器 → 设备）**：

```json
{
  "proto": 1,
  "type": "pong",
  "ts": 1739876543567
}
```

**心跳规则**：

- 设备每30秒发送一次`ping`
- 超时180秒无`pong`则重连

#### 4. 状态通知消息

**唤醒应答**（检测到唤醒词后立即返回）：

```json
{
  "proto": 1,
  "type": "wakeup",
  "ts": 1739876543567
}
```

**查询结果应答**（返回OpenClaw结果时发送）：

```json
{
  "proto": 1,
  "type": "feedback",
  "ts": 1739876543567
}
```

**休眠通知**（唤醒后超时无命令，进入idle状态）：

```json
{
  "proto": 1,
  "type": "sleep",
  "ts": 1739876543567
}
```

## 工作流程

```
设备连接 → 持续发送音频 (80ms/chunk)
    ↓
VAD 实时切割语音片段
    ↓
SpeechRecognizer 异步识别（独立线程，不阻塞录音）
    ↓
检测到唤醒词 → 发送 wakeup，进入 awakened 状态
    ↓
识别问题 → 发送 process，查询 OpenClaw
    ↓
收到响应 → 发送 feedback，gTTS 生成 PCM 回传设备
    ↓
发送队列清空 → 发送 sleep，回到 waiting 状态
```

任何时刻检测到唤醒词，都会立即中断当前识别/TTS，重新进入 awakened 状态。

## 安装

```bash
cd skills/Pulaoecho-voice-assistant
pip3 install -r requirements.txt
```

`pydub` 需要 `ffmpeg`：

```bash
brew install ffmpeg   # macOS
```

## Usage

### 1. Generate Self-Signed Certificate

First, generate a self-signed certificate for your local machine:

```bash
cd skills/Pulaoecho-voice-assistant
chmod +x scripts/gen_cert.sh
./scripts/gen_cert.sh
```

This will create the certificate files in the `certs/` directory.

### 2. Configure Password

Edit the `config.json` file and set your password:

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 18181,
    "tls": {
      "enabled": true,
      "certFile": "./certs/server.crt",
      "keyFile": "./certs/server.key"
    },
    "password": "your_password_here"
  },
  ...
}
```

### 3. Configure PulaoEcho App

1. Open the PulaoEcho app on your device
2. Enable the OpenClaw switch
3. Fill in the following information:
   - **URL**: `wss://your_server_ip:18181/v1/audio/stream`
   - **Certificate**: Copy the content of `certs/server.crt`
   - **Password**: The password you set in `config.json`
4. Click "Save"

### 4. Verify Connection

After successful connection:

- The volume up and volume down buttons on your device will flash 5 times
- You can see the device connection information in your OpenClaw backend

## 启动

```bash
cd scripts
python3 main.py
```

日志输出到 `~/.openclaw/logs/pulaoecho-voice-assistant.log`，同时打印到 stdout。

```bash
tail -f ~/.openclaw/logs/pulaoecho-voice-assistant.log
```

## 配置

在 `scripts/voice_assistant_remote.py` 的 `VoiceAssistantRemote.__init__` 中修改：

| 参数     | 默认值                 | 说明                    |
| -------- | ---------------------- | ----------------------- |
| `ws_url` | `ws://127.0.0.1:18789` | OpenClaw gateway 地址   |
| `token`  | hardcoded              | OpenClaw operator token |

VAD 参数在 `recording_thread` 中：

| 参数                      | 默认值    | 说明                           |
| ------------------------- | --------- | ------------------------------ |
| `SILENCE_FRAMES_WAITING`  | 8 (160ms) | waiting 状态静音触发阈值       |
| `SILENCE_FRAMES_AWAKENED` | 6 (120ms) | awakened 状态静音触发阈值      |
| `MIN_SPEECH_FRAMES`       | 8 (160ms) | 最短有效语音（低于此视为噪声） |
| `QUEUE_TIMEOUT`           | 1.5s      | 设备无音频超时后强制冲刷       |

唤醒词在 `_on_asr_result` 中：`["hi claw", "hey claw", "hi google", "hey google"]`

## 测试

分步测试（唤醒词 + 问题分两个文件）：

```bash
cd test
python3 step_by_step_test.py
```

单文件连续测试（唤醒词 + 问题在一个 WAV 中）：

```bash
python3 single_file_test.py test1.wav
```

TTS 响应音频保存在 `test/output/`。

## 故障排除

**语音识别失败**：需要网络访问 Google Speech API，检查 PCM 格式（16kHz, 16-bit, mono）。

**OpenClaw 查询失败**：

```bash
openclaw gateway status
openclaw channels status --probe
```

**端口冲突**：

```bash
lsof -i :18181
```

## 许可

与 OpenClaw 主项目保持一致
