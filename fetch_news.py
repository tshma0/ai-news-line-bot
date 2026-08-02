import os
import json
from html import escape
import requests
import feedparser

try:
    from google import genai as google_genai
except ImportError:
    google_genai = None

try:
    import google.generativeai as google_generativeai
except ImportError:
    google_generativeai = None

# === 環境変数から設定を取得 ===
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 記事重複チェック用のキャッシュファイル
HISTORY_FILE = "notified_history.json"
DEFAULT_MAX_SAVED_ARTICLES = 30
NEWS_DASHBOARD_FILE = "news_dashboard.html"
NEWS_SHEETS_WEBAPP_URL = (
    os.environ.get("NEWS_SHEETS_WEBAPP_URL") or os.environ.get("SHEETS_WEBAPP_URL") or ""
)

# RSSフィード一覧（Google News RSS）
RSS_URLS = [
    "https://news.google.com/rss/search?q=OpenAI&hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=Anthropic&hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=Google+Gemini&hl=ja&gl=JP&ceid=JP:ja",
]

# スコアリングルール
MAX_LINE_MESSAGE_LENGTH = 1900
SCORE_RULES = {
    "companies": {
        "openai": 50, "anthropic": 50, "google": 50, "gemini": 50, "claude": 50,
        "meta": 40, "xai": 40, "grok": 40, "llama": 40, "mistral": 30,
    },
    "release_keywords": {
        "発表": 20, "リリース": 20, "新モデル": 25, "launch": 20, "release": 20,
        "introducing": 20, "update": 15, "available": 10,
    },
    "topic_keywords": {
        "agent": 10, "mcp": 15, "reasoning": 10, "multimodal": 10, "api": 5,
    },
    "min_score": 60,
}

def load_history():
    """履歴ファイル(リスト形式 []) を安全に読み込む"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(data)
                elif isinstance(data, dict):
                    return set(data.get("urls", []))
        except Exception as e:
            print(f"History load warning: {e}")
            return set()
    return set()

def save_history(history):
    """通知済みURLのセットをJSON配列として保存"""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(list(history), f, ensure_ascii=False, indent=2)

def get_max_saved_articles():
    """保存するニュース件数の上限を環境変数またはデフォルト値から取得する。"""
    raw_value = os.environ.get("MAX_SAVED_ARTICLES", str(DEFAULT_MAX_SAVED_ARTICLES))
    try:
        return max(1, int(raw_value))
    except ValueError:
        return DEFAULT_MAX_SAVED_ARTICLES


def score_title(title):
    score = 0
    title_lower = title.lower()

    for word, pts in SCORE_RULES["companies"].items():
        if word in title_lower:
            score += pts
    for word, pts in SCORE_RULES["release_keywords"].items():
        if word in title_lower:
            score += pts
    for word, pts in SCORE_RULES["topic_keywords"].items():
        if word in title_lower:
            score += pts

    return score


def select_top_articles(articles, limit=2):
    scored_articles = []
    for article in articles:
        title = article.get("title", "")
        score = score_title(title)
        scored_articles.append({
            "title": title,
            "score": score,
            "url": article.get("url") or article.get("link") or "",
        })

    scored_articles.sort(key=lambda x: x["score"], reverse=True)
    return scored_articles[:limit]


def build_dashboard_payload(articles, sheet_url):
    representative_news = [
        {"title": article["title"], "link": article.get("link") or article.get("url") or ""}
        for article in articles[:5]
    ]
    return {
        "representative_news": representative_news,
        "saved_count": len(articles),
        "sheet_link": sheet_url or "",
    }


def build_dashboard_html(articles, sheet_url):
    payload = build_dashboard_payload(articles, sheet_url)
    items_html = ""
    for item in payload["representative_news"]:
        link = escape(item.get("link") or "")
        title = escape(item.get("title") or "")
        if link:
            items_html += f'<li><a href="{link}" target="_blank" rel="noreferrer">{title}</a></li>'
        else:
            items_html += f"<li>{title}</li>"

    if payload["sheet_link"]:
        sheet_link_html = (
            f'<p><strong>保存先:</strong> <a href="{escape(payload["sheet_link"])}" '
            'target="_blank" rel="noreferrer">Google Sheets</a></p>'
        )
    else:
        sheet_link_html = "<p><strong>保存先:</strong> 未設定</p>"

    return f"""<!doctype html>
<html lang=\"ja\">
<head>
  <meta charset=\"utf-8\" />
  <title>AIニュースダッシュボード</title>
  <style>
    body {{ font-family: sans-serif; margin: 2rem; line-height: 1.6; }}
    .card {{ border: 1px solid #ddd; padding: 1rem 1.25rem; border-radius: 8px; max-width: 900px; }}
    li {{ margin-bottom: 0.5rem; }}
  </style>
</head>
<body>
  <div class=\"card\">
    <h1>AIニュースダッシュボード</h1>
    <h2>代表ニュース（5件）</h2>
    <ul>{items_html}</ul>
    <p><strong>保存済みニュース件数:</strong> {payload['saved_count']}件</p>
    {sheet_link_html}
  </div>
</body>
</html>
"""


def save_articles_to_sheets(articles, sheet_webapp_url):
    """Google Apps Script Web App にニュースを送って Sheets に保存する。"""
    if not sheet_webapp_url:
        print("Google Sheets への保存先URLが未設定のため、保存をスキップしました。")
        return False

    payload = {
        "news": [
            {
                "title": article["title"],
                "link": article.get("link") or article.get("url") or "",
                "score": article.get("score", 0),
            }
            for article in articles
        ],
        "saved_count": len(articles),
        "sheet_link": sheet_webapp_url,
    }

    try:
        res = requests.post(sheet_webapp_url, json=payload, timeout=20)
    except requests.RequestException as e:
        print(f"Sheets 保存リクエストエラー: {e}")
        return False

    print(f"Sheets API Status Code: {res.status_code}")
    print(f"Sheets API Response: {res.text}")
    return res.status_code in (200, 201)


def summarize_with_gemini(title, url):
    prompt = (
        f"以下のAI関連ニュース記事のタイトルとURLをもとに、内容を推測して日本語で要約してください。\n"
        f"タイトル: {title}\n"
        f"URL: {url}\n\n"
        f"要約は箇条書き3点以内で、各項目は20字以内に収めてください。\n"
        f"「・」で始まる箇条書きのみ出力し、前置き・後書きは不要です。"
    )

    if not GEMINI_API_KEY:
        return None

    candidate_models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"]

    if google_genai is not None:
        try:
            client = google_genai.Client(api_key=GEMINI_API_KEY)
            for model_name in candidate_models:
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                    )
                    if response and getattr(response, "text", None):
                        return response.text.strip()
                except Exception as e:
                    print(f"Gemini ({model_name}) Summarize Warning: {e}")
                    continue
        except Exception as e:
            print(f"Gemini Client Error: {e}")
    elif google_generativeai is not None:
        try:
            model = google_generativeai.GenerativeModel("gemini-2.0-flash")
            response = model.generate_content(prompt)
            if response and getattr(response, "text", None):
                return response.text.strip()
        except Exception as e:
            print(f"Gemini Summarize Error: {e}")

    return None


def build_notification_messages(articles, summaries):
    blocks = []
    for article, summary in zip(articles, summaries):
        if summary:
            block = f"📰 【AIニュース】(Score: {article['score']})\n■ {article['title']}\n\n{summary}\n\n🔗 {article['link']}"
        else:
            block = f"📰 【AIニュース速報】(Score: {article['score']})\n■ {article['title']}\n\n🔗 {article['link']}"
        blocks.append(block)

    messages = []
    for block in blocks:
        if len(block) <= MAX_LINE_MESSAGE_LENGTH:
            messages.append(block)
            continue

        chunks = []
        start = 0
        while start < len(block):
            end = min(start + MAX_LINE_MESSAGE_LENGTH, len(block))
            chunks.append(block[start:end])
            start = end

        messages.extend(chunks)

    return messages

def send_line_message(text):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}]
    }
    try:
        res = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=headers,
            json=payload,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"LINE API Request Error: {e}")
        return False

    print(f"LINE API Status Code: {res.status_code}")
    print(f"LINE API Response: {res.text}")
    if res.status_code != 200:
        print("LINE メッセージ送信に失敗しました。LINE のトークンやユーザー ID を確認してください。")
    return res.status_code == 200

def main():
    if not all([LINE_ACCESS_TOKEN, LINE_USER_ID, GEMINI_API_KEY]):
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, GEMINI_API_KEY を環境変数に設定してください")

    notified_urls = load_history()
    new_notified_urls = set(notified_urls)
    articles_to_notify = []
    total_articles = 0

    print("--- 取得記事一覧とスコア判定 ---")
    for rss_url in RSS_URLS:
        feed = feedparser.parse(rss_url)
        for entry in feed.entries:
            link = entry.link
            title = entry.title
            total_articles += 1

            score = score_title(title)
            passed = score >= SCORE_RULES["min_score"]
            status = "Pass" if passed else "Skip"
            print(f"[Score: {score} / {status}] {title}")

            if link in notified_urls:
                continue

            if passed:
                articles_to_notify.append({
                    "title": title,
                    "link": link,
                    "score": score
                })
                new_notified_urls.add(link)

    print("---------------------------------")
    print(f"取得総件数: {total_articles}")
    print(f"スコア条件をクリアした件数: {len(articles_to_notify)}")

    # スコアが高い順に並び替え
    articles_to_notify.sort(key=lambda x: x["score"], reverse=True)

    if not articles_to_notify:
        print("通知対象の新しいニュースはありませんでした。")
        return

    max_saved_articles = get_max_saved_articles()
    saved_articles = articles_to_notify[:max_saved_articles]
    save_articles_to_sheets(saved_articles, NEWS_SHEETS_WEBAPP_URL)

    dashboard_html = build_dashboard_html(saved_articles, NEWS_SHEETS_WEBAPP_URL)
    with open(NEWS_DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(dashboard_html)
    print(f"ダッシュボードHTMLを {NEWS_DASHBOARD_FILE} に保存しました。")

    # スコア上位（最大2件）を要約して複数メッセージに分けて送る
    top_articles = articles_to_notify[:2]
    summaries = []
    for item in top_articles:
        summary = summarize_with_gemini(item["title"], item["link"])
        summaries.append(summary)

    messages = build_notification_messages(top_articles, summaries)

    # LINEに送信
    success = True
    for message in messages:
        if not send_line_message(message):
            success = False
            break

    if success:
        save_history(new_notified_urls)
        print("LINEへの送信が成功しました。")

if __name__ == "__main__":
    main()