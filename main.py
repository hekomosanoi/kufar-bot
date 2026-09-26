import http.server
import json
import logging
import os
import socketserver
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional
import requests

# Настройка логирования для вывода в консоль сервера
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("KufarSniper")

# Токены: берутся из переменных окружения или задаются напрямую
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID_HERE")

# Настройки API Kufar
# cat=1010 (Аренда квартир), typ=let (Снять долгосрочно), rgn=7 (Минск)
KUFAR_API_URL = "https://cre-api.kufar.by/ads-search/v1/engine/v1/search/rendered-paginated"
CHECK_INTERVAL_SECONDS = 50  # Оптимальный интервал без риска получить бан по IP
DB_FILE = "kufar_seen_ads.db"

# Стоп-слова риелторов и агентств недвижимости
AGENCY_STOP_WORDS = [
    "по факту",
    "агентств",
    "риэлт",
    "риелтор",
    "комисси",
    "договор об оказании",
    "оплата услуг",
    "предоплат",
    "юр. адрес",
    "уп «",
    "ооо «"
]

# Инициализация постоянной сессии с эмуляцией живого браузера
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://re.kufar.by",
    "Referer": "https://re.kufar.by/l/minsk/snyat/kvartiru-dolgosrochno",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
})


def init_db() -> None:
    """Создает локальную базу SQLite для исключения дубликатов сообщений."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_ads (
                ad_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def is_ad_seen(ad_id: str) -> bool:
    """Проверяет, отсылалось ли уже данное объявление."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM seen_ads WHERE ad_id = ?", (str(ad_id),))
        return cursor.fetchone() is not None


def mark_ad_as_seen(ad_id: str) -> None:
    """Фиксирует объявление как прочитанное."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO seen_ads (ad_id) VALUES (?)", (str(ad_id),))
        conn.commit()


def fetch_kufar_flats() -> List[Dict[str, Any]]:
    """Безопасный запрос к открытому каталогу Kufar Недвижимость."""
    params = {
        "cat": "1010",         # Квартиры долгосрочно
        "typ": "let",          # Снять
        "rgn": "7",            # Минск
        "cur": "USD",          # Валюта цен
        "sort": "lst.d",       # Сортировка: сначала самые новые
        "size": "30",          # 30 последних объявлений
    }

    try:
        response = session.get(KUFAR_API_URL, params=params, timeout=12)
        
        if response.status_code == 200:
            data = response.json()
            return data.get("ads", [])
        elif response.status_code == 429:
            logger.warning("Kufar вернул 429 (слишком много запросов). Увеличиваем паузу...")
            time.sleep(30)
        elif response.status_code == 403:
            logger.error("Kufar вернул 403 Forbidden. Проверьте заголовки или смените регион сервера.")
            time.sleep(60)
        else:
            logger.error(f"Неожиданный ответ Kufar: HTTP {response.status_code}")
    except requests.RequestException as err:
        logger.error(f"Сетевой сбой при опросе Kufar: {err}")

    return []


def is_owner_listing(ad: Dict[str, Any]) -> bool:
    """Многоуровневый фильтр риелторов и посредников."""
    # 1. Проверка системного флага юрлица/компании Kufar
    if ad.get("company_ad") is True:
        return False

    # 2. Проверка внутренних параметров (флаг rem = Real Estate Management)
    raw_params = ad.get("ad_parameters", [])
    params_dict = {}
    for p in raw_params:
        if isinstance(p, dict) and "p" in p:
            params_dict[p["p"]] = p.get("v")

    if params_dict.get("rem") is True:
        return False

    # 3. Анализ описания и заголовка на типичные агентские маркеры
    body_text = (str(ad.get("body", "")) + " " + str(ad.get("subject", ""))).lower()
    for stop_word in AGENCY_STOP_WORDS:
        if stop_word in body_text:
            return False

    return True


def format_flat_message(ad: Dict[str, Any]) -> str:
    """Форматирование карточки квартиры для Telegram с безопасным парсингом цен."""
    ad_id = str(ad.get("ad_id", ""))
    ad_link = ad.get("ad_link") or f"https://re.kufar.by/vi/{ad_id}"
    subject = ad.get("subject") or "Квартира в аренду"

    # Обработка цены в долларах
    raw_price_usd = ad.get("price_usd", "0")
    try:
        usd_val = int(float(raw_price_usd))
    except (ValueError, TypeError):
        usd_val = 0

    # Обработка цены в BYN (Kufar часто отдает в копейках, делим на 100)
    raw_price_byn = ad.get("price_byn", "0")
    try:
        byn_val = int(float(raw_price_byn) / 100)
    except (ValueError, TypeError):
        byn_val = 0

    # Сбор параметров: комнатность, адрес, метро
    rooms = "Не указано"
    metro = ""
    for p in ad.get("ad_parameters", []):
        if p.get("p") == "rooms":
            rooms = str(p.get("vl") or p.get("v") or "1")
        elif p.get("p") == "metro":
            metro = str(p.get("vl") or p.get("v") or "")

    address = ad.get("account_parameters", {}).get("address", "Минск")
    metro_line = f"\n🚇 <b>Метро:</b> {metro}" if metro else ""

    message = (
        f"🔥 <b>НОВАЯ КВАРТИРА ОТ СОБСТВЕННИКА!</b>\n\n"
        f"🏢 <b>{subject}</b>\n"
        f"💵 <b>Цена:</b> ${usd_val} / ~{byn_val} BYN в месяц\n"
        f"🚪 <b>Комнат:</b> {rooms}\n"
        f"📍 <b>Адрес:</b> {address}{metro_line}\n"
        f"🛡️ <b>Проверка:</b> Без посредников и агентств\n\n"
        f"🔗 <a href=\"{ad_link}\">👉 Открыть объявление на Kufar</a>"
    )
    return message


def send_telegram_alert(text: str) -> None:
    """Отправка уведомления пользователю через Bot API."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.info("[ТЕСТОВЫЙ РЕЖИМ] Найдена квартира (токен не настроен):\n" + text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }

    try:
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code != 200:
            logger.error(f"Ошибка отправки Telegram: {resp.text}")
    except requests.RequestException as err:
        logger.error(f"Не удалось связаться с сервером Telegram: {err}")


def run_sniper_loop() -> None:
    """Фоновый цикл непрерывного мониторинга."""
    init_db()
    logger.info("Мониторинг Kufar Минск запущен. Интервал: %s сек.", CHECK_INTERVAL_SECONDS)

    while True:
        try:
            ads = fetch_kufar_flats()
            logger.info("Получено %d объявлений. Сканирование...", len(ads))

            for ad in ads:
                ad_id = str(ad.get("ad_id", ""))
                if not ad_id or is_ad_seen(ad_id):
                    continue

                if is_owner_listing(ad):
                    logger.info("⚡ Найдена квартира от хозяина: %s", ad_id)
                    send_telegram_alert(format_flat_message(ad))

                # Помечаем объявление, чтобы не проверять его повторно
                mark_ad_as_seen(ad_id)

        except Exception as exc:
            logger.exception("Ошибка в цикле мониторинга: %s", exc)

        time.sleep(CHECK_INTERVAL_SECONDS)


class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
    """Микро-сервер для прохождения проверок Render Free Web Service."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Minsk Sniper Bot is ACTIVE 24/7\n")

    def log_message(self, format, *args):
        # Отключаем спам в консоль от регулярных пингов
        return


def run_healthcheck_server() -> None:
    """Запускает HTTP-сервер на порту, требуемом облаком (Render/Railway)."""
    port = int(os.getenv("PORT", 8080))
    with socketserver.TCPServer(("", port), HealthCheckHandler) as httpd:
        logger.info("Встроенный Healthcheck-сервер слушает порт %s", port)
        httpd.serve_forever()


if __name__ == "__main__":
    # Запускаем мониторинг Kufar в отдельном фоновом потоке
    monitor_thread = threading.Thread(target=run_sniper_loop, daemon=True)
    monitor_thread.start()

    # В основном потоке держим активным микро-вебсервер для облака
    run_healthcheck_server()
