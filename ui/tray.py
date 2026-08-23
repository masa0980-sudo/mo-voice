"""タスクトレイ常駐 UI。状態色付きアイコン＋メニュー（再スキャン/修正/終了）。"""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import QAction, QMenu, QSystemTrayIcon

STATE_COLORS = {
    "loading": QColor(150, 150, 150),
    "idle": QColor(90, 200, 120),
    "recording": QColor(235, 60, 60),
    "transcribing": QColor(80, 150, 255),
    "failed": QColor(200, 120, 40),   # モデル読み込み失敗（要リトライ）
}


def _make_icon(color: QColor) -> QIcon:
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawEllipse(4, 4, 24, 24)
    # マイクっぽい白い縦棒
    p.setBrush(QColor(255, 255, 255))
    p.drawRoundedRect(13, 9, 6, 11, 3, 3)
    p.drawRect(15, 20, 2, 5)
    p.end()
    return QIcon(pm)


class Tray(QSystemTrayIcon):
    def __init__(self, on_rescan, on_correct, on_quit, parent=None,
                 vault_configured=True, on_retry_load=None):
        super().__init__(parent)
        self._icons = {k: _make_icon(c) for k, c in STATE_COLORS.items()}
        self.setIcon(self._icons["loading"])
        self.setToolTip("MO Voice - モデルロード中...")

        menu = QMenu()
        act_correct = QAction("直前の結果を修正して学習 (Ctrl+Alt+Z)", menu)
        act_correct.triggered.connect(on_correct)
        menu.addAction(act_correct)
        # モデルのDL失敗（オフライン等）から復帰する手段。これが無いと
        # LOADING のまま固着して再起動以外に手がなくなる
        self._act_retry = QAction("モデルを再読み込み", menu)
        if on_retry_load is not None:
            self._act_retry.triggered.connect(on_retry_load)
        self._act_retry.setVisible(False)  # 失敗時のみ表示
        menu.addAction(self._act_retry)
        # Obsidian を使わないユーザーには押しても0語彙で終わる操作を見せない
        if vault_configured:
            act_rescan = QAction("Obsidian vault を再スキャン", menu)
            act_rescan.triggered.connect(on_rescan)
        else:
            act_rescan = QAction("Obsidian vault を再スキャン（未設定）", menu)
            act_rescan.setEnabled(False)
            act_rescan.setToolTip(
                "config.json の vault_path を設定すると有効になります")
        menu.addAction(act_rescan)
        menu.addSeparator()
        act_quit = QAction("終了", menu)
        act_quit.triggered.connect(on_quit)
        menu.addAction(act_quit)
        self.setContextMenu(menu)
        self.show()

    def set_state(self, state: str):
        self.setIcon(self._icons.get(state, self._icons["idle"]))
        tips = {
            "loading": "MO Voice - モデルロード中...",
            "idle": "MO Voice - 待機中（Ctrl+Alt+Space で録音）",
            "recording": "MO Voice - 録音中",
            "transcribing": "MO Voice - 認識中",
            "failed": "MO Voice - モデル読み込み失敗（メニューから再読み込み）",
        }
        self.setToolTip(tips.get(state, "MO Voice"))
        self._act_retry.setVisible(state == "failed")

    def notify(self, title: str, body: str):
        self.showMessage(title, body, QSystemTrayIcon.Information, 4000)
