import os
import sys
import json
import re
import difflib
from html import escape
import requests
import feedparser


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')


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


def normalize_title(title):
    """タイトルから出展・メディア表記や記号を除去して正規化する"""
    t = re.sub(r'[\(（].*?[\)）]', '', title)
    t = re.sub(r'\s*[\-\|ー—]\s*.*$', '', t)
    t = re.sub(r'[^\w\s]', '', t)
    return t.strip().lower()

def is_similar_title(t1, t2, threshold=0.55):
    """2つのニュースタイトルが類似しているかチェックする"""
    n1 = normalize_title(t1)
    n2 = normalize_title(t2)

    if not n1 or not n2:
        return False

    if n1 in n2 or n2 in n1:
        return True

    ratio = difflib.SequenceMatcher(None, n1, n2).ratio()
    if ratio >= threshold:
        return True

    words1 = set(re.findall(r'\w{2,}', n1))
    words2 = set(re.findall(r'\w{2,}', n2))
    if words1 and words2:
        jaccard = len(words1 & words2) / len(words1 | words2)
        if jaccard >= 0.45:
            return True

    return False

def filter_similar_articles(articles):
    """スコアが高い順にソートされた記事から、重複・類似トピックを除外する"""
    unique_articles = []
    for article in articles:
        is_dup = False
        for saved in unique_articles:
            if is_similar_title(article["title"], saved["title"]):
                is_dup = True
                print(f"[重複除外] 「{article['title']}」 (類似元: 「{saved['title']}」)")
                break
        if not is_dup:
            unique_articles.append(article)
    return unique_articles


def build_notification_messages(top_articles, summaries, other_count, sheet_webapp_url=""):
    """
    スコア上位ニュース（最大5件）とその他件数・Web App URLを1つの集約メッセージとして組み立てる。
    """
    blocks = ["🤖【本日のAIニュース厳選まとめ】\n"]
    for i, (article, summary) in enumerate(zip(top_articles, summaries), 1):
        if summary:
            block = f"{i}. 📰 (Score: {article['score']})\n■ {article['title']}\n{summary}\n🔗 {article['link']}"
        else:
            block = f"{i}. 📰 【速報】(Score: {article['score']})\n■ {article['title']}\n🔗 {article['link']}"
        blocks.append(block)

    footer = "\n--------------------------------"
    if other_count > 0:
        footer += f"\n📊 その他 {other_count} 件のニュースを以下のウェブアプリに追加しました："
    else:
        footer += "\n📊 本日のニュース一覧は以下のウェブアプリで確認できます："

    if sheet_webapp_url:
        footer += f"\n🔗 {sheet_webapp_url}"
    else:
        footer += f"\n(※Web App URL未設定)"

    blocks.append(footer)

    full_message = "\n\n".join(blocks)

    if len(full_message) <= MAX_LINE_MESSAGE_LENGTH:
        return [full_message]

    # 文字数制限(1900文字)を超える場合のフォールバック分割処理
    messages = []
    current_msg = ""
    for block in blocks:
        if len(current_msg) + len(block) + 2 <= MAX_LINE_MESSAGE_LENGTH:
            current_msg = (current_msg + "\n\n" + block).strip()
        else:
            if current_msg:
                messages.append(current_msg)
            current_msg = block
    if current_msg:
        messages.append(current_msg)

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

    # 類似・重複トピックのニュースを除外
    articles_to_notify = filter_similar_articles(articles_to_notify)
    print(f"類似除外後の通知対象件数: {len(articles_to_notify)}")

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

    # スコア上位（最大5件）を要約して1つのまとめメッセージに送る
    top_articles = articles_to_notify[:5]
    other_count = len(articles_to_notify) - len(top_articles)

    summaries = []
    for item in top_articles:
        summary = summarize_with_gemini(item["title"], item["link"])
        summaries.append(summary)

    messages = build_notification_messages(top_articles, summaries, other_count, NEWS_SHEETS_WEBAPP_URL)

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