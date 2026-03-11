#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单文件连续音频测试
发送一个包含唤醒词+命令的完整WAV文件，等待完整响应流程
"""

import asyncio
import base64
import json
import os
import ssl
import sys
import time
import wave
import websockets
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_device_client import TestDeviceClient

SERVER_URL = "wss://localhost:18181/v1/audio/stream"
CHUNK_SIZE = 2560   # 80ms @ 16kHz 16-bit mono


def _make_ssl_context(url: str) -> ssl.SSLContext | None:
    """WSS 测试用：验证证书"""
    if not url.startswith("wss://"):
        return None
    # 创建SSL上下文并加载证书
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    # 加载自签名证书
    cert_path = os.path.join(os.path.dirname(__file__), "..", "certs", "server.crt")
    ctx.load_verify_locations(cert_path)
    # 检查主机名
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def aes_encrypt(plaintext: str) -> str:
    """AES加密"""
    # 使用固定长度的密钥（AES-256需要32字节）
    key_bytes = b'0123456789abcdef0123456789abcdef'  # 恰好32字节
    # 生成随机IV
    iv = os.urandom(16)
    # 创建加密器
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    # 对明文进行填充
    padder = padding.PKCS7(128).padder()
    padded_data = padder.update(plaintext.encode('utf-8')) + padder.finalize()
    # 加密
    ciphertext = encryptor.update(padded_data) + encryptor.finalize()
    # 将IV和密文一起编码为base64
    return base64.b64encode(iv + ciphertext).decode('utf-8')


class SingleFileTest:

    def __init__(self, wav_file: str):
        self.wav_file = wav_file
        self.client = TestDeviceClient(SERVER_URL, ssl_context=_make_ssl_context(SERVER_URL))
        self.signals: list[str] = []
        self.audio_chunks = 0
        self.done = asyncio.Event()

    async def _receive(self, ws):
        try:
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("type", "")
                if t == "audio":
                    pcm = base64.b64decode(msg.get("data", ""))
                    self.client.recv_pcm.extend(pcm)
                    self.audio_chunks += 1
                elif t == "pong":
                    pass
                else:
                    self.signals.append(t)
                    icon = {"wakeup": "🔔", "process": "⏳", "feedback": "✅", "sleep": "😴"}.get(t, "📨")
                    print(f"[RECV] {icon} {t}")
                    if t == "sleep":
                        self.done.set()
        except websockets.exceptions.ConnectionClosed:
            self.done.set()

    async def run(self):
        print("=" * 60)
        print(f"单文件连续音频测试: {os.path.basename(self.wav_file)}")
        print("=" * 60)

        pcm = self._load_pcm(self.wav_file)
        n_chunks = (len(pcm) + CHUNK_SIZE - 1) // CHUNK_SIZE
        print(f"PCM: {len(pcm)} bytes → {n_chunks} chunks (80ms each)\n")

        # 生成唯一的deviceid
        device_id = f"test_device_{int(time.time() * 1000)}"
        print(f"Using deviceid: {device_id}")

        # 服务端密码
        server_password = "pulaoecho_secure_password_2026"
        # 加密密码
        encrypted_password = aes_encrypt(server_password)
        
        async with websockets.connect(SERVER_URL, ssl=_make_ssl_context(SERVER_URL)) as ws:
            # 发送初始化消息，包含deviceid和加密的密码
            init_msg = {
                "deviceid": device_id,
                "password": encrypted_password,
                "ts": int(time.time() * 1000)
            }
            await ws.send(json.dumps(init_msg))
            print("✅ Connected")
            recv_task = asyncio.create_task(self._receive(ws))

            # 发送ping，包含deviceid
            ping_msg = self.client.create_ping_message()
            ping_msg["deviceid"] = device_id
            await ws.send(json.dumps(ping_msg))

            print(f"Sending audio at realtime rate...")
            seq = 0
            for i in range(0, len(pcm), CHUNK_SIZE):
                chunk = pcm[i:i + CHUNK_SIZE]
                if len(chunk) < CHUNK_SIZE:
                    chunk = chunk + b'\x00' * (CHUNK_SIZE - len(chunk))
                seq += 1
                msg = {
                    "proto": 1, "seq": seq, "type": "audio",
                    "format": "pcm", "sampleRate": 16000,
                    "channels": 1, "bits": 16,
                    "txdata": base64.b64encode(chunk).decode(),
                    "ts": int(time.time() * 1000),
                    "deviceid": device_id
                }
                await ws.send(json.dumps(msg))
                if seq % 20 == 0:
                    print(f"  Progress: {seq}/{n_chunks}")
                await asyncio.sleep(0.08)

            print(f"✓ Sent all {n_chunks} chunks")
            print("\n⏳ Waiting for response (sleep signal or 90s timeout)...")

            try:
                await asyncio.wait_for(self.done.wait(), timeout=90)
            except asyncio.TimeoutError:
                print("❌ Timeout")

            recv_task.cancel()
            try:
                await recv_task
            except (asyncio.CancelledError, Exception):
                pass

        # 保存TTS音频
        print()
        if self.client.recv_pcm:
            os.makedirs(os.path.join(os.path.dirname(self.wav_file), "output"), exist_ok=True)
            fname = f"test1_response_{int(time.time() * 1000)}.wav"
            self.client.save_pcm_as_wav(fname)
        else:
            print("⚠️  No TTS audio received")

        print("\n" + "=" * 60)
        print("测试总结")
        print("=" * 60)
        for s in ["wakeup", "process", "feedback", "sleep"]:
            print(f"{'✅' if s in self.signals else '❌'} {s}")
        print(f"📦 TTS chunks received: {self.audio_chunks}")
        ok = "wakeup" in self.signals and "feedback" in self.signals
        print()
        print("🎉 测试成功！" if ok else f"❌ 测试失败，未收到: {[s for s in ['wakeup','feedback'] if s not in self.signals]}")

    def _load_pcm(self, path: str) -> bytes:
        with wave.open(path, "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            fr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        print(f"WAV: {ch}ch {sw*8}bit {fr}Hz")
        if fr != 16000 or ch != 1 or sw != 2:
            print("Converting to 16kHz mono 16-bit...")
            from pydub import AudioSegment
            import io
            audio = AudioSegment.from_wav(path)
            audio = audio.set_frame_rate(16000).set_channels(1).set_sample_width(2)
            buf = io.BytesIO()
            audio.export(buf, format="raw")
            return buf.getvalue()
        return raw


if __name__ == "__main__":
    wav = sys.argv[1] if len(sys.argv) > 1 else "test1.wav"
    if not os.path.isabs(wav):
        wav = os.path.join(os.path.dirname(os.path.abspath(__file__)), wav)
    if not os.path.exists(wav):
        print(f"❌ Not found: {wav}")
        sys.exit(1)
    asyncio.run(SingleFileTest(wav).run())
