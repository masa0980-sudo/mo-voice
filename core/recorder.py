"""マイク録音モジュール。PyAudio で 16kHz mono int16 を録り、numpy float32 で返す。"""
import threading

import numpy as np
import pyaudio

RATE = 16000
CHUNK = 1024
CHANNELS = 1
FORMAT = pyaudio.paInt16


class Recorder:
    def __init__(self):
        self._pa = None
        self._stream = None
        self._frames = []
        self._recording = False
        self._thread = None
        self._lock = threading.Lock()
        self._device_lost = False

    @property
    def is_recording(self):
        return self._recording

    @property
    def device_lost(self):
        """直前の録音中にマイクが切断された等でストリームが途中終了したか。"""
        return self._device_lost

    def start(self):
        with self._lock:
            if self._recording:
                return
            self._frames = []
            self._device_lost = False
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
                self._frames.append(data)
            except OSError:
                # マイク切断等でストリームが読めなくなった。ここまでの録音を
                # 「正常な結果」として扱わないよう device_lost で呼び出し側に伝える
                self._device_lost = True
                break

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
            self._frames = []
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return audio

    def cancel(self):
        """録音を破棄して止める。"""
        self.stop()
