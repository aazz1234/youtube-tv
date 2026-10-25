# YouTube TV（10分未満の動画を自動再生）

ブラウザだけで動く版です（bat・ローカルサーバ不要）。動画リストは GitHub Actions が自動更新します。

## URL
- 埋め込み版（全自動の連続再生）: https://aazz1234.github.io/youtube-tv/
- YouTube本体で再生（Enhancer for YouTube 対応）: https://aazz1234.github.io/youtube-tv/native.html

ブックマークして開くだけです。動画は全ジャンル **10分未満** だけです。

## 動画リストの自動更新
- `.github/workflows/refresh.yml` が **6時間ごと**（UTC `17 */6 * * *`）に `refresh_site.py` を実行し、`videos.json` を更新します。
- 取得は RSS → yt-dlp → だめなら前回の結果を保持。全部失敗したときや、前回の50%未満になるときは更新しません（コミットされません）。
- 手動で今すぐ更新: GitHub のリポジトリ → **Actions** → *refresh video list* → **Run workflow**。
  またはコマンド: `gh workflow run refresh.yml -R aazz1234/youtube-tv`
- ページの 🔄 ボタンは、最新の `videos.json` を取り直すだけです。

## チャンネル・ジャンルを編集する
`channels.json` を GitHub 上で編集（鉛筆アイコン）してコミットするだけです。次回の自動更新（または手動実行）で反映されます。
- `max_duration_sec`: 長さの上限（秒・未満）。既定 600。
- `categories`: ジャンルの id / 表示名 / 色 / 重み（`weight` が大きいほど頻繁に出ます）。
- `channels`: ジャンルごとのチャンネル（`id` は `UC…` のチャンネルID）。`include` / `exclude` はタイトルの正規表現。

## 手元で更新を試す
```
pip install yt-dlp
python refresh_site.py --dry-run   # 書き込まずに結果だけ確認
python refresh_site.py             # videos.json / state.json を更新
```

## ファイル
`index.html` / `native.html`（ページ）, `videos.json`（公開用リスト）, `state.json`（長さ・確認済みの記憶。自動更新用）, `channels.json`, `refresh_site.py`, `ytv_core.py`, `ytv_common.py`
