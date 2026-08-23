"""アクティブウィンドウから語彙カテゴリを判定するモジュール。"""
import logging
import re

import psutil
import win32gui
import win32process

log = logging.getLogger("injector")  # injector と同じログファイルに出す


def get_active_window():
    """(exe名小文字, ウィンドウタイトル) を返す。取得失敗時は ('', '')。

    取得できなくても録音自体は続行できるため例外は握るが、注入先ガードの
    判定材料になる情報なので、無言にせずログには残す。
    """
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd) or ""
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = psutil.Process(pid).name().lower()
        return exe, title
    except Exception as e:
        log.warning("アクティブウィンドウを取得できませんでした: %s", e)
        return "", ""


def resolve_categories(context_rules, exe: str, title: str):
    """config の context_rules を順にマッチして categories を返す。"""
    for rule in context_rules:
        exe_pat = rule.get("exe", "")
        title_pat = rule.get("title", "")
        if exe_pat and exe_pat.lower() not in exe:
            continue
        if title_pat and not re.search(title_pat, title, re.IGNORECASE):
            continue
        return rule.get("categories", ["global"])
    return ["global"]
