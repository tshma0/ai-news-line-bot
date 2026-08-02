import os
import json
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

# RSSフィード一覧（Google News RSS）
RSS_URLS = [
    "https://news.google.com/rss/search?q=OpenAI&hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=Anthropic&hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=Google+Gemini&hl=ja&gl=JP&ceid=JP:ja",
]

# スコアリングルール
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


def select_top_articles(articles):
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
    return scored_articles[:2]


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

    candidate_models = ["gemini-2.5-flash", "gemini-2.0-flash"]

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
            model = google_generativeai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content(prompt)
            if response and getattr(response, "text", None):
                return response.text.strip()
        except Exception as e:
            print(f"Gemini Summarize Error: {e}")

    return None

def send_line_message(text):
    if len(text) > 1900:
        text = text[:1900] + "\n...(省略)"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}]
    }
    res = requests.post("https://api.line.me/v2/bot/message/push", headers=headers, json=payload)
    print(f"LINE API Status Code: {res.status_code}")
    print(f"LINE API Response: {res.text}")
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

    # スコア上位（最大2件）を要約して1通で送る
    messages = []
    for item in articles_to_notify[:2]:
        summary = summarize_with_gemini(item["title"], item["link"])
        if summary:
            msg_block = f"📰 【AIニュース】(Score: {item['score']})\n■ {item['title']}\n\n{summary}\n\n🔗 {item['link']}"
        else:
            msg_block = f"📰 【AIニュース速報】(Score: {item['score']})\n■ {item['title']}\n\n🔗 {item['link']}"
        messages.append(msg_block)

    full_message = "\n\n" + ("="*20) + "\n\n".join(messages)
    
    # LINEに送信
    if send_line_message(full_message):
        save_history(new_notified_urls)
        print("LINEへの送信が成功しました。")

if __name__ == "__main__":
    main()