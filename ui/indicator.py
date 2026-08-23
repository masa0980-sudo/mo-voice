"""録音中インジケーター: 画面下部中央のピル型オーバーレイ。クリックでキャンセル。"""
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QPainter
from PyQt5.QtWidgets import QApplication, QWidget

WIDTH, HEIGHT = 220, 44


class Indicator(QWidget):
    def __init__(self, on_cancel=None):
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 重要: 表示時にフォーカスを奪うと Ctrl+V の注入先が入力欄からずれる
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(WIDTH, HEIGHT)
        self.on_cancel = on_cancel
        self.mode = "recording"  # recording / transcribing
        self.elapsed = 0
        self._blink = True

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.center().x() - WIDTH // 2,
                  screen.bottom() - HEIGHT - 60)

    def show_recording(self):
        self.mode = "recording"
        self.elapsed = 0
        self._blink = True
        self._timer.start(500)
        self.show()

    def show_transcribing(self):
        self.mode = "transcribing"
        self.update()

    def hide_all(self):
        self._timer.stop()
        self.hide()

    def _tick(self):
        self._blink = not self._blink
        if self._blink:
            self.elapsed += 1
        self.update()

    def mousePressEvent(self, _event):
        if self.mode == "recording" and self.on_cancel:
            self.on_cancel()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 20, 24, 230))
        p.drawRoundedRect(self.rect(), HEIGHT // 2, HEIGHT // 2)

        p.setFont(QFont("Meiryo", 10))
        if self.mode == "recording":
            if self._blink:
                p.setBrush(QColor(235, 60, 60))
                p.drawEllipse(16, HEIGHT // 2 - 6, 12, 12)
            p.setPen(QColor(255, 255, 255))
            p.drawText(self.rect().adjusted(38, 0, -10, 0),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       f"録音中 {self.elapsed}s（クリックで中止）")
        else:
            p.setBrush(QColor(80, 150, 255))
            p.drawEllipse(16, HEIGHT // 2 - 6, 12, 12)
            p.setPen(QColor(255, 255, 255))
            p.drawText(self.rect().adjusted(38, 0, -10, 0),
                       Qt.AlignVCenter | Qt.AlignLeft, "認識中...")
