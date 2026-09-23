"""テキスト注入モジュール。クリップボード経由 Ctrl+V（元の内容は復元する）。"""
import ctypes
import logging
import time
from pathlib import Path

import win32clipboard
import win32con
import win32gui
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
    """指定ウィンドウを前面に戻す。

    2026-09-23: 以前は無条件に「Alt を押す → SetForegroundWindow → Alt を離す」を行っていた。
    対象が**既に前面のとき**、これは「Alt を単独で押して離した」操作になり、Electron 製の
    Obsidian ではメニューバーにキーボードフォーカスが移って、以後の Shift+← や Ctrl+C が
    エディタに届かなくなる（修正の反映が「選択内容を取得できなかった」で失敗する主因。
    認識直後にその場で修正する典型パターンがまさにこれ）。
    → 既に前面なら何もしない。前面化が必要なときも、まず AttachThreadInput（キーを送らない）で
    試し、それで駄目なときだけ Alt トリックに落とす。
    """
    try:
        user32 = ctypes.windll.user32
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        if user32.GetForegroundWindow() == hwnd:
            log.info("focus_window: hwnd=%s 既に前面（Alt は送らない）", hwnd)
            return True
        # 1) AttachThreadInput: 前面ウィンドウのスレッドに入力を結び付けてから前面化する
        try:
            fg = user32.GetForegroundWindow()
            fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            my_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            attached = bool(fg_tid) and fg_tid != my_tid and \
                bool(user32.AttachThreadInput(my_tid, fg_tid, True))
            try:
                user32.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(my_tid, fg_tid, False)
            time.sleep(0.15)
            if user32.GetForegroundWindow() == hwnd:
                log.info("focus_window: hwnd=%s ok=True (AttachThreadInput)", hwnd)
                return True
        except Exception as e:
            log.info("focus_window: AttachThreadInput 方式が失敗: %s", e)
        # 2) Alt トリック（対象が前面でないときだけなので、Alt の押下は元の前面ウィンドウに、
        #    解放は対象に届く。対象側では「Alt 単独」と扱われずメニューは開かない）
        user32.keybd_event(VK_MENU, 0, 0, 0)
        try:
            win32gui.SetForegroundWindow(hwnd)
        finally:
            user32.keybd_event(VK_MENU, 0, 2, 0)  # Alt解放
        time.sleep(0.25)
        ok = user32.GetForegroundWindow() == hwnd
        log.info("focus_window: hwnd=%s ok=%s (Alt)", hwnd, ok)
        return ok
    except Exception as e:
        log.error("focus_window failed: %s", e)
        return False


# 検証コピーの成否判定に使う番兵文字列（通常の入力欄に現れない不可視文字入り）
_VERIFY_SENTINEL = "⁣MOVoice_VERIFY⁣"
MAX_REPLACE_CHARS = 3000  # これより長い注入分の選択置換はしない（一括送出でも上限は設ける）


# ---- 選択キーの一括送出（2026-09-23）----
# 旧実装は Shift+← を1文字ずつ press/release し、間に 8ms 置いていた。500文字で5〜7秒かかり、
# その間カーソルが1文字ずつ戻っていく様子が見える（ユーザーが「修正箇所までさかのぼる時間が
# 無駄」と感じた正体）。SendInput に全キーイベントを1回で渡すと、アプリ側の処理だけになり
# 数百文字でも一瞬で選択が終わる。選択内容の検証（Ctrl+C）は従来どおり行うので、アプリが
# キーを取りこぼしても誤った置換にはならず、その場合は旧来の1文字ずつの方法でやり直す。

_ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", _ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT（最大32バイト）ぶんの領域を確保して sizeof(INPUT)=40 を Windows と一致させる
    _fields_ = [("ki", _KEYBDINPUT), ("_pad", ctypes.c_ubyte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


_INPUT_KEYBOARD = 1
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
VK_SHIFT = 0x10
VK_LEFT = 0x25
VK_RIGHT = 0x27


def _send_keys(events) -> bool:
    """[(仮想キー, 離す?)...] を1回の SendInput で送る。全件受理されれば True。"""
    n = len(events)
    arr = (_INPUT * n)()
    for i, (vk, up) in enumerate(events):
        arr[i].type = _INPUT_KEYBOARD
        arr[i].u.ki.wVk = vk
        flags = _KEYEVENTF_EXTENDEDKEY if vk in (VK_LEFT, VK_RIGHT) else 0
        if up:
            flags |= _KEYEVENTF_KEYUP
        arr[i].u.ki.dwFlags = flags
    return ctypes.windll.user32.SendInput(n, arr, ctypes.sizeof(_INPUT)) == n


def _select_left_fast(n: int) -> bool:
    """Shift を押したまま ← を n 回、まとめて送る。"""
    if not _send_keys([(VK_SHIFT, False)]):
        return False
    ok = True
    try:
        for i in range(0, n, 500):
            k = min(500, n - i)
            if not _send_keys([(VK_LEFT, False), (VK_LEFT, True)] * k):
                ok = False
                break
    finally:
        _send_keys([(VK_SHIFT, True)])
    # アプリ側がキーを処理し終えるのを待つ（Electron 等は数百文字で数百ms かかることがある）
    time.sleep(min(0.6, 0.05 + n * 0.0006))
    return ok


def _select_left_slow(n: int):
    """旧実装（1文字ずつ・8ms 間隔）。一括送出で選択内容が一致しなかったときの再試行用。"""
    with _kb.pressed(Key.shift):
        for _ in range(n):
            _kb.press(Key.left)
            _kb.release(Key.left)
            time.sleep(0.008)
    time.sleep(0.1)


def _send_group_left(k: int) -> bool:
    """Shift+Ctrl+← を k 回（語・文単位で選択を左へ広げる）。"""
    return _send_keys([(VK_SHIFT, False), (VK_CONTROL, False)]
                      + [(VK_LEFT, False), (VK_LEFT, True)] * k
                      + [(VK_CONTROL, True), (VK_SHIFT, True)])


def _send_shift_right(k: int) -> bool:
    """Shift+→ を k 回（後ろ向き選択の左端を右へ戻して縮める）。"""
    return _send_keys([(VK_SHIFT, False)]
                      + [(VK_RIGHT, False), (VK_RIGHT, True)] * k
                      + [(VK_SHIFT, True)])


def _lf(s: str) -> str:
    return (s or "").replace("\r\n", "\n").replace("\r", "\n")


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
    n_old = len(old_text)
    if n_old == 0:
        return False, "置換対象のテキストがありません"
    if n_old > MAX_REPLACE_CHARS:
        return False, f"注入テキストが長すぎます（{n_old}文字 > {MAX_REPLACE_CHARS}）"

    # 修正で変わっていない先頭部分は触らない（2026-09-23）。変わった位置から末尾だけを
    # 選択・置換する。文末近くの直しなら数十文字の選択で済み、旧実装のように注入分を
    # 全部さかのぼる必要がない。検証もその範囲で行う
    p = 0
    lim = min(n_old, len(new_text))
    while p < lim and old_text[p] == new_text[p]:
        p += 1
    if p >= n_old:
        # 末尾への追記だけ: 最後の1文字を選んで位置を検証し、その文字ごと貼り直す
        p = n_old - 1
    sel_text = old_text[p:]
    paste_text = new_text[p:]
    n = len(sel_text)
    expected = _normalize_field_text(sel_text)

    _wait_modifiers_released()
    time.sleep(0.05)
    t0 = time.time()
    backup = _get_clipboard_text()

    def _restore_backup():
        if backup is not None:
            _set_clipboard_text(backup)

    def _deselect():
        _kb.press(Key.right)
        _kb.release(Key.right)

    def _copy_selection():
        _set_clipboard_text(_VERIFY_SENTINEL)
        with _kb.pressed(Key.ctrl):
            time.sleep(0.03)  # Ctrl押下がアプリに届く前にCが着弾するのを防ぐ
            _kb.press('c')
            _kb.release('c')
        # Electronアプリ（Obsidian等）はコピーが非同期で、CPU負荷時は0.2秒では
        # クリップボードに反映されないことがある（実ログで誤判定を確認）。
        # sentinelから変わるまで最大2.5秒ポーリングする（旧1.5秒。実測で
        # 「選択内容を取得できなかった」が全失敗の4割弱を占めていたため延長した）
        # 2026-09-23: Obsidian（Electron）は選択キー1文字あたり数msかけて処理するため、
        # 数百文字の選択では Ctrl+C の処理が 2.5 秒より後になり「取得できなかった」に
        # 誤判定していた。選択文字数に応じて待ちを延ばす（500文字で +3 秒）
        got = None
        deadline = time.time() + 2.5 + 0.02 * n
        while time.time() < deadline:
            time.sleep(0.1)
            got = _get_clipboard_text()
            if got is not None and got != _VERIFY_SENTINEL:
                break
        return got

    def _fg_window_desc() -> str:
        """診断用: 失敗時にどの画面を対象にしていたかをログへ残す。
        タイトル・クラス名はカーソルが編集欄以外（サイドバー等）に
        移っていた可能性を後から切り分けるための手がかり。"""
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            return f"hwnd={hwnd} title={win32gui.GetWindowText(hwnd)!r} class={win32gui.GetClassName(hwnd)!r}"
        except Exception:
            return "hwnd取得失敗"

    def _select_by_groups():
        """Shift+Ctrl+← の語・文単位ジャンプで大まかに選び、コピーで実際の選択長を測って
        Shift+→ で余分を戻す（2026-09-23）。Obsidian（CodeMirror）は選択キー1文字あたり約13ms
        かかるため、数百文字を1文字ずつ選ぶと7〜8秒かかって検証が間に合わなかった。
        文単位（「。」まで）のジャンプならキー数が数十回で済む。ジャンプ幅がアプリごとに
        違っても、コピーで測って合わせるので選択範囲は正確。合わせられなければ None を返し、
        呼び出し側が従来の1文字ずつの方式に戻す。"""
        want = _lf(sel_text)
        k = 2
        for _ in range(14):
            _send_group_left(k)
            time.sleep(0.05 + 0.03 * k)
            got = _copy_selection()
            if got is None or got == _VERIFY_SENTINEL:
                return None
            g = _lf(got)
            if _normalize_field_text(g) == expected:
                return got
            if len(g) < len(want) and want.endswith(g):
                k = min(16, k * 2)           # まだ足りない: 歩幅を広げて続ける
                continue
            if len(g) > len(want) and g.endswith(want):
                over = len(g) - len(want)    # 行き過ぎ: 左端を右へ戻す
                if over > 400:
                    return None
                _send_shift_right(over)
                time.sleep(0.05 + 0.004 * over)
                got = _copy_selection()
                if got is not None and got != _VERIFY_SENTINEL \
                        and _normalize_field_text(got) == expected:
                    return got
                return None
            return None                      # 注入テキストの末尾にカーソルが無い等
        return None

    # 1) 語・文単位ジャンプ → 2) Shift+← 一括送出 → 3) 1文字ずつ、の順に試す。いずれも
    #    選択内容をコピーして注入テキストと照合するので、誤った範囲を置換することはない
    # 短い選択（40文字以下）はジャンプ方式だと行き過ぎの戻しとコピー回数で逆に遅い
    # （Qt 実測: 15文字で 2.3 秒 vs 一括送出 0.6 秒）ので、最初から一括送出にする
    got = _select_by_groups() if n > 40 else None
    method = "group"
    if got is None:
        _deselect()
        time.sleep(0.05)
        method = "burst"
        _select_left_fast(n)
        got = _copy_selection()
        if (got is not None and got != _VERIFY_SENTINEL
                and _normalize_field_text(got) != expected):
            log.warning("replace_tail: 一括選択の内容が不一致（%d文字）。1文字ずつで再試行", n)
            _deselect()
            time.sleep(0.05)
            method = "slow"
            _select_left_slow(n)
            got = _copy_selection()

    if got is None or got == _VERIFY_SENTINEL:
        # コピーが発生しなかった＝選択できていない（別のUI要素にフォーカス等）
        _deselect()
        _restore_backup()
        log.warning("replace_tail: 選択内容を取得できなかった (%s)", _fg_window_desc())
        return False, "入力欄の選択内容を取得できませんでした"

    if _normalize_field_text(got) != expected:
        # 中身が注入時と違う＝カーソル移動や手編集があった。触らず選択解除
        _deselect()
        _restore_backup()
        log.warning("replace_tail: 内容不一致 got=%r expected=%r (%s)",
                    got[:40], sel_text[:40], _fg_window_desc())
        return False, "入力欄の内容が注入時と一致しません（カーソル移動や編集の可能性）"

    # 検証OK: 選択部分を修正文で置換（修正後が空＝末尾の削除なら Delete）
    if paste_text:
        _set_clipboard_text(paste_text)
        with _kb.pressed(Key.ctrl):
            _kb.press('v')
            _kb.release('v')
    else:
        _kb.press(Key.delete)
        _kb.release(Key.delete)
    time.sleep(0.3)
    _restore_backup()
    log.info("replace_tail: 置換成功 %d→%d文字（先頭%d文字は共通で未選択・選択%d文字・%.2fs・%s）",
             n_old, len(new_text), p, n, time.time() - t0, method)
    return True, ""
