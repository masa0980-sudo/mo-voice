"""タスクトレイ常駐 UI。状態色付きアイコン＋メニュー（再スキャン/修正/終了）。

アイコンの設計方針（2026-08-28 に作り直し）:
    旧アイコンは「小さな円＋細い白い棒」で、16px に縮むとただの色付きの点に
    なってしまい、通知領域に並ぶ他のアプリと見分けがつかなかった。
    - 形で識別できるよう **角丸の四角** にした（トレイは円形アイコンが多い）
    - マイクの絵を余白いっぱいまで大きくし、線を太くした
    - 16/20/24/32/48/64px を **それぞれ実寸で描き分ける**。1枚を縮小すると
      細部が潰れるため
"""
from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QAction, QMenu, QSystemTrayIcon

STATE_COLORS = {
    "loading": QColor(140, 148, 158),
    "idle": QColor(34, 160, 90),
    "recording": QColor(225, 45, 45),
    "transcribing": QColor(40, 120, 235),
    "failed": QColor(205, 110, 20),   # モデル読み込み失敗（要リトライ）
}

# 通知領域が白背景でも輪郭が消えないよう、背景色を少し暗くした枠線を引く
_EDGE_DARKEN = 122


def _draw_icon(p: QPainter, color: QColor, s: float):
    """一辺 s の正方形にアイコンを描く（すべての寸法を s 基準で決める）。

    22px 未満では脚と台座を省き、カプセル＋U字だけにする。細部を残したまま
    小さく描くと線同士がにじんで一塊になり、かえって判別しづらくなるため
    （3倍に拡大して見比べて決めた）。
    """
    p.setRenderHint(QPainter.Antialiasing, True)
    simple = s < 22

    # 背景（角丸の四角）。トレイは円形アイコンが多いので形で見分けられるようにする
    pad = s * 0.04
    body = QRectF(pad, pad, s - pad * 2, s - pad * 2)
    p.setPen(QPen(color.darker(_EDGE_DARKEN), max(1.0, s * 0.03)))
    p.setBrush(color)
    p.drawRoundedRect(body, s * 0.24, s * 0.24)

    # マイク本体（カプセル）
    white = QColor(255, 255, 255)
    p.setPen(Qt.NoPen)
    p.setBrush(white)
    cap_w = s * 0.30
    cap_h = s * (0.46 if simple else 0.42)
    cap_y = s * (0.16 if simple else 0.17)
    p.drawRoundedRect(QRectF((s - cap_w) / 2, cap_y, cap_w, cap_h),
                      cap_w / 2, cap_w / 2)

    # マイクを受ける U 字。線を太くしないと 16px で消える
    stroke = max(1.6, s * 0.095)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(white, stroke, Qt.SolidLine, Qt.RoundCap))
    arc_w = s * 0.52
    arc = QRectF((s - arc_w) / 2, s * (0.36 if simple else 0.34),
                 arc_w, s * (0.42 if simple else 0.40))
    p.drawArc(arc, 200 * 16, 140 * 16)   # 下側の U 字（Qt の角度は反時計回り）

    if simple:
        return

    # スタンドの脚と台座
    p.setPen(Qt.NoPen)
    p.setBrush(white)
    leg_w = stroke * 0.9
    p.drawRect(QRectF((s - leg_w) / 2, s * 0.72, leg_w, s * 0.10))
    base_w = s * 0.38
    base_h = max(1.4, s * 0.085)
    p.drawRoundedRect(
        QRectF((s - base_w) / 2, s * 0.81, base_w, base_h),
        base_h / 2, base_h / 2)


# 16px 枠に入れる絵をどのサイズで描くか。
#
# 100% スケーリング（96 DPI）の Windows では通知領域が 16px を要求するため、
# 通常はその実寸で描いた絵が表示される。ただし 16px 直描きはマイクの
# U 字が1〜2ドットまで痩せてしまう。20px で描いてから 16px へ縮小すると、
# 曲線が中間調で表現されるぶん形が残る（実際に並べて比較して決めた）。
#
# 20 に変えると輪郭が少し柔らかくなる代わりに、マイクの丸みが分かりやすくなる。
# 16 に戻せば従来どおりドットがくっきりした見た目になる。
ICON_16_SOURCE = 16


def _render(color: QColor, size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    _draw_icon(p, color, float(size))
    p.end()
    return pm


def _make_icon(color: QColor) -> QIcon:
    """複数サイズを実寸で描き分けた QIcon を返す。

    16px だけは ICON_16_SOURCE のサイズで描いてから縮小する。
    """
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64):
        if size == 16 and ICON_16_SOURCE != 16:
            pm = _render(color, ICON_16_SOURCE).scaled(
                16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            pm = _render(color, size)
        icon.addPixmap(pm)
    return icon


class Tray(QSystemTrayIcon):
    def __init__(self, on_rescan, on_correct, on_quit, parent=None,
                 vault_configured=True, on_retry_load=None, on_settings=None):
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
        # Obsidian を使わないユーザーには押しても0語彙で終わる操作を見せない。
        # 設定ダイアログから vault_path を設定したら set_vault_configured() で
        # その場で有効化できるよう、Action を保持しておく
        self._act_rescan = QAction("Obsidian vault を再スキャン", menu)
        self._act_rescan.triggered.connect(on_rescan)
        menu.addAction(self._act_rescan)
        self.set_vault_configured(vault_configured)
        menu.addSeparator()

        if on_settings is not None:
            act_settings = QAction("設定...", menu)
            act_settings.triggered.connect(on_settings)
            menu.addAction(act_settings)
            menu.addSeparator()
        act_quit = QAction("終了", menu)
        act_quit.triggered.connect(on_quit)
        menu.addAction(act_quit)
        self.setContextMenu(menu)
        self.show()

    def set_vault_configured(self, configured: bool):
        """Obsidian 連携の有効／無効に応じて再スキャンメニューを切り替える。"""
        self._act_rescan.setEnabled(bool(configured))
        if configured:
            self._act_rescan.setText("Obsidian vault を再スキャン")
            self._act_rescan.setToolTip("")
        else:
            self._act_rescan.setText("Obsidian vault を再スキャン（未設定）")
            self._act_rescan.setToolTip(
                "「設定...」から vault のフォルダを指定すると有効になります")

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
