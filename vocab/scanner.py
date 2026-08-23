"""Obsidian vault から専門用語・固有名詞を抽出して vocabulary.json を作る。

抽出対象:
- 各ノートのファイル名（Obsidian ではノート名自体が概念名になる）
- 見出し（# 行）・frontmatter タグ・wikilink [[...]]
- 本文中のカタカナ語（3文字以上）・英単語（3文字以上）・漢字複合語を頻度付きで

カテゴリ付け:
vault のフォルダ構成をそのままカテゴリに使う（特定の命名規約に依存しない）。
ファイルのパス上の全フォルダ名がそのファイル由来の語のカテゴリになる。
先頭の数字プレフィックス（`02_` 等。Obsidian で並び順を制御する慣習）は除去する。

  knowledge/keyword/MCP.md      -> ["knowledge", "keyword"]
  02_整理ノート/ClaudeCode/x.md -> ["整理ノート", "ClaudeCode"]
  05_ブログ記事/x.md            -> ["ブログ記事"]
  （vault 直下のファイル）      -> ["general"]

どのカテゴリを音声認識のヒントに使うかは config.json の context_rules で
アクティブなアプリごとに指定する。
"""
import json
import re
import time
from collections import defaultdict
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "vocabulary.json"

KATAKANA_RE = re.compile(r"[ァ-ヴー]{3,}")
ENGLISH_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+#-]{2,}")
KANJI_RUN_RE = re.compile(r"[一-龥]{2,6}")  # 漢字複合語（博物館・美術館 等）

# 頻出しすぎて語彙ヒントにならない一般的な漢字語（時間・動作・様子など）。
# initial_prompt の予算は223トークンしかないため、Whisperが確実に知っている
# 一般語で枠を潰さないためのフィルタ
KANJI_STOPWORDS = {
    "今日", "明日", "昨日", "今回", "今年", "去年", "来年", "今週", "先週",
    "来週", "今月", "先月", "来月", "午前", "午後", "最近", "最初", "最後",
    "自分", "仕事", "対応", "確認", "作成", "修正", "追加", "削除", "変更",
    "設定", "実行", "完了", "開始", "終了", "利用", "使用", "必要", "可能",
    "場合", "方法", "問題", "結果", "内容", "情報", "状態", "予定", "時間",
    "部分", "全体", "以下", "以上", "以外", "参照", "記事", "画像", "画面",
    "一つ", "二つ", "三つ", "一部", "一番", "簡単", "重要", "普通", "本当",
    "索引", "目次", "一覧", "関連", "参考", "備考", "以降", "現在", "今後",
}
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
TAG_RE = re.compile(r"(?:^|\s)#([^\s#/]+)")

# ノイズ除去用: コードブロック・インラインコード・URL・データURI・HTMLタグ
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
URL_RE = re.compile(r"https?://\S+|www\.\S+|data:\S+")
HTML_TAG_RE = re.compile(r"<[^>\n]+>")
MD_LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")

# 英単語らしさの判定: 単語/CamelCase/末尾数字許可、または短い大文字略語
WORDISH_RE = re.compile(r"^[A-Za-z][A-Za-z-]*\d{0,2}$")
ACRONYM_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")
VOWEL_RE = re.compile(r"[aeiouAEIOU]")

# 頻出しすぎて語彙ヒントにならない英単語
STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "http", "https", "com",
    "www", "png", "jpg", "md", "true", "false", "null", "yes", "not",
    "you", "are", "was", "can", "all", "use", "run", "get", "set",
}


# フォルダ名の先頭に付く並び順制御用の数字プレフィックス（`02_`, `05-`, `1. ` 等）
NUM_PREFIX_RE = re.compile(r"^\d+[_\-.\s]+")

# ファイル名が日付だけのノート（日記など）は用語として無意味なので除外する
DATEISH_RE = re.compile(r"^\d{4}[-_/]?\d{1,2}[-_/]?\d{1,2}")


def _categories_for(rel_path: Path) -> list:
    """ファイルのパス上の全フォルダ名をカテゴリとして返す。

    vault のフォルダ構成をそのままカテゴリにするため、どんな命名規約の
    vault でも動作する（旧実装は `knowledge/`・`02_`・`05_` という作者固有の
    命名を直接判定していた）。
    """
    cats = []
    for name in rel_path.parts[:-1]:  # 末尾はファイル名なので除く
        if name.startswith("."):
            continue
        clean = NUM_PREFIX_RE.sub("", name).strip()
        if clean and clean not in cats:
            cats.append(clean)
    return cats or ["general"]


# 索引・目次など、閲覧用の構造ファイル名（発話には現れないので語彙にしない）
STRUCTURAL_NAME_RE = re.compile(
    r"索引|目次|一覧|^index$|^readme$|^toc$|^moc$", re.IGNORECASE)


def _filename_term(stem: str) -> str:
    """ノート名を用語として使えるなら返す（使えなければ空文字）。

    Obsidian ではノート名がそのまま概念名になることが多いため、
    ファイル名は重み付きの語彙候補として扱う。ただし以下は語彙にしない:
    - 日付ノート（日記等）
    - 索引・目次のような閲覧用の構造ファイル（口に出して言う語ではない）
    - 極端に短い/長い名前
    先頭の並び順制御プレフィックス（`00_` 等）は取り除く。
    """
    term = (stem or "").strip()
    term = NUM_PREFIX_RE.sub("", term).strip()
    if len(term) < 2 or len(term) > 30:
        return ""
    if DATEISH_RE.match(term) or term.isdigit():
        return ""
    if STRUCTURAL_NAME_RE.search(term):
        return ""
    return term


def _clean_text(text: str) -> str:
    """コードブロック・URL 等のノイズ源を除去してから用語抽出に回す。"""
    text = FENCED_CODE_RE.sub(" ", text)
    text = INLINE_CODE_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = MD_LINK_TARGET_RE.sub("]", text)
    text = HTML_TAG_RE.sub(" ", text)
    return text


def _valid_english(term: str) -> bool:
    """英単語として妥当か（base64断片・URL断片を弾く）。"""
    if term.lower() in STOPWORDS:
        return False
    if ACRONYM_RE.match(term):
        return True
    if not WORDISH_RE.match(term):
        return False
    # 母音なしの長い文字列はランダム文字列とみなす
    if len(term) >= 6 and not VOWEL_RE.search(term):
        return False
    return True


def _extract_terms(text: str):
    """本文から (term, weight) を列挙する。"""
    # frontmatter タグ・wikilink・見出しは重み高め。
    # wikilink の中身はノート名なのでファイル名と同じ基準で選別する
    # （索引ノートは全ノートからリンクされるため、素通しすると高頻度語になる）
    for m in WIKILINK_RE.finditer(text):
        link = _filename_term(m.group(1).strip())
        if link:
            yield link, 5
    for m in TAG_RE.finditer(text):
        yield m.group(1).strip(), 3
    for m in HEADING_RE.finditer(text):
        heading = m.group(1)
        for km in KATAKANA_RE.finditer(heading):
            yield km.group(0), 3
        for em in ENGLISH_RE.finditer(heading):
            if _valid_english(em.group(0)):
                yield em.group(0), 3
    for m in KATAKANA_RE.finditer(text):
        yield m.group(0), 1
    for m in ENGLISH_RE.finditer(text):
        if _valid_english(m.group(0)):
            yield m.group(0), 1
    # 漢字複合語（博物館・美術館 等）。同音異義語の誤変換（対照→対象 等）は
    # カタカナ・英字だけでは救えないため漢字語も語彙ヒントに含める
    for m in KANJI_RUN_RE.finditer(text):
        if m.group(0) not in KANJI_STOPWORDS:
            yield m.group(0), 1


def scan_vault(vault_path: str, out_path: Path = DATA_FILE) -> dict:
    """vault をスキャンして語彙DBを作る。

    vault_path が未設定・存在しない場合は既存の vocabulary.json を壊さずに
    error 付きの結果を返す（Obsidian を使わないユーザーでも落ちないため）。
    """
    if not vault_path or not str(vault_path).strip():
        return {"terms": [], "file_count": 0, "error": "vault_path が未設定です"}
    vault = Path(vault_path)
    if not vault.is_dir():
        return {"terms": [], "file_count": 0,
                "error": f"vault が見つかりません: {vault}"}

    freq = defaultdict(int)            # term -> 重み付き頻度
    categories = defaultdict(set)      # term -> {category}
    sources = defaultdict(set)         # term -> {相対パス}

    files = [p for p in vault.rglob("*.md") if ".obsidian" not in p.parts
             and ".trash" not in p.parts]
    for p in files:
        rel = p.relative_to(vault)
        cats = _categories_for(rel)
        # ノート名自体を語彙候補にする（Obsidian ではノート名＝概念名のことが多い）
        fname_term = _filename_term(p.stem)
        if fname_term:
            freq[fname_term] += 5
            categories[fname_term].update(cats)
            sources[fname_term].add(str(rel))
        try:
            text = _clean_text(p.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            continue
        for term, weight in _extract_terms(text):
            if len(term) < 2 or term.lower() in STOPWORDS:
                continue
            freq[term] += weight
            categories[term].update(cats)
            sources[term].add(str(rel))

    terms = [
        {
            "term": t,
            "categories": sorted(categories[t]),
            "freq": f,
            "sources": sorted(sources[t])[:5],
        }
        for t, f in freq.items() if f >= 2  # 1回きりの語は除外
    ]
    terms.sort(key=lambda x: -x["freq"])

    result = {
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "vault": str(vault),
        "file_count": len(files),
        "terms": terms,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def load_vocabulary(path: Path = DATA_FILE) -> dict:
    if Path(path).exists():
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"terms": []}
