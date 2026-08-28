"""設定ダイアログ。

config.json を直接編集しなくても、よく触る項目を GUI から変更できるようにする。
あわせて「学習した修正」を一覧・削除できるようにした（誤学習が溜まっても
気づけないのが実運用でいちばん困る点だったため）。

反映のしかた:
    config は controller などが **使用時に** config.get() で読むため、
    dict を in-place 更新すればほとんどの項目が再起動なしで効く。
    モデル・言語・beam_size・compute_type だけは Transcriber の __init__ で
    確定するため再起動が必要（RESTART_KEYS）。
"""
import json
import time
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QKeySequenceEdit, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

# 変更しても再起動しないと効かない項目（Transcriber の __init__ で確定するため）
RESTART_KEYS = {"model", "compute_type", "language", "beam_size"}

MODELS = [
    ("tiny", "最速・精度は低い"),
    ("base", "速い・固有名詞が崩れやすい"),
    ("small", "推奨。速度と精度のバランスが良い"),
    ("medium", "高精度・small の2〜3倍遅い"),
    ("large-v3", "最高精度・CPUでは実用が厳しい"),
]

COMPUTE_TYPES = [
    ("int8", "推奨。CPUで最も速い"),
    ("int8_float32", "int8 より少し高精度・少し遅い"),
    ("float32", "最高精度・CPUでは大幅に遅い"),
]

INJECTION_METHODS = [
    ("clipboard", "推奨。クリップボード経由で貼り付ける"),
    ("sendinput", "1文字ずつ送る。クリップボードを汚さないが遅い"),
]

# Qt のキー名 → pynput のキー名
_QT_TO_PYNPUT = {
    "ctrl": "<ctrl>", "alt": "<alt>", "shift": "<shift>", "meta": "<cmd>",
    "space": "<space>", "tab": "<tab>", "return": "<enter>",
    "enter": "<enter>", "esc": "<esc>", "escape": "<esc>",
    "backspace": "<backspace>", "del": "<delete>", "delete": "<delete>",
    "ins": "<insert>", "insert": "<insert>", "home": "<home>", "end": "<end>",
    "pgup": "<page_up>", "pgdown": "<page_down>",
    "up": "<up>", "down": "<down>", "left": "<left>", "right": "<right>",
}
_PYNPUT_TO_QT = {
    "<ctrl>": "Ctrl", "<alt>": "Alt", "<shift>": "Shift", "<cmd>": "Meta",
    "<space>": "Space", "<tab>": "Tab", "<enter>": "Return", "<esc>": "Esc",
    "<backspace>": "Backspace", "<delete>": "Del", "<insert>": "Ins",
    "<home>": "Home", "<end>": "End", "<page_up>": "PgUp",
    "<page_down>": "PgDown", "<up>": "Up", "<down>": "Down",
    "<left>": "Left", "<right>": "Right",
}


def pynput_to_qt(seq: str) -> str:
    """'<ctrl>+<alt>+<space>' → 'Ctrl+Alt+Space'

    QKeySequence がパースできる形に必ず戻すこと。'<f5>' のような山括弧付きを
    そのまま返すとキー入力欄が空欄になり、保存した時点で設定が消えてしまう。
    """
    parts = []
    for p in (seq or "").split("+"):
        p = p.strip()
        if not p:
            continue
        low = p.lower()
        if low in _PYNPUT_TO_QT:
            parts.append(_PYNPUT_TO_QT[low])
        elif len(p) == 1:
            parts.append(p.upper())
        elif low.startswith("<f") and low[2:-1].isdigit():
            parts.append("F" + low[2:-1])          # <f5> → F5
        else:
            parts.append(p.strip("<>").capitalize())
    return "+".join(parts)


def qt_to_pynput(seq: str) -> str:
    """'Ctrl+Alt+Space' → '<ctrl>+<alt>+<space>'

    pynput の GlobalHotKeys は修飾キーと特殊キーを <> で囲む形式を要求する。
    通常の英数字キーはそのまま小文字で渡す。
    """
    parts = []
    for p in (seq or "").split("+"):
        p = p.strip()
        if not p:
            continue
        low = p.lower()
        if low in _QT_TO_PYNPUT:
            parts.append(_QT_TO_PYNPUT[low])
        elif len(p) == 1:
            parts.append(low)
        elif low.startswith("f") and low[1:].isdigit():
            parts.append(f"<{low}>")
        else:
            parts.append(low)
    return "+".join(parts)


def validate_hotkey(seq: str):
    """pynput 形式のホットキーとして成立するか検証する。

    誤った文字列を保存するとアプリ再起動時にホットキーが一切効かなくなり、
    しかも原因が分からない状態になる。保存前にここで弾く。
    Returns: (ok, reason)
    """
    if not seq:
        return False, "ホットキーが空です"
    parts = [p for p in seq.split("+") if p]
    mods = [p for p in parts if p in ("<ctrl>", "<alt>", "<shift>", "<cmd>")]
    keys = [p for p in parts if p not in ("<ctrl>", "<alt>", "<shift>", "<cmd>")]
    if not mods:
        return False, "Ctrl / Alt / Shift のいずれかを含めてください（誤爆防止）"
    if len(keys) != 1:
        return False, "修飾キー以外のキーをちょうど1つ指定してください"
    try:
        from pynput import keyboard
        keyboard.HotKey.parse(seq)
    except Exception as e:
        return False, f"この組み合わせは使えません（{e}）"
    return True, ""


def save_config(config: dict, path: Path) -> str:
    """config.json をアトミックに書き出す。失敗理由を文字列で返す（成功なら空）。

    このアプリは restart スクリプトで強制終了されることがあるため、
    書き込み中断で設定ファイルが壊れないよう一時ファイル経由にする
    （corrections.json と同じ作法）。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
        return ""
    except OSError as e:
        return str(e)


def _combo(items, current):
    """(値, 説明) のリストからコンボボックスを作る。"""
    box = QComboBox()
    for value, desc in items:
        box.addItem(f"{value} — {desc}", value)
    idx = box.findData(current)
    if idx < 0:  # config に未知の値が入っていても選択肢として残す
        box.addItem(f"{current} — （現在の設定）", current)
        idx = box.count() - 1
    box.setCurrentIndex(idx)
    return box


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #4a4a4a; font-size: 13px; line-height: 165%;")
    return label


# ホットキーの候補。他のアプリと衝突しにくい組み合わせだけを挙げる。
# 「何でも指定できる」状態だと、よく使うショートカット（Ctrl+C など）を
# 奪ってしまい、原因が分かりにくい不具合として現れるため。
#
# 意図的に外したもの:
#   Ctrl+Shift+Z … 多くのアプリで「やり直し（Redo）」
#   Ctrl+Alt+Del … OSが予約しており、そもそも捕まえられない
#   F1 / F5 単体 … ヘルプ・再読み込みに割り当てられていることが多い
SAFE_HOTKEYS = [
    ("<ctrl>+<alt>+<space>", "録音の開始／停止の既定"),
    ("<ctrl>+<alt>+z", "直前の結果を修正の既定"),
    ("<ctrl>+<alt>+m", "マイク（Mic）を連想しやすい"),
    ("<ctrl>+<alt>+r", "録音（Record）を連想しやすい"),
    ("<ctrl>+<alt>+v", "音声（Voice）を連想しやすい"),
    ("<ctrl>+<alt>+d", "空いていることが多い"),
    ("<ctrl>+<alt>+q", "空いていることが多い"),
    ("<ctrl>+<shift>+<space>", "Alt を使いたくない場合に"),
    ("<ctrl>+<alt>+<f9>", "文字キーを使いたくない場合に"),
    ("<ctrl>+<alt>+<f10>", "文字キーを使いたくない場合に"),
]

_CUSTOM = "__custom__"


class HotkeyPicker(QWidget):
    """ホットキーの選択UI。候補から選ぶか、「その他」で自由入力する。

    自由入力を残しているのは、候補が既に他アプリで埋まっている環境が
    あり得るため。ただし既定は候補から選ばせて事故を減らす。
    """

    def __init__(self, current: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.combo = QComboBox()
        for seq, desc in SAFE_HOTKEYS:
            self.combo.addItem(f"{pynput_to_qt(seq)} — {desc}", seq)
        self.combo.addItem("その他（自分で指定する）...", _CUSTOM)
        layout.addWidget(self.combo)

        self.edit = QKeySequenceEdit()
        self.edit.setVisible(False)
        layout.addWidget(self.edit)

        idx = self.combo.findData(current)
        if idx >= 0:
            self.combo.setCurrentIndex(idx)
        else:
            # 候補に無い組み合わせが既に設定されている場合は自由入力側で受ける
            self.combo.setCurrentIndex(self.combo.count() - 1)
            self.edit.setKeySequence(QKeySequence(pynput_to_qt(current)))
            self.edit.setVisible(True)
        self.combo.currentIndexChanged.connect(self._on_changed)

    def _on_changed(self):
        custom = self.combo.currentData() == _CUSTOM
        self.edit.setVisible(custom)
        if custom and self.edit.keySequence().isEmpty():
            self.edit.setFocus()

    def value(self) -> str:
        """pynput 形式のホットキー文字列を返す。"""
        data = self.combo.currentData()
        if data != _CUSTOM:
            return data
        return qt_to_pynput(self.edit.keySequence().toString())


class SettingsDialog(QDialog):
    def __init__(self, config: dict, corrections, config_path: Path,
                 state_text: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("MO Voice の設定")
        self.setMinimumSize(620, 560)
        self._config = config
        self._corrections = corrections
        self._config_path = Path(config_path)
        self._before = {k: config.get(k) for k in RESTART_KEYS}
        self.restart_required = False

        root = QVBoxLayout(self)

        if state_text:
            head = QLabel(state_text)
            head.setWordWrap(True)
            head.setTextInteractionFlags(Qt.TextSelectableByMouse)
            head.setStyleSheet(
                "background:#eef1f5; border:1px solid #dde2e8; border-radius:6px;"
                "padding:11px 13px; color:#1f2328; font-size:15px;"
                "line-height:170%;")
            root.addWidget(head)

        tabs = QTabWidget()
        tabs.addTab(self._tab_basic(), "基本")
        tabs.addTab(self._tab_recognition(), "認識")
        tabs.addTab(self._tab_corrections(), "学習した修正")
        tabs.addTab(self._tab_obsidian(), "Obsidian 連携")
        root.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("キャンセル")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ---- タブ: 基本 ----

    def _tab_basic(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self.ed_toggle = HotkeyPicker(
            self._config.get("hotkey_toggle", "<ctrl>+<alt>+<space>"))
        form.addRow("録音の開始／停止", self.ed_toggle)

        self.ed_correct = HotkeyPicker(
            self._config.get("hotkey_correct", "<ctrl>+<alt>+z"))
        form.addRow("直前の結果を修正", self.ed_correct)
        form.addRow("", _hint(
            "他のアプリと衝突しにくい組み合わせを候補にしています。"
            "どうしても別のキーにしたい場合だけ「その他」を選び、"
            "入力欄をクリックして押したいキーを実際に押してください"
            "（Ctrl / Alt / Shift のいずれかを必ず含めます）。"))

        self.sp_seconds = QSpinBox()
        self.sp_seconds.setRange(10, 900)
        self.sp_seconds.setSingleStep(10)
        self.sp_seconds.setSuffix(" 秒")
        self.sp_seconds.setValue(int(self._config.get("max_record_seconds", 300)))
        form.addRow("録音の最大時間", self.sp_seconds)
        form.addRow("", _hint(
            "この時間を過ぎると自動で録音を止めて認識します。"
            "長時間の録音は認識にも比例して時間がかかります。"))

        self.cb_injection = _combo(
            INJECTION_METHODS, self._config.get("injection_method", "clipboard"))
        form.addRow("文字の入力方法", self.cb_injection)
        return w

    # ---- タブ: 認識 ----

    def _tab_recognition(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)

        box = QGroupBox("音声モデル（変更後は再起動が必要）")
        form = QFormLayout(box)
        self.cb_model = _combo(MODELS, self._config.get("model", "small"))
        form.addRow("モデル", self.cb_model)
        self.cb_compute = _combo(
            COMPUTE_TYPES, self._config.get("compute_type", "int8"))
        form.addRow("演算精度", self.cb_compute)

        self.ed_language = QLineEdit(str(self._config.get("language", "ja")))
        self.ed_language.setMaximumWidth(80)
        form.addRow("言語", self.ed_language)

        self.sp_beam = QSpinBox()
        self.sp_beam.setRange(1, 10)
        self.sp_beam.setValue(int(self._config.get("beam_size", 2)))
        form.addRow("ビームサイズ", self.sp_beam)
        form.addRow("", _hint(
            "大きいほど候補を広く探しますが遅くなります。実測では 1 と 2 に"
            "有意な精度差はありませんでした（既定は 2）。"))
        outer.addWidget(box)

        box2 = QGroupBox("低信頼語のハイライト")
        form2 = QFormLayout(box2)
        conf = self._config.get("confidence_highlight", {}) or {}
        self.ck_conf = QCheckBox("自信のない語があれば修正画面を自動で開く")
        self.ck_conf.setChecked(bool(conf.get("enabled", True)))
        form2.addRow(self.ck_conf)

        self.sp_conf = QDoubleSpinBox()
        self.sp_conf.setRange(0.0, 1.0)
        self.sp_conf.setSingleStep(0.05)
        self.sp_conf.setDecimals(2)
        self.sp_conf.setValue(float(conf.get("threshold", 0.6)))
        form2.addRow("しきい値", self.sp_conf)
        form2.addRow("", _hint(
            "認識の確信度がこの値を下回った語を黄色く表示します。"
            "上げるほど頻繁に修正画面が開きます（既定は 0.60）。"))
        outer.addWidget(box2)
        outer.addStretch()
        return w

    # ---- タブ: 学習した修正 ----

    def _tab_corrections(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        layout.addWidget(_hint(
            "「Ctrl+Alt+Z」で修正するたびにここへ溜まり、次回から自動で置き換わります。"
            "意図しない置換が起きている場合は、その行を選んで削除してください。"))

        bar = QHBoxLayout()
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("絞り込み（誤り・正しい語のどちらでも）")
        self.ed_filter.textChanged.connect(self._reload_corrections)
        bar.addWidget(self.ed_filter)
        btn_del = QPushButton("選択した行を削除")
        btn_del.clicked.connect(self._delete_selected)
        bar.addWidget(btn_del)
        layout.addLayout(bar)

        self.tbl = QTableWidget(0, 3)
        self.tbl.setHorizontalHeaderLabels(["誤って認識された語", "正しい語", "回数"])
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.tbl.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.tbl.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents)
        layout.addWidget(self.tbl)

        self.lbl_count = QLabel()
        layout.addWidget(self.lbl_count)
        self._reload_corrections()
        return w

    def _reload_corrections(self):
        pairs = list(getattr(self._corrections, "pairs", []) or [])
        needle = self.ed_filter.text().strip()
        if needle:
            pairs = [p for p in pairs
                     if needle in p.get("wrong", "")
                     or needle in p.get("right", "")]
        pairs.sort(key=lambda p: p.get("last_used", 0), reverse=True)
        self.tbl.setRowCount(len(pairs))
        for row, p in enumerate(pairs):
            for col, key in enumerate(("wrong", "right", "count")):
                item = QTableWidgetItem(str(p.get(key, "")))
                if col == 0:
                    # 削除時に元データを特定するため、行に実体を紐づけておく
                    item.setData(Qt.UserRole, p)
                self.tbl.setItem(row, col, item)
        total = len(getattr(self._corrections, "pairs", []) or [])
        self.lbl_count.setText(
            f"全 {total} 件" + (f"（表示 {len(pairs)} 件）" if needle else ""))

    def _delete_selected(self):
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "MO Voice", "削除する行を選んでください")
            return
        targets = []
        for r in rows:
            item = self.tbl.item(r, 0)
            if item is not None:
                targets.append(item.data(Qt.UserRole))
        preview = "\n".join(
            f"　「{t.get('wrong')}」→「{t.get('right')}」" for t in targets[:8])
        if len(targets) > 8:
            preview += f"\n　… ほか {len(targets) - 8} 件"
        if QMessageBox.question(
                self, "MO Voice",
                f"次の学習を削除します（元に戻せません）。\n\n{preview}",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        # 同一内容の別ペアを巻き込まないよう、オブジェクトの同一性で消す
        keep = [p for p in self._corrections.pairs
                if not any(p is t for t in targets)]
        self._corrections.pairs = keep
        try:
            self._corrections._save()
        except Exception as e:
            QMessageBox.warning(self, "MO Voice", f"保存に失敗しました: {e}")
        self._reload_corrections()

    # ---- タブ: Obsidian ----

    def _tab_obsidian(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        layout.addWidget(_hint(
            "Obsidian のノートから語彙を集めて認識のヒントに使う機能です。"
            "実測では<b>ヒントを入れないほうが精度が高かった</b>ため既定でオフにしています"
            "（一致率 0.9019 → 0.8958）。固有名詞は改善する一方で、"
            "「印字」→「インジン」のように普通の日本語が崩れる例が上回りました。"
            "ノートに書いてある言葉が、そのまま口に出す言葉とは限らないためです。"))

        self.ck_vault = QCheckBox("ノートから集めた語彙を認識のヒントに使う")
        self.ck_vault.setChecked(bool(self._config.get("use_vault_vocab", False)))
        layout.addWidget(self.ck_vault)

        row = QHBoxLayout()
        self.ed_vault = QLineEdit(str(self._config.get("vault_path", "")))
        self.ed_vault.setPlaceholderText("Obsidian vault のフォルダ（未設定で無効）")
        row.addWidget(self.ed_vault)
        btn = QPushButton("参照...")
        btn.clicked.connect(self._pick_vault)
        row.addWidget(btn)
        layout.addLayout(row)
        layout.addWidget(_hint(
            "フォルダを変更した場合は再起動後、トレイメニューの"
            "「Obsidian vault を再スキャン」を実行してください。"))
        layout.addStretch()
        return w

    def _pick_vault(self):
        start = self.ed_vault.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(
            self, "Obsidian vault のフォルダを選択", start)
        if path:
            self.ed_vault.setText(path)

    # ---- 保存 ----

    def _on_save(self):
        toggle = self.ed_toggle.value()
        correct = self.ed_correct.value()
        for label, seq in (("録音の開始／停止", toggle), ("直前の結果を修正", correct)):
            ok, reason = validate_hotkey(seq)
            if not ok:
                QMessageBox.warning(
                    self, "MO Voice", f"「{label}」のホットキー: {reason}")
                return
        if toggle == correct:
            QMessageBox.warning(
                self, "MO Voice", "2つのホットキーに同じ組み合わせは使えません")
            return

        vault = self.ed_vault.text().strip()
        if vault and not Path(vault).is_dir():
            if QMessageBox.question(
                    self, "MO Voice",
                    "指定されたフォルダが見つかりません。このまま保存しますか？\n"
                    f"{vault}",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No) != QMessageBox.Yes:
                return

        # config を in-place 更新する（controller 等が同じ dict を参照しており、
        # 使用時に get() で読むため、これだけで大半の項目が即反映される）
        self._config.update({
            "hotkey_toggle": toggle,
            "hotkey_correct": correct,
            "max_record_seconds": self.sp_seconds.value(),
            "injection_method": self.cb_injection.currentData(),
            "model": self.cb_model.currentData(),
            "compute_type": self.cb_compute.currentData(),
            "language": self.ed_language.text().strip() or "ja",
            "beam_size": self.sp_beam.value(),
            "confidence_highlight": {
                "enabled": self.ck_conf.isChecked(),
                "threshold": round(self.sp_conf.value(), 2),
            },
            "use_vault_vocab": self.ck_vault.isChecked(),
            "vault_path": vault,
        })

        error = save_config(self._config, self._config_path)
        if error:
            QMessageBox.critical(
                self, "MO Voice",
                f"設定を保存できませんでした。\n{error}\n\n"
                "変更はこの起動中のみ有効です。")
            return

        self.restart_required = any(
            self._before.get(k) != self._config.get(k) for k in RESTART_KEYS)
        self.accept()
