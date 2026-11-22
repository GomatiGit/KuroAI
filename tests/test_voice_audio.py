import numpy as np

from kuro.voice.audio import (
    FRAME_BYTES, PCMBuffer, StreamResampler, discord_to_mono16k, rms, to_wav_bytes,
)
from kuro.voice.vad import END, START, UtteranceDetector


def tone(seconds, rate, freq=220.0, amp=8000):
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype("<i2")


def test_discord_frame_becomes_16k_mono():
    mono48 = tone(0.02, 48000)
    stereo = np.repeat(mono48, 2).tobytes()
    assert len(stereo) == FRAME_BYTES
    out = discord_to_mono16k(stereo)
    assert out.dtype == np.int16 and len(out) == 320
    assert rms(out) > 4000


def test_resampler_is_seamless_across_chunks():
    source = tone(0.5, 24000)
    whole = StreamResampler(24000, 1).feed(source.tobytes())
    pieces = StreamResampler(24000, 1)
    raw = source.tobytes()
    chunked = b"".join(pieces.feed(raw[i:i + 1001]) for i in range(0, len(raw), 1001))  # krumme Stücke
    a = np.frombuffer(whole, dtype="<i2").reshape(-1, 2)
    b = np.frombuffer(chunked, dtype="<i2").reshape(-1, 2)
    assert abs(len(a) - 24000) <= 2, "0,5 s -> 24000 Stereo-Frames bei 48 kHz"
    n = min(len(a), len(b))
    assert abs(len(a) - len(b)) <= 2
    assert np.max(np.abs(a[:n].astype(int) - b[:n].astype(int))) <= 1
    assert np.array_equal(a[:, 0], a[:, 1])


def test_resampler_downsamples_stereo_44k():
    stereo = np.repeat(tone(0.25, 44100), 2).tobytes()
    out = np.frombuffer(StreamResampler(44100, 2).feed(stereo), dtype="<i2").reshape(-1, 2)
    assert abs(len(out) - 12000) <= 2 and rms(out[:, 0]) > 4000


def test_pcm_buffer_plays_silence_until_finished_then_ends():
    buf = PCMBuffer()
    assert buf.read_frame() == b"\x00" * FRAME_BYTES      # noch nichts da -> Stille
    buf.feed(b"\x01" * (FRAME_BYTES + 100))
    assert buf.read_frame() == b"\x01" * FRAME_BYTES
    buf.finish()
    last = buf.read_frame()
    assert last[:100] == b"\x01" * 100 and len(last) == FRAME_BYTES
    assert buf.read_frame() == b""                        # Ende der Wiedergabe
    assert abs(buf.played_seconds - 0.04) < 1e-9


def test_pcm_buffer_clear_stops_immediately():
    buf = PCMBuffer()
    buf.feed(b"\x01" * FRAME_BYTES * 10)
    buf.clear()
    buf.finish()
    assert buf.read_frame() == b""


def test_wav_bytes_have_header():
    wav = to_wav_bytes(tone(0.1, 16000))
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and len(wav) == 44 + 3200


def frames(samples, size=320):
    return [samples[i:i + size] for i in range(0, len(samples) - size + 1, size)]


def feed_all(det, samples, start=0.0):
    events, now = [], start
    for frame in frames(samples):
        now += 0.02
        events += det.feed(frame, now)
    return events, now


def test_vad_detects_start_and_end_by_silence():
    det = UtteranceDetector()
    quiet = np.zeros(16000, dtype=np.int16)
    speech = tone(1.0, 16000)
    events, now = feed_all(det, np.concatenate([quiet[:4800], speech, quiet]))
    kinds = [kind for kind, _ in events]
    assert kinds == [START, END]
    audio = events[1][1]
    assert 1.0 <= len(audio) / 16000 <= 2.0, "Äußerung plus Vorlauf und Nachlauf"


def test_vad_ends_when_packets_stop_arriving():
    det = UtteranceDetector()
    events, now = feed_all(det, tone(0.8, 16000))
    assert [k for k, _ in events] == [START]
    assert det.tick(now + 0.3) == []
    ended = det.tick(now + 0.8)
    assert [k for k, _ in ended] == [END] and ended[0][1] is not None


def test_vad_ignores_clicks_and_quiet_noise():
    det = UtteranceDetector()
    noise = (np.random.default_rng(1).normal(0, 60, 16000)).astype(np.int16)
    click = np.concatenate([np.zeros(3200, np.int16), tone(0.06, 16000), np.zeros(8000, np.int16)])
    events, now = feed_all(det, np.concatenate([noise, click]))
    assert events == [] and det.tick(now + 2) == []


def test_vad_caps_very_long_utterances():
    det = UtteranceDetector(max_utterance_seconds=2)
    events, _ = feed_all(det, tone(3.0, 16000))
    assert [k for k, _ in events][:2] == [START, END]
