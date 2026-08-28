"""状態機械: IDLE → RECORDING → TRANSCRIBING → (注入) → IDLE

pynput ホットキースレッドからは Qt シグナル経由でトグルされる。
認識はワーカースレッドで実行し、結果を Qt スロットで受けて注入する。
"""
import json
import threading
import time
from pathlib import Path

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from core import context as ctx
from core import injector
from core.recorder import Recorder

APP_DIR = Path(__file__).resolve().parent.parent
HISTORY_FILE = APP_DIR / "data" / "history.jsonl"
CORRECTIONS_LOG_FILE = APP_DIR / "data" / "corrections_log.jsonl"
AUDIO_DIR = APP_DIR / "data" / "audio"   # 評価用の発話音声リングバッファ
AUDIO_KEEP = 50                          # 直近N件のみ保持（1分音声≒1.9MB）

IDLE = "idle"
RECORDING = "recording"
TRANSCRIBING = "transcribing"
LOADING = "loading"
FAILED = "failed"      # モデルのロードに失敗（リトライ待ち）


def _mic_error_message(exc: Exception) -> str:
    """PortAudio の英語エラーを、対処が分かる日本語メッセージに変換する。"""
    raw = str(exc)
    low = raw.lower()
    if ("no default input" in low or "device unavailable" in low
            or "invalid device" in low or "no such device" in low
            or "invalid input device" in low or "-9996" in low
            or "device not found" in low):
        return ("マイクが見つかりません。マイクが接続されているか、"
                "Windowsの「サウンド設定」で既定の入力デバイスが"
                "選ばれているか確認してください")
    if "access" in low or "denied" in low or "permission" in low:
        return ("マイクへのアクセスが拒否されました。Windowsの"
                "「設定 → プライバシーとセキュリティ → マイク」で、"
                "デスクトップアプリのマイクアクセスを有効にしてください")
    if "in use" in low or "busy" in low or "already" in low:
        return ("マイクを他のアプリが使用中の可能性があります。"
                "通話アプリや録音アプリを終了してからお試しください")
    return f"マイクを開けませんでした。マイクの接続と設定を確認してください（{raw}）"


class DictationController(QObject):
    sig_toggle = pyqtSignal()          # ホットキースレッド → Qt スレッド
    sig_cancel = pyqtSignal()
    sig_state = pyqtSignal(str)        # UI 更新用: idle/recording/transcribing/loading
    sig_message = pyqtSignal(str, str)  # (タイトル, 本文) トレイ通知
    sig_transcribed = pyqtSignal(str)  # 内部: ワーカー → Qt スレッド
    sig_suggest_correction = pyqtSignal(object)  # 低信頼語リスト → 修正ダイアログ自動表示

    def __init__(self, config, transcriber, vocabulary, corrections,
                 prompt_builder):
        super().__init__()
        self.config = config
        self.transcriber = transcriber
        self.vocabulary = vocabulary
        self.corrections = corrections
        self.prompt_builder = prompt_builder

        self.state = LOADING
        self.load_error = ""            # モデルロード失敗時の理由
        self._load_in_progress = False  # ロード中の多重実行防止
        self.recorder = Recorder()
        self.last_result = ""
        self.last_injected_text = ""   # 入力欄に実際に注入したテキスト
        self.last_inject_hwnd = None   # 注入先ウィンドウ（修正の再注入用）
        self.last_inject_time = 0.0    # 注入時刻（時間が経ちすぎた修正の誤爆防止）
        self.last_result_injected = False  # last_result が実際に注入されたか
        # （注入先ガードで保留された場合 False。その場合 Ctrl+Alt+Z は
        #   古い注入の置換を試みず、修正文のコピーだけ行う）
        self._record_started = 0.0
        self._record_context = ("", "")  # 録音開始時点の (exe, title)
        self._record_hwnd = None         # 録音開始時点のウィンドウ（自動復帰注入用）

        self.sig_toggle.connect(self._on_toggle)
        self.sig_cancel.connect(self._on_cancel)
        self.sig_transcribed.connect(self._on_transcribed)

        self._max_timer = QTimer(self)
        self._max_timer.setSingleShot(True)
        self._max_timer.timeout.connect(self._auto_stop)

    # ---- モデルロード ----

    def start_model_load(self):
        """モデルをバックグラウンドでロードする。

        初回起動時は HuggingFace から数百MBのダウンロードが走るため、
        オフライン・プロキシ・ディスク不足で失敗しうる。従来は例外を捕まえて
        おらずスレッドが死に、状態が LOADING のまま永久固着していた
        （ユーザーにはホットキーを押すたび「ロード中です」が出るだけだった）。
        """
        if self._load_in_progress:
            return
        self._load_in_progress = True
        self.state = LOADING
        self.sig_state.emit(LOADING)

        def _load():
            try:
                self.transcriber.load()
            except Exception as e:
                injector.log.exception("モデルのロードに失敗")
                self.load_error = str(e)
                self.state = FAILED
                self.sig_state.emit(FAILED)
                self.sig_message.emit(
                    "MO Voice",
                    "音声モデルを読み込めませんでした。ネットワーク接続を確認して、"
                    "トレイメニューの「モデルを再読み込み」をお試しください\n"
                    f"（{type(e).__name__}: {e}）")
                return
            finally:
                self._load_in_progress = False
            self.load_error = ""
            self.state = IDLE
            self.sig_state.emit(IDLE)
            self.sig_message.emit("MO Voice", "準備完了。Ctrl+Alt+Space で録音開始")
        threading.Thread(target=_load, daemon=True).start()

    def retry_model_load(self):
        """モデルのロードをやり直す（トレイメニューから呼ばれる）。"""
        if self.state == IDLE:
            self.sig_message.emit("MO Voice", "モデルは既に読み込み済みです")
            return
        if self._load_in_progress:
            self.sig_message.emit("MO Voice", "モデルを読み込み中です")
            return
        self.sig_message.emit("MO Voice", "モデルを再読み込みしています...")
        self.start_model_load()

    # ---- トグル ----

    def _on_toggle(self):
        if self.state == FAILED:
            self.sig_message.emit(
                "MO Voice",
                "音声モデルが読み込めていないため録音できません。"
                "トレイメニューの「モデルを再読み込み」をお試しください")
            return
        if self.state == LOADING:
            self.sig_message.emit("MO Voice", "モデルをロード中です。少しお待ちください")
            return
        if self.state == TRANSCRIBING:
            return  # 連打対策: 認識中は無視
        if self.state == IDLE:
            self._start_recording()
        elif self.state == RECORDING:
            self._stop_and_transcribe()

    def _start_recording(self):
        self._record_context = ctx.get_active_window()
        import ctypes
        self._record_hwnd = ctypes.windll.user32.GetForegroundWindow()
        try:
            self.recorder.start()
        except Exception as e:
            # PortAudio の生の英語メッセージをそのまま見せない
            # （OSError 以外にも PyAudio 初期化時の例外がありうるので広く捕まえる）
            injector.log.exception("マイクを開けませんでした")
            self.sig_message.emit("MO Voice", _mic_error_message(e))
            return
        self.state = RECORDING
        self._record_started = time.time()
        self.sig_state.emit(RECORDING)
        self._max_timer.start(self.config.get("max_record_seconds", 60) * 1000)

    def _auto_stop(self):
        if self.state == RECORDING:
            self._stop_and_transcribe()

    def _on_cancel(self):
        if self.state == RECORDING:
            self._max_timer.stop()
            self.recorder.cancel()
            self.state = IDLE
            self.sig_state.emit(IDLE)

    def _stop_and_transcribe(self):
        self._max_timer.stop()
        audio = self.recorder.stop()
        device_lost = self.recorder.device_lost
        duration = time.time() - self._record_started
        self.state = TRANSCRIBING
        self.sig_state.emit(TRANSCRIBING)

        if device_lost:
            # マイクが切断された等で録音が途中終了。ここまでの音声を
            # 「正常な認識結果」として扱うと誤ったテキストが注入されうるため中止する
            self.state = IDLE
            self.sig_state.emit(IDLE)
            self.sig_message.emit(
                "MO Voice", "マイクが切断されたため録音を中止しました")
            return

        exe, title = self._record_context
        categories = ctx.resolve_categories(
            self.config.get("context_rules", []), exe, title)
        # 語彙は initial_prompt で渡す。hotwords は全ウィンドウに毎回223トークンを
        # 渡すため認識時間が2.5倍に悪化し（60秒音声で14.2秒→36.5秒・実測）、
        # かつ認識品質に有意差がなかったため不採用（2026-08-22 検証）
        prompt = self.prompt_builder.build_prompt(
            self.vocabulary, self.corrections, categories,
            use_vault_vocab=self.config.get("use_vault_vocab", False))

        def _work():
            audio_file = self._save_audio(audio)
            try:
                text = self.transcriber.transcribe(audio, initial_prompt=prompt)
                text = self.corrections.apply(text)
                # 低信頼語の抽出（誤りの可能性がある箇所の可視化用）。
                # 自動置換はしない: 探索的な自動修正は precision 25% で棄却済み。
                # ダイアログで見せるだけなら誤検出の被害は「1回余計に開く」だけ
                self._last_low_words = self._pick_low_confidence_words()
            except Exception as e:
                text = ""
                self._last_low_words = []
                self.sig_message.emit("MO Voice", f"認識エラー: {e}")
            self._log_history(text, exe, title, duration, categories,
                              audio_file)
            self.sig_transcribed.emit(text)
        threading.Thread(target=_work, daemon=True).start()

    def _pick_low_confidence_words(self):
        """直近の認識から信頼度が閾値未満の「区間」を抽出する。

        日本語のWhisperトークンは大半が1文字単位のため、単語単体で見ると
        ほぼすべて1文字になる。連続する低信頼トークンを結合して区間として扱い、
        結合後2文字以上のものだけを返す（「P」+「ytho」+「n」→「Python」）。
        孤立した1文字の低信頼はセグメント先頭バイアス等の誤検出が多く、
        ハイライトしても文中の同じ文字が全部黄色になるだけなので拾わない。
        """
        cfg = self.config.get("confidence_highlight", {})
        if not cfg.get("enabled", True):
            return []
        threshold = cfg.get("threshold", 0.6)
        spans, current = [], []
        for w in getattr(self.transcriber, "last_words", []):
            word = w.get("word") or ""
            if word.strip() and w.get("probability", 1.0) < threshold:
                current.append(word)
            else:
                if current:
                    spans.append("".join(current).strip())
                    current = []
        if current:
            spans.append("".join(current).strip())
        # 2文字以上・重複除去（順序維持）
        seen, out = set(), []
        for s in spans:
            if len(s) >= 2 and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def _save_audio(self, audio) -> str:
        """発話音声を評価用にwav保存する（直近AUDIO_KEEP件のリングバッファ）。

        corrections_log.jsonl（修正前後の全文）と突合すると「音声＋正解テキスト」の
        テストセットが自動で得られる。改善施策のA/B比較に使う。
        失敗しても認識処理は妨げない。
        """
        try:
            import wave
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            name = time.strftime("%Y%m%d_%H%M%S") + ".wav"
            path = AUDIO_DIR / name
            pcm = (audio * 32767.0).clip(-32768, 32767).astype("int16")
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm.tobytes())
            # 古いものから削除して直近AUDIO_KEEP件のみ残す
            files = sorted(AUDIO_DIR.glob("*.wav"))
            for old in files[:-AUDIO_KEEP]:
                old.unlink(missing_ok=True)
            return name
        except Exception:
            return ""

    def _on_transcribed(self, text):
        # Qtスロット内の例外はstderrにしか出ず、起動方法によっては闇に消える。
        # 「注入が無言で行われない」事故の診断のため必ずログに残す
        try:
            self._on_transcribed_inner(text)
        except Exception:
            injector.log.exception("_on_transcribed で例外")
            self.state = IDLE
            self.sig_state.emit(IDLE)
            injector._set_clipboard_text(text)
            self.sig_message.emit(
                "MO Voice", "内部エラーで自動貼り付けに失敗しました。"
                "テキストはコピー済みです（Ctrl+V で貼り付け）")

    def _on_transcribed_inner(self, text):
        injector.log.info("on_transcribed: text=%r", (text or "")[:30])
        self.state = IDLE
        self.sig_state.emit(IDLE)
        if not text:
            self.sig_message.emit("MO Voice", "音声を認識できませんでした")
            return
        self.last_result = text

        # 注入先ガード: 録音開始時と別ウィンドウなら注入せず通知のみ
        exe_now, _ = ctx.get_active_window()
        exe_rec = self._record_context[0]
        if exe_rec and exe_now and exe_now != exe_rec:
            # 録音中〜認識中にウィンドウが切り替わった。長い発話ほど認識に
            # 時間がかかり、その間の自然なウィンドウ切り替えで頻発するため、
            # 保留ではなく録音開始時のウィンドウへフォーカスを戻して注入する
            injector.log.info(
                "注入先変更を検知: 録音時=%s 現在=%s → 元ウィンドウへ復帰注入を試行",
                exe_rec, exe_now)
            if injector.focus_window(self._record_hwnd):
                if injector.inject(
                        text, self.config.get("injection_method", "clipboard")):
                    self.last_injected_text = text
                    self.last_inject_hwnd = self._record_hwnd
                    self.last_inject_time = time.time()
                    self.last_result_injected = True
                    self.sig_message.emit(
                        "MO Voice", "録音時のウィンドウに戻って貼り付けました")
                    low = getattr(self, "_last_low_words", [])
                    if low:
                        self.sig_suggest_correction.emit(low)
                    return
            # フォーカス復帰または注入に失敗 → 従来どおり保留
            injector.log.warning("復帰注入に失敗。クリップボード保留にフォールバック")
            self.last_result_injected = False
            injector._set_clipboard_text(text)
            self.sig_message.emit(
                "MO Voice", "ウィンドウが変わったため貼り付けを保留しました。"
                "クリップボードにコピー済みです（Ctrl+V で貼り付け）")
            return
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        self.last_result_injected = False
        if injector.inject(text, self.config.get("injection_method", "clipboard")):
            self.last_injected_text = text
            self.last_inject_hwnd = hwnd
            self.last_inject_time = time.time()
            self.last_result_injected = True
            # 低信頼語があれば修正ダイアログを自動で開く（ハイライト付き）。
            # 注入は完了済みなので、ダイアログを閉じるだけなら何も起きない
            low = getattr(self, "_last_low_words", [])
            if low:
                self.sig_suggest_correction.emit(low)

    # 置換前に選択内容を検証する方式のため、時間制限は安全弁として緩めでよい
    # （旧Backspace盲打ち方式は検証がなく60秒制限が必要で、時間切れの無言失敗が多発した）
    MAX_REPLACE_AGE_SEC = 900

    def replace_last_injection(self, corrected: str):
        """修正ダイアログの結果を、注入済みの入力欄のテキストにも反映する。

        注入先ウィンドウにフォーカスを戻し、注入分を選択して内容を検証してから
        修正文で置換する（injector.replace_tail）。検証に失敗した場合は入力欄を
        一切変更せず、修正文をクリップボードに積んで手動 Ctrl+V に委ねる。

        Returns: (ok: bool, reason: str)
        """
        old = self.last_injected_text
        was_injected = self.last_result_injected
        self.last_result = corrected
        if not was_injected:
            # 修正対象のテキストは入力欄に注入されていない（ガード保留等）。
            # 古い注入の置換を試みても意味がなく誤解を招くため、コピーのみ行う
            injector._set_clipboard_text(corrected)
            return False, "このテキストは入力欄に貼り付けられていません"
        if not old:
            return False, "直前の注入テキストがありません"
        if corrected == old:
            return True, ""  # 変更なし
        if time.time() - self.last_inject_time > self.MAX_REPLACE_AGE_SEC:
            injector._set_clipboard_text(corrected)
            return False, "注入から時間が経ちすぎています"
        if not injector.focus_window(self.last_inject_hwnd):
            injector._set_clipboard_text(corrected)
            return False, "入力欄のウィンドウにフォーカスを戻せませんでした"
        ok, reason = injector.replace_tail(old, corrected)
        if not ok:
            injector._set_clipboard_text(corrected)
            return False, reason
        self.last_injected_text = corrected
        self.last_inject_time = time.time()
        return True, ""

    def _log_history(self, text, exe, title, duration, categories,
                     audio_file=""):
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "text": text,
                "exe": exe,
                "title": title[:80],
                "categories": categories,
                "duration_sec": round(duration, 1),
                "audio_file": audio_file,
            }
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def log_correction(self, original, corrected, learned, replaced):
        """Ctrl+Alt+Z での修正内容（修正前後の全文・学習ペア）を書き出す。"""
        try:
            CORRECTIONS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "original": original,
                "corrected": corrected,
                "learned": [{"wrong": w, "right": r} for w, r in learned],
                "replaced": replaced,
            }
            with open(CORRECTIONS_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
