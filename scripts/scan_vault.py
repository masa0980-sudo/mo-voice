"""Obsidian vault 語彙スキャンの単体実行CLI。

使い方: python -X utf8 scripts/scan_vault.py [vault_path]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vocab.scanner import scan_vault, DATA_FILE


def main():
    if len(sys.argv) > 1:
        vault = sys.argv[1]
    else:
        config = json.loads(
            (Path(__file__).resolve().parent.parent / "config.json")
            .read_text(encoding="utf-8"))
        vault = config["vault_path"]

    print(f"スキャン中: {vault}")
    result = scan_vault(vault)
    print(f"完了: {result['file_count']} ファイル / {len(result['terms'])} 語彙")
    print(f"保存先: {DATA_FILE}")
    print("\n上位20語:")
    for item in result["terms"][:20]:
        cats = ",".join(item["categories"])
        print(f"  {item['term']}  (freq={item['freq']}, cat={cats})")


if __name__ == "__main__":
    main()
