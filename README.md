# ai-news-line-bot

Google News RSS から AI 関連ニュースを取得し、Gemini で要約して LINE に通知するスクリプトです。

## 使い方

1. 依存関係をインストールします。
   - `py -m pip install -r requirements.txt`
2. 環境変数を設定します。
   - `set LINE_CHANNEL_ACCESS_TOKEN=...`
   - `set LINE_USER_ID=...`
   - `set GEMINI_API_KEY=...`
3. スクリプトを実行します。
   - `py fetch_news.py`

## 仕様

- Google News RSS から OpenAI / Anthropic / Google Gemini 関連の記事を取得します。
- 記事タイトルのキーワードからスコアを計算し、60 点以上の記事だけを抽出します。
- Gemini API で要約を生成し、上位 2 件をまとめて LINE に通知します。
- `notified_history.json` で重複通知を防止します。
