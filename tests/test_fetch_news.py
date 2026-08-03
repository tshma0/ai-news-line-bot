import importlib.util
from pathlib import Path
import os


MODULE_PATH = Path(__file__).resolve().parents[1] / "fetch_news.py"

spec = importlib.util.spec_from_file_location("fetch_news", MODULE_PATH)
fetch_news = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_news)


# === 1. NEWS_SHEETS_WEBAPP_URL 単一認識＆LINEメッセージ埋め込みテスト ===
def test_get_webapp_url_only_uses_news_sheets_webapp_url(monkeypatch):
    monkeypatch.setenv("NEWS_SHEETS_WEBAPP_URL", "https://script.google.com/macros/s/test_app/exec")
    monkeypatch.delenv("SHEETS_WEBAPP_URL", raising=False)
    
    assert fetch_news.get_webapp_url() == "https://script.google.com/macros/s/test_app/exec"

    articles = [{"title": "OpenAIが発表", "score": 90, "link": "https://example.com/1"}]
    summaries = ["【要約】OpenAI新機能の発表について"]

    messages = fetch_news.build_notification_messages(
        articles, summaries, other_count=5, sheet_webapp_url=fetch_news.get_webapp_url()
    )

    assert len(messages) >= 1
    assert "https://script.google.com/macros/s/test_app/exec" in messages[0]
    assert "(※Web App URL未設定)" not in messages[0]


def test_get_webapp_url_unsets_shows_not_set(monkeypatch):
    monkeypatch.delenv("NEWS_SHEETS_WEBAPP_URL", raising=False)
    
    assert fetch_news.get_webapp_url() == ""

    articles = [{"title": "OpenAIが発表", "score": 90, "link": "https://example.com/1"}]
    summaries = ["【要約】テスト"]

    messages = fetch_news.build_notification_messages(
        articles, summaries, other_count=5, sheet_webapp_url=fetch_news.get_webapp_url()
    )

    assert "(※Web App URL未設定)" in messages[0]


# === 2. URL直リンク解像 ＆ 短縮化テスト ===
def test_resolve_final_url_decodes_google_news_links():
    google_url = "https://news.google.com/rss/articles/CBMibkFVX3lxTFBIY21hdkJzVEpTTHM4cGFlTTlxZ3FMNm9NdzVCZUd1RnZBU2lOQUpEMXpMWDJfbVlLME5uQ1U4bnNVN05YbFVBVUZYYzZ0cnJkYzNmbjdyb1dmdmQtZnZlZUdTbUlCN2ZPeVFxekdB"
    resolved = fetch_news.resolve_final_url(google_url)

    assert "news.google.com/rss/articles/" not in resolved
    assert "http" in resolved


# === 3. 重複排除 ＆ 5大カテゴリ均等配分テスト ===
def test_filter_similar_articles_removes_duplicates():
    articles = [
        {"title": "OpenAI、ChatGPTの新機能を発表 - ITmedia", "score": 100},
        {"title": "OpenAI、ChatGPTの新機能を発表 - Yahoo!ニュース", "score": 95},
    ]
    unique = fetch_news.filter_similar_articles(articles)
    assert len(unique) == 1
    assert unique[0]["title"] == "OpenAI、ChatGPTの新機能を発表 - ITmedia"


def test_select_balanced_articles_distributes_categories():
    articles = [
        {"title": "Google Geminiの新機能", "score": 100},
        {"title": "OpenAI ChatGPTのアップデート", "score": 90},
        {"title": "Claude Sonnetの発表", "score": 85},
        {"title": "フィジカルAIロボットアーム導入", "score": 80},
        {"title": "Python開発自動化コード生成Copilot", "score": 75},
    ]
    balanced = fetch_news.select_balanced_articles(articles, 5)
    assert len(balanced) == 5
    categories = [fetch_news.detect_category(a["title"]) for a in balanced]
    assert set(categories) == {"google", "openai", "claude", "physical", "business"}


# === 4. スコアリング ＆ キーワード判定テスト ===
def test_score_title_highlights_ai_releases():
    title = "OpenAIがGPT-4.1を発表し、APIをリリース"
    assert fetch_news.score_title(title) >= 60


def test_get_max_saved_articles_uses_env_override(monkeypatch):
    monkeypatch.setenv("MAX_SAVED_ARTICLES", "7")
    assert fetch_news.get_max_saved_articles() == 7


# === 5. Web App UI / スマホ 3段レイアウト検証 ===
def test_build_dashboard_html_contains_mobile_layout():
    articles = [
        {"title": "Google Gemini新機能を発表", "link": "https://example.com/1", "score": 150, "saved_at": "2026/8/3"},
        {"title": "Anthropic Claude Codeをリリース", "link": "https://example.com/2", "score": 120, "saved_at": "2026/8/3"},
    ]

    html = fetch_news.build_dashboard_html(articles, "https://script.google.com/macros/s/test/exec")

    assert "card-header-row" in html
    assert "card-title-row" in html
    assert "card-footer-row" in html
    assert "scroll-to-top-btn" in html
    assert "https://script.google.com/macros/s/test/exec" in html
