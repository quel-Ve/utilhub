"""成功提示音 — 合成 zorder 同款"叮咚" (587Hz→440Hz, 48kHz, 20ms 淡入淡出)。

与 hotkey_sort.py 同源; winsound 禁止 SND_MEMORY+SND_ASYNC, 故在后台线程同步播放。
"""
import math
import struct
import threading
import winsound


def _build_chime_wav() -> bytes:
    RATE = 48000
    AMP = 0.25
    FADE = RATE * 20 // 1000

    def tone(freq: float, ms: int) -> bytes:
        n = RATE * ms // 1000
        out = bytearray()
        for i in range(n):
            env = 1.0
            if i < FADE:
                env = i / FADE
            remain = n - i
            if remain < FADE:
                env = remain / FADE
            s = (math.sin(2 * math.pi * freq * i / RATE) if freq > 0 else 0.0) * AMP * env
            out += struct.pack("<h", int(s * 32767))
        return bytes(out)

    data = tone(587.0, 70) + tone(0.0, 20) + tone(440.0, 110)
    header = (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16)
        + b"data" + struct.pack("<I", len(data))
    )
    return header + data


_CHIME = _build_chime_wav()


def play_success():
    """异步播放 (后台线程同步阻塞 ~200ms, 不卡热键钩子)。"""
    def _play():
        winsound.PlaySound(_CHIME, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
    threading.Thread(target=_play, daemon=True).start()
