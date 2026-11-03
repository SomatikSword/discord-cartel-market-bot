import json
import logging
import os
import re
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import tasks
from dotenv import load_dotenv


# =========================================================
# ПУТИ
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"


# =========================================================
# НАСТРОЙКИ
# =========================================================

load_dotenv(ENV_FILE)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")

if not DISCORD_TOKEN:
    raise RuntimeError("Не найден DISCORD_TOKEN в файле .env")

if not CHANNEL_ID_RAW:
    raise RuntimeError("Не найден CHANNEL_ID в файле .env")

try:
    CHANNEL_ID = int(CHANNEL_ID_RAW)
except ValueError as error:
    raise RuntimeError(
        "CHANNEL_ID должен содержать только цифры"
    ) from error


# =========================================================
# РАСПИСАНИЕ НЕДЕЛЬНОГО ОТЧЁТА
# =========================================================

# Москва круглый год UTC+3.
MOSCOW_TIMEZONE = timezone(timedelta(hours=3))

# datetime.weekday():
# понедельник = 0
# вторник = 1
# ...
# воскресенье = 6
WEEKLY_DIGEST_WEEKDAY = 1

# Планировщик запускается каждый день в 15:00 МСК,
# но сообщение отправляется только во вторник.
WEEKLY_DIGEST_TIME = time(
    hour=15,
    minute=0,
    tzinfo=MOSCOW_TIMEZONE,
)


# =========================================================
# ИСТОЧНИК ДАННЫХ
# =========================================================

SWTOR_FLASH_SALES_URL = (
    "https://forums.swtor.com/topic/"
    "926686-on-going-list-of-cm-flash-sales-"
    "tracking-purposes-cuz-rng-is-fun/"
)

USER_AGENT = (
    "Mozilla/5.0 "
    "(compatible; DiscordCartelMarketBot/5.0; "
    "+https://github.com/SomatikSword/discord-cartel-market-bot)"
)


# =========================================================
# ЛОГИ
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("cartel-market-bot")


# =========================================================
# STATE.JSON
# =========================================================

def load_state():
    """
    Читает ID последнего отправленного недельного отчёта.
    """

    if not STATE_FILE.exists():
        return {"last_weekly_digest": None}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            return {"last_weekly_digest": None}

        return data

    except (OSError, json.JSONDecodeError) as error:
        logger.warning(
            "Не удалось прочитать state.json: %s",
            error,
        )
        return {"last_weekly_digest": None}


def save_state(state):
    """
    Сохраняет состояние после успешной отправки отчёта.
    """

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=4,
        )


# =========================================================
# ПЕРИОД НЕДЕЛЬНОГО ОТЧЁТА
# =========================================================

def get_digest_period(report_date):
    """
    Если отчёт отправляется во вторник,
    берём семь полностью завершённых дней:

    прошлый вторник -> вчерашний понедельник.
    """

    period_end = report_date - timedelta(days=1)
    period_start = period_end - timedelta(days=6)

    return period_start, period_end


# =========================================================
# ЗАГРУЗКА SWTOR FORUMS
# =========================================================

async def download_forum_page(session, url):
    """
    Загружает обычную HTML-страницу SWTOR Forums.
    Reddit/API здесь не используются.
    """

    async with session.get(url) as response:
        response.raise_for_status()
        return await response.text()


async def get_latest_forum_pages():
    """
    Загружает последнюю и предпоследнюю страницы треда.

    Первая страница сначала используется для определения
    номера последней страницы.
    """

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout,
    ) as session:

        first_html = await download_forum_page(
            session,
            SWTOR_FLASH_SALES_URL,
        )

        soup = BeautifulSoup(first_html, "html.parser")
        page_numbers = {1}

        for link in soup.find_all("a", href=True):
            match = re.search(
                r"/page/(\d+)/",
                link["href"],
            )

            if match:
                page_numbers.add(
                    int(match.group(1))
                )

        last_page = max(page_numbers)

        logger.info(
            "Последняя страница SWTOR Flash Sale tracker: %s",
            last_page,
        )

        pages = []

        for page_number in range(
            max(1, last_page - 1),
            last_page + 1,
        ):
            if page_number == 1:
                page_url = SWTOR_FLASH_SALES_URL
                page_html = first_html
            else:
                page_url = (
                    SWTOR_FLASH_SALES_URL
                    + f"page/{page_number}/"
                )
                page_html = await download_forum_page(
                    session,
                    page_url,
                )

            pages.append(
                {
                    "number": page_number,
                    "url": page_url,
                    "html": page_html,
                }
            )

        return pages


# =========================================================
# ПАРСИНГ SWTOR FORUMS
# =========================================================

MONTH_NUMBERS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


SALE_LINE_PATTERN = re.compile(
    r"^(?P<month>"
    r"Jan(?:uary)?|"
    r"Feb(?:ruary)?|"
    r"Mar(?:ch)?|"
    r"Apr(?:il)?|"
    r"May|"
    r"Jun(?:e)?|"
    r"Jul(?:y)?|"
    r"Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|"
    r"Nov(?:ember)?|"
    r"Dec(?:ember)?"
    r")"
    r"\s+"
    r"(?P<day>\d{1,2})"
    r"(?:st|nd|rd|th)?"
    r"(?:,\s*(?P<year>\d{4}))?"
    r"\s*[-–—=]\s*"
    r"(?P<body>.+)$",
    re.IGNORECASE,
)


ITEM_PATTERN = re.compile(
    r"^(?P<item>.+?)"
    r"\s*\(?\s*"
    r"(?P<price>[\d,]+)"
    r"\s*CC(?:'?s)?"
    r"\s*\)?"
    r"(?:\s*[-–—]\s*"
    r"(?P<discount>\d+)%\s*off"
    r")?"
    r"\s*$",
    re.IGNORECASE,
)


def get_post_year(post):
    """
    Берёт год публикации сообщения форума из
    тега <time datetime="...">.

    Это защищает от смешивания списков 2025 и 2026 годов.
    """

    for time_tag in post.find_all("time"):
        datetime_value = time_tag.get("datetime")

        if not datetime_value:
            continue

        try:
            normalized = datetime_value.replace(
                "Z",
                "+00:00",
            )

            return datetime.fromisoformat(
                normalized
            ).year

        except ValueError:
            continue

    return None


def parse_sale_segment(segment):
    """
    Разбирает строку товара вида:

    Imperial Director's Armor Set (1100 CC)

    или:

    Advanced White Eviscerating Crystal
    (880 CC) - 60% off
    """

    segment = (
        segment
        .replace("\xa0", " ")
        .strip()
    )

    match = ITEM_PATTERN.match(segment)

    if not match:
        return None

    item = (
        match.group("item")
        .strip(" -–—/")
        .strip()
    )

    if not item:
        return None

    price = (
        match.group("price")
        .replace(",", "")
    )

    return {
        "item": item,
        "price": price,
        "discount": match.group("discount"),
    }


def parse_forum_sales(
    forum_pages,
    period_start,
    period_end,
):
    """
    Извлекает Flash Sale только за нужные семь дней.

    Каждое сообщение форума разбирается отдельно,
    чтобы правильно определить год.

    Цитаты blockquote удаляются, чтобы старые
    процитированные списки не создавали дубли.
    """

    sales = {}

    for page in forum_pages:
        soup = BeautifulSoup(
            page["html"],
            "html.parser",
        )

        posts = soup.find_all("article")

        if not posts:
            posts = soup.select(
                "[data-commentid]"
            )

        if not posts:
            logger.warning(
                "Не удалось найти сообщения форума "
                "на странице %s.",
                page["number"],
            )
            continue

        for original_post in posts:
            post = BeautifulSoup(
                str(original_post),
                "html.parser",
            )

            # Удаляем цитаты старых сообщений.
            for quote in post.find_all("blockquote"):
                quote.decompose()

            post_year = get_post_year(post)

            post_text = post.get_text(
                "\n",
                strip=True,
            )

            for raw_line in post_text.splitlines():
                line = (
                    raw_line
                    .replace("\xa0", " ")
                    .strip()
                )

                line_match = SALE_LINE_PATTERN.match(
                    line
                )

                if not line_match:
                    continue

                month_key = (
                    line_match
                    .group("month")
                    .lower()
                )

                month_number = MONTH_NUMBERS.get(
                    month_key
                )

                if month_number is None:
                    continue

                day_number = int(
                    line_match.group("day")
                )

                explicit_year = line_match.group(
                    "year"
                )

                if explicit_year:
                    candidate_years = [
                        int(explicit_year)
                    ]
                elif post_year:
                    candidate_years = [
                        post_year
                    ]
                else:
                    candidate_years = sorted(
                        {
                            period_start.year,
                            period_end.year,
                        }
                    )

                sale_date = None

                for candidate_year in candidate_years:
                    try:
                        candidate_date = date(
                            candidate_year,
                            month_number,
                            day_number,
                        )
                    except ValueError:
                        continue

                    if (
                        period_start
                        <= candidate_date
                        <= period_end
                    ):
                        sale_date = candidate_date
                        break

                if sale_date is None:
                    continue

                body = (
                    line_match
                    .group("body")
                    .strip()
                )

                # Иногда форум указывает два товара
                # в один день через " / ".
                segments = re.split(
                    r"\s+/\s+",
                    body,
                )

                for segment in segments:
                    parsed_item = parse_sale_segment(
                        segment
                    )

                    if parsed_item is None:
                        continue

                    unique_key = (
                        sale_date.isoformat(),
                        parsed_item["item"].casefold(),
                        parsed_item["price"],
                    )

                    sales[unique_key] = {
                        "date": sale_date,
                        "item": parsed_item["item"],
                        "price": parsed_item["price"],
                        "discount": (
                            parsed_item["discount"]
                        ),
                    }

    result = list(sales.values())

    result.sort(
        key=lambda sale: (
            sale["date"],
            sale["item"].casefold(),
        )
    )

    return result


# =========================================================
# DISCORD BOT
# =========================================================

class CartelMarketBot(discord.Client):

    async def setup_hook(self):
        """
        Запускается только недельный планировщик.
        Ежедневной задачи больше нет.
        """

        self.weekly_digest_watcher.start()


    async def on_ready(self):
        logger.info(
            "Бот успешно подключился к Discord!"
        )

        logger.info(
            "Имя бота: %s",
            self.user,
        )

        logger.info(
            "Канал: %s",
            CHANNEL_ID,
        )

        logger.info(
            "Недельный отчёт: "
            "каждый вторник в 15:00 МСК."
        )


    async def get_sales_channel(self):
        channel = self.get_channel(
            CHANNEL_ID
        )

        if channel is not None:
            return channel

        try:
            return await self.fetch_channel(
                CHANNEL_ID
            )

        except discord.DiscordException as error:
            logger.error(
                "Не удалось получить Discord-канал: %s",
                error,
            )

            return None


    @tasks.loop(time=WEEKLY_DIGEST_TIME)
    async def weekly_digest_watcher(self):
        """
        Задача просыпается каждый день в 15:00 МСК.
        Отправка выполняется только во вторник.
        """

        now = datetime.now(
            MOSCOW_TIMEZONE
        )

        if (
            now.weekday()
            != WEEKLY_DIGEST_WEEKDAY
        ):
            return

        try:
            await self.send_weekly_digest()

        except aiohttp.ClientError as error:
            logger.error(
                "Ошибка при загрузке SWTOR Forums: %s",
                error,
            )

        except Exception:
            logger.exception(
                "Ошибка недельного "
                "Cartel Market отчёта."
            )


    @weekly_digest_watcher.before_loop
    async def before_weekly_digest_watcher(self):
        await self.wait_until_ready()


    async def send_weekly_digest(self):
        now = datetime.now(
            MOSCOW_TIMEZONE
        )

        period_start, period_end = (
            get_digest_period(
                now.date()
            )
        )

        digest_id = (
            f"{period_start.isoformat()}_"
            f"{period_end.isoformat()}"
        )

        state = load_state()

        if (
            state.get("last_weekly_digest")
            == digest_id
        ):
            logger.info(
                "Отчёт за %s — %s уже был отправлен.",
                period_start,
                period_end,
            )
            return

        logger.info(
            "Получаем Flash Sale с SWTOR Forums "
            "за %s — %s...",
            period_start,
            period_end,
        )

        forum_pages = (
            await get_latest_forum_pages()
        )

        weekly_sales = parse_forum_sales(
            forum_pages,
            period_start,
            period_end,
        )

        logger.info(
            "Найдено записей Flash Sale: %s",
            len(weekly_sales),
        )

        channel = await self.get_sales_channel()

        if channel is None:
            return

        embed = discord.Embed(
            title=(
                "🛒 Cartel Market — "
                "скидки за неделю"
            ),
            url=SWTOR_FLASH_SALES_URL,
            description=(
                f"📅 **"
                f"{period_start.strftime('%d.%m.%Y')}"
                f" — "
                f"{period_end.strftime('%d.%m.%Y')}"
                f"**\n\n"
                f"Найдено распродаж: "
                f"**{len(weekly_sales)}**"
            ),
            color=discord.Color.gold(),
        )

        if weekly_sales:
            for index, sale in enumerate(
                weekly_sales,
                start=1,
            ):
                value = (
                    f"💰 **{sale['price']} "
                    f"Cartel Coins**\n"
                    f"📅 "
                    f"{sale['date'].strftime('%d.%m.%Y')}"
                )

                if sale["discount"]:
                    value += (
                        f"\n🏷️ **Скидка: "
                        f"{sale['discount']}%**"
                    )

                embed.add_field(
                    name=(
                        f"{index}. "
                        f"{sale['item']}"
                    ),
                    value=value,
                    inline=False,
                )
        else:
            embed.add_field(
                name="ℹ️ Данные не найдены",
                value=(
                    "За этот период в трекере "
                    "SWTOR Forums нет распознанных "
                    "записей Flash Sale."
                ),
                inline=False,
            )

        embed.add_field(
            name="🔗 Источник",
            value=(
                "[SWTOR Forums — "
                "Flash Sale Tracker]"
                f"({SWTOR_FLASH_SALES_URL})"
            ),
            inline=False,
        )

        embed.set_footer(
            text=(
                "SWTOR Cartel Market "
                "• Weekly Digest"
            )
        )

        await channel.send(
            embed=embed
        )

        # Запоминаем отчёт только после
        # успешной отправки Discord.
        state["last_weekly_digest"] = (
            digest_id
        )

        save_state(state)

        logger.info(
            "Недельный отчёт успешно отправлен."
        )


# =========================================================
# ЗАПУСК
# =========================================================

intents = discord.Intents.default()

bot = CartelMarketBot(
    intents=intents
)

bot.run(
    DISCORD_TOKEN,
    log_handler=None,
)
