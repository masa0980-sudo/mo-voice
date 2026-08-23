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
_special_overhead = 0  # encode() が1回ごとに付ける特殊トークン数


def set_tokenizer(tok):
    """ロード済み WhisperModel の hf_tokenizer を注入する。

    以前は openai-whisper のトークナイザを import しようとしていたが、そのパッケージは
    venv に入っていないため常に例外へ落ち、概算フォールバック（1文字≒1.1トークン）が
    使われていた。日本語は1トークンで複数文字を表すことが多く、概算は実測の約1.4倍に
    過大評価される（224予算のうち約31%を無駄にしていた）。
    追加ダウンロードを避けるため、既にロード済みのモデルが持つトークナイザを使う。

    注入時に「空文字のエンコード長」を測って特殊トークン数を求めておく。
    Whisper のトークナイザは encode() のたびに <|startoftranscript|> 等の制御用
    トークンを3個付けるため、これを引かないと語ごとの見積もりが大幅に水増しされる
    （実測: 30語で90トークン＝予算223の41%が実在しないトークンで埋まっていた）。
    """
    global _tokenizer, _special_overhead
    _tokenizer = tok
    try:
        _special_overhead = len(tok.encode("").ids)
    except Exception:
        _special_overhead = 0


def count_tokens(text: str) -> int:
    """text が prompt 文字列に占める実トークン数を返す（特殊トークンを除く）。"""
    if _tokenizer is not None:
        try:
            return max(0, len(_tokenizer.encode(text).ids) - _special_overhead)
        except Exception:
            pass
    # フォールバック: 日本語は1文字≒1トークン強で概算（モデル未ロード時のみ）
    return int(len(text) * 1.1) + 1


def build_hotwords(vocabulary: dict, corrections=None, categories=None,
                   budget: int = TOKEN_BUDGET,
                   use_vault_vocab: bool = False) -> str:
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
        # 語の区切りに「、」を使うため、それ自体を含む語は入れない
        # （corrections には「ァ、しんど」のように区切り文字を含む学習結果が
        #   混ざることがあり、そのまま入れると断片に割れて意味をなさない）
        if not term or "、" in term:
            return
        if term not in seen and term not in blocked:
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

    # vault語彙の注入は既定で無効（use_vault_vocab=False）。
    # 2026-08-23に実発話11件でA/B測定した結果、vault語彙を入れると平均一致率が
    # 0.9019 → 0.8958 と悪化した。「英字混じりの技術用語が悪い」という仮説で
    # 絞り込みも試したが、スラッグ除外 0.8900 / 英字全除外 0.8834 とかえって悪化し、
    # 絞り方の問題ではなく「ノートに書いてある＝発話される」という推測自体が
    # 成り立たないと判断した（corrections の実績ベース学習は有効なので残す）。
    # スキャン機能自体は将来の用途（hotwords 等）のために保持する。
    terms = vocabulary.get("terms", []) if use_vault_vocab else []
    # 2. アクティブカテゴリに「特徴的な」語を優先する。
    #    単にカテゴリ一致で絞ると、頻出語（Claude・リンク等）は十数カテゴリに
    #    属するためどのアプリでも同じ顔ぶれになり、文脈判定が実質無効になる
    #    （実測: カテゴリ一致だけだと全語彙の65〜83%がヒットしてしまう）。
    #    freq をカテゴリ数で割り、少数カテゴリに集中している語を上位に出す。
    active = set(categories)
    if "global" not in active or len(active) > 1:
        matched = [it for it in terms if set(it["categories"]) & active]
        matched.sort(
            key=lambda it: it["freq"] / max(1, len(it["categories"])),
            reverse=True)
        for item in matched:
            add(item["term"])
    # 3. グローバル頻出語（カテゴリを問わず頻出するものを残り枠で拾う）
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
                 budget: int = TOKEN_BUDGET,
                 use_vault_vocab: bool = False) -> str:
    """initial_prompt 形式（リード文＋語彙）を返す。

    initial_prompt はリード文も同じ予算を消費するため、その分を差し引いた
    残りを語彙に割り当てる（build_hotwords 単体は語彙のみで予算を使う想定）。
    """
    head = LEAD + "用語: "
    hot = build_hotwords(
        vocabulary, corrections, categories,
        max(0, budget - count_tokens(head)),
        use_vault_vocab=use_vault_vocab)
    return head + hot if hot else LEAD
