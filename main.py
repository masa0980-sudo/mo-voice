"""MO Voice - 常駐型音声ディクテーションアプリ

起動: python -X utf8 main.py
ホットキー: Ctrl+Alt+Space = 録音トグル / Ctrl+Alt+Z = 直前結果の修正（学習）
"""
import json
import sys
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

# 重要: PyQt5 より先に import しないと torch の DLL 初期化が失敗する
# (WinError 1114 / c10.dll)。Windows の DLL ロード順序問題。
from faster_whisper import WhisperModel  # noqa: F401

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon
from pynput import keyboard


class Invoker(QObject):
    """pynput スレッドから Qt スレッドへ処理を渡すためのシグナルホルダー。"""
    sig_correct = pyqtSignal()

from core.controller import DictationController
from core.transcriber import Transcriber
from ui.correction_dialog import CorrectionDialog
from ui.indicator import Indicator
from ui.tray import Tray
from vocab import prompt_builder
from vocab.corrections import Corrections
from vocab.scanner import load_vocabulary, scan_vault


# config.json が無い/壊れている場合に使う既定値。
# 起動不能にせず「とりあえず動く」状態にするのが目的（ホットキーとモデルさえ
# 決まっていれば音声入力は成立する）。
DEFAULT_CONFIG = {
    "hotkey_toggle": "<ctrl>+<alt>+<space>",
    "hotkey_correct": "<ctrl>+<alt>+z",
    "model": "small",
    "compute_type": "int8",
    "language": "ja",
    "beam_size": 2,
    "max_record_seconds": 300,
    "injection_method": "clipboard",
    "confidence_highlight": {"enabled": True, "threshold": 0.6},
    "use_vault_vocab": False,
    "vault_path": "",
    "context_rules": [{"exe": "", "title": "", "categories": ["global"]}],
}


def load_config():
    """config.json を読む。無い/壊れている場合も落ちずに既定値で起動する。

    Returns: (config, warning)  warning は問題があった場合の日本語メッセージ
    """
    path = APP_DIR / "config.json"
    if not path.exists():
        return dict(DEFAULT_CONFIG), (
            "config.json が見つからないため既定設定で起動しました")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return dict(DEFAULT_CONFIG), (
            f"config.json を読めないため既定設定で起動しました（{e}）")
    except OSError as e:
        return dict(DEFAULT_CONFIG), (
            f"config.json を開けないため既定設定で起動しました（{e}）")
    if not isinstance(loaded, dict):
        return dict(DEFAULT_CONFIG), (
            "config.json の形式が不正なため既定設定で起動しました")
    # 欠けているキーは既定値で補う（部分的に壊れた設定でも起動できるように）
    config = dict(DEFAULT_CONFIG)
    config.update(loaded)
    missing = [k for k in DEFAULT_CONFIG if k not in loaded]
    warning = ""
    if missing:
        warning = f"config.json に未設定の項目があります（既定値を使用: {', '.join(missing)}）"
    return config, warning


_MUTEX_NAME = "Global\\MOVoice_SingleInstance_Mutex"


def _acquire_single_instance_lock():
    """多重起動を防ぐ。既に起動中なら False を返す（プロセスクラッシュ時も
    OSがMutexを自動解放するため、ロックファイル方式と違い残留しない）。"""
    import win32api
    import win32event
    import winerror
    handle = win32event.CreateMutex(None, False, _MUTEX_NAME)
    already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    return (not already_running), handle


def _raise_priority():
    """他の常駐ソフト（OneDrive/Teams/Trend Micro等）と起動時に競合しても
    優先的にCPU・ディスクI/Oをもらえるようにする。"""
    try:
        import psutil
        p = psutil.Process()
        p.nice(psutil.HIGH_PRIORITY_CLASS)
        p.ionice(psutil.IOPRIO_HIGH)
    except Exception:
        pass


def main():
    _raise_priority()
    got_lock, _mutex_handle = _acquire_single_instance_lock()
    if not got_lock:
        print("MO Voice は既に起動しています", file=sys.stderr)
        sys.exit(0)

    config, config_warning = load_config()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("システムトレイが利用できません", file=sys.stderr)
        sys.exit(1)

    transcriber = Transcriber(
        model_name=config.get("model", "small"),
        compute_type=config.get("compute_type", "int8"),
        language=config.get("language", "ja"),
        beam_size=config.get("beam_size", 2),
    )
    vocabulary = load_vocabulary()
    corrections = Corrections()

    # Obsidian 連携は任意。未設定でも corrections（修正学習）だけで動作する
    _vp = (config.get("vault_path") or "").strip()
    vault_configured = bool(_vp) and Path(_vp).is_dir()

    controller = DictationController(
        config, transcriber, vocabulary, corrections, prompt_builder)

    indicator = Indicator(on_cancel=controller.sig_cancel.emit)

    def open_correction(highlight_words=None):
        try:
            _open_correction_inner(highlight_words)
        except Exception:
            from core import injector
            injector.log.exception("open_correction で例外")
            tray.notify("MO Voice", "修正ダイアログ処理で内部エラーが発生しました")

    def _open_correction_inner(highlight_words=None):
        if not controller.last_result:
            tray.notify("MO Voice", "修正できる認識結果がまだありません")
            return
        original = controller.last_result
        dlg = CorrectionDialog(original, highlight_words=highlight_words)
        if dlg.exec_():
            corrected = dlg.corrected_text()
            learned = corrections.learn(original, corrected)
            replaced, fail_reason = controller.replace_last_injection(corrected)
            controller.log_correction(original, corrected, learned, replaced)
            msgs = []
            if learned:
                pairs = " / ".join(f"「{w}」→「{r}」" for w, r in learned)
                msgs.append(f"学習: {pairs}")
            if replaced and corrected != original:
                msgs.append("入力欄のテキストも置き換えました")
            elif not replaced and corrected != original:
                # 自動反映に失敗しても修正文はクリップボードに積んである
                # （controller側でフォールバック済み）ので手動で貼れる
                msgs.append(
                    f"入力欄への自動反映は失敗（{fail_reason}）。"
                    "修正文をコピー済みなので Ctrl+V で貼り付けてください")
            tray.notify("MO Voice", " / ".join(msgs) if msgs
                        else "差分から学習できる置換はありませんでした")

    _rescan_lock = threading.Lock()

    def rescan_vault():
        # Obsidian を使わないユーザーでも落ちないよう、未設定・不正パスは
        # スキャンせず理由を通知して終わる（旧実装は config["vault_path"] で
        # KeyError を投げ、バックグラウンドスレッド内のため無言で死んでいた）
        if not vault_configured:
            tray.notify(
                "MO Voice",
                "Obsidian 連携は未設定です（config.json の vault_path を"
                "設定すると語彙ヒントが有効になります）")
            return
        if not _rescan_lock.acquire(blocking=False):
            tray.notify("MO Voice", "vault は既にスキャン中です")
            return
        tray.notify("MO Voice", "vault をスキャン中...")
        def _scan():
            try:
                result = scan_vault(config.get("vault_path", ""))
                if result.get("error"):
                    tray.notify("MO Voice", f"スキャンできません: {result['error']}")
                    return
                vocabulary.clear()
                vocabulary.update(load_vocabulary())
                tray.notify(
                    "MO Voice",
                    f"スキャン完了: {result['file_count']}ファイル / "
                    f"{len(result['terms'])}語彙")
            finally:
                _rescan_lock.release()
        threading.Thread(target=_scan, daemon=True).start()

    def quit_app():
        controller.sig_cancel.emit()  # 録音中ならマイクストリームを閉じる
        hotkeys.stop()
        app.quit()

    tray = Tray(on_rescan=rescan_vault, on_correct=open_correction,
                on_quit=quit_app, vault_configured=vault_configured,
                on_retry_load=controller.retry_model_load)

    # 状態 → UI 反映
    def on_state(state):
        tray.set_state(state)
        if state == "recording":
            indicator.show_recording()
        elif state == "transcribing":
            indicator.show_transcribing()
        else:
            indicator.hide_all()

    controller.sig_state.connect(on_state)
    controller.sig_message.connect(tray.notify)
    # 低信頼語があった場合、注入後に修正ダイアログを自動で開く（該当語をハイライト）
    controller.sig_suggest_correction.connect(
        lambda words: open_correction(highlight_words=words))

    # config.json に問題があった場合はユーザーに知らせる（既定値で起動済み）
    if config_warning:
        from core import injector
        injector.log.warning("config: %s", config_warning)
        QTimer.singleShot(1500, lambda: tray.notify("MO Voice", config_warning))

    # グローバルホットキー（pynput スレッド → Qt シグナル経由で安全に実行）
    invoker = Invoker()
    invoker.sig_correct.connect(open_correction)
    hotkeys = keyboard.GlobalHotKeys({
        config.get("hotkey_toggle", "<ctrl>+<alt>+<space>"):
            controller.sig_toggle.emit,
        config.get("hotkey_correct", "<ctrl>+<alt>+z"):
            invoker.sig_correct.emit,
    })
    hotkeys.start()
    controller.start_model_load()

    # 初回起動時に語彙が空なら自動スキャン
    if not vocabulary.get("terms"):
        rescan_vault()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
