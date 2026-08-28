# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller ビルド定義（Windows / onedir）。

使い方:
    .venv\\Scripts\\pyinstaller.exe packaging\\MOVoice.spec --noconfirm

なぜ onedir か（--onefile ではない）:
    --onefile は起動のたびに数百MBを一時フォルダへ展開するため、起動が
    数十秒単位で遅くなる。このアプリは「ホットキーを押したらすぐ使える」
    ことが価値なので、起動時間を犠牲にする選択はしない。

exe 名を MOVoice にしている理由:
    Windows の通知領域は実行ファイル単位でアイコンを管理する。python.exe の
    ままだと「タスクバーの設定 > その他のシステム トレイ アイコン」の一覧に
    "python" と表示され、他の Python 製常駐アプリと区別が付かない。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH).parent

datas = []
# faster-whisper は VAD 用の onnx モデルを同梱している。vad_filter=True で
# 使うため、これが無いと録音のたびに実行時エラーになる
datas += collect_data_files("faster_whisper")
# pykakasi は読み仮名の辞書を .db として持つ。無いと読み一致の置換が落ちる
datas += collect_data_files("pykakasi")

binaries = []
# ctranslate2 / onnxruntime はネイティブ DLL を自前で持つ。フックだけでは
# 拾い切れないことがあるため明示的に集める
binaries += collect_dynamic_libs("ctranslate2")
binaries += collect_dynamic_libs("onnxruntime")

hiddenimports = [
    # pynput はプラットフォーム実装を実行時に動的 import する
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    # pywin32
    "win32gui", "win32process", "win32clipboard", "win32con",
    "win32api", "win32event", "winerror",
    "psutil",
    "pyaudio",
]

# 明らかに使わない大物を外してサイズを削る。PyQt5 は QtCore/QtGui/QtWidgets
# しか使っていないのに、既定では Qt の全モジュールが候補に入る
excludes = [
    "tkinter", "unittest", "pydoc", "doctest", "test",
    "matplotlib", "pandas", "scipy", "IPython", "notebook",
    "torch", "transformers", "tensorflow",
    "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngineCore", "PyQt5.QtWebEngine",
    "PyQt5.QtQml", "PyQt5.QtQuick", "PyQt5.QtQuick3D", "PyQt5.QtQuickWidgets",
    "PyQt5.QtMultimedia", "PyQt5.QtMultimediaWidgets",
    "PyQt5.QtBluetooth", "PyQt5.QtNfc", "PyQt5.QtPositioning",
    "PyQt5.QtLocation", "PyQt5.QtSerialPort", "PyQt5.QtSensors",
    "PyQt5.QtWebSockets", "PyQt5.QtWebChannel", "PyQt5.QtXmlPatterns",
    "PyQt5.QtHelp", "PyQt5.QtDesigner", "PyQt5.QtTest", "PyQt5.QtSql",
    "PyQt5.Qt3DCore", "PyQt5.Qt3DRender", "PyQt5.QtCharts",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MOVoice",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # コンソール窓を出さない。常駐アプリなので黒い窓が残ると邪魔になる
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "movoice.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MOVoice",
)
