"""PCM-Hilfen: Discord liefert und erwartet 48 kHz, Stereo, 16 Bit."""

from __future__ import annotations

import io
import threading
import wave

import numpy as np

DISCORD_RATE = 48000
DISCORD_CHANNELS = 2
FRAME_BYTES = 3840  # 20 ms bei 48 kHz Stereo s16le
STT_RATE = 16000
_SILENCE = b"\x00" * FRAME_BYTES


def discord_to_mono16k(pcm: bytes) -> np.ndarray:
    """48 kHz Stereo -> 16 kHz Mono (int16) für die Spracherkennung."""
    usable = len(pcm) - len(pcm) % 4
    stereo = np.frombuffer(pcm[:usable], dtype="<i2").reshape(-1, 2).astype(np.int32)
    mono = stereo.sum(axis=1) // 2
    usable = len(mono) - len(mono) % 3
    return (mono[:usable].reshape(-1, 3).sum(axis=1) // 3).astype(np.int16)


def rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))


def to_wav_bytes(samples: np.ndarray, rate: int = STT_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.astype("<i2").tobytes())
    return buffer.getvalue()


class StreamResampler:
    """Wandelt einen PCM-Strom (s16le) stückweise nach 48 kHz Stereo.

    Lineare Interpolation mit Übertrag zwischen den Stücken, damit an den
    Nahtstellen kein Knacken entsteht.
    """

    def __init__(self, rate: int, channels: int = 1) -> None:
        if rate <= 0 or channels not in (1, 2):
            raise ValueError("Ungültiges Audioformat.")
        self.rate = rate
        self.channels = channels
        self._step = rate / DISCORD_RATE
        self._rest = b""
        self._prev: float | None = None
        self._phase = 0.0

    def feed(self, chunk: bytes) -> bytes:
        data = self._rest + chunk
        width = 2 * self.channels
        usable = len(data) - len(data) % width
        self._rest = data[usable:]
        if not usable:
            return b""
        samples = np.frombuffer(data[:usable], dtype="<i2").astype(np.float64)
        if self.channels == 2:
            samples = samples.reshape(-1, 2).mean(axis=1)
        if self._prev is not None:
            samples = np.concatenate(([self._prev], samples))
        last = len(samples) - 1
        if self._phase > last:
            self._phase -= last
            self._prev = float(samples[-1])
            return b""
        count = int((last - self._phase) / self._step) + 1
        positions = self._phase + self._step * np.arange(count)
        out = np.interp(positions, np.arange(len(samples)), samples)
        self._phase = float(positions[-1] + self._step - last)
        self._prev = float(samples[-1])
        mono = np.clip(np.rint(out), -32768, 32767).astype("<i2")
        return np.repeat(mono, 2).tobytes()


class PCMBuffer:
    """Thread-sicherer Puffer zwischen TTS (asyncio) und Discords Audio-Thread."""

    def __init__(self) -> None:
        self._data = bytearray()
        self._lock = threading.Lock()
        self._finished = False
        self.played_frames = 0

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            if not self._finished:
                self._data.extend(pcm)

    def finish(self) -> None:
        with self._lock:
            self._finished = True

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    @property
    def played_seconds(self) -> float:
        return self.played_frames * 0.02

    def read_frame(self) -> bytes:
        """20 ms Audio. Leer = Wiedergabe beenden; Stille, solange noch Audio kommt."""
        with self._lock:
            if len(self._data) >= FRAME_BYTES:
                frame = bytes(self._data[:FRAME_BYTES])
                del self._data[:FRAME_BYTES]
            elif self._finished:
                if not self._data:
                    return b""
                frame = bytes(self._data) + b"\x00" * (FRAME_BYTES - len(self._data))
                self._data.clear()
            else:
                return _SILENCE
            self.played_frames += 1
            return frame
