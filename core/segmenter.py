"""録音中のリアルタイム区切り（ストリーミング認識用）。

faster-whisper 同梱の Silero VAD（ONNX）を 512 サンプル（32ms）ごとに逐次実行し、
「発話時間の累計が約 24〜30 秒に達したあとの、次のポーズ」で区切る。

なぜこの区切り方か（実測は voice_app/CLAUDE.md「録音と音声認識の並行処理」）:
- Whisper のエンコーダは入力長に関係なく常に 30 秒ぶんを処理する（不足分はゼロ埋め）。
  細かく区切るほど「無音の処理」に計算が消え、無音があれば即区切る方式は +431〜+735% で棄却。
- 区間内の無音は transcribe() 側の vad_filter が除くので、エンコーダが見るのは
  「無音を除いた発話時間」。これを 30 秒に近づけるほど無駄が減る（30 秒を超えると
  2 ウィンドウ目がほぼ空になり逆に倍のコストになる）。
- そのため、生の秒数ではなく「vad_filter 後に残る発話時間の見積もり」を数えて
  27 秒以上で最初の長めのポーズ、30 秒以上で短いポーズ、33 秒で無音の最初の 1 ブロック、
  40 秒で強制、と段階的に区切る。この見積もりは実際の vad_filter より 15% ほど多めに出る
  （実録音 3 件で、しきい値 24/27/30 のとき実測 19〜26 秒）ので、しきい値は狙いの
  「実測 22〜29 秒」より高めに置いてある。

StreamingVAD は SileroVADModel.__call__ と同じ ONNX セッションを、h/c 状態と 64 サンプルの
文脈を持ち越して 1 ブロックずつ回す（オフラインの一括判定と出力が完全一致することを確認済み）。
"""
import numpy as np

SAMPLE_RATE = 16000
BLOCK = 512                 # Silero VAD の 1 ブロック（32ms @16kHz）
CONTEXT = 64                # ブロック先頭に付ける直前サンプル数（モデル仕様）
BLOCK_SEC = BLOCK / SAMPLE_RATE


class StreamingVAD:
    """faster-whisper 同梱の Silero VAD を逐次実行し、ブロックごとの発話確率を返す。"""

    def __init__(self):
        from faster_whisper.vad import get_vad_model
        self._session = get_vad_model().session
        self.reset()

    def reset(self):
        self._h = np.zeros((1, 1, 128), dtype="float32")
        self._c = np.zeros((1, 1, 128), dtype="float32")
        self._ctx = np.zeros(CONTEXT, dtype="float32")
        self._pending = np.zeros(0, dtype="float32")

    def feed(self, raw: bytes):
        """int16 PCM のバイト列を受け取り、完成したブロックぶんの発話確率のリストを返す。"""
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        buf = np.concatenate([self._pending, audio])
        probs = []
        n = (len(buf) // BLOCK) * BLOCK
        for i in range(0, n, BLOCK):
            blk = buf[i:i + BLOCK]
            x = np.concatenate([self._ctx, blk]).reshape(1, -1)
            out, self._h, self._c = self._session.run(
                None, {"input": x, "h": self._h, "c": self._c})
            probs.append(float(out.reshape(-1)[0]))
            self._ctx = blk[-CONTEXT:]
        self._pending = buf[n:]
        return probs


class SpeechSegmenter:
    """発話確率の列を受け取り、区切るべきブロック境界で True を返す（純ロジック・音声非依存）。

    filtered（vad_filter 後に残る発話時間の見積もり）の数え方:
      発話中のブロックは全部数える。無音は、発話が途切れてから merge_gap+2*pad（1.3 秒）
      までは数える（vad_filter が 500ms 未満の隙間を結合し、前後 400ms を残すため）。
      それより長い無音は数えない。
    """

    def __init__(self, threshold=0.5, neg_threshold=0.35,
                 merge_gap_sec=0.5, pad_sec=0.4,
                 target_sec=27.0, target_hi_sec=30.0, target_max_sec=33.0,
                 hard_max_sec=40.0, pause_sec=0.7, short_pause_sec=0.3,
                 block_sec=BLOCK_SEC):
        self.threshold = threshold
        self.neg_threshold = neg_threshold
        self.count_gap_sec = merge_gap_sec + 2 * pad_sec
        self.target_sec = target_sec
        self.target_hi_sec = target_hi_sec
        self.target_max_sec = target_max_sec
        self.hard_max_sec = hard_max_sec
        self.pause_sec = pause_sec
        self.short_pause_sec = short_pause_sec
        self.block_sec = block_sec
        self.reset()

    def reset(self):
        self.in_speech = False
        self.silence_run = 0.0   # 直近の発話終了からの無音の長さ
        self.filtered = 0.0      # 現在の区間の発話時間（vad_filter 後の見積もり）
        self.raw = 0.0           # 現在の区間の生の長さ
        self.segments = 0        # 区切った回数

    def _cut(self):
        self.filtered = 0.0
        self.raw = 0.0
        self.silence_run = 0.0
        self.segments += 1
        return True

    def push(self, prob: float) -> bool:
        """1 ブロックぶんの発話確率を入れる。このブロックの末尾で区切るなら True。"""
        self.raw += self.block_sec
        speech = prob >= (self.neg_threshold if self.in_speech else self.threshold)
        if speech:
            self.in_speech = True
            self.silence_run = 0.0
            self.filtered += self.block_sec
            if self.filtered >= self.hard_max_sec:
                return self._cut()      # ポーズが来ないので強制（30 秒超は 2 ウィンドウ目に入る）
            return False
        # 無音ブロック
        was_speech = self.in_speech
        self.in_speech = False
        self.silence_run += self.block_sec
        if self.silence_run <= self.count_gap_sec:
            self.filtered += self.block_sec
        if self.filtered <= 0.0:
            return False                # まだ何も話していない
        if self.filtered >= self.target_max_sec:
            return self._cut()          # 無音の最初の 1 ブロックで区切る
        if self.filtered >= self.target_hi_sec and self.silence_run >= self.short_pause_sec:
            return self._cut()
        if self.filtered >= self.target_sec and self.silence_run >= self.pause_sec:
            return self._cut()
        return False


def _selftest():
    """python -m core.segmenter --selftest"""
    def run(probs, **kw):
        seg = SpeechSegmenter(**kw)
        cuts = []
        for i, p in enumerate(probs):
            f_before = seg.filtered
            if seg.push(p):
                cuts.append((round((i + 1) * BLOCK_SEC, 2), round(f_before, 2)))
        return cuts, seg

    nblk = lambda sec: int(round(sec / BLOCK_SEC))
    speech = lambda sec: [0.9] * nblk(sec)
    silence = lambda sec: [0.05] * nblk(sec)

    # 1) 短い発話（20 秒）は区切らない → 従来どおり停止後に一括認識
    cuts, _ = run(speech(20) + silence(2))
    assert cuts == [], cuts

    # 2) 10 秒話して 1 秒休む、を繰り返す → 累計 24 秒以上で最初のポーズ（≥0.7s）で区切る
    probs = (speech(10) + silence(1)) * 5
    cuts, seg = run(probs)
    assert len(cuts) >= 1, cuts
    t, f = cuts[0]
    # 10+1+10+1 = 22 (<27) では切らず、次の 10 秒のあとの短いポーズで切る（filtered≈32）
    assert 27.0 <= f <= 33.5, cuts
    assert t >= 24.0, cuts

    # 3) 5 秒話して 1 秒休む、を繰り返す → 27〜33 秒あたりの休みで切れる（前後の区間も同様）
    probs = (speech(5) + silence(1)) * 12
    cuts, _ = run(probs)
    assert len(cuts) >= 2, cuts
    for t, f in cuts:
        assert 27.0 <= f <= 33.1, cuts

    # 4) 一度も無音ブロックが無い連続発話 → hard_max（40 秒）で強制的に区切る
    #    （実際の発話は語間に 32ms の無音ブロックが頻繁にあるので 30 秒で切れる。これは安全弁）
    cuts, _ = run(speech(70))
    assert len(cuts) == 1 and abs(cuts[0][1] - 40.0) < 0.1, cuts

    # 5) 長い無音は filtered に数えない（考え中の間が長くても区切りは発話量で決まる）
    probs = speech(12) + silence(20) + speech(12) + silence(20) + speech(2) + silence(1)
    cuts, seg = run(probs)
    assert len(cuts) == 1, cuts       # 12+1.3+12+1.3+2 ≈ 28.6 → 最後の休みで 1 回
    assert cuts[0][1] < 33.0, cuts

    # 6) 区切り後は累計がリセットされる
    probs = (speech(10) + silence(1)) * 10
    cuts, _ = run(probs)
    assert len(cuts) >= 3, cuts
    print(f"segmenter selftest OK ({len(cuts)} cuts in case 6: {cuts})")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: python -m core.segmenter --selftest")
