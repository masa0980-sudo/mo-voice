"""直前の認識結果を修正して学習させるダイアログ（Ctrl+Alt+Z で開く）。

低信頼語ハイライト（2026-08 追加）: Whisperの単語別信頼度が閾値未満だった語を
黄色背景で示す。認識精度そのものは変えず「怪しい箇所の見逃し」を減らすのが目的
（自動置換は探索タスクの精度25%で棄却済み。可視化なら誤検出の被害が小さい）。
"""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QTextEdit, QVBoxLayout,
)

HIGHLIGHT_COLOR = QColor(255, 235, 120)  # 黄色（怪しい箇所）


class CorrectionDialog(QDialog):
    def __init__(self, original: str, parent=None, highlight_words=None):
        super().__init__(parent)
        self.setWindowTitle("MO Voice - 認識結果の修正")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.resize(480, 220)
        self.original = original

        layout = QVBoxLayout(self)
        if highlight_words:
            label = QLabel(
                "認識に自信のない箇所を黄色で示しています。"
                "必要なら修正してください（修正内容を学習します）:")
        else:
            label = QLabel("認識結果を正しい文に修正してください（修正内容を学習します）:")
        layout.addWidget(label)
        self.edit = QTextEdit()
        self.edit.setPlainText(original)
        layout.addWidget(self.edit)

        if highlight_words:
            self._highlight(highlight_words)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _highlight(self, words):
        """指定された語の出現箇所を黄色背景にする。"""
        fmt = QTextCharFormat()
        fmt.setBackground(HIGHLIGHT_COLOR)
        doc = self.edit.document()
        for word in words:
            if not word:
                continue
            cursor = QTextCursor(doc)
            while True:
                cursor = doc.find(word, cursor)
                if cursor.isNull():
                    break
                cursor.mergeCharFormat(fmt)

    def corrected_text(self) -> str:
        return self.edit.toPlainText().strip()
