"""Erkennt je Sprecher, wann eine Äußerung beginnt und endet.

Discord liefert Audio pro Nutzer getrennt und sendet nur, solange jemand
spricht. Eine Äußerung endet deshalb entweder nach genug leisen Frames oder
wenn eine Weile gar keine Pakete mehr kommen (``tick``).
"""

from __future__ import annotations

from collections import deque

import numpy as np

from .audio import STT_RATE, rms

START = "start"
END = "end"

Event = tuple[str, "np.ndarray | None"]


class UtteranceDetector:
    def __init__(
        self,
        *,
        rate: int = STT_RATE,
        start_rms: float = 500.0,
        min_speech_ms: int = 240,
        end_silence_ms: int = 700,
        max_utterance_seconds: float = 25.0,
        min_utterance_ms: int = 350,
        pre_roll_ms: int = 200,
    ) -> None:
        self.rate = rate
        self.start_rms = float(start_rms)
        self.min_speech_ms = min_speech_ms
        self.end_silence = end_silence_ms / 1000.0
        self.max_seconds = float(max_utterance_seconds)
        self.min_utterance_ms = min_utterance_ms
        self.pre_roll_ms = pre_roll_ms
        self.speaking = False
        self._pre: deque[np.ndarray] = deque()
        self._pre_ms = 0.0
        self._frames: list[np.ndarray] = []
        self._voiced_ms = 0.0
        self._quiet_ms = 0.0
        self._last_voice = 0.0
        self._last_frame = 0.0
        self._noise = 0.0

    def _ms(self, frame: np.ndarray) -> float:
        return 1000.0 * len(frame) / self.rate

    def _threshold(self) -> float:
        return max(self.start_rms, self._noise * 3.0)

    def _finish(self) -> list[Event]:
        frames, self._frames = self._frames, []
        self.speaking = False
        self._voiced_ms = self._quiet_ms = 0.0
        self._pre.clear()
        self._pre_ms = 0.0
        if not frames:
            return []
        audio = np.concatenate(frames)
        if 1000.0 * len(audio) / self.rate < self.min_utterance_ms:
            return [(END, None)]
        return [(END, audio)]

    def feed(self, frame: np.ndarray, now: float) -> list[Event]:
        """Einen Audio-Frame (int16, mono) verarbeiten."""
        events: list[Event] = []
        if self.speaking and now - self._last_frame >= self.end_silence:
            events.extend(self._finish())  # lange Funkstille seit dem letzten Paket
        self._last_frame = now
        loud = rms(frame) >= self._threshold()
        length = self._ms(frame)

        if self.speaking:
            self._frames.append(frame)
            if loud:
                self._last_voice = now
            duration = sum(len(f) for f in self._frames) / self.rate
            if now - self._last_voice >= self.end_silence or duration >= self.max_seconds:
                events.extend(self._finish())
            return events

        if loud:
            self._voiced_ms += length
            self._quiet_ms = 0.0
            self._frames.append(frame)
            if self._voiced_ms >= self.min_speech_ms:
                self.speaking = True
                self._last_voice = now
                self._frames = list(self._pre) + self._frames
                self._pre.clear()
                self._pre_ms = 0.0
                events.append((START, None))
            return events

        # leiser Frame im Ruhezustand
        self._noise = 0.95 * self._noise + 0.05 * rms(frame) if self._noise else rms(frame)
        if self._frames:
            self._quiet_ms += length
            self._frames.append(frame)
            if self._quiet_ms > 200:  # nur ein Knacken, keine Sprache
                for kept in self._frames:
                    self._push_pre(kept)
                self._frames = []
                self._voiced_ms = self._quiet_ms = 0.0
        else:
            self._push_pre(frame)
        return events

    def _push_pre(self, frame: np.ndarray) -> None:
        self._pre.append(frame)
        self._pre_ms += self._ms(frame)
        while self._pre and self._pre_ms - self._ms(self._pre[0]) >= self.pre_roll_ms:
            self._pre_ms -= self._ms(self._pre.popleft())

    def tick(self, now: float) -> list[Event]:
        """Regelmäßig aufrufen: beendet Äußerungen, wenn keine Pakete mehr kommen."""
        if self.speaking and now - self._last_frame >= self.end_silence:
            return self._finish()
        if not self.speaking and self._frames and now - self._last_frame >= self.end_silence:
            self._frames = []
            self._voiced_ms = self._quiet_ms = 0.0
        return []
