"""マイク録音モジュール。PyAudio で 16kHz mono int16 を録り、numpy float32 で返す。

start(on_segment=...) を渡すと、録音中に core.segmenter でリアルタイムに区切り、
確定した区間をその都度コールバックで渡す（ストリーミング認識用）。stop() の戻り値は
従来どおり全体の音声で、最後の区切り以降の先頭位置は tail_start（サンプル番号）で読める。
"""
import logging
import threading

import numpy as np
import pyaudio

RATE = 16000
CHUNK = 1024
CHANNELS = 1
FORMAT = pyaudio.paInt16

log = logging.getLogger("injector")


def _to_float(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


class Recorder:
    def __init__(self):
        self._pa = None
        self._stream = None
        self._frames = []
        self._recording = False
        self._thread = None
        self._lock = threading.Lock()
        self._device_lost = False
        self._on_segment = None
        self._vad = None
        self._segmenter = None
        self._cut_frame = 0     # 直近の区切り位置（self._frames のインデックス）
        self.tail_start = 0     # stop() 後: 最後の区切り以降の先頭サンプル番号

    @property
    def is_recording(self):
        return self._recording

    @property
    def device_lost(self):
        """直前の録音中にマイクが切断された等でストリームが途中終了したか。"""
        return self._device_lost

    def start(self, on_segment=None):
        with self._lock:
            if self._recording:
                return
            self._frames = []
            self._device_lost = False
            self._cut_frame = 0
            self.tail_start = 0
            self._on_segment = on_segment
            self._vad = self._segmenter = None
            if on_segment is not None:
                try:
                    from core.segmenter import SpeechSegmenter, StreamingVAD
                    self._vad = StreamingVAD()
                    self._segmenter = SpeechSegmenter()
                except Exception:
                    # VAD が使えなくても録音は続ける（区切り無し＝従来どおり停止後に一括認識）
                    log.exception("ストリーミング用VADを初期化できないため区切り無しで録音")
                    self._vad = self._segmenter = None
            self._pa = pyaudio.PyAudio()
            self._stream = self._pa.open(
                format=FORMAT, channels=CHANNELS, rate=RATE,
                input=True, frames_per_buffer=CHUNK,
            )
            self._recording = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self):
        while self._recording:
            try:
                data = self._stream.read(CHUNK, exception_on_overflow=False)
            except OSError:
                # マイク切断等でストリームが読めなくなった。ここまでの録音を
                # 「正常な結果」として扱わないよう device_lost で呼び出し側に伝える
                self._device_lost = True
                break
            self._frames.append(data)
            if self._segmenter is not None:
                self._feed_segmenter(data)

    def _feed_segmenter(self, data: bytes):
        try:
            for prob in self._vad.feed(data):
                if self._segmenter.push(prob):
                    self._emit_segment()
        except Exception:
            # 区切りに失敗しても録音は壊さない。以降は区切り無し（末尾＝残り全部）で続行
            log.exception("ストリーミング区切りでエラー。以降は区切り無しで続行")
            self._segmenter = None

    def _emit_segment(self):
        end = len(self._frames)
        if end <= self._cut_frame:
            return
        audio = _to_float(b"".join(self._frames[self._cut_frame:end]))
        self._cut_frame = end
        self._on_segment(audio)

    def stop(self):
        """録音を止めて float32 numpy 配列（16kHz mono, -1..1）を返す。"""
        with self._lock:
            if not self._recording:
                return np.zeros(0, dtype=np.float32)
            self._recording = False
            self._thread.join(timeout=2)
            try:
                self._stream.stop_stream()
                self._stream.close()
            finally:
                self._pa.terminate()
                self._stream = None
                self._pa = None
            raw = b"".join(self._frames)
            self.tail_start = sum(len(f) for f in self._frames[:self._cut_frame]) // 2
            self._frames = []
            self._on_segment = self._vad = self._segmenter = None
        return _to_float(raw)

    def cancel(self):
        """録音を破棄して止める。"""
        self.stop()
