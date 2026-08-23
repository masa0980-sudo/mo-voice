"""Whisper の initial_prompt を 224 トークン予算内で組み立てる。

優先順:
1. リード文（日本語ディクテーションの文体ヒント）
2. corrections.json の正解語（誤認識実績のある語 = 最優先）
3. アクティブカテゴリの語彙（文脈判定の結果）
4. グローバル頻出語
"""
TOKEN_BUDGET = 223  # hotwords の実際の上限（max_length // 2 - 1）
LEAD = "以下は日本語の発話です。"


_tokenizer = None  # set_tokenizer() で外部から注入する


def set_tokenizer(tok):
    """ロード済み WhisperModel の hf_tokenizer を注入する。

    以前は openai-whisper のトークナイザを import しようとしていたが、そのパッケージは
    venv に入っていないため常に例外へ落ち、概算フォールバック（1文字≒1.1トークン）が
    使われていた。日本語は1トークンで複数文字を表すことが多く、概算は実測の約1.4倍に
    過大評価される（224予算のうち約31%を無駄にしていた）。
    追加ダウンロードを避けるため、既にロード済みのモデルが持つトークナイザを使う。
    """
    global _tokenizer
    _tokenizer = tok


def count_tokens(text: str) -> int:
    if _tokenizer is not None:
        try:
            return len(_tokenizer.encode(text).ids)
        except Exception:
            pass
    # フォールバック: 日本語は1文字≒1トークン強で概算（モデル未ロード時のみ）
    return int(len(text) * 1.1) + 1


def build_hotwords(vocabulary: dict, corrections=None, categories=None,
                   budget: int = TOKEN_BUDGET) -> str:
    """語彙DB・修正学習・アクティブカテゴリから hotwords 文字列を作る。

    hotwords は initial_prompt と違い、全デコードウィンドウに毎回渡される
    （initial_prompt は condition_on_previous_text=False のとき2ウィンドウ目
    以降で失われる）。予算も previous_tokens とは別枠で 223 トークン。
    """
    categories = categories or ["global"]
    candidates = []  # 優先順に並んだ用語リスト
    seen = set()

    # 誤認識の実績がある語（corrections の wrong 側）は vault のノートにも
    # そのまま書き残されていることがある。これを hotwords に渡すとモデルに
    # 誤認識を教え込むことになるため除外する
    blocked = set()
    if corrections is not None:
        blocked = {p["wrong"] for p in corrections.pairs}

    def add(term):
        if term and term not in seen and term not in blocked:
            seen.add(term)
            candidates.append(term)

    # 1. 誤認識実績のある正解語（最優先）
    if corrections is not None:
        for t in corrections.right_terms(limit=20):
            add(t)
        # 1.5. 最近の修正後テキストに含まれる語（ユーザーが実際に発話した確定語彙。
        # vault にまだ書かれていない語もここから拾える。「対象→対照」のような
        # 同音異義語の誤変換対策）
        for t in corrections.recent_corrected_terms(limit=15):
            add(t)

    terms = vocabulary.get("terms", [])
    # 2. アクティブカテゴリの語彙（freq 降順のまま）
    if "global" not in categories or len(categories) > 1:
        for item in terms:
            if set(item["categories"]) & set(categories):
                add(item["term"])
    # 3. グローバル頻出語
    for item in terms:
        add(item["term"])

    # トークン予算内で詰める（hotwords はリード文を含まず語彙のみ）。
    # 入りきらない語はスキップして後続の短い語を拾い続ける（打ち切らない）。
    # 予算にほぼ余地がなくなった時点で終了する。
    used = 0
    picked = []
    for term in candidates:
        if budget - used < 2:  # 最短の語すら入らない
            break
        cost = count_tokens(term + "、")
        if used + cost > budget:
            continue
        picked.append(term)
        used += cost
    return "、".join(picked)


def build_prompt(vocabulary: dict, corrections=None, categories=None,
                 budget: int = TOKEN_BUDGET) -> str:
    """後方互換用。旧 initial_prompt 形式（リード文＋語彙）を返す。"""
    hot = build_hotwords(vocabulary, corrections, categories, budget)
    return LEAD + "用語: " + hot if hot else LEAD
