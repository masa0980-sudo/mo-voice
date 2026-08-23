"""テキスト注入モジュール。クリップボード経由 Ctrl+V（元の内容は復元する）。"""
import ctypes
import logging
import time
from pathlib import Path

import win32clipboard
import win32con
from pynput.keyboard import Controller as KeyController, Key

log = logging.getLogger("injector")
_log_file = Path(__file__).resolve().parent.parent / "data" / "app.log"
_log_file.parent.mkdir(parents=True, exist_ok=True)
_handler = logging.FileHandler(_log_file, encoding="utf-8")
_handler.setFormatter(
    logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
log.addHandler(_handler)
log.setLevel(logging.INFO)

VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_SPACE = 0x20
VK_Z = 0x5A

_kb = KeyController()


def _get_clipboard_text():
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                return None
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)
    return None


def _clipboard_is_text_safe() -> bool:
    """クリップボードが空、またはテキスト形式を含む場合 True を返す。

    画像・ファイル等テキスト形式を持たないデータがクリップボードにある場合は
    False を返す。その場合クリップボード経由の注入を使うと EmptyClipboard() で
    元データが失われ、テキストしか保存しない _get_clipboard_text() では
    復元できない（無言のデータ消失事故になる）。呼び出し側は SendInput 方式に
    フォールバックしてクリップボードに一切触れないようにする。
    """
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.CountClipboardFormats() == 0:
                    return True
                return bool(win32clipboard.IsClipboardFormatAvailable(
                    win32con.CF_UNICODETEXT))
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)
    return True  # 判定不能なら従来どおりクリップボード方式を試みる


def _set_clipboard_text(text):
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                return True
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)
    return False


def _wait_modifiers_released(timeout=0.5):
    """ホットキーの Ctrl/Alt/Space/Z が離されるまで待つ（貼り付け誤動作防止）。"""
    deadline = time.time() + timeout
    keys = (VK_CONTROL, VK_MENU, VK_SPACE, VK_Z)
    while time.time() < deadline:
        if not any(ctypes.windll.user32.GetAsyncKeyState(k) & 0x8000 for k in keys):
            return True
        time.sleep(0.02)
    return False


def inject_clipboard(text: str) -> bool:
    """クリップボードに text をセットして Ctrl+V、その後元の内容を復元する。

    クリップボードに画像・ファイル等テキスト以外のデータがある場合は、
    それを壊さないよう SendInput 方式にフォールバックする。
    """
    if not text:
        return False
    if not _clipboard_is_text_safe():
        log.warning(
            "クリップボードに非テキストデータがあるため SendInput にフォールバック"
            "（データ破壊防止）")
        return inject_sendinput(text)
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    log.info("inject start: fg_hwnd=%s text=%r", hwnd, text[:40])
    backup = _get_clipboard_text()
    if not _set_clipboard_text(text):
        log.error("クリップボードへのセットに失敗")
        return False
    released = _wait_modifiers_released()
    if not released:
        log.warning("修飾キーが解放されないまま貼り付けを実行")
    time.sleep(0.05)
    with _kb.pressed(Key.ctrl):
        _kb.press('v')
        _kb.release('v')
    time.sleep(0.3)
    hwnd_after = ctypes.windll.user32.GetForegroundWindow()
    log.info("inject done: fg_hwnd_after=%s (同一=%s)", hwnd_after,
             hwnd == hwnd_after)
    if hwnd != hwnd_after:
        # 貼り付けが届いていない可能性が高い → 認識テキストをクリップボードに残す
        log.warning("フォーカスが移動していたためクリップボード復元をスキップ")
        return False
    if backup is not None:
        _set_clipboard_text(backup)
    return True


def inject_sendinput(text: str) -> bool:
    """pynput の type() で直接タイプする（フォールバック用）。"""
    if not text:
        return False
    _wait_modifiers_released()
    time.sleep(0.05)
    _kb.type(text)
    return True


def inject(text: str, method: str = "clipboard") -> bool:
    if method == "sendinput":
        return inject_sendinput(text)
    return inject_clipboard(text)


def focus_window(hwnd) -> bool:
    """指定ウィンドウを前面に戻す（Altキー同時押しで SetForegroundWindow 制限を回避）。"""
    try:
        import win32gui
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        ctypes.windll.user32.keybd_event(VK_MENU, 0, 0, 0)
        try:
            win32gui.SetForegroundWindow(hwnd)
        finally:
            ctypes.windll.user32.keybd_event(VK_MENU, 0, 2, 0)  # Alt解放
        time.sleep(0.25)
        ok = ctypes.windll.user32.GetForegroundWindow() == hwnd
        log.info("focus_window: hwnd=%s ok=%s", hwnd, ok)
        return ok
    except Exception as e:
        log.error("focus_window failed: %s", e)
        return False


# 検証コピーの成否判定に使う番兵文字列（通常の入力欄に現れない不可視文字入り）
_VERIFY_SENTINEL = "⁣MOVoice_VERIFY⁣"
MAX_REPLACE_CHARS = 500  # これより長い注入分の選択置換はしない（Shift+←連打の時間上限）


def _normalize_field_text(s: str) -> str:
    """入力欄からコピーしたテキストと注入テキストの比較用正規化。

    入力欄によっては改行がCRLFで返る・末尾に改行が付くことがあるため吸収する。
    """
    return (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def replace_tail(old_text: str, new_text: str):
    """カーソル直前にあるはずの old_text を検証してから new_text に置き換える。

    手順: Shift+← を old_text の文字数ぶん送って選択 → Ctrl+C で選択内容を取得 →
    old_text と一致した場合のみ Ctrl+V で new_text に置換。一致しなければ
    選択を解除して何も変更しない（カーソル移動・手編集後の誤破壊を防ぐ）。

    旧実装（Backspace盲打ち）はカーソル位置を検証しないため60秒の時間制限で
    誤爆を防ぐしかなく、時間切れで無言で失敗することが多かった。
    検証方式なら時間が経っていても安全に試せる。

    Returns: (ok: bool, reason: str)  失敗時 reason に日本語の理由
    """
    n = len(old_text)
    if n == 0:
        return False, "置換対象のテキストがありません"
    if n > MAX_REPLACE_CHARS:
        return False, f"注入テキストが長すぎます（{n}文字 > {MAX_REPLACE_CHARS}）"

    _wait_modifiers_released()
    time.sleep(0.05)

    # Shift+← で注入分を後ろから選択
    with _kb.pressed(Key.shift):
        for _ in range(n):
            _kb.press(Key.left)
            _kb.release(Key.left)
            time.sleep(0.008)
    time.sleep(0.1)

    backup = _get_clipboard_text()
    _set_clipboard_text(_VERIFY_SENTINEL)
    with _kb.pressed(Key.ctrl):
        time.sleep(0.03)  # Ctrl押下がアプリに届く前にCが着弾するのを防ぐ
        _kb.press('c')
        _kb.release('c')
    # Electronアプリ（Obsidian等）はコピーが非同期で、CPU負荷時は0.2秒では
    # クリップボードに反映されないことがある（実ログで誤判定を確認）。
    # sentinelから変わるまで最大1.5秒ポーリングする
    got = None
    deadline = time.time() + 1.5
    while time.time() < deadline:
        time.sleep(0.1)
        got = _get_clipboard_text()
        if got is not None and got != _VERIFY_SENTINEL:
            break

    def _restore_backup():
        if backup is not None:
            _set_clipboard_text(backup)

    if got is None or got == _VERIFY_SENTINEL:
        # コピーが発生しなかった＝選択できていない（別のUI要素にフォーカス等）
        _kb.press(Key.right)
        _kb.release(Key.right)
        _restore_backup()
        log.warning("replace_tail: 選択内容を取得できなかった")
        return False, "入力欄の選択内容を取得できませんでした"

    if _normalize_field_text(got) != _normalize_field_text(old_text):
        # 中身が注入時と違う＝カーソル移動や手編集があった。触らず選択解除
        _kb.press(Key.right)
        _kb.release(Key.right)
        _restore_backup()
        log.warning("replace_tail: 内容不一致 got=%r expected=%r",
                    got[:40], old_text[:40])
        return False, "入力欄の内容が注入時と一致しません（カーソル移動や編集の可能性）"

    # 検証OK: 選択部分を修正文で置換
    _set_clipboard_text(new_text)
    with _kb.pressed(Key.ctrl):
        _kb.press('v')
        _kb.release('v')
    time.sleep(0.3)
    _restore_backup()
    log.info("replace_tail: 置換成功 %d→%d文字", n, len(new_text))
    return True, ""
