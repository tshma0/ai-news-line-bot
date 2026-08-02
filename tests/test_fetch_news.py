import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "fetch_news.py"

spec = importlib.util.spec_from_file_location("fetch_news", MODULE_PATH)
fetch_news = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_news)


def test_score_title_highlights_ai_releases():
    title = "OpenAIがGPT-4.1を発表し、APIをリリース"
    assert fetch_news.score_title(title) >= 60


def test_get_max_saved_articles_uses_env_override(monkeypatch):
    monkeypatch.setenv("MAX_SAVED_ARTICLES", "7")
    assert fetch_news.get_max_saved_articles() == 7


def test_score_title_ignores_unrelated_articles():
    title = "ローカルイベントの開催情報"
    assert fetch_news.score_title(title) < 60


def test_select_top_articles_limits_to_two():
    articles = [
        {"title": "OpenAIが発表", "url": "https://example.com/1"},
        {"title": "Anthropicがリリース", "url": "https://example.com/2"},
        {"title": "Googleが新モデルを発表", "url": "https://example.com/3"},
    ]
    selected = fetch_news.select_top_articles(articles)
    assert len(selected) == 2
    assert all(item["title"] for item in selected)


def test_build_notification_messages_splits_long_text():
    articles = [{"title": "OpenAIが発表", "score": 90, "link": "https://example.com/1"}]
    summaries = ["x" * 6000]

    messages = fetch_news.build_notification_messages(articles, summaries)

    assert len(messages) >= 2
    assert all(len(message) <= fetch_news.MAX_LINE_MESSAGE_LENGTH for message in messages)


def test_build_dashboard_html_contains_summary_and_links():
    articles = [
        {"title": "OpenAIが新モデルを発表", "link": "https://example.com/1"},
        {"title": "Anthropicが新機能を公開", "link": "https://example.com/2"},
    ]

    html = fetch_news.build_dashboard_html(articles, "https://example.com/sheet")

    assert "代表ニュース（5件）" in html
    assert "保存済みニュース件数" in html
    assert "https://example.com/1" in html
    assert "https://example.com/sheet" in html
