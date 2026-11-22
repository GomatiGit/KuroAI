"""LLM-, STT- und TTS-Client gegen einen echten lokalen HTTP-Server."""

import json
import struct

import numpy as np
import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from conftest import run  # noqa: E402
from kuro.llm import (  # noqa: E402
    LLMClient, LLMError, LLMUnavailable, ThinkFilter, chat_tool, strip_think,
)
from kuro.voice.stt import STTClient, clean_transcript  # noqa: E402
from kuro.voice.tts import TTSClient, parse_wav_header  # noqa: E402


def make_app(state):
    async def chat(request):
        body = await request.json()
        state["chat"].append({"body": body, "auth": request.headers.get("Authorization")})
        if body["model"] == "kaputt":
            return web.json_response({"error": "model not found"}, status=404)
        if body.get("stream"):
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            for piece in ["<thi", "nk>geheim</think>", "Nya~ ", "na ", "endlich!"]:
                chunk = {"choices": [{"delta": {"content": piece}}]}
                await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
            await resp.write(b": keep-alive\n\ndata: [DONE]\n\n")
            return resp
        if body.get("tools"):
            message = {"content": None, "tool_calls": [{"type": "function", "function": {
                "name": "create_poll",
                "arguments": json.dumps({"question": "Wann?", "answers": ["Sa", "So"],
                                         "duration_hours": 24, "multiple": False})}}]}
        else:
            message = {"content": "<think>hm</think>Hmpf. Na gut."}
        return web.json_response({"model": body["model"], "choices": [{"message": message}],
                                  "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})

    async def transcribe(request):
        form = await request.post()
        audio = form["file"].file.read()
        state["stt"].append({"model": form["model"], "language": form.get("language"),
                             "prompt": form.get("prompt"), "wav": audio[:4], "bytes": len(audio)})
        return web.json_response({"text": state.get("stt_text", "  Kuro,  bring mir einen Met! ")})

    async def speech(request):
        body = await request.json()
        state["tts"].append(body)
        pcm = (np.arange(4800) % 100).astype("<i2").tobytes()
        if body.get("as_wav"):
            header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack(
                "<IHHIIHH", 16, 1, 1, 22050, 44100, 2, 16) + b"data" + struct.pack("<I", len(pcm))
            return web.Response(body=header + pcm, content_type="audio/wav")
        resp = web.StreamResponse(headers={"x-audio-sample-rate": "24000", "x-audio-channels": "1"})
        await resp.prepare(request)
        for i in range(0, len(pcm), 1500):
            await resp.write(pcm[i:i + 1500])
        return resp

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_post("/v1/audio/transcriptions", transcribe)
    app.router.add_post("/v1/audio/speech", speech)
    return app


async def with_server(test):
    state = {"chat": [], "stt": [], "tts": []}
    server = TestServer(make_app(state))
    await server.start_server()
    try:
        return await test(str(server.make_url("/v1")), state)
    finally:
        await server.close()


def test_chat_strips_think_sends_params_and_key():
    async def scenario(base, state):
        llm = LLMClient(base, "mistral-small3.2:24b", "geheim",
                        params={"temperature": 0.85, "top_p": None, "max_tokens": 700},
                        extra_body={"reasoning_effort": "low"})
        result = await llm.chat([{"role": "user", "content": "hi"}], temperature=0.2, json_mode=True)
        await llm.close()
        sent = state["chat"][0]
        assert result.text == "Hmpf. Na gut." and result.usage["total_tokens"] == 8
        assert sent["auth"] == "Bearer geheim"
        assert sent["body"]["temperature"] == 0.2 and "top_p" not in sent["body"]
        assert sent["body"]["max_tokens"] == 700 and sent["body"]["reasoning_effort"] == "low"
        assert sent["body"]["response_format"] == {"type": "json_object"}

    run(with_server(scenario))


def test_chat_returns_parsed_tool_calls():
    async def scenario(base, state):
        llm = LLMClient(base, "m")
        tool = chat_tool("create_poll", "Umfrage", {"type": "object", "properties": {}})
        result = await llm.chat([{"role": "user", "content": "Umfrage bitte"}], tools=[tool])
        await llm.close()
        assert result.text == ""
        assert result.tool_calls == [{"name": "create_poll", "arguments": {
            "question": "Wann?", "answers": ["Sa", "So"], "duration_hours": 24, "multiple": False}}]
        assert state["chat"][0]["body"]["tools"][0]["function"]["name"] == "create_poll"

    run(with_server(scenario))


def test_stream_yields_text_without_think_block():
    async def scenario(base, state):
        llm = LLMClient(base, "m")
        pieces = [p async for p in llm.stream([{"role": "user", "content": "hi"}], max_tokens=50)]
        await llm.close()
        assert "".join(pieces) == "Nya~ na endlich!" and len(pieces) >= 2
        assert state["chat"][0]["body"]["stream"] is True and state["chat"][0]["body"]["max_tokens"] == 50

    run(with_server(scenario))


def test_errors_become_llm_error():
    async def scenario(base, state):
        llm = LLMClient(base, "kaputt")
        with pytest.raises(LLMError, match="404"):
            await llm.chat([{"role": "user", "content": "hi"}])
        with pytest.raises(LLMError, match="404"):
            async for _ in llm.stream([{"role": "user", "content": "hi"}]):
                pass
        await llm.close()
        offline = LLMClient("http://127.0.0.1:9/v1", "m")
        with pytest.raises(LLMError, match="nicht erreichbar"):
            await offline.chat([{"role": "user", "content": "hi"}])
        await offline.close()

    run(with_server(scenario))


def test_think_filter_handles_split_tags():
    f = ThinkFilter()
    out = "".join(f.feed(p) for p in ["Hal", "lo <", "think>ab", "c</th", "ink>\nWelt", " <b>"]) + f.flush()
    assert out == "Hallo Welt <b>"
    assert strip_think("<think>offen ohne Ende") == "" and strip_think("a<think>x</think>b") == "ab"


def test_stt_uploads_wav_and_cleans_text():
    async def scenario(base, state):
        stt = STTClient(base, "whisper-turbo", language="de", prompt="Kuro, Gomati")
        samples = (np.sin(np.arange(16000) / 10) * 5000).astype(np.int16)
        text = await stt.transcribe(samples)
        state["stt_text"] = "Untertitel der Amara.org-Community"
        junk = await stt.transcribe(samples)
        await stt.close()
        sent = state["stt"][0]
        assert text == "Kuro, bring mir einen Met!" and junk == ""
        assert sent == {"model": "whisper-turbo", "language": "de", "prompt": "Kuro, Gomati",
                        "wav": b"RIFF", "bytes": 44 + 32000}

    run(with_server(scenario))


def test_clean_transcript_filters_hallucinations():
    assert clean_transcript("Vielen Dank fürs Zuschauen!") == ""
    assert clean_transcript(" ... ") == ""
    assert clean_transcript("Danke dir, Kuro.") == "Danke dir, Kuro."


def test_tts_streams_pcm_with_instruction():
    async def scenario(base, state):
        tts = TTSClient(base, "qwen3-tts", "vivian", extra_body={"language": "German"})
        chunks = [c async for c in tts.synthesize("Nja, na endlich!", "Neckisch und frech.")]
        await tts.close()
        assert b"".join(c.pcm for c in chunks) == (np.arange(4800) % 100).astype("<i2").tobytes()
        assert len(chunks) > 1 and {(c.rate, c.channels) for c in chunks} == {(24000, 1)}
        assert state["tts"][0] == {"model": "qwen3-tts", "input": "Nja, na endlich!", "voice": "vivian",
                                   "response_format": "pcm", "language": "German",
                                   "instructions": "Neckisch und frech."}

    run(with_server(scenario))


def test_tts_accepts_wav_answers_and_can_omit_instructions():
    async def scenario(base, state):
        tts = TTSClient(base, "x", "v", instructions_field="", extra_body={"as_wav": True})
        chunks = [c async for c in tts.synthesize("Hallo", "wird ignoriert")]
        await tts.close()
        assert b"".join(c.pcm for c in chunks) == (np.arange(4800) % 100).astype("<i2").tobytes()
        assert (chunks[0].rate, chunks[0].channels) == (22050, 1)
        assert "instructions" not in state["tts"][0]

    run(with_server(scenario))


def test_wav_header_parser_needs_complete_header():
    assert parse_wav_header(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None
    assert parse_wav_header(b"keinwav-----") is None


def test_params_set_to_null_are_never_sent_even_as_override():
    async def scenario(base, state):
        llm = LLMClient(base, "gpt-5.1", params={"temperature": None, "top_p": None, "max_tokens": None},
                        extra_body={"reasoning_effort": "low"})
        await llm.chat([{"role": "user", "content": "JSON bitte"}], temperature=0.2, max_tokens=600)
        _ = [p async for p in llm.stream([{"role": "user", "content": "hi"}], max_tokens=220)]
        await llm.close()
        for sent in state["chat"]:
            assert "temperature" not in sent["body"] and "max_tokens" not in sent["body"]
            assert sent["body"]["reasoning_effort"] == "low"

    run(with_server(scenario))


def test_unreachable_endpoint_means_asleep_and_is_not_hammered():
    async def scenario(base, state):
        import time
        changes = []
        llm = LLMClient("http://127.0.0.1:9/v1", "m", connect_timeout=1, retry_after=30)
        llm.on_state_change = changes.append
        with pytest.raises(LLMUnavailable):
            await llm.chat([{"role": "user", "content": "hi"}])
        assert llm.available is False and changes == [False]

        started = time.perf_counter()
        with pytest.raises(LLMUnavailable, match="Wartezeit"):
            await llm.chat([{"role": "user", "content": "hi"}])
        with pytest.raises(LLMUnavailable):
            async for _ in llm.stream([{"role": "user", "content": "hi"}]):
                pass
        assert time.perf_counter() - started < 0.2, "während der Wartezeit wird nicht erneut verbunden"
        assert await llm.ping() is False and changes == [False], "kein doppeltes Einschlafen"
        await llm.close()

    run(with_server(scenario))


def test_ping_wakes_her_up_and_http_errors_do_not_count_as_asleep():
    async def scenario(base, state):
        changes = []
        llm = LLMClient(base, "m", retry_after=30)
        llm.on_state_change = changes.append
        llm._set_available(False)                      # eben noch nicht erreichbar
        assert await llm.ping() is True
        assert llm.available is True and changes == [False, True]
        result = await llm.chat([{"role": "user", "content": "hi"}])   # sofort wieder nutzbar
        assert result.text == "Hmpf. Na gut."

        broken = LLMClient(base, "kaputt")
        with pytest.raises(LLMError) as info:
            await broken.chat([{"role": "user", "content": "hi"}])
        assert not isinstance(info.value, LLMUnavailable) and broken.available is True
        await llm.close()
        await broken.close()

    run(with_server(scenario))
