"""faster-whisper 常駐ラッパー。モデルは load() で1回だけロードする。"""
import numpy as np

SAMPLE_RATE = 16000

# 注: VADチャンク分割（30秒超を無音位置で自前分割し各チャンクにpromptを渡す方式）は
# 2026-08-22 のA/B検証で不採用になった。60秒音声で時間+76%（15.3→26.9秒）かつ
# チャンク境界で文脈が切れて品質も悪化（「Claude Code」→「クロードコード」等）。
# 詳細は CLAUDE.md「認識精度そのものの改善を検証した結果」参照。


class Transcriber:
    def __init__(self, model_name="small", compute_type="int8",
                 language="ja", beam_size=2):
        self.model_name = model_name
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self._model = None
        # 直近の transcribe() で得た単語別信頼度 [{word, probability}]。
        # 低信頼語ハイライト（誤りの可能性がある箇所の可視化）に使う
        self.last_words = []

    def load(self):
        """モデルをロードする（初回のみ数秒〜数十秒）。バックグラウンドスレッドから呼ぶ。"""
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type=self.compute_type,
            )
            # 正確なトークン数計算のため、モデル同梱のトークナイザを共有する
            # （追加ダウンロード不要。旧実装は未インストールの openai-whisper を
            #   参照していたため常に概算フォールバックになっていた）
            try:
                from vocab import prompt_builder
                prompt_builder.set_tokenizer(self._model.hf_tokenizer)
            except Exception:
                pass
        return self._model

    def transcribe(self, audio: np.ndarray, hotwords: str = None,
                   initial_prompt: str = None) -> str:
        """16kHz mono float32 の numpy 配列を日本語テキストに変換する。

        語彙ヒントは initial_prompt で渡す運用（呼び出し側もそうなっている）。
        hotwords も渡せるようにしてあるが実運用では使わない: 全ウィンドウに毎回
        223トークンを渡すため認識時間が2.5倍に悪化し（60秒音声で14.2秒→36.5秒・実測）、
        認識品質には有意差がなかった（2026-08-22 検証）。
        """
        if self._model is None:
            raise RuntimeError("モデルが未ロードです。load() を先に呼んでください")
        if audio.size < 1600:  # 0.1秒未満は無視
            return ""
        segments, _info = self._model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam_size,
            hotwords=hotwords,
            initial_prompt=initial_prompt,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            word_timestamps=True,  # 単語別信頼度の取得（CPUコストほぼゼロ・実測済み）
        )
        parts = []
        words = []
        for seg in segments:
            parts.append(seg.text.strip())
            for w in (seg.words or []):
                words.append({"word": w.word, "probability": w.probability})
        self.last_words = words
        return "".join(p for p in parts if p)
