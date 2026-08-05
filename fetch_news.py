import os
import sys
import json
import re
import difflib
import datetime
from html import escape
import requests
# pyrefly: ignore [missing-import]
import feedparser


if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')


try:
    # pyrefly: ignore [missing-import]
    from google import genai as google_genai  # type: ignore
except ImportError:
    google_genai = None

try:
    # pyrefly: ignore [missing-import]
    import google.generativeai as google_generativeai  # type: ignore
except ImportError:
    google_generativeai = None

def _load_env_file():
    env_paths = [
        r"C:\SecretKey\MyEnvironment.env",
        os.path.join(os.path.dirname(__file__), ".env")
    ]
    for path in env_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            if k.strip() not in os.environ:
                                os.environ[k.strip()] = v.strip()
            except Exception:
                pass

_load_env_file()

# === 環境変数から設定を取得 ===
LINE_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 記事重複チェック用のキャッシュファイル
HISTORY_FILE = "notified_history.json"
DEFAULT_MAX_SAVED_ARTICLES = 30
NEWS_DASHBOARD_FILE = "news_dashboard.html"
MAX_ARTICLE_AGE_DAYS = 7  # 過去7日以内の新鮮なニュースのみを通知対象とする
def get_webapp_url():
    """環境変数 NEWS_SHEETS_WEBAPP_URL から Web App URL を取得する"""
    return os.environ.get("NEWS_SHEETS_WEBAPP_URL", "").strip()

NEWS_SHEETS_WEBAPP_URL = get_webapp_url()

# RSSフィード一覧（Google News RSS: when:7d で直近1週間以内に絞り込み）
RSS_URLS = [
    "https://news.google.com/rss/search?q=OpenAI+when:7d&hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=Anthropic+OR+Claude+when:7d&hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=Google+Gemini+when:7d&hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=Google+Workspace+AI+OR+Gemini+when:7d&hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=フィジカルAI+OR+ヒューマノイド+OR+ロボティクス+AI+when:7d&hl=ja&gl=JP&ceid=JP:ja",
    "https://news.google.com/rss/search?q=AI+業務活用+OR+開発自動化+OR+コード生成+OR+問い合わせ対応+when:7d&hl=ja&gl=JP&ceid=JP:ja",
]

# スコアリングルール
MAX_LINE_MESSAGE_LENGTH = 1900
SCORE_RULES = {
    "companies": {
        "openai": 65, "chatgpt": 65, "anthropic": 65, "claude": 65,
        "google workspace": 70, "workspace": 60, "gemini": 65, "google ai": 55, "google": 40,
        "meta": 45, "xai": 45, "grok": 45, "llama": 45, "mistral": 40,
        "gmail": 50, "google docs": 45, "google ドキュメント": 45, "google ドライブ": 45, "google meet": 45,
    },
    "release_keywords": {
        "新機能": 30, "アップデート": 30, "新モデル": 30, "発表": 20, "リリース": 20,
        "導入": 25, "活用": 25, "launch": 20, "release": 20, "update": 20,
    },
    "topic_keywords": {
        "フィジカルai": 45, "physical ai": 45, "ヒューマノイド": 45, "ロボティクス": 40, "ロボット": 35,
        "コード生成": 40, "業務効率化": 40, "業務活用": 40, "自動化": 30, "開発": 25,
        "copilot": 35, "cursor": 35, "devin": 35, "問い合わせ": 30, "メール": 25,
        "agent": 20, "mcp": 20, "reasoning": 15, "multimodal": 15,
    },
    "min_score": 60,
}

import time
import calendar

def is_recent_article(entry, max_days=14):
    """記事の公開日時が過去 max_days 日以内かチェックする"""
    parsed_time = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed_time:
        return True

    try:
        article_timestamp = calendar.timegm(parsed_time)
        now_timestamp = time.time()
        age_seconds = now_timestamp - article_timestamp
        
        if age_seconds < -172800:
            return False

        return age_seconds <= (max_days * 86400)
    except Exception as e:
        print(f"Date check warning: {e}")
        return True


def load_history():
    """
    履歴ファイル (JSON) を安全に読み込み、
    {"urls": set(), "articles": list()} の辞書形式で返す。
    旧形式 (リスト ["url1", "url2"]) の場合は自動移行する。
    """
    default_res = {"urls": set(), "articles": []}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {"urls": set(data), "articles": []}
                elif isinstance(data, dict):
                    urls = set(data.get("urls", []))
                    articles = data.get("articles", [])
                    return {"urls": urls, "articles": articles}
        except Exception as e:
            print(f"History load warning: {e}")
            return default_res
    return default_res

def clean_old_history(history, max_days=14):
    """max_days 日以上経過した古い articles 履歴を自動クリーンアップする"""
    now = time.time()
    cutoff = now - (max_days * 86400)
    cleaned_articles = []

    for item in history.get("articles", []):
        pub = item.get("published") or 0
        notified = item.get("notified_timestamp") or 0
        ref_time = max(pub, notified)
        if ref_time >= cutoff or ref_time == 0:
            cleaned_articles.append(item)

    history["articles"] = cleaned_articles
    return history

def save_history(history):
    """通知済みURLおよび記事メタデータ（タイトル、概要、日付）のセットをJSONとして保存"""
    history = clean_old_history(history, max_days=14)
    data = {
        "urls": list(history.get("urls", set())),
        "articles": history.get("articles", [])
    }
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_max_saved_articles():
    """保存するニュース件数の上限を環境変数またはデフォルト値から取得する。"""
    raw_value = os.environ.get("MAX_SAVED_ARTICLES", str(DEFAULT_MAX_SAVED_ARTICLES))
    try:
        return max(1, int(raw_value))
    except ValueError:
        return DEFAULT_MAX_SAVED_ARTICLES


def detect_category(title):
    """タイトルからカテゴリ・主要トピックを判定する（トピック優先）"""
    t = title.lower()

    # 1. フィジカルAI/ロボティクス優先判定
    if any(kw in t for kw in ["フィジカルai", "physical ai", "ヒューマノイド", "ロボティクス", "ロボット"]):
        return "physical"

    # 2. 業務活用・開発支援優先判定
    if any(kw in t for kw in ["業務", "効率化", "開発", "コード", "プログラミング", "copilot", "cursor", "devin", "メール", "問い合わせ", "自動化", "導入", "活用", "dx", "社内", "エンタープライズ"]):
        return "business"

    # 3. 各開発企業判定
    if any(kw in t for kw in ["google", "gemini", "workspace", "gmail", "deepmind", "gemma"]):
        return "google"
    elif any(kw in t for kw in ["openai", "chatgpt", "gpt-4", "gpt-5", "sora", "dall-e"]):
        return "openai"
    elif any(kw in t for kw in ["anthropic", "claude"]):
        return "claude"
    else:
        return "other"


def select_balanced_articles(articles, max_total=30):
    """各カテゴリから均等にバランスよく抽出して合計 max_total 件にする"""
    cat_groups = {}
    for a in articles:
        c = detect_category(a.get("title", ""))
        if c not in cat_groups:
            cat_groups[c] = []
        cat_groups[c].append(a)

    for c in cat_groups:
        cat_groups[c].sort(key=lambda x: x.get("score", 0), reverse=True)

    selected = []
    categories = ["business", "physical", "google", "openai", "claude", "other"]

    while len(selected) < max_total:
        added_in_round = False
        for c in categories:
            if cat_groups.get(c) and len(cat_groups[c]) > 0:
                selected.append(cat_groups[c].pop(0))
                added_in_round = True
                if len(selected) >= max_total:
                    break
        if not added_in_round:
            break

    selected.sort(key=lambda x: x.get("score", 0), reverse=True)
    return selected



try:
    # pyrefly: ignore [missing-import]
    from googlenewsdecoder import new_decoderv1  # type: ignore
except ImportError:
    new_decoderv1 = None


import ipaddress
from urllib.parse import urlparse, parse_qs, urlunparse

SUSPICIOUS_TLDS = {".zip", ".mov", ".top", ".click", ".buzz", ".country", ".work", ".space"}

def is_safe_url(url):
    """
    URLのセキュリティ検証を行う。
    1. HTTP / HTTPS スキーム制限 (javascript:, data: 等を拒否)
    2. SSRF対策 (127.0.0.1, 10.x, 192.168.x などのプライベートIP排除)
    3. 危険TLD・ブラックリスト検証
    """
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("http", "https"):
            return False

        hostname = parsed.hostname
        if not hostname:
            return False

        # SSRFチェック (IPアドレス直指定の場合のプライベートIP排除)
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                print(f"[セキュリティ遮断 (SSRF)] プライベートIPアクセスを検知: {hostname}")
                return False
        except ValueError:
            pass

        # 危険TLDチェック
        lower_host = hostname.lower()
        if any(lower_host.endswith(tld) for tld in SUSPICIOUS_TLDS):
            print(f"[セキュリティ遮断 (危険TLD)] {hostname}")
            return False

        return True
    except Exception as e:
        print(f"URL Safety Check Error: {e}")
        return False

def get_domain_name(url):
    """URLから簡潔なドメイン名（例: itmedia.co.jp）を抽出する"""
    if not url:
        return ""
    try:
        hostname = urlparse(url).hostname or ""
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname
    except Exception:
        return ""

def clean_url_parameters(url):
    """外部サービスを使わずに、URLから不要なトラッキングパラメータ（utm_*, oc, fbclid 等）を削ぎ落としてスリム化する"""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)
        cleaned_params = {k: v for k, v in query_params.items() if not (k.startswith("utm_") or k in ["oc", "ref", "fbclid"])}
        new_query = "&".join(f"{k}={v[0]}" for k, v in cleaned_params.items())
        cleaned_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
        return cleaned_url
    except Exception:
        return url.split("?")[0] if ("?" in url and "news.google.com" in url) else url

def shorten_url_if_needed(url):
    """互換性のためのエイリアス（クエリパラメータのスリム化を行う）"""
    return clean_url_parameters(url)


def resolve_final_url(url):
    """Google News RSSの長いURLから実際の元記事の直リンクを取得し、安全性チェックとスリム化を行う"""
    if not url or not is_safe_url(url):
        return url

    final_url = url
    if "news.google.com/rss/articles/" in url:
        # 1. googlenewsdecoder による直リンクデコード
        if new_decoderv1:
            try:
                decoded_res = new_decoderv1(url)
                if isinstance(decoded_res, dict) and decoded_res.get("status") and decoded_res.get("decoded_url"):
                    cand_url = decoded_res["decoded_url"]
                    if is_safe_url(cand_url):
                        final_url = cand_url
            except Exception as e:
                print(f"googlenewsdecoder 解像エラー: {e}")

        # 2. フォールバック: HTTPリダイレクト追跡
        if "news.google.com" in final_url:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            try:
                res = requests.head(url, headers=headers, allow_redirects=True, timeout=4)
                if res.status_code == 200 and "news.google.com" not in res.url and is_safe_url(res.url):
                    final_url = res.url
                else:
                    res = requests.get(url, headers=headers, allow_redirects=True, stream=True, timeout=4)
                    if res.status_code == 200 and "news.google.com" not in res.url and is_safe_url(res.url):
                        final_url = res.url
            except Exception as e:
                pass

    cleaned = clean_url_parameters(final_url)
    return cleaned if is_safe_url(cleaned) else url


def is_google_ai_article(title):
    """Google / Gemini / Workspace 関連のAIニュースか判定する"""
    return detect_category(title) == "google"


def extract_summary(entry):
    """RSS entry から概要文（summary / description）を取得してHTMLタグを除去する"""
    raw = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
    clean_text = re.sub(r'<[^>]+>', '', raw)
    return clean_text.strip()

def score_article(title, summary=""):
    """タイトルと概要文をもとに重要度スコアを計算する（タイトルはフルポイント、概要文は部分加点）"""
    score = 0
    title_lower = title.lower()
    summary_lower = summary.lower() if summary else ""

    for word, pts in SCORE_RULES["companies"].items():
        if word in title_lower:
            score += pts
        elif summary_lower and word in summary_lower:
            score += int(pts * 0.5)

    for word, pts in SCORE_RULES["release_keywords"].items():
        if word in title_lower:
            score += pts
        elif summary_lower and word in summary_lower:
            score += int(pts * 0.5)

    for word, pts in SCORE_RULES["topic_keywords"].items():
        if word in title_lower:
            score += pts
        elif summary_lower and word in summary_lower:
            score += int(pts * 0.5)

    # フィジカルAI・業務活用のボーナス加算
    physical_kw = ["フィジカルai", "physical ai", "ヒューマノイド", "ロボティクス", "ロボット ai"]
    if any(kw in title_lower for kw in physical_kw):
        score += 25
    elif summary_lower and any(kw in summary_lower for kw in physical_kw):
        score += 12

    business_kw = ["業務活用", "業務効率化", "開発自動化", "コード生成", "問い合わせ", "メール返信", "copilot", "cursor"]
    if any(kw in title_lower for kw in business_kw):
        score += 25
    elif summary_lower and any(kw in summary_lower for kw in business_kw):
        score += 12

    return score

def score_title(title):
    return score_article(title, "")



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


def get_category_badge_html(cat):
    badges = {
        "google": '<span class="tag-cat tag-google">🔷 Google AI</span>',
        "openai": '<span class="tag-cat tag-openai">🟢 OpenAI</span>',
        "claude": '<span class="tag-cat tag-claude">🟣 Claude</span>',
        "physical": '<span class="tag-cat tag-physical">🤖 フィジカルAI</span>',
        "business": '<span class="tag-cat tag-business">💼 業務・開発AI</span>',
        "other": '<span class="tag-cat tag-other">🌐 その他AI</span>'
    }
    return badges.get(cat, badges["other"])

def build_dashboard_html(articles, sheet_url):
    """
    過去1週間分の保存済みニュースをスマホ最適化レイアウト、日付別タブフィルター、カテゴリフィルター付きで構築する
    """
    top_5_articles = articles[:5]

    # 日付リスト（降順）の取得
    all_dates = sorted(list(set(a.get("date", "最新") for a in articles)), reverse=True)
    
    date_tabs_html = '<button class="date-btn active" onclick="filterDate(\'all\', this)">全期間</button>'
    for d in all_dates:
        display_d = d
        if len(d) == 10 and "-" in d:
            parts = d.split("-")
            display_d = f"{int(parts[1])}/{int(parts[2])}"
        date_tabs_html += f'<button class="date-btn" onclick="filterDate(\'{d}\', this)">📅 {display_d}</button>'

    # 🏆 トップ5専用カードHTML
    top5_cards_html = ""
    for i, article in enumerate(top_5_articles, 1):
        title = escape(article.get("title", ""))
        link = escape(article.get("link") or article.get("url") or "")
        score = article.get("score", 0)
        saved_at = escape(article.get("saved_at", "🕒 最新"))
        item_date = escape(article.get("date", "最新"))
        cat = detect_category(title)
        cat_badge = get_category_badge_html(cat)

        top5_cards_html += f"""
        <div class="top5-card" data-category="{cat}" data-date="{item_date}">
          <div class="card-header-row">
            <span class="top-rank">🏆 第{i}位</span>
            <span class="badge badge-high">🔥 {score} pts</span>
            {cat_badge}
          </div>
          <h3 class="card-title-row">{title}</h3>
          <div class="card-footer-row">
            <span class="save-time">{saved_at}</span>
            <a href="{link}" target="_blank" rel="noreferrer" class="btn-read">記事を読む &rarr;</a>
          </div>
        </div>
        """

    # 全ニュースカードHTML
    cards_html = ""
    for i, article in enumerate(articles, 1):
        title = escape(article.get("title", ""))
        link = escape(article.get("link") or article.get("url") or "")
        score = article.get("score", 0)
        saved_at = escape(article.get("saved_at", "🕒 最新"))
        item_date = escape(article.get("date", "最新"))
        cat = detect_category(title)
        cat_badge = get_category_badge_html(cat)

        if score >= 120:
            badge_class = "badge-high"
            badge_label = f"🔥 {score} pts"
        elif score >= 80:
            badge_class = "badge-med"
            badge_label = f"⭐ {score} pts"
        else:
            badge_class = "badge-low"
            badge_label = f"📌 {score} pts"

        cards_html += f"""
        <div class="news-card" data-category="{cat}" data-date="{item_date}">
          <div class="card-header-row">
            <span class="card-num">#{i}</span>
            <span class="badge {badge_class}">{badge_label}</span>
            {cat_badge}
          </div>
          <h3 class="card-title-row">{title}</h3>
          <div class="card-footer-row">
            <span class="save-time">{saved_at}</span>
            <a href="{link}" target="_blank" rel="noreferrer" class="btn-read">記事を読む &rarr;</a>
          </div>
        </div>
        """

    sheet_btn_html = ""
    if sheet_url:
        sheet_btn_html = f'<a href="{escape(sheet_url)}" target="_blank" rel="noreferrer" class="header-btn">📊 Google Sheets で見る</a>'

    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AI ニュースダッシュボード</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&family=Outfit:wght@600;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg-color: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent-blue: #38bdf8;
      --accent-purple: #a855f7;
      --accent-gold: #facc15;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Noto Sans JP', sans-serif;
      background-color: var(--bg-color);
      color: var(--text-main);
      padding: 0.75rem 0.5rem;
      line-height: 1.5;
      word-break: break-all;
      overflow-wrap: anywhere;
    }}
    .container {{
      max-width: 1000px;
      margin: 0 auto;
      padding-bottom: 3rem;
    }}
    header {{
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 1rem;
    }}
    .logo-area h1 {{
      font-family: 'Outfit', 'Noto Sans JP', sans-serif;
      font-size: 1.5rem;
      background: linear-gradient(135deg, var(--accent-blue), var(--accent-purple));
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }}
    .logo-area p {{
      color: var(--text-muted);
      font-size: 0.8rem;
      margin-top: 0.2rem;
    }}
    .header-btn {{
      display: block;
      text-align: center;
      background: linear-gradient(135deg, #2563eb, #1d4ed8);
      color: #fff;
      padding: 0.6rem 1rem;
      border-radius: 8px;
      text-decoration: none;
      font-weight: 500;
      font-size: 0.85rem;
    }}

    .filter-section {{
      margin-bottom: 1rem;
    }}
    .filter-title {{
      font-size: 0.8rem;
      color: var(--text-muted);
      margin-bottom: 0.4rem;
      font-weight: 500;
    }}
    .filter-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem;
    }}
    .filter-btn, .date-btn {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text-main);
      padding: 0.35rem 0.7rem;
      border-radius: 20px;
      font-size: 0.75rem;
      font-weight: 500;
      cursor: pointer;
      white-space: nowrap;
      transition: all 0.2s;
    }}
    .filter-btn:hover, .filter-btn.active {{
      background: var(--accent-blue);
      color: #0f172a;
      border-color: var(--accent-blue);
      font-weight: 700;
    }}
    .date-btn:hover, .date-btn.active {{
      background: var(--accent-purple);
      color: #ffffff;
      border-color: var(--accent-purple);
      font-weight: 700;
    }}

    .section-title {{
      font-size: 1.1rem;
      font-weight: 700;
      margin: 1.25rem 0 0.85rem 0;
      display: flex;
      align-items: center;
      gap: 0.4rem;
      color: var(--accent-gold);
    }}
    .top5-container {{
      display: flex;
      flex-direction: column;
      gap: 0.85rem;
      margin-bottom: 2rem;
    }}
    .top5-card {{
      background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
      border: 2px solid rgba(250, 204, 21, 0.45);
      border-radius: 10px;
      padding: 0.85rem 1rem;
      display: flex;
      flex-direction: column;
    }}

    .news-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 0.85rem;
    }}
    @media (min-width: 640px) {{
      .news-grid {{ grid-template-columns: repeat(2, 1fr); }}
      header {{ flex-direction: row; justify-content: space-between; align-items: center; }}
      .header-btn {{ display: inline-block; }}
    }}
    .news-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 0.85rem 1rem;
      display: flex;
      flex-direction: column;
    }}

    .card-header-row {{
      display: flex;
      align-items: center;
      flex-wrap: nowrap;
      gap: 0.4rem;
      margin-bottom: 0.4rem;
      overflow-x: auto;
      white-space: nowrap;
    }}
    .top-rank {{
      font-family: 'Outfit', sans-serif;
      font-weight: 700;
      color: var(--accent-gold);
      font-size: 0.85rem;
      white-space: nowrap;
    }}
    .card-num {{
      font-family: 'Outfit', sans-serif;
      color: var(--text-muted);
      font-size: 0.8rem;
      white-space: nowrap;
    }}
    .badge {{
      font-size: 0.75rem;
      font-weight: 700;
      padding: 0.15rem 0.45rem;
      border-radius: 12px;
      white-space: nowrap;
    }}
    .badge-high {{ background: rgba(234, 179, 8, 0.2); color: #facc15; border: 1px solid rgba(234, 179, 8, 0.4); }}
    .badge-med {{ background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); }}
    .badge-low {{ background: rgba(148, 163, 184, 0.2); color: #cbd5e1; border: 1px solid rgba(148, 163, 184, 0.4); }}
    
    .tag-cat {{
      font-size: 0.75rem;
      font-weight: 700;
      padding: 0.15rem 0.45rem;
      border-radius: 12px;
      white-space: nowrap;
    }}
    .tag-google {{ background: rgba(96, 165, 250, 0.2); color: #93c5fd; border: 1px solid rgba(96, 165, 250, 0.4); }}
    .tag-openai {{ background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.4); }}
    .tag-claude {{ background: rgba(168, 85, 247, 0.2); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.4); }}
    .tag-physical {{ background: rgba(249, 115, 22, 0.2); color: #fb923c; border: 1px solid rgba(249, 115, 22, 0.4); }}
    .tag-business {{ background: rgba(20, 184, 166, 0.2); color: #2dd4bf; border: 1px solid rgba(20, 184, 166, 0.4); }}
    .tag-other {{ background: rgba(148, 163, 184, 0.2); color: #cbd5e1; border: 1px solid rgba(148, 163, 184, 0.4); }}

    .card-title-row {{
      font-size: 0.95rem;
      font-weight: 600;
      color: var(--text-main);
      line-height: 1.45;
      margin: 0.35rem 0 0.65rem 0;
    }}

    .card-footer-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: auto;
      padding-top: 0.35rem;
    }}
    .save-time {{
      font-size: 0.75rem;
      color: var(--text-muted);
      white-space: nowrap;
    }}
    .btn-read {{
      display: inline-block;
      background: rgba(56, 189, 248, 0.15);
      color: var(--accent-blue);
      border: 1px solid rgba(56, 189, 248, 0.4);
      padding: 0.35rem 0.75rem;
      border-radius: 6px;
      text-decoration: none;
      font-size: 0.8rem;
      font-weight: 600;
      white-space: nowrap;
    }}

    .scroll-to-top-btn {{
      position: fixed;
      bottom: 1.25rem;
      left: 1.25rem;
      width: 44px;
      height: 44px;
      border-radius: 50%;
      background: linear-gradient(135deg, #38bdf8, #2563eb);
      color: #ffffff;
      border: none;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.5);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.2rem;
      font-weight: 700;
      cursor: pointer;
      z-index: 999;
      transition: transform 0.2s, opacity 0.2s;
    }}
    .scroll-to-top-btn:active {{
      transform: scale(0.9);
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="logo-area">
        <h1>AI News Dashboard</h1>
        <p>AIトレンド・フィジカルAI・業務活用速報</p>
      </div>
      {sheet_btn_html}
    </header>

    <!-- 📅 日付絞り込みフィルターバー -->
    <div class="filter-section">
      <div class="filter-title">📅 配信日で絞り込み (過去1週間)</div>
      <div class="filter-bar">
        {date_tabs_html}
      </div>
    </div>

    <!-- 🔍 カテゴリ絞り込みフィルターバー -->
    <div class="filter-section">
      <div class="filter-title">🔍 カテゴリ・企業で絞り込み</div>
      <div class="filter-bar">
        <button class="filter-btn active" onclick="filterCategory('all', this)">すべて</button>
        <button class="filter-btn" onclick="filterCategory('google', this)">🔷 Google AI</button>
        <button class="filter-btn" onclick="filterCategory('openai', this)">🟢 OpenAI</button>
        <button class="filter-btn" onclick="filterCategory('claude', this)">🟣 Claude</button>
        <button class="filter-btn" onclick="filterCategory('physical', this)">🤖 フィジカルAI</button>
        <button class="filter-btn" onclick="filterCategory('business', this)">💼 業務・開発AI</button>
      </div>
    </div>

    <!-- 🏆 トップ5別枠セクション -->
    <h2 class="section-title">🏆 本日のトップ5厳選ニュース</h2>
    <section class="top5-container" id="top5-list">
      {top5_cards_html}
    </section>

    <!-- 📰 全ニュース一覧セクション -->
    <h2 class="section-title" style="color: var(--text-main);">📰 保存ニュース一覧 ({len(articles)}件)</h2>
    <main class="news-grid" id="news-grid">
      {cards_html}
    </main>
  </div>

  <button class="scroll-to-top-btn" onclick="window.scrollTo({{top: 0, behavior: 'smooth'}})" title="一番上に戻る">▲</button>

  <script>
    let activeDate = 'all';
    let activeCategory = 'all';

    function filterDate(d, btn) {{
      document.querySelectorAll('.date-btn').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      activeDate = d;
      applyFilters();
    }}

    function filterCategory(cat, btn) {{
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      activeCategory = cat;
      applyFilters();
    }}

    function applyFilters() {{
      const items = document.querySelectorAll('.news-card, .top5-card');
      items.forEach(item => {{
        const itemCat = item.getAttribute('data-category');
        const itemDate = item.getAttribute('data-date');
        
        const matchCat = (activeCategory === 'all' || itemCat === activeCategory);
        const matchDate = (activeDate === 'all' || itemDate === activeDate);

        if (matchCat && matchDate) {{
          item.style.display = 'flex';
        }} else {{
          item.style.display = 'none';
        }}
      }});
    }}
  </script>
</body>
</html>
"""



def save_articles_to_sheets(articles, sheet_webapp_url):
    """Google Apps Script Web App にニュースを送って Sheets に保存する。"""
    if not sheet_webapp_url:
        print("Google Sheets への保存先URLが未設定のため、保存をスキップしました。")
        return False

    payload = {
        "clear": True,
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
        res = requests.post(sheet_webapp_url, json=payload, timeout=60)
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

    candidate_models = [
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ]

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
            google_generativeai.configure(api_key=GEMINI_API_KEY)
            for model_name in candidate_models:
                try:
                    model = google_generativeai.GenerativeModel(model_name)
                    response = model.generate_content(prompt)
                    if response and getattr(response, "text", None):
                        return response.text.strip()
                except Exception as e:
                    print(f"Gemini ({model_name}) Summarize Warning: {e}")
                    continue
        except Exception as e:
            print(f"Gemini Summarize Error: {e}")

    return None


def normalize_title(title):
    """タイトルから出展・メディア表記や記号を除去して正規化する"""
    t = re.sub(r'[\(（].*?[\)）]', '', title)
    t = re.sub(r'\s*[\-\|ー—]\s*.*$', '', t)
    t = re.sub(r'[^\w\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]', '', t)
    return t.strip().lower()

def is_similar_title(t1, t2, threshold=0.50):
    """2つのニュースタイトルが類似しているかチェックする"""
    n1 = normalize_title(t1)
    n2 = normalize_title(t2)

    if not n1 or not n2:
        return False

    if n1 == n2 or n1 in n2 or n2 in n1:
        return True

    ratio = difflib.SequenceMatcher(None, n1, n2).ratio()
    if ratio >= threshold:
        return True

    words1 = set(re.findall(r'\w{2,}', n1))
    words2 = set(re.findall(r'\w{2,}', n2))
    if words1 and words2:
        jaccard = len(words1 & words2) / len(words1 | words2)
        if jaccard >= 0.40:
            return True

    return False

def is_duplicate_with_history(entry, raw_link, final_link, history):
    """
    過去送信履歴 (history) と照合し、多段階で重複を判定する。
    Stage 1: URL完全一致
    Stage 2: GUID / ID 一致
    Stage 3: タイトルおよび概要文の類似度一致（公開日時が新しい更新記事の場合は通過）
    """
    urls = history.get("urls", set())
    past_articles = history.get("articles", [])

    # Stage 1: URL完全一致
    if raw_link in urls or final_link in urls:
        return True, f"URL一致 ({final_link})"

    guid = getattr(entry, "id", "") or getattr(entry, "guid", "")
    title = getattr(entry, "title", "")
    norm_title = normalize_title(title)
    summary = extract_summary(entry)

    pub_time = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    entry_pub_ts = calendar.timegm(pub_time) if pub_time else time.time()

    for past in past_articles:
        # Stage 2: GUID一致
        if guid and past.get("guid") and past.get("guid") == guid:
            return True, f"GUID一致 ({guid})"

        # Stage 3: タイトル・概要文類似性
        past_title = past.get("title", "")
        past_summary = past.get("summary", "")
        past_norm_title = past.get("normalized_title") or normalize_title(past_title)

        title_similar = (norm_title and norm_title == past_norm_title) or is_similar_title(title, past_title, threshold=0.75)

        summary_similar = False
        if summary and past_summary and len(summary) > 20 and len(past_summary) > 20:
            if is_similar_title(summary[:100], past_summary[:100], threshold=0.70):
                summary_similar = True

        if title_similar or summary_similar:
            past_pub_ts = past.get("published", 0)
            # 公開日時が前回の配信記事より 6時間以上新しい場合は「最新アップデート」とみなして通過許可
            if entry_pub_ts > past_pub_ts + 21600:
                print(f"[最新アップデート検知] 「{title}」 (前回の過去配信日時より新しい)")
                return False, ""
            return True, f"過去送信済み類似ニュース: 「{past_title}」"

    return False, ""


def filter_similar_articles(articles):
    """スコアが高い順にソートされた記事から、重複・類似トピックを除外する（概要文も照合）"""
    unique_articles = []
    seen_normalized_keys = set()

    for article in articles:
        norm_key = normalize_title(article.get("title", ""))
        if not norm_key or norm_key in seen_normalized_keys:
            print(f"[完全重複除外] 「{article.get('title')}」")
            continue

        is_dup = False
        for saved in unique_articles:
            if is_similar_title(article["title"], saved["title"]):
                is_dup = True
                print(f"[類似除外] 「{article['title']}」 (類似元: 「{saved['title']}」)")
                break

            sum1 = article.get("summary", "")
            sum2 = saved.get("summary", "")
            if sum1 and sum2 and len(sum1) > 20 and len(sum2) > 20:
                if is_similar_title(sum1[:100], sum2[:100], threshold=0.75):
                    is_dup = True
                    print(f"[概要類似除外] 「{article['title']}」 (類似元: 「{saved['title']}」)")
                    break

        if not is_dup:
            seen_normalized_keys.add(norm_key)
            unique_articles.append(article)

    return unique_articles

def build_no_news_message(sheet_webapp_url=""):
    """新規配信ニュースが0件の際、システム正常稼働中を伝える安心メッセージ"""
    msg = (
        "🤖【AIニュース定期チェック】\n\n"
        "現在、前回の配信から新しく追加されたニュースはありませんでした。（システム正常稼働中）\n\n"
        "--------------------------------\n"
        "📊 過去1週間分のニュース一覧は以下のウェブアプリで確認できます：\n"
    )
    if sheet_webapp_url:
        msg += f"🔗 {sheet_webapp_url}"
    else:
        msg += "(※Web App URL未設定)"
    return msg


def build_notification_messages(top_articles, summaries, other_count, sheet_webapp_url=""):
    """
    スコア上位ニュース（最大5件）とその他件数・Web App URLを1つの集約メッセージとして組み立てる。
    Google / Gemini 関連ニュースには 🔷 [Google AI] マークを付与する。
    """
    blocks = ["🤖【本日のAIニュース厳選まとめ】\n"]
    for i, (article, summary) in enumerate(zip(top_articles, summaries), 1):
        is_google = is_google_ai_article(article["title"])
        prefix = "🔷 [Google AI] " if is_google else ""
        display_title = f"{prefix}{article['title']}"

        link_url = article.get("link") or article.get("url") or ""
        domain_name = get_domain_name(link_url)
        domain_label = f"[{domain_name}] " if domain_name else ""

        if summary:
            block = f"{i}. 📰 (Score: {article['score']})\n■ {display_title}\n{summary}\n🔗 {domain_label}{link_url}"
        else:
            block = f"{i}. 📰 【速報】(Score: {article['score']})\n■ {display_title}\n🔗 {domain_label}{link_url}"
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

    history = load_history()
    articles_to_notify = []
    total_articles = 0

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    now_ts = time.time()

    print("--- 取得記事一覧とスコア・重複判定 ---")
    for rss_url in RSS_URLS:
        feed = feedparser.parse(rss_url)
        for entry in feed.entries:
            raw_link = entry.link
            title = entry.title
            summary = extract_summary(entry)
            total_articles += 1

            # 公開日時の判定（過去7日以内の新鮮な記事のみ通過させる）
            if not is_recent_article(entry, MAX_ARTICLE_AGE_DAYS):
                print(f"[日付除外] 直近{MAX_ARTICLE_AGE_DAYS}日より古い記事のためスキップ: {title}")
                continue

            score = score_article(title, summary)
            passed = score >= SCORE_RULES["min_score"]

            if not passed:
                print(f"[Score: {score} / Skip] {title}")
                continue

            # リダイレクト先URLの展開
            final_link = resolve_final_url(raw_link)

            # 過去送信履歴との多層重複判定
            is_dup, reason = is_duplicate_with_history(entry, raw_link, final_link, history)
            if is_dup:
                print(f"[重複スキップ ({reason})] {title}")
                continue

            print(f"[Score: {score} / Pass] {title}")

            pub_time = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
            entry_pub_ts = calendar.timegm(pub_time) if pub_time else now_ts

            articles_to_notify.append({
                "title": title,
                "normalized_title": normalize_title(title),
                "summary": summary,
                "link": final_link,
                "raw_link": raw_link,
                "score": score,
                "guid": getattr(entry, "id", "") or getattr(entry, "guid", ""),
                "published": entry_pub_ts,
                "saved_at": f"🕒 {today_str}",
                "date": today_str
            })

    print("---------------------------------")
    print(f"取得総件数: {total_articles}")
    print(f"スコア・過去重複判定をクリアした件数: {len(articles_to_notify)}")

    # スコアが高い順に並び替え
    articles_to_notify.sort(key=lambda x: x["score"], reverse=True)

    # 同一バッチ内の類似・重複トピックニュースを除外
    articles_to_notify = filter_similar_articles(articles_to_notify)
    print(f"同バッチ類似除外後の通知対象件数: {len(articles_to_notify)}")

    webapp_url = get_webapp_url()

    # 新規配信ニュースが 0件 の場合の安心メッセージ送信処理
    if not articles_to_notify:
        print("通知対象の新しいニュースはありませんでした。安心メッセージを送信します。")
        no_news_msg = build_no_news_message(webapp_url)
        send_line_message(no_news_msg)

        # 過去7日分の日付別ダッシュボードを再更新
        past_articles = history.get("articles", [])
        if past_articles:
            dashboard_html = build_dashboard_html(past_articles, webapp_url)
            with open(NEWS_DASHBOARD_FILE, "w", encoding="utf-8") as f:
                f.write(dashboard_html)
            print(f"ダッシュボードHTMLを {NEWS_DASHBOARD_FILE} に更新しました。")
        return

    # 新規ニュースを Google Sheets へ送る
    max_saved_articles = get_max_saved_articles()
    saved_articles = select_balanced_articles(articles_to_notify, max_saved_articles)
    save_articles_to_sheets(saved_articles, webapp_url)

    # スコア上位（最大5件）を要約して1つのまとめメッセージに送る
    top_articles = articles_to_notify[:5]
    other_count = len(articles_to_notify) - len(top_articles)

    summaries = []
    for item in top_articles:
        summary = summarize_with_gemini(item["title"], item["link"])
        summaries.append(summary)

    messages = build_notification_messages(top_articles, summaries, other_count, webapp_url)

    # LINEに送信
    success = True
    for message in messages:
        if not send_line_message(message):
            success = False
            break

    if success:
        # 送信成功した記事を履歴に追加
        urls_set = history.get("urls", set())
        past_list = history.get("articles", [])

        for item in articles_to_notify:
            item["notified_timestamp"] = now_ts
            if item.get("raw_link"):
                urls_set.add(item["raw_link"])
            if item.get("link"):
                urls_set.add(item["link"])
            past_list.insert(0, item)

        history["urls"] = urls_set
        history["articles"] = past_list

        save_history(history)
        print("LINEへの送信および履歴の更新が成功しました。")

        # 過去7日分蓄積された全記事でダッシュボードHTMLを生成
        all_dashboard_articles = history.get("articles", [])
        dashboard_html = build_dashboard_html(all_dashboard_articles, webapp_url)
        with open(NEWS_DASHBOARD_FILE, "w", encoding="utf-8") as f:
            f.write(dashboard_html)
        print(f"ダッシュボードHTMLを {NEWS_DASHBOARD_FILE} に保存しました。")

if __name__ == "__main__":
    main()
