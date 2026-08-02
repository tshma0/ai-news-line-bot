import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import requests
from google import generativeai as genai


RSS_URLS = [
    "https://news.google.com/rss/search?q=OpenAI&hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=Anthropic&hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=Google%20Gemini&hl=ja&gl=JP&ceid=JP:ja",
]

HISTORY_PATH = "notified_history.json"


def load_history() -> Dict[str, Any]:
    if not os.path.exists(HISTORY_PATH):
        return {"articles": []}
    with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_history(history: Dict[str, Any]) -> None:
    with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, indent=2)


def score_title(title: str) -> int:
    text = title.lower()
    score = 0
    keywords = [
        ("openai", 25),
        ("anthropic", 25),
        ("google", 20),
        ("gemini", 20),
        ("発表", 15),
        ("リリース", 15),
        ("新モデル", 15),
        ("ai", 10),
        ("llm", 10),
        ("model", 10),
        ("chatgpt", 20),
        ("claude", 20),
        ("deepmind", 20),
        ("copilot", 15),
    ]
    for keyword, value in keywords:
        if keyword in text:
            score += value
    if re.search(r"(が|を|で).*(発表|リリース|公開)", text):
        score += 10
    return min(score, 100)


def select_top_articles(articles: List[Dict[str, Any]], limit: int = 2) -> List[Dict[str, Any]]:
    scored = []
    for article in articles:
        title = article.get("title", "")
        score = score_title(title)
        if score >= 60:
            scored.append({**article, "score": score})
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:limit]


def fetch_rss_articles() -> List[Dict[str, Any]]:
    articles: List[Dict[str, Any]] = []
    for url in RSS_URLS:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = getattr(entry, "title", "") or ""
            link = getattr(entry, "link", "") or ""
            if title and link:
                articles.append({"title": title, "url": link})
    return articles


def summarize_article(title: str, url: str, api_key: str) -> str:
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = (
        "次のニュース記事について、日本語で箇条書き3行以内で要約してください。"
        f"\nタイトル: {title}\nURL: {url}"
    )
    response = model.generate_content(prompt)
    text = getattr(response, "text", "") or ""
    return text.strip() or "要約を生成できませんでした。"


def send_line_notification(message: str, token: str, user_id: str) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": message}],
    }
    response = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers=headers,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()


def main() -> None:
    line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    line_user_id = os.getenv("LINE_USER_ID")
    gemini_api_key = os.getenv("GEMINI_API_KEY")

    if not line_token or not line_user_id or not gemini_api_key:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, GEMINI_API_KEY を環境変数に設定してください")

    history = load_history()
    notified_urls = {item.get("url") for item in history.get("articles", []) if item.get("url")}

    articles = fetch_rss_articles()
    filtered = [article for article in articles if article.get("url") not in notified_urls]
    selected = select_top_articles(filtered)

    if not selected:
        print("新しい関連記事はありません")
        return

    summaries = []
    for article in selected:
        summary = summarize_article(article["title"], article["url"], gemini_api_key)
        summaries.append((article["title"], article["url"], summary))

    lines = ["AI関連ニュースの要約です。"]
    for title, url, summary in summaries:
        lines.append(f"- {title}\n{summary}\n{url}")
    message = "\n\n".join(lines)

    send_line_notification(message, line_token, line_user_id)

    history.setdefault("articles", [])
    history["articles"].extend(
        [{"url": article["url"], "title": article["title"], "notified_at": datetime.now(timezone.utc).isoformat()} for article in selected]
    )
    save_history(history)


if __name__ == "__main__":
    main()
