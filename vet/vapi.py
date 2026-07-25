"""Live vendor calls through Vapi.

One Vapi assistant, N transcription vendors: `assistantOverrides.transcriber`
swaps the speech stack per call, so every vendor hears byte-identical audio and
the comparison is genuinely apples-to-apples. We open a websocket-transport call,
stream our synthesized PCM in, and read the vendor's `transcript` events back on
the same socket.

We never listen to the assistant's replies — we only care what the vendor heard.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
import websockets

from . import dsp

API = "https://api.vapi.ai"
UA = "vet-eval/1.0"          # Vapi's WAF 403s the default Python urllib agent

# Transcribers verified to run on Vapi-provided credentials.
VENDOR_TRANSCRIBERS = {
    "deepgram": {"provider": "deepgram", "model": "nova-3", "language": "multi"},
    "assembly": {"provider": "assembly-ai", "language": "multi"},
    "openai":   {"provider": "openai", "model": "gpt-4o-transcribe"},
    "gladia":   {"provider": "gladia"},
}

ASSISTANT = {
    "name": "vet-probe-target",
    "model": {"provider": "openai", "model": "gpt-4o-mini",
              "messages": [{"role": "system",
                            "content": "Reply with exactly one period. Never say anything else."}],
              "maxTokens": 50},
    "voice": {"provider": "vapi", "voiceId": "Elliot"},
    "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "multi"},
    "firstMessage": "",
    "silenceTimeoutSeconds": 10,
    "maxDurationSeconds": 90,
}

STATE = os.path.join(os.path.dirname(__file__), "cache", "vapi_assistant.json")


def load_key() -> str | None:
    env = os.environ.get("VAPI_PRIVATE_KEY")
    if env:
        return env
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env.vapi")
    if not os.path.exists(path):
        return None
    for line in open(path):
        if line.lower().startswith("private"):
            return line.split(":", 1)[1].strip()
    return None


class VapiClient:
    def __init__(self, key: str, speed: float = 1.0):
        self.key = key
        self.speed = speed            # audio push rate vs realtime
        self.assistant_id: str | None = None
        self.h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                  "User-Agent": UA}

    async def ensure_assistant(self) -> str:
        if self.assistant_id:
            return self.assistant_id
        if os.path.exists(STATE):
            saved = json.load(open(STATE))
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(f"{API}/assistant/{saved['id']}", headers=self.h)
                if r.status_code == 200:
                    self.assistant_id = saved["id"]
                    return self.assistant_id
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{API}/assistant", headers=self.h, json=ASSISTANT)
            r.raise_for_status()
            self.assistant_id = r.json()["id"]
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump({"id": self.assistant_id}, open(STATE, "w"))
        return self.assistant_id

    async def transcribe(self, vendor_id: str, wav_path: str, attempts: int = 3) -> dict:
        """Stream one probe to one vendor, retrying transient socket failures.

        A websocketCallUrl whose first handshake fails is dead for good, so a
        retry has to mint a whole new call rather than redial the same URL.
        """
        last = None
        for i in range(attempts):
            try:
                return await self._transcribe_once(vendor_id, wav_path)
            except Exception as exc:
                last = exc
                await asyncio.sleep(1.2 * (i + 1))
        raise last

    async def _transcribe_once(self, vendor_id: str, wav_path: str) -> dict:
        aid = await self.ensure_assistant()
        body = {
            "assistantId": aid,
            "transport": {"provider": "vapi.websocket",
                          "audioFormat": {"format": "pcm_s16le", "container": "raw",
                                          "sampleRate": dsp.SR}},
            "assistantOverrides": {
                "transcriber": VENDOR_TRANSCRIBERS[vendor_id],
                "clientMessages": ["transcript", "speech-update", "status-update"],
                # Long enough that a quiet probe can't be mistaken for a dead line.
                "silenceTimeoutSeconds": 60,
                "maxDurationSeconds": 120,
            },
        }
        t0 = time.time()
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(f"{API}/call", headers=self.h, json=body)
            r.raise_for_status()
            call = r.json()
        url = call["transport"]["websocketCallUrl"]

        audio = dsp.read_wav(wav_path)
        pcm = dsp.pcm16(audio)
        frame = int(dsp.SR * 0.1) * 2                     # 100 ms of 16-bit mono
        finals: list[str] = []
        first_partial = None

        # Connect immediately — a websocketCallUrl that isn't picked up goes dead.
        async with websockets.connect(url, max_size=None, open_timeout=20) as ws:
            async def pump():
                for i in range(0, len(pcm), frame):
                    await ws.send(pcm[i:i + frame])
                    await asyncio.sleep(0.1 / self.speed)
                # Generous trailing silence: the slower vendors only finalise a
                # segment once they've heard enough quiet to call it a turn, and
                # cutting the stream early loses their last (often only) result.
                for _ in range(40):
                    await ws.send(b"\x00" * frame)
                    await asyncio.sleep(0.1 / self.speed)

            async def listen():
                nonlocal first_partial
                async for msg in ws:
                    if isinstance(msg, bytes):
                        continue                           # assistant audio; not our concern
                    try:
                        m = json.loads(msg)
                    except Exception:
                        continue
                    if m.get("type") == "transcript" and m.get("role") == "user":
                        if first_partial is None:
                            first_partial = time.time()
                        if m.get("transcriptType") == "final":
                            finals.append(m.get("transcript", ""))

            task = asyncio.create_task(listen())
            try:
                await pump()
                await asyncio.wait_for(asyncio.shield(task), timeout=6.0)
            except Exception:
                pass
            task.cancel()
            try:
                await ws.send(json.dumps({"type": "hangup"}))
            except Exception:
                pass

        # The websocket transcript stream is not uniform across vendors — AssemblyAI,
        # for one, never emits live `transcript` events but does record a perfectly
        # good transcript. The call record is the authoritative, vendor-neutral read,
        # and it carries real cost and Vapi's own transcriber latency alongside.
        rec = await self._await_record(call["id"])
        hyp = rec.get("hyp") or " ".join(t.strip() for t in finals if t).strip()
        return {"hyp": hyp,
                "latency_ms": rec.get("latency_ms")
                or int(((first_partial or time.time()) - t0) * 1000),
                "cost_usd": rec.get("cost_usd", 0.0),
                "call_id": call["id"], "wall_s": round(time.time() - t0, 2),
                "source": rec.get("source", "ws")}

    async def _await_record(self, call_id: str, timeout: float = 25.0) -> dict:
        """Poll until the call is written up, then mine it for the real numbers."""
        deadline = time.time() + timeout
        async with httpx.AsyncClient(timeout=30) as c:
            while time.time() < deadline:
                await asyncio.sleep(1.5)
                r = await c.get(f"{API}/call/{call_id}", headers=self.h)
                if r.status_code != 200:
                    continue
                d = r.json()
                if d.get("status") != "ended" and not d.get("transcript"):
                    continue
                cb = d.get("costBreakdown") or {}
                perf = ((d.get("artifact") or {}).get("performanceMetrics") or {})
                return {
                    "hyp": user_lines(d.get("transcript") or ""),
                    "cost_usd": float(cb.get("stt") or 0.0) or float(d.get("cost") or 0.0),
                    "latency_ms": int(perf.get("transcriberLatencyAverage") or 0) or None,
                    "source": "record",
                }
        return {}


def user_lines(transcript: str) -> str:
    """Vapi records a two-sided transcript; only the caller side is under test."""
    out = []
    for line in transcript.splitlines():
        if line.startswith("User:"):
            out.append(line[len("User:"):].strip())
    return " ".join(out).strip()
