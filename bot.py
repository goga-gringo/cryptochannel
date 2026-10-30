"""Бесплатный новостной Telegram-бот: берёт свежие новости из RSS-лент и постит в канал.

Без ИИ, без платных API и без сервера: нужен только токен бота и id канала.
Запуск: python bot.py --once   (один пост/несколько постов и выход, для GitHub Actions)
"""
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from calendar import timegm
from pathlib import Path

import feedparser

BASE_DIR = Path(__file__).resolve().parent


def env(name: str, default: str = "") -> str:
    """Значение переменной окружения; пустая строка считается «не задано»."""
    return os.getenv(name, "").strip() or default


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()  # @my_channel или -100123456789
POSTS_PER_RUN = int(env("POSTS_PER_RUN", "1"))
MAX_AGE_HOURS = int(env("MAX_AGE_HOURS", "48"))  # старше этого возраста новости не берём
MAX_CHARS = int(env("MAX_CHARS", "350"))  # длина выдержки из новости
FEEDS_FILE = Path(env("FEEDS_FILE", str(BASE_DIR / "feeds.txt")))
DB_PATH = env("DB_PATH", str(BASE_DIR / "posted.db"))
DRY_RUN = env("DRY_RUN", "0") == "1"  # 1 = только показать пост в логе, ничего не отправлять
# Хештеги в конце поста: подбираются по теме новости (правила ниже, HASHTAG_RULES).
MAX_HASHTAGS = 4  # сколько хештегов ставить под постом; 0 отключает их совсем
ALWAYS_TAG = "#crypto"  # ставится первым в каждом посте ("" = не ставить)
FALLBACK_TAGS = ["#bitcoin", "#altcoin", "#telegram", "#blockchain"]  # добирают до нужного числа

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("news_bot")

# Эмодзи по ключевым словам в заголовке (первое совпадение). Список можно дополнять.
EMOJI_RULES = [
    # --- крипто: сначала события (суд, взлом, рост, падение), потом общие темы ---
    ((" суд ", " суда ", " суде ", "судебн", "судья", "обвинен", "обвинил", "прокуратур", "иск к ",
      "приговор", "штраф", "арестов", "регулятор", " sec ", "lawsuit", "charged"), "⚖️"),
    (("взлом", "хакер", "украл", "украд", "кража", "утечк", "уязвимост", "эксплойт", "мошенн",
      "скам", "hack", "exploit", "rug pull"), "🚨"),
    (("упал", "упад", "падени", "обвал", "просел", "подешевел", "снизил", "рухнул", "рухнули",
      "минимум", "plunge", "crash", "drops"), "📉"),
    (("вырос", "вырастет", "подорожал", "рекорд", "ралли", "взлет", "взлёт", "растёт", "растет",
      " рост ", "surge", "rally", "all-time high"), "📈"),
    (("etf", "blackrock", "фонд ", "институционал", "инвестор", "центробанк", " цб "), "🏦"),
    (("binance", "coinbase", "bybit", "okx", "kraken", "биржа", "биржи", "листинг", "делистинг"), "🏷️"),
    (("биткоин", "bitcoin", " btc", "эфир", "ethereum", " eth ", "солана", "solana", "xrp", "ripple",
      "токен", "альткоин", "стейблкоин", "stablecoin", "usdt", "usdc", "крипт", "блокчейн",
      "майнинг", "майнер", "defi", "nft", "мем-коин", "memecoin"), "🪙"),
    # --- общие темы ---
    (("война", "обстрел", "армия", "военн", "ракет", "war ", "military", "missile"), "🪖"),
    (("выбор", "президент", "правительств", "парламент", "санкци", "election", "president"), "🏛️"),
    (("нейросет", "искусственн", " ии ", "chatgpt", "openai", "claude", " ai ", "gpt"), "🤖"),
    (("смартфон", "iphone", "android", "apple", "google", "microsoft", "процессор", "чип", "ноутбук"), "💻"),
    (("космос", "nasa", "спутник", "ракета-носитель", "марс", "луна", "spacex"), "🚀"),
    (("доллар", "евро", "рубл", "курс", "инфляц", "банк", "акци", "биржа", "рынок", "нефть", "экономик"), "💰"),
    (("крипт", "биткоин", "bitcoin", "ethereum", "блокчейн"), "🪙"),
    (("футбол", "хоккей", "теннис", "олимпи", "спорт", "матч", "чемпионат", "уефа", "нба", "uefa"), "⚽"),
    (("учён", "ученые", "учёные", "исследовани", "открыти", "science", "study"), "🔬"),
    (("климат", "погода", "шторм", "землетряс", "наводнен", "пожар", "ураган", "earthquake"), "🌪️"),
    (("здоровь", "вирус", "врач", "болезн", "вакцин", "медицин", "health", "covid"), "🩺"),
    (("авиа", "самолёт", "самолет", "аэропорт", "поезд", "дтп", "авария", "катастроф"), "✈️"),
    (("кино", "фильм", "сериал", "музык", "концерт", "премьер", "оскар"), "🎬"),
]
DEFAULT_EMOJI = "📰"

# Фразы, которые не считаются при подборе хештегов (иначе «прямой эфир» дал бы #ethereum).
HASHTAG_IGNORE = ["прямой эфир", "прямого эфира", "прямом эфире", "в эфире", "в эфир"]

# Хештеги по ключевым словам. Новость может подойти под несколько правил, берутся первые
# совпадения (сначала по заголовку, потом по описанию), всего не больше MAX_HASHTAGS.
# Слова пишите в нижнем регистре; знаки препинания при сравнении игнорируются.
HASHTAG_RULES = [
    (("биткоин", "bitcoin", "btc"), ["#bitcoin", "#btc"]),
    (("эфир", "эфира", "эфиру", "эфиром", "эфириум", "ethereum", "eth"), ["#ethereum", "#eth"]),
    (("solana", "солана", "sol"), ["#solana"]),
    (("xrp", "ripple", "рипл"), ["#xrp", "#ripple"]),
    (("toncoin", "ton", "тонкоин"), ["#ton", "#toncoin"]),
    (("bnb", "bnb chain"), ["#bnb"]),
    (("cardano", "кардано", "ada"), ["#cardano"]),
    (("dogecoin", "доджкоин", "shiba", "мем коин", "мемкоин", "memecoin", "мемкоины"), ["#memecoins"]),
    (("стейблкоин", "стейблкоины", "stablecoin", "usdt", "usdc", "tether", "тезер"), ["#stablecoins"]),
    (("etf", "blackrock", "fidelity", "грейскейл", "grayscale"), ["#etf", "#institutions"]),
    (("цифровой рубль", "cbdc", "цифровая валюта центробанка"), ["#cbdc"]),
    (("sec", "регулятор", "регулирование", "законопроект", "закон", "запрет", "лицензия",
      "лицензию", "суд", "суда", "суде", "судебный", "обвинения", "обвинил", "иск"), ["#regulation", "#law"]),
    (("взлом", "взломали", "взломан", "хакер", "хакеры", "украли", "украл", "кража", "утечка",
      "уязвимость", "эксплойт", "мошенники", "мошенничество", "скам", "hack", "exploit"), ["#security", "#hack"]),
    (("binance", "бинанс"), ["#binance", "#exchange"]),
    (("coinbase", "кобейс"), ["#coinbase", "#exchange"]),
    (("bybit", "okx", "kraken", "kucoin", "bitget", "биржа", "биржи", "бирже", "листинг", "делистинг"), ["#exchange"]),
    (("defi", "децентрализованные", "децентрализованный", "ликвидность", "протокол"), ["#defi"]),
    (("стейкинг", "стейкинга", "staking", "рестейкинг"), ["#staking"]),
    (("nft", "нфт"), ["#nft"]),
    (("майнинг", "майнинга", "майнер", "майнеры", "хешрейт", "халвинг"), ["#mining"]),
    (("airdrop", "эирдроп", "аирдроп", "аирдропа"), ["#airdrop"]),
    (("kalshi", "polymarket", "рынки предсказаний", "рынков предсказаний"), ["#predictionmarkets"]),
    (("нейросеть", "нейросети", "искусственный интеллект", "ии", "openai", "chatgpt"), ["#ai"]),
    (("telegram", "телеграм", "телеграма", "дуров", "дурова"), ["#telegram"]),
    (("фрс", "fed", "инфляция", "инфляции", "ставка", "ставки", "доллар", "доллара"), ["#economy", "#macro"]),
    (("упал", "упали", "падение", "обвал", "просел", "рухнул", "вырос", "выросла", "рекорд",
      "ралли", "взлет", "взлёт", "курс", "цена", "котировки", "минимум"), ["#market", "#price"]),
]


# ---------- хранилище ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS posted (url TEXT PRIMARY KEY, title TEXT, ts INTEGER)")
    return conn


def already_posted(url: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM posted WHERE url = ?", (url,)).fetchone() is not None


def mark_posted(url: str, title: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO posted (url, title, ts) VALUES (?, ?, ?)",
            (url, title, int(time.time())),
        )


# ---------- чтение лент ----------
def load_feeds() -> list[str]:
    if not FEEDS_FILE.exists():
        raise SystemExit(f"Не найден файл со списком лент: {FEEDS_FILE}")
    feeds = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            feeds.append(line)
    if not feeds:
        raise SystemExit("В feeds.txt нет ни одной ленты")
    return feeds


def clean_text(raw: str) -> str:
    """Убирает HTML-теги и лишние пробелы."""
    text = re.sub(r"<(script|style).*?</\1>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"</?(p|br|div|li|ul|ol|h\d|blockquote|tr)\b[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)  # остальные теги (b, a, img...) убираем без пробела
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def shorten(text: str, limit: int) -> str:
    """Обрезает текст по границе предложения (или слова), не длиннее limit символов."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?…](?=\s|$)", cut)]
    if ends and ends[-1] >= limit * 0.5:
        return cut[: ends[-1]].strip()
    return cut.rsplit(" ", 1)[0].rstrip(",;:- ") + "…"


def entry_time(entry) -> float | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return float(timegm(parsed)) if parsed else None


def fetch_feed(url: str) -> list[dict]:
    """Скачивает одну ленту. Если лента недоступна, возвращает [] (бот идёт дальше)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (news-bot)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
    except Exception as e:  # noqa: BLE001
        log.warning("Лента недоступна, пропускаю: %s (%s)", url, e)
        return []
    parsed = feedparser.parse(data)
    if not parsed.entries:
        log.warning("В ленте нет записей, пропускаю: %s", url)
        return []
    source = clean_text(parsed.feed.get("title", "")) or urllib.parse.urlparse(url).netloc
    items = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        title = clean_text(e.get("title", ""))
        if not link or not title:
            continue
        summary = clean_text(e.get("summary") or e.get("description") or "")
        items.append(
            {"title": title, "summary": summary, "url": link, "ts": entry_time(e), "source": source}
        )
    items.sort(key=lambda i: i["ts"] or 0, reverse=True)
    log.info("Лента %s: записей %d", source, len(items))
    return items


def title_key(title: str) -> str:
    """Заголовок без регистра, пробелов и знаков препинания — для поиска дублей."""
    return re.sub(r"\W+", "", title.lower())


def posted_title_keys(limit: int = 500) -> set[str]:
    with db() as conn:
        rows = conn.execute("SELECT title FROM posted ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return {title_key(r[0]) for r in rows if r[0]}


# Слова, которые встречаются в каждой второй крипто-новости: по ним темы не различаем.
GENERIC_NAMES = {
    "bitcoin", "биткоин", "ethereum", "эфир", "crypto", "крипт", "defi", "nft", "web3", "usdt",
    "usdc", "etf", "сша", "росси", "китай", "украи", "евро", "долла", "the", "this", "that",
    "what", "why", "how", "new", "after", "will", "says", "could", "with", "from", "more",
}
GENERIC_STEMS = {g[:5] for g in GENERIC_NAMES}
RECENT_POSTS_FOR_TOPICS = 6  # сколько последних постов учитываем при сравнении тем


def entities(title: str) -> set[str]:
    """Имена в заголовке (компании, проекты, люди): слова с заглавной буквы не в начале
    фразы, а также латинское слово в самом начале. Сравниваются по первым 5 буквам."""
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-]*", title)
    names = set()
    for i, word in enumerate(words):
        if len(word) < 3:
            continue
        is_latin = bool(re.fullmatch(r"[A-Za-z0-9\-]+", word))
        is_name = (i > 0 and word[0].isupper()) or (i == 0 and is_latin and len(word) >= 4)
        stem = word.lower()[:5]
        if is_name and stem not in GENERIC_STEMS:
            names.add(stem)
    return names


def recent_entities() -> set[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT title FROM posted ORDER BY ts DESC LIMIT ?", (RECENT_POSTS_FOR_TOPICS,)
        ).fetchall()
    names: set[str] = set()
    for (title,) in rows:
        names |= entities(title or "")
    return names


def pick_items(feeds: list[str]) -> list[dict]:
    """Берёт самые свежие ещё не опубликованные новости, по очереди из разных лент."""
    now = time.time()
    per_feed = []
    for url in feeds:
        fresh = [
            i
            for i in fetch_feed(url)
            if not already_posted(i["url"])
            and (i["ts"] is None or now - i["ts"] <= MAX_AGE_HOURS * 3600)
        ]
        per_feed.append(fresh)
    # Та же новость из другой ленты (совпадает заголовок) не публикуется повторно.
    # Новости про ту же компанию/проект, что в последних постах, откладываются: берутся
    # другие, а отложенные идут в дело, только если ничего другого нет.
    result, seen_titles = [], posted_title_keys()
    recent_names = recent_entities()
    deferred = []
    while len(result) < POSTS_PER_RUN and any(per_feed):
        for lst in per_feed:
            while lst:
                item = lst.pop(0)
                key = title_key(item["title"])
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                names = entities(item["title"])
                if names & recent_names:
                    deferred.append(item)
                    continue
                recent_names |= names
                result.append(item)
                break
            if len(result) >= POSTS_PER_RUN:
                break
    if len(result) < POSTS_PER_RUN and deferred:
        log.info("Свежих новостей на другие темы не нашлось, беру из отложенных")
        result += deferred[: POSTS_PER_RUN - len(result)]
    return result


# ---------- оформление и отправка ----------
def pick_emoji(title: str) -> str:
    low = f" {title.lower()} "
    for keys, emoji in EMOJI_RULES:
        if any(k in low for k in keys):
            return emoji
    return DEFAULT_EMOJI


def _norm(text: str) -> str:
    """Нижний регистр, знаки препинания заменены пробелами, пробелы по краям."""
    return " " + re.sub(r"[\W_]+", " ", text.lower()).strip() + " "


def pick_hashtags(item: dict) -> list[str]:
    """Хештеги по теме новости: #crypto, затем по правилам (заголовок важнее описания), затем добор."""
    if MAX_HASHTAGS <= 0:
        return []
    tags: list[str] = [ALWAYS_TAG] if ALWAYS_TAG else []

    def add(tag: str) -> None:
        if tag not in tags and len(tags) < MAX_HASHTAGS:
            tags.append(tag)

    sources = []
    for text in (item["title"], item.get("summary", "")):
        norm = _norm(text)
        for phrase in HASHTAG_IGNORE:  # фразы, которые только похожи на крипто-слова
            norm = norm.replace(_norm(phrase), " ")
        sources.append(norm)
    matched = [
        rule_tags
        for source in sources
        for keywords, rule_tags in HASHTAG_RULES
        if any(_norm(k) in source for k in keywords)
    ]
    # Сначала по одному главному хештегу от каждого сработавшего правила (так тем больше),
    # и только если место осталось, добавляются вторые хештеги правил.
    for position in (0, 1):
        for rule_tags in matched:
            if position < len(rule_tags):
                add(rule_tags[position])
    for tag in FALLBACK_TAGS:
        add(tag)
    return tags


def format_post(item: dict) -> str:
    title = html.escape(item["title"])
    summary = item["summary"]
    # Некоторые ленты дублируют заголовок в описании, такое не показываем.
    if summary.lower().startswith(item["title"].lower()[:40]):
        summary = ""
    body = html.escape(shorten(summary, MAX_CHARS)) if summary else ""
    parts = [f"{pick_emoji(item['title'])} <b>{title}</b>"]
    if body:
        parts.append(body)
    tags = pick_hashtags(item)
    if tags:
        parts.append(html.escape(" ".join(tags)))
    return "\n\n".join(parts)


def send_to_telegram(text: str) -> None:
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML"}
    ).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(api, data=payload), timeout=30) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Telegram отклонил сообщение: {e.code} {detail}") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram вернул ошибку: {result}")


def run_once() -> None:
    if not DRY_RUN and (not BOT_TOKEN or not CHANNEL_ID):
        raise SystemExit("Не заданы BOT_TOKEN и CHANNEL_ID (секреты GitHub или файл .env)")
    items = pick_items(load_feeds())
    if not items:
        log.info("Новых новостей нет, публиковать нечего")
        return
    for n, item in enumerate(items):
        message = format_post(item)
        if DRY_RUN:
            print("-" * 40)
            print(message)
            continue
        send_to_telegram(message)
        mark_posted(item["url"], item["title"])
        log.info("Опубликовано: %s", item["title"])
        if n < len(items) - 1:
            time.sleep(3)
    if DRY_RUN:
        log.info("Пробный режим: в канал ничего не отправлено, ссылки не запомнены")


if __name__ == "__main__":
    # Скрипт всегда делает один проход и выходит; параметр --once оставлен для совместимости.
    run_once()
