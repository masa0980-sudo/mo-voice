# MO Voice

Windows常駐型の音声入力（ディクテーション）アプリです。ホットキーを押して話すと、
**今カーソルがある入力欄にそのままテキストが入ります**。メモ帳でも、ブラウザでも、
エディタでも、アプリを選びません。

音声認識は [faster-whisper](https://github.com/SYSTRAN/faster-whisper) を使い、
**すべてお使いのPC内で処理されます**。音声もテキストも外部に送信しません。

開発の経緯は note の記事
「[Claude Codeで自分専用の音声入力アプリを作ってみた](https://note.com/mo0980/n/n2b17e64f4599)」（無料）に書いています。

## 特徴

- **どのアプリでも使える** — 常駐してホットキーを待ち、アクティブな入力欄に注入します
- **完全オフライン動作** — 初回のモデルダウンロード以降、ネットワーク接続は不要です
- **誤認識を覚える** — 間違いを一度直すと、次から自動で同じ修正が適用されます
- **自信のない箇所を色で示す** — 認識が怪しい部分を黄色でハイライトします

## 動作環境

| | |
|---|---|
| OS | **Windows 専用**（クリップボード操作・ウィンドウ制御にWin32 APIを使用） |
| Python | 3.10 以降（3.13 で動作確認） |
| GPU | 不要（CPUのみで動作。開発環境は Core i7-8565U） |
| ディスク | モデル用に約500MB（初回起動時に自動ダウンロード） |

認識速度の目安は、20秒の発話でおよそ5〜7秒です（CPU・`small`モデル）。

## インストール

```bash
git clone https://github.com/masa0980-sudo/mo-voice.git
cd mo-voice
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

> **起動が極端に遅い場合は専用の仮想環境を作ってください。**
> 多数のパッケージが入ったPython環境を共用していると、`import faster_whisper` だけで
> 60秒以上かかることがあります（`ctranslate2` が推論に不要な `transformers` を
> 読み込むため）。専用venvにすると10秒程度になります。

## 起動

```bash
.venv\Scripts\python -X utf8 main.py
```

初回起動時は音声モデル（約500MB）を自動ダウンロードします。タスクトレイのアイコンが
**グレー（読み込み中）→ 緑（準備完了）** に変われば使えます。

### タスクバーに常に表示させる

通知領域のアイコンは既定で「隠れているアイコン」の中に入ります。常に表示したい場合は、
`^` を押して出るポップアップから**アイコンをタスクバーへドラッグ**するか、
タスクバーを右クリック →「タスクバーの設定」→「その他のシステム トレイ アイコン」で
オンにしてください。

> Windows は**実行ファイル単位**でトレイアイコンを管理します。MO Voice は Python
> インタープリタ上で動くため、この一覧には **`python`** と表示されます。
> 他にも Python 製の常駐アプリを使っている場合は、アイコンの絵で見分けてください。

### 常駐させる（任意）

PC起動時に自動で立ち上げたい場合は、タスクスケジューラに登録します。

```powershell
$action = New-ScheduledTaskAction -Execute "<インストール先>\.venv\Scripts\python.exe" `
  -Argument "-X utf8 main.py" -WorkingDirectory "<インストール先>"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden
Register-ScheduledTask -TaskName "MO Voice" -Action $action -Trigger $trigger `
  -Settings $settings
```

## 使い方

| 操作 | 動作 |
|------|------|
| `Ctrl + Alt + Space` | 録音開始 → もう一度押すと停止・認識・入力欄へ注入 |
| `Ctrl + Alt + Z` | 直前の結果を修正する（**修正内容を学習します**） |
| 画面下のバーをクリック | 録音をキャンセル |

### 誤認識を覚えさせる

1. 音声入力する
2. 間違っていたら `Ctrl + Alt + Z`
3. 正しい文に直して **OK**

これで入力欄のテキストも置き換わり、同時に「誤り→正解」のペアを記憶します。
次回から同じ誤認識は自動で修正されます。

認識に自信がない箇所があった場合は、この修正ダイアログが**自動で開き**、
該当箇所が黄色でハイライトされます。

## 設定

トレイアイコンを右クリック →「**設定...**」で変更できます。4つのタブがあります。

| タブ | 内容 |
|------|------|
| 基本 | ホットキー、録音の最大時間、文字の入力方法 |
| 認識 | モデル、演算精度、言語、ビームサイズ、低信頼語ハイライト |
| 学習した修正 | 覚えた誤認識の一覧・絞り込み・削除 |
| Obsidian 連携 | オン/オフ、vault フォルダの選択 |

ホットキーは**他のアプリと衝突しにくい候補から選ぶ**方式です。`Ctrl+C` のような
常用ショートカットを奪ってしまうと、原因の分かりにくい不具合になるためです。
候補以外を使いたい場合は「その他」を選ぶと自由に指定できます。

> **「学習した修正」タブを一度は見てください。** 文全体を大きく書き換えたときの
> 断片が誤って学習されていることがあります。意図しない置換が起きている場合は
> その行を削除してください。

変更はすぐ反映されます。ただし**モデル・演算精度・言語・ビームサイズの4項目だけは
再起動が必要**です（起動時にモデルを読み込むため）。保存時に案内が出ます。

### config.json を直接編集する場合

| 項目 | 既定値 | 説明 |
|------|--------|------|
| `hotkey_toggle` | `<ctrl>+<alt>+<space>` | 録音のオン/オフ |
| `hotkey_correct` | `<ctrl>+<alt>+z` | 修正ダイアログを開く |
| `model` | `small` | `tiny`/`base`/`small`/`medium`/`large-v3`。大きいほど高精度で低速 |
| `compute_type` | `int8` | CPUでは `int8` が最速 |
| `language` | `ja` | 認識する言語 |
| `beam_size` | `2` | 探索幅。大きいほど高精度で低速 |
| `max_record_seconds` | `300` | 録音の上限（秒） |
| `injection_method` | `clipboard` | `clipboard` または `sendinput` |
| `confidence_highlight` | `{enabled: true, threshold: 0.6}` | 低信頼箇所の自動ハイライト |
| `use_vault_vocab` | `false` | Obsidian語彙をヒントに使うか（下記参照） |
| `vault_path` | `""` | Obsidian vault のパス（空なら連携なし） |
| `context_rules` | — | アプリごとに使う語彙カテゴリの指定 |

設定を変えたらアプリを再起動してください。
`config.json` が無い・壊れている場合も、既定値で起動して理由を通知します。

### Obsidian連携について（既定オフ）

Obsidian vault からユーザー固有の用語を抽出し、認識のヒントに使う機能があります。
ただし**既定では無効**です。実発話11件でA/B測定したところ、有効にすると
平均一致率が 0.9019 → 0.8958 と**わずかに悪化**したためです。

固有名詞が改善する例がある一方、普通の日本語が崩れる例
（`印字` → `インジン`、`機種` → `記者`）の方が上回りました。
「ノートに書いてある語＝発話される語」とは限らない、というのが理由と考えています。

一方、`Ctrl+Alt+Z` による**修正学習は実績ベースなので有効に機能します**。
こちらは常に有効です。

試したい場合は `vault_path` を設定したうえで `use_vault_vocab` を `true` にしてください。

## プライバシー

音声認識はすべてローカルで完結し、音声・テキストとも外部送信しません。
ただし以下がPC内に保存される点はご承知おきください（`data/` 配下・gitignore済み）。

| ファイル | 内容 |
|---|---|
| `data/history.jsonl` | 認識したテキスト全文、アクティブアプリ名、ウィンドウタイトル（80文字まで） |
| `data/corrections.json` | 学習した「誤り→正解」のペア |
| `data/corrections_log.jsonl` | 修正の前後テキスト全文 |
| `data/audio/*.wav` | 発話音声（直近50件のリングバッファ） |
| `data/app.log` | 注入処理のログ（テキスト先頭40文字を含む） |

**ウィンドウタイトルには閲覧中のファイル名やページ名が含まれます。**
不要な場合はこれらのファイルを削除してください（起動時に再生成されます）。
なお `data/` は `.gitignore` 済みなので、リポジトリに混入することはありません。

## うまく動かないとき

| 症状 | 対処 |
|------|------|
| トレイアイコンが灰色のまま | モデルをダウンロード中です（初回は数分）。失敗した場合はオレンジ色になり、メニューに「モデルを再読み込み」が出ます |
| 「マイクが見つかりません」 | Windowsの「サウンド設定」で既定の入力デバイスを確認してください |
| 「マイクへのアクセスが拒否されました」 | 設定 → プライバシーとセキュリティ → マイク → デスクトップアプリのマイクアクセスをオン |
| 入力欄にテキストが入らない | 録音〜認識中に別ウィンドウへ切り替えると、録音時のウィンドウへ戻って注入を試みます。それも失敗した場合はクリップボードに残るので `Ctrl+V` で貼れます |
| 起動に1分以上かかる | 上記「インストール」の注記を参照（専用venvを作ってください） |
| ホットキーが効かない | 他のアプリが同じキーを使っている可能性があります。MO Voice はキーを横取りしないので、両方が同時に反応します。「設定...」から別の候補に変えてください |
| トレイアイコンが見つからない | 既定では「隠れているアイコン」の中に入ります。上記「タスクバーに常に表示させる」を参照 |

ログは `data/app.log` に出ます。

導入から実運用までのつまずきどころ（起動が2〜3分かかる問題の解決、入力欄に
文字が入らない5つの原因、誤認識の育て方、実運用中の `config.json` 全文など）を
1本にまとめた解説記事も用意しています:
[【無料ツール付き】話すだけで文字入力！Windows音声入力アプリ導入＆実運用ガイド](https://brain-market.com/u/ma0980/a/b4YjNyYjMgoTZsNWa0JXY)（有料・980円）

## 既知の制約

- **Windows専用**です。macOS / Linux では動作しません
- クリップボード方式の注入では、貼り付け時に一瞬クリップボードを使います
  （元の内容は復元します。画像などテキスト以外が入っている場合は、
  それを壊さないよう自動でSendInput方式に切り替えます）
- 30秒を超える発話では、認識の後半で語彙ヒントが効きにくくなります
  （Whisperの仕様。分割処理も試しましたが、処理時間が76%増える割に品質が
  悪化したため採用していません）

## ライセンス

**GPL v3** — 詳細は [LICENSE](LICENSE) を参照してください。

GUIに **PyQt5（GPL v3）**、読み正規化に **pykakasi（GPL-3.0-or-later）** を
使用しているため、本ソフトウェアもGPL v3で配布します。

### 主な依存パッケージ

| パッケージ | ライセンス | 用途 |
|---|---|---|
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | 音声認識 |
| [PyQt5](https://www.riverbankcomputing.com/software/pyqt/) | **GPL v3** | GUI（トレイ・ダイアログ） |
| [pynput](https://github.com/moses-palmer/pynput) | LGPL v3 | グローバルホットキー |
| [PyAudio](https://people.csail.mit.edu/hubert/pyaudio/) | MIT | マイク録音 |
| [pywin32](https://github.com/mhammond/pywin32) | PSF | クリップボード・ウィンドウ操作 |
| [psutil](https://github.com/giampaolo/psutil) | BSD-3-Clause | プロセス情報・優先度制御 |
| [pykakasi](https://codeberg.org/miurahr/pykakasi) | **GPL-3.0-or-later** | 読み正規化（任意。未導入でも動作） |
| [numpy](https://numpy.org/) | BSD-3-Clause | 音声データ処理 |

音声モデルは [Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small)
（MIT）を初回起動時にダウンロードします。

## 関連リンク

- [Claude Codeで自分専用の音声入力アプリを作ってみた](https://note.com/mo0980/n/n2b17e64f4599) — 開発の経緯・設計の裏話（note・無料）
- [【無料ツール付き】話すだけで文字入力！Windows音声入力アプリ導入＆実運用ガイド](https://brain-market.com/u/ma0980/a/b4YjNyYjMgoTZsNWa0JXY) — 導入〜実運用の完全ガイド（Brain・980円）
- [作者のnote（MO）](https://note.com/mo0980) — Claude Code・AI活用の記事を書いています
