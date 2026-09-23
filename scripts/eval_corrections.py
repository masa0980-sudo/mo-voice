# -*- coding: utf-8 -*-
"""学習辞書（vocab/corrections.py）の自動置換の効果を、実データで評価する。

  python -X utf8 scripts/eval_corrections.py

1) 単体の回帰チェック（検証で見つかった誤爆の型を固定）
2) data/corrections_log.jsonl を時系列に再生し、各修正の時点では「それより前の修正だけ
   から learn() した辞書」で apply する（未来の正解を使わない＝リーク無し）。
   第3段（読み・綴りの近似照合）の有無で、正解テキストとの文字誤り率（CER）を比較し、
   第3段が置換した語を「正解／有害／不明」に判定して列挙する。
3) data/history.jsonl のうち修正されなかった認識結果に、現在の辞書を当てたときの
   第3段の発火を列挙する（ユーザーが直さなかった文での誤爆の目安）。
個人データ（data/）が無い環境では 2・3 をスキップする。
"""
import difflib
import json
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from vocab import corrections as C  # noqa: E402


def norm(s):
    return "".join(ch for ch in s if ch not in "、。 ，．,.！？!?\n\r")


def cer(ref, hyp):
    r, h = norm(ref), norm(hyp)
    sm = difflib.SequenceMatcher(None, r, h, autojunk=False)
    return 1.0 - sum(b.size for b in sm.get_matching_blocks()) / max(len(r), 1)


def make(pairs=None):
    corr = C.Corrections(Path(tempfile.mkdtemp()) / "corr.json")
    corr._save = lambda: None
    corr.pairs = [dict(wrong=w, right=r, count=1, last_used=0) for w, r in (pairs or [])]
    return corr


def selftest():
    corr = make([("クロードコード", "Claude Code"), ("オリオンフォー", "Orion4"),
                 # 別の誤り「ヒマイチ」を直した学習。正解側「いまいち」と読みが同じ「イマイチ」は直さない
                 ("Oriun4", "Orion4"), ("コーデック", "Codec"), ("ヒマイチ", "いまいち"),
                 ("ピクセルカウンター", "ピクセルカウント"), ("アンドロメダフォー", "Andromeda4")])
    cases = [
        ("今日はプロドコードで作業", "今日はClaude Codeで作業"),         # 読み近似（カタカナ）
        ("Orian4のチェック", "Orion4のチェック"),                          # 綴り近似（英字）
        ("Claude Codeで開発", "Claude Codeで開発"),                        # 正解語の構成語「Code」は触らない
        ("Codeを書いた", "Codeを書いた"),                                  # 「Code」→「Codec」にしない
        ("イマイチだった", "イマイチだった"),                                # 仮名の表記差は直さない
        ("Orionシリーズ", "Orionシリーズ"),                                # 正解語の一部（略称）は触らない
        ("ペクセルカウントを取る", "ピクセルカウントを取る"),                  # 読み近似
        ("プログラムを書く", "プログラムを書く"),                            # 無関係なカタカナ語は触らない
        ("アンドロメダ4の資料", "Andromeda4の資料"),                       # 数字を二重にしない（Andromeda44 にならない）
    ]
    for src, want in cases:
        got = corr.apply(src)
        assert got == want, f"{src!r} → {got!r}（期待 {want!r}）"
    print(f"selftest OK（{len(cases)}件）")


def replay():
    log = APP / "data" / "corrections_log.jsonl"
    if not log.exists():
        print("data/corrections_log.jsonl が無いため時系列評価はスキップ")
        return
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    corr = make()
    sums = {"off": 0.0, "on": 0.0}
    better = worse = n = 0
    verdict = {"正解": [], "有害": [], "不明": []}
    orig_fuzzy = C.Corrections._apply_fuzzy_match
    for r in rows:
        o, t = r.get("original", ""), r.get("corrected", "")
        if o and t and o != t:
            C.Corrections._apply_fuzzy_match = lambda self, text, active: text
            off = corr.apply(o)
            C.Corrections._apply_fuzzy_match = orig_fuzzy
            on = corr.apply(o)
            c0, c1 = cer(t, off), cer(t, on)
            sums["off"] += c0
            sums["on"] += c1
            n += 1
            better += c1 < c0 - 1e-9
            worse += c1 > c0 + 1e-9
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, off, on).get_opcodes():
                if tag == "equal":
                    continue
                w, rt = off[i1:i2], on[j1:j2]
                k = "正解" if (rt in t and w not in t) else ("有害" if (w in t and rt not in t) else "不明")
                verdict[k].append((w, rt))
        if o and t:
            corr.learn(o, t)
    print(f"\n時系列評価（修正あり {n} 件・各時点の過去の修正だけで学習）")
    print(f"  平均CER: 第3段なし {sums['off']/n:.4f} → 第3段あり {sums['on']/n:.4f}  改善{better} 悪化{worse}")
    print("  第3段の置換:", {k: len(v) for k, v in verdict.items()})
    for k, v in verdict.items():
        if v:
            print(f"    {k}: {v}")


def uncorrected():
    hist, log, data = APP / "data/history.jsonl", APP / "data/corrections_log.jsonl", APP / "data/corrections.json"
    if not (hist.exists() and data.exists()):
        return
    logged = set()
    if log.exists():
        logged = {json.loads(l).get("original") for l in log.read_text(encoding="utf-8").splitlines() if l.strip()}
    corr = C.Corrections(data)
    corr._save = lambda: None
    orig_fuzzy = C.Corrections._apply_fuzzy_match
    fires, total = [], 0
    for l in hist.read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        h = json.loads(l)
        text = h.get("text", "")
        if not text or text in logged:
            continue
        total += 1
        # 本番と同じ apply()（第1・2段 → 第3段）で、第3段の有無だけを変えて比べる
        C.Corrections._apply_fuzzy_match = lambda self, t, a: t
        before = corr.apply(text)
        C.Corrections._apply_fuzzy_match = orig_fuzzy
        after = corr.apply(text)
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after).get_opcodes():
            if tag != "equal":
                fires.append((h["ts"][:10], before[i1:i2], after[j1:j2]))
    print(f"\n修正されなかった認識結果 {total} 件への第3段の発火: {len(fires)} 件")
    for f in fires:
        print("   ", f)


if __name__ == "__main__":
    selftest()
    replay()
    uncorrected()
