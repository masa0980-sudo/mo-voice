"""誤認識→正解ペアの学習・適用モジュール。

- learn(): 修正ダイアログの編集前後テキストを difflib で比較し、置換ペアを蓄積。
  文全体を大きく書き換えた場合（MIN_EDIT_RATIO未満）は、diffが単語単位の
  訂正を表さない断片になりやすいため学習をスキップする
- apply(): count>=AUTO_APPLY_MIN_COUNT のペアを自動置換（誤爆時は corrections.json から削除）
"""
import difflib
import json
import re
import time
from pathlib import Path

KATAKANA_RE = re.compile(r"[ァ-ヴー]")
LATIN_RE = re.compile(r"[A-Za-z0-9]")
HIRAGANA_ONLY_RE = re.compile(r"^[ぁ-んー]+$")
KATAKANA_RUN_RE = re.compile(r"[ァ-ヴー・]+")  # 読み一致置換の対象（連続カタカナ域）

_SMALL_KANA = str.maketrans({
    "ァ": "ア", "ィ": "イ", "ゥ": "ウ", "ェ": "エ", "ォ": "オ",
    "ャ": "ヤ", "ュ": "ユ", "ョ": "ヨ", "ヮ": "ワ", "ヵ": "カ", "ヶ": "ケ",
    "ヴ": "ブ",
})


def _normalize_kata(s: str) -> str:
    """カタカナ文字列を照合用の正準形に落とす（長音・中黒・空白を除去、小書き→大書き）。

    「クロードコードー」「クロード・コード」のような表記ゆれを同一視するための
    決定的な正規化。探索的な類似度マッチはしない（precision 25% で棄却済み）。
    """
    import unicodedata
    s = unicodedata.normalize("NFKC", s)
    # ひらがなが混ざっていたらカタカナへ
    s = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)
    s = s.translate(_SMALL_KANA)
    return re.sub(r"[ー・\s]", "", s)


_kakasi = None


def _reading_of(s: str) -> str:
    """語の読み（正規化カタカナ）を返す。漢字を含む場合は pykakasi を使う。

    pykakasi 未導入でも例外にせず空文字を返す（読み一致置換が無効になるだけで
    完全一致置換は従来どおり動く）。
    """
    if not s:
        return ""
    if re.fullmatch(r"[ぁ-ゖァ-ヴー・\s]+", s):
        return _normalize_kata(s)
    global _kakasi
    try:
        if _kakasi is None:
            import pykakasi
            _kakasi = pykakasi.kakasi()
        kana = "".join(c["kana"] for c in _kakasi.convert(s))
        return _normalize_kata(kana)
    except Exception:
        return ""


def _script(ch: str):
    """文字種を返す（カタカナ/英数字のみ拡張対象。ひらがな・漢字は None）。"""
    if KATAKANA_RE.match(ch):
        return "kata"
    if LATIN_RE.match(ch):
        return "latin"
    return None

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "corrections.json"
AUTO_APPLY_MIN_COUNT = 1  # 1回の修正で即自動置換（誤爆時は corrections.json から削除）
MIN_PAIR_LEN = 2   # 1文字だけの置換は学習しない（助詞ゆらぎ等の誤爆防止）
MAX_PAIR_LEN = 30  # 長すぎる置換は文単位の書き換えなので学習しない
MIN_EDIT_RATIO = 0.7  # 全体の類似度がこれ未満＝大きな書き換えとみなし学習をスキップ
MAX_HIRAGANA_ONLY_WRONG_LEN = 3  # wrong側がひらがなのみでこの文字数以下は
# 助詞・頻出機能語（「という」「それ」「する」等）である可能性が高く、
# 誤爆時の被害が大きいため学習対象外にする（right側は対象外＝「お越→起こ」等の
# 正しい学習は妨げない）


class Corrections:
    def __init__(self, path: Path = DATA_FILE):
        self.path = Path(path)
        self.pairs = []  # [{wrong, right, count, last_used}]
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.pairs = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.pairs = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.pairs, ensure_ascii=False, indent=2), encoding="utf-8")

    def learn(self, original: str, corrected: str):
        """編集前後のテキストから置換ペアを抽出して蓄積する。"""
        if original == corrected or not original or not corrected:
            return []
        sm = difflib.SequenceMatcher(None, original, corrected)
        if sm.ratio() < MIN_EDIT_RATIO:
            # 文全体を大きく書き換えた場合、diffの各opは単語単位の訂正を
            # 表さない断片になりやすい（助詞レベルの誤学習の主因だった）
            return []
        learned = []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op != "replace":
                continue
            # 1文字差分（プ→ブ等）は前後の同種文字を含めて語単位に拡張する
            # 例: 「プログを作る」→「ブログを作る」 ⇒ 「プログ」→「ブログ」
            if len(original[i1:i2]) < MIN_PAIR_LEN or len(corrected[j1:j2]) < MIN_PAIR_LEN:
                while (i2 < len(original) and j2 < len(corrected)
                       and original[i2] == corrected[j2]
                       and _script(original[i2]) is not None
                       and (i2 - i1) < MAX_PAIR_LEN):
                    i2 += 1
                    j2 += 1
                while (i1 > 0 and j1 > 0
                       and original[i1 - 1] == corrected[j1 - 1]
                       and _script(original[i1 - 1]) is not None
                       and (i2 - i1) < MAX_PAIR_LEN):
                    i1 -= 1
                    j1 -= 1
            wrong, right = original[i1:i2], corrected[j1:j2]
            if not (MIN_PAIR_LEN <= len(wrong) <= MAX_PAIR_LEN):
                continue
            if not (MIN_PAIR_LEN <= len(right) <= MAX_PAIR_LEN):
                continue
            if (HIRAGANA_ONLY_RE.match(wrong)
                    and len(wrong) <= MAX_HIRAGANA_ONLY_WRONG_LEN):
                continue
            learned.append((wrong, right))
            for p in self.pairs:
                if p["wrong"] == wrong and p["right"] == right:
                    p["count"] += 1
                    p["last_used"] = time.time()
                    break
            else:
                self.pairs.append(
                    {"wrong": wrong, "right": right, "count": 1,
                     "last_used": time.time()})
        if learned:
            self._save()
        return learned

    def apply(self, text: str) -> str:
        """学習済みペアで自動置換する（長い wrong から先に適用）。

        カタカナ語・英単語は前後に同種文字が続く場合は置換しない
        （例: 「プログ」→「ブログ」の学習で「プログラム」を壊さない）。
        """
        if not text:
            return text
        active = [p for p in self.pairs if p["count"] >= AUTO_APPLY_MIN_COUNT]
        for p in sorted(active, key=lambda p: -len(p["wrong"])):
            wrong, right = p["wrong"], p["right"]
            if wrong not in text:
                continue
            pattern = re.escape(wrong)
            if KATAKANA_RE.match(wrong[0]):
                pattern = r"(?<![ァ-ヴー])" + pattern
            elif LATIN_RE.match(wrong[0]):
                pattern = r"(?<![A-Za-z0-9])" + pattern
            if KATAKANA_RE.match(wrong[-1]):
                pattern = pattern + r"(?![ァ-ヴー])"
            elif LATIN_RE.match(wrong[-1]):
                pattern = pattern + r"(?![A-Za-z0-9])"
            # repl を関数にすることで right 中の \1 等が後方参照として
            # 誤解釈されるのを防ぐ（文字列repl版のエスケープは不完全だった）
            new_text = re.sub(pattern, lambda m, r=right: r, text)
            if new_text != text:
                text = new_text
                p["last_used"] = time.time()
        text = self._apply_reading_match(text, active)
        return text

    # 読みが短い語は「ビル/ビール」のような別語衝突のリスクがあるため対象外
    MIN_READING_LEN = 4

    def _apply_reading_match(self, text: str, active) -> str:
        """第2段: 完全一致しなかった wrong を、読みの一致で回収する。

        テキスト中の連続カタカナ域のみを対象に、wrong の読み（正規化カタカナ）と
        完全一致した場合だけ right へ置換する。「クロードコードー」
        「クロード・コード」のような表記ゆれの回収が目的。
        探索的なファジーマッチはしない（既知ペアの決定的照合のみ）。
        """
        if not text:
            return text
        runs = [r for r in set(KATAKANA_RUN_RE.findall(text)) if len(r) >= 3]
        if not runs:
            return text
        run_keys = {r: _normalize_kata(r) for r in runs}
        for p in active:
            wrong, right = p["wrong"], p["right"]
            wkey = _reading_of(wrong)
            if len(wkey) < self.MIN_READING_LEN:
                continue
            for run, rkey in run_keys.items():
                # run == wrong は第1段（完全一致）の担当なのでここでは扱わない
                if run == wrong or run == right or rkey != wkey:
                    continue
                pattern = (r"(?<![ァ-ヴー])" + re.escape(run) + r"(?![ァ-ヴー])")
                new_text = re.sub(pattern, lambda m, r=right: r, text)
                if new_text != text:
                    text = new_text
                    p["last_used"] = time.time()
        return text

    def recent_corrected_terms(self, limit=15, max_entries=30):
        """corrections_log.jsonl の「修正後テキスト」から語を抽出して新しい順で返す。

        ユーザーが実際に発話し、手で直した確定テキストは最も信頼できる語彙ソース。
        vault にまだ書かれていない語（今日初めて話した固有名詞等）もここから拾える。
        カタカナ語・英単語・漢字複合語を対象とし、頻出一般語は除外する。
        """
        log_path = self.path.parent / "corrections_log.jsonl"
        if not log_path.exists():
            return []
        try:
            lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        except OSError:
            return []
        from vocab.scanner import (
            KATAKANA_RE, ENGLISH_RE, KANJI_RUN_RE, KANJI_STOPWORDS,
            _valid_english,
        )
        seen, out = set(), []

        def add(term):
            if term and len(term) >= 2 and term not in seen:
                seen.add(term)
                out.append(term)

        for line in reversed(lines[-max_entries:]):  # 新しい修正から
            try:
                corrected = json.loads(line).get("corrected", "")
            except json.JSONDecodeError:
                continue
            for m in KATAKANA_RE.finditer(corrected):
                add(m.group(0))
            for m in ENGLISH_RE.finditer(corrected):
                if _valid_english(m.group(0)):
                    add(m.group(0))
            for m in KANJI_RUN_RE.finditer(corrected):
                if m.group(0) not in KANJI_STOPWORDS:
                    add(m.group(0))
            if len(out) >= limit:
                break
        return out[:limit]

    def right_terms(self, limit=20):
        """prompt 注入用: 誤認識実績のある正解語を count 降順・同数なら最近使った順で返す。"""
        seen, out = set(), []
        for p in sorted(self.pairs,
                        key=lambda p: (-p["count"], -p.get("last_used", 0))):
            if p["right"] not in seen:
                seen.add(p["right"])
                out.append(p["right"])
            if len(out) >= limit:
                break
        return out
