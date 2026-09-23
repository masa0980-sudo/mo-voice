# -*- coding: utf-8 -*-
"""ストリーミング認識（config "streaming_transcribe" / "model_long"）の結合テスト。マイク・実モデル不要。

  python -X utf8 scripts/streaming_selfcheck.py

A) data/audio/ の長い実録音3件を Recorder と同じ 1024 サンプル刻みで StreamingVAD+SpeechSegmenter
   に流し、区切られた区間の生の長さと「vad_filter 後に残る発話時間」を出す（狙い 20〜30 秒・30 秒超なし）。
B) DictationController のワーカーを偽 transcriber で回し、区間が順序どおりに結合されること・
   キャンセル後は結果が捨てられること・空の末尾でも壊れないこと・第2モデル（model_long）の
   選択規則（区切りが起きた録音は全区間 long、末尾だけの録音は通常モデル、未ロードならフォールバック）
   を確認する。

!!! B は controller を実際に組み立てる。controller は __init__ で sig_transcribed →
_on_transcribed（実際の貼り付け）を接続しているので、必ず切り離してから発火させる
（2026-09-22 に切り離し忘れで、作業中の画面へダミー文字列が 4 回貼り付いた）。
"""
import sys
import time
import types
import wave
from pathlib import Path

import numpy as np

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from core.segmenter import SpeechSegmenter, StreamingVAD  # noqa: E402
from faster_whisper.vad import VadOptions, get_speech_timestamps  # noqa: E402

CHUNK_BYTES = 1024 * 2
SR = 16000


def filtered_sec(audio):
    ts = get_speech_timestamps(
        audio, VadOptions(min_silence_duration_ms=500, speech_pad_ms=400), sampling_rate=SR)
    return sum(t["end"] - t["start"] for t in ts) / SR


def longest_wavs(n=3):
    files = []
    for p in (APP / "data" / "audio").glob("*.wav"):
        with wave.open(str(p), "rb") as wf:
            files.append((wf.getnframes(), p))
    files.sort(reverse=True)
    return [p for _, p in files[:n]]


def part_a():
    print("=== A) 実録音を Recorder と同じ刻みで区切る ===")
    wavs = longest_wavs()
    if not wavs:
        print("data/audio/ に wav が無いためスキップ")
        return
    for path in wavs:
        with wave.open(str(path), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        vad, seg = StreamingVAD(), SpeechSegmenter()
        frames, cut, segments = [], 0, []
        t0 = time.time()
        for i in range(0, len(raw), CHUNK_BYTES):
            data = raw[i:i + CHUNK_BYTES]
            frames.append(data)
            for p in vad.feed(data):
                if seg.push(p):
                    segments.append(b"".join(frames[cut:]))
                    cut = len(frames)
        segments.append(b"".join(frames[cut:]))
        vad_cost = time.time() - t0
        segs = [np.frombuffer(s, dtype=np.int16).astype(np.float32) / 32768.0 for s in segments]
        raw_len = [round(len(s) / SR, 1) for s in segs]
        filt = [round(filtered_sec(s), 1) for s in segs]
        print(f"{path.name}: 区間{len(segs)}個（末尾含む） VAD処理{vad_cost:.2f}s / 音声{len(raw)/2/SR:.0f}s")
        print(f"   生の長さ      : {raw_len}")
        print(f"   発話(filtered): {filt}")
        body = filt[:-1]
        if body:
            assert max(body) <= 31.0, f"30秒超の区間がある: {body}"
            print(f"   末尾以外の発話時間: min={min(body)} max={max(body)}  (狙い 20〜30)")


class FakeTranscriber:
    model_name = "fake-small"
    last_words = []

    def transcribe(self, audio, hotwords=None, initial_prompt=None):
        time.sleep(0.05)
        self.last_words = [{"word": f"w{len(audio)}", "probability": 0.9}]
        return f"[{len(audio)}]"


class FakeLong:
    model_name = "fake-medium"
    last_words = []
    _loaded = True

    @property
    def is_loaded(self):
        return self._loaded

    def transcribe(self, audio, hotwords=None, initial_prompt=None):
        time.sleep(0.05)
        self.last_words = [{"word": f"m{len(audio)}", "probability": 0.9}]
        return f"[m{len(audio)}]"


def make_controller():
    from PyQt5.QtCore import QCoreApplication
    app = QCoreApplication.instance() or QCoreApplication([])
    from core import injector
    from core.controller import DictationController

    class FakeCorrections:
        def apply(self, t):
            return t

    pb = types.SimpleNamespace(build_prompt=lambda *a, **k: "")
    cfg = {"streaming_transcribe": True, "context_rules": [],
           "confidence_highlight": {"enabled": False}}
    c = DictationController(cfg, FakeTranscriber(), {}, FakeCorrections(), pb)
    # 実際の貼り付け・クリップボード・実データへの書き込みを全部切り離す（冒頭の注意参照）
    c.sig_transcribed.disconnect(c._on_transcribed)
    injector.inject = lambda *a, **k: True
    injector.focus_window = lambda *a, **k: True
    injector._set_clipboard_text = lambda *a, **k: True
    c._save_audio = lambda audio: ""
    c._log_history = lambda *a, **k: None
    c._record_context = ("test.exe", "title")
    return app, c


def wait_result(app, holder, timeout=5.0):
    t0 = time.time()
    while holder["text"] is None and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    return holder["text"]


def part_b():
    print("\n=== B) controller のワーカー（順序・キャンセル・モデル選択） ===")
    app, c = make_controller()
    holder = {"text": None}
    c.sig_transcribed.connect(lambda t: holder.__setitem__("text", t))

    def run(seg_lens, tail_len):
        holder["text"] = None
        on_seg = c._stream_begin()
        for n in seg_lens:
            on_seg(np.zeros(n, dtype=np.float32))
        c.recorder = types.SimpleNamespace(tail_start=sum(seg_lens))
        c._stream_finish(np.zeros(sum(seg_lens) + tail_len, dtype=np.float32), duration=1.0)
        return wait_result(app, holder)

    c.transcriber_long = None
    text = run([1000, 2000, 3000], 500)
    assert text == "[1000][2000][3000][500]", text
    assert c._stream_q is None
    print("順序どおりに結合:", text)
    assert [w["word"] for w in c.transcriber.last_words] == ["w1000", "w2000", "w3000", "w500"]
    print("低信頼語用 last_words も区間順に連結: OK")
    assert run([1000], 0) == "[1000][0]"
    print("空の末尾区間: OK")

    holder["text"] = None
    on_seg = c._stream_begin()
    on_seg(np.zeros(1000, dtype=np.float32))
    c._stream_abort()
    assert c._stream_q is None
    time.sleep(0.3)
    app.processEvents()
    assert holder["text"] is None
    print("キャンセル後に結果が捨てられる: OK")

    c.transcriber_long = FakeLong()
    text = run([1000, 2000], 500)
    assert text == "[m1000][m2000][m500]", text
    print("区切りが起きた録音は全区間を第2モデルで認識:", text)
    text = run([], 1500)
    assert text == "[1500]", text
    print("末尾しかない短い録音は通常モデル:", text)
    c.transcriber_long._loaded = False
    text = run([1000], 500)
    assert text == "[1000][500]", text
    print("第2モデル未ロード時は通常モデルにフォールバック:", text)


if __name__ == "__main__":
    part_a()
    part_b()
    print("\nstreaming selfcheck OK")
