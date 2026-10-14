import json
import logging
import os
import re
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path

from io import BytesIO
import aiohttp
import discord
import feedparser
from discord.ext import tasks
from dotenv import load_dotenv


# =========================================================
# ПУТИ
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

ENV_FILE = BASE_DIR / ".env"

DATA_DIR = BASE_DIR / "data"

STATE_FILE = DATA_DIR / "state.json"

HISTORY_FILE = DATA_DIR / "sales_history.json"


# =========================================================
# НАСТРОЙКИ
# =========================================================

load_dotenv(ENV_FILE)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")


if not DISCORD_TOKEN:
    raise RuntimeError(
        "Не найден DISCORD_TOKEN в файле .env"
    )


if not CHANNEL_ID_RAW:
    raise RuntimeError(
        "Не найден CHANNEL_ID в файле .env"
    )


try:
    CHANNEL_ID = int(CHANNEL_ID_RAW)

except ValueError:
    raise RuntimeError(
        "CHANNEL_ID должен содержать только цифры"
    )


# =========================================================
# НАСТРОЙКИ ПРОВЕРОК
# =========================================================

# Проверяем Reddit каждые 10 минут
CHECK_INTERVAL_MINUTES = 10


# Воскресенье.
#
# В Python:
# понедельник = 0
# вторник = 1
# ...
# воскресенье = 6
# Москва — UTC+3
MOSCOW_TIMEZONE = timezone(
    timedelta(hours=3)
)


# В Python:
# понедельник = 0
# вторник = 1
# среда = 2
# ...
# воскресенье = 6
WEEKLY_DIGEST_WEEKDAY = 1


# Недельный отчёт:
# каждый день планировщик просыпается в 15:00 МСК,
# но отправка происходит только во вторник.
WEEKLY_DIGEST_TIME = time(
    hour=15,
    minute=0,
    tzinfo=MOSCOW_TIMEZONE
)


# =========================================================
# REDDIT
# =========================================================

REDDIT_RSS_URL = (
    "https://www.reddit.com/"
    "r/swtor/new.rss?limit=100"
)

REDDIT_JSON_URL = (
    "https://www.reddit.com/"
    "r/swtor/new.json?limit=100&raw_json=1"
)

# =========================================================
# ЛОГИ
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    )
)

logger = logging.getLogger(
    "cartel-market-bot"
)


# =========================================================
# REGEX ДЛЯ FLASH SALE
# =========================================================

SALE_PATTERN = re.compile(
    r"^Flash(?: \(\?\))? Sale for "
    r"(?P<date>.+?): "
    r"(?P<item>.+?),\s*"
    r"(?P<price>[\d,]+)\s*CCs?\s*$",
    re.IGNORECASE
)


# =========================================================
# МЕСЯЦЫ
# =========================================================

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12
}


# =========================================================
# STATE.JSON
# =========================================================

def load_state():
    """
    Загружает состояние бота.

    Здесь мы храним:
    - последнюю опубликованную Flash Sale;
    - последний опубликованный недельный дайджест.
    """

    if not STATE_FILE.exists():

        return {
            "last_sale": None,
            "last_weekly_digest": None
        }

    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        return data

    except (
        OSError,
        json.JSONDecodeError
    ) as error:

        logger.warning(
            "Не удалось прочитать state.json: %s",
            error
        )

        return {
            "last_sale": None,
            "last_weekly_digest": None
        }


def save_state(state):
    """
    Сохраняет состояние бота.
    """

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    with STATE_FILE.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=4
        )


# =========================================================
# ИСТОРИЯ РАСПРОДАЖ
# =========================================================

def load_sales_history():
    """
    Загружает всю сохранённую историю Flash Sale.
    """

    if not HISTORY_FILE.exists():
        return []

    try:

        with HISTORY_FILE.open(
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(file)

    except (
        OSError,
        json.JSONDecodeError
    ) as error:

        logger.warning(
            "Не удалось прочитать sales_history.json: %s",
            error
        )

        return []


def save_sales_history(history):
    """
    Сохраняет историю Flash Sale.
    """

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    with HISTORY_FILE.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            history,
            file,
            ensure_ascii=False,
            indent=4
        )


def add_sale_to_history(
    parsed_sale,
    sale_link,
    sale_title
):
    """
    Добавляет Flash Sale в историю.

    Если такая ссылка уже присутствует,
    второй раз ничего не добавляет.
    """

    history = load_sales_history()

    for existing_sale in history:

        if existing_sale.get("link") == sale_link:
            return False

    history.append(
        {
            "date": parsed_sale["date"],
            "sale_date": parsed_sale["sale_date"],
            "item": parsed_sale["item"],
            "price": parsed_sale["price"],
            "link": sale_link,
            "title": sale_title,
        }
    )

    save_sales_history(
        history
    )

    logger.info(
        "Flash Sale добавлена в историю."
    )

    return True


# =========================================================
# ПРЕОБРАЗОВАНИЕ ДАТЫ
# =========================================================

def parse_reddit_date(date_text):
    """
    Превращает:

    September 13th, 2026

    в:

    date(2026, 9, 13)

    Мы не используем названия месяцев Windows,
    поэтому код работает независимо от языка системы.
    """

    match = re.match(
        r"^(?P<month>[A-Za-z]+)\s+"
        r"(?P<day>\d{1,2})"
        r"(?:st|nd|rd|th),\s+"
        r"(?P<year>\d{4})$",
        date_text.strip(),
        re.IGNORECASE
    )

    if not match:
        return None

    month_name = (
        match.group("month").lower()
    )

    month_number = MONTHS.get(
        month_name
    )

    if month_number is None:
        return None

    day_number = int(
        match.group("day")
    )

    year_number = int(
        match.group("year")
    )

    try:

        return date(
            year_number,
            month_number,
            day_number
        )

    except ValueError:

        return None


# =========================================================
# RSS REDDIT
# =========================================================

async def get_reddit_feed():
    """
    Загружает RSS r/swtor.
    """

    headers = {
        "User-Agent": (
            "discord-cartel-market-bot/2.0"
        )
    }

    timeout = aiohttp.ClientTimeout(
        total=20
    )

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout
    ) as session:

        async with session.get(
            REDDIT_RSS_URL
        ) as response:

            response.raise_for_status()

            rss_data = await response.read()

    return feedparser.parse(
        rss_data
    )


# =========================================================
# ПОИСК FLASH SALE
# =========================================================

async def get_reddit_post_image(post_url):
    """
    Получает изображение из Reddit-поста.

    Сначала пытаемся взять оригинальную картинку.
    Если её нет — используем preview.
    Если изображения вообще нет — возвращаем None.
    """

    if not post_url:
        return None

    json_url = (
        post_url.rstrip("/")
        + ".json?raw_json=1"
    )

    headers = {
        "User-Agent": (
            "discord-cartel-market-bot/2.0"
        )
    }

    timeout = aiohttp.ClientTimeout(
        total=20
    )

    try:

        async with aiohttp.ClientSession(
            headers=headers,
            timeout=timeout
        ) as session:

            async with session.get(
                json_url
            ) as response:

                response.raise_for_status()

                reddit_data = await response.json()

    except (
        aiohttp.ClientError,
        ValueError,
        KeyError,
        IndexError
    ) as error:

        logger.warning(
            "Не удалось получить изображение Reddit: %s",
            error
        )

        return None


    try:

        post = (
            reddit_data[0]
            ["data"]
            ["children"][0]
            ["data"]
        )

    except (
        KeyError,
        IndexError,
        TypeError
    ):

        return None


    # -------------------------------------------------
    # 1. Пытаемся получить оригинальное изображение
    # -------------------------------------------------

    image_url = post.get(
        "url_overridden_by_dest"
    )

    if image_url:

        if image_url.lower().endswith(
            (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp"
            )
        ):

            return image_url


    # -------------------------------------------------
    # 2. Если оригинала нет — берём Reddit preview
    # -------------------------------------------------

    try:

        image_url = (
            post["preview"]
            ["images"][0]
            ["source"]
            ["url"]
        )

        return image_url

    except (
        KeyError,
        IndexError,
        TypeError
    ):

        return None

async def get_latest_flash_sale():
    """
    Получает последнюю Flash Sale через Reddit JSON.

    Вместе с названием, ссылкой и ценой
    получаем прямую ссылку на изображение поста.
    """

    headers = {
        "User-Agent": (
            "discord-cartel-market-bot/3.0"
        )
    }

    timeout = aiohttp.ClientTimeout(
        total=20
    )

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout
    ) as session:

        async with session.get(
            REDDIT_JSON_URL
        ) as response:

            response.raise_for_status()

            reddit_data = await response.json(
                content_type=None
            )


    posts = (
        reddit_data
        .get("data", {})
        .get("children", [])
    )


    for child in posts:

        post = child.get(
            "data",
            {}
        )

        title = post.get(
            "title",
            ""
        ).strip()

        title_lower = title.lower()


        if (
            title_lower.startswith("flash")
            and "sale for" in title_lower
        ):

            # -----------------------------------------
            # ССЫЛКА НА REDDIT-ПОСТ
            # -----------------------------------------

            permalink = post.get(
                "permalink",
                ""
            )

            post_link = (
                "https://www.reddit.com"
                + permalink
            )


            # -----------------------------------------
            # ОРИГИНАЛЬНАЯ КАРТИНКА
            # -----------------------------------------

            image_url = post.get(
                "url_overridden_by_dest"
            )


            # -----------------------------------------
            # FALLBACK:
            # если прямой картинки нет,
            # пробуем взять Reddit Preview
            # -----------------------------------------

            if not image_url:

                try:

                    image_url = (
                        post["preview"]
                        ["images"][0]
                        ["source"]
                        ["url"]
                    )

                except (
                    KeyError,
                    IndexError,
                    TypeError
                ):

                    image_url = None


            logger.info(
                "Изображение Flash Sale: %s",
                image_url
            )


            return {
                "title": title,
                "link": post_link,
                "image_url": image_url
            }


# =========================================================
# РАЗБОР FLASH SALE
# =========================================================

def parse_sale_title(title):
    """
    Разбирает Reddit-заголовок на:

    дату;
    предмет;
    цену.
    """

    match = SALE_PATTERN.match(
        title
    )

    if not match:
        return None

    date_text = (
        match.group("date").strip()
    )

    sale_date = parse_reddit_date(
        date_text
    )

    if sale_date is None:
        return None

    return {
        "date": date_text,

        "sale_date": (
            sale_date.isoformat()
        ),

        "item": (
            match.group("item").strip()
        ),

        "price": (
            match.group("price")
            .replace(",", "")
            .strip()
        )
    }


# =========================================================
# НЕДЕЛЯ
# =========================================================

def get_digest_period(
    report_date
):
    """
    Возвращает полный завершённый 7-дневный период.

    Если отчёт отправляется во вторник,
    то берём:

    прошлый вторник -> вчерашний понедельник.
    """

    period_end = (
        report_date
        - timedelta(days=1)
    )

    period_start = (
        period_end
        - timedelta(days=6)
    )

    return period_start, period_end


def get_sales_for_week(
    monday,
    sunday
):
    """
    Возвращает Flash Sale только
    за выбранную неделю.
    """

    history = load_sales_history()

    result = []

    for sale in history:

        sale_date_text = sale.get(
            "sale_date"
        )

        if not sale_date_text:
            continue

        try:

            sale_date = date.fromisoformat(
                sale_date_text
            )

        except ValueError:
            continue

        if monday <= sale_date <= sunday:

            result.append(
                sale
            )

    result.sort(
        key=lambda sale: sale["sale_date"]
    )

    return result

# =========================================================
# Добавляем скачивание картинки
# =========================================================

async def download_sale_image(
    image_url
):
    """
    Скачивает картинку лота с Reddit.

    Возвращает discord.File,
    который можно прикрепить к Embed.
    """

    if not image_url:
        return None


    headers = {
        "User-Agent": (
            "discord-cartel-market-bot/3.0"
        )
    }

    timeout = aiohttp.ClientTimeout(
        total=20
    )


    try:

        async with aiohttp.ClientSession(
            headers=headers,
            timeout=timeout
        ) as session:

            async with session.get(
                image_url
            ) as response:

                response.raise_for_status()

                image_bytes = (
                    await response.read()
                )

                content_type = (
                    response.headers.get(
                        "Content-Type",
                        ""
                    )
                )


    except aiohttp.ClientError as error:

        logger.warning(
            "Не удалось скачать изображение: %s",
            error
        )

        return None


    # ---------------------------------------------
    # Определяем расширение
    # ---------------------------------------------

    if "png" in content_type:

        filename = "cartel-sale.png"

    elif "webp" in content_type:

        filename = "cartel-sale.webp"

    else:

        filename = "cartel-sale.jpg"


    image_buffer = BytesIO(
        image_bytes
    )


    return discord.File(
        image_buffer,
        filename=filename
    )

# =========================================================
# DISCORD BOT
# =========================================================

class CartelMarketBot(discord.Client):

    async def setup_hook(self):
        """
        Запускаем две независимые фоновые задачи:

        1. проверка ежедневной Flash Sale;
        2. недельный отчёт.
        """

        self.cartel_watcher.start()
        self.weekly_digest_watcher.start()


    async def on_ready(self):
        """
        Срабатывает после подключения
        к Discord.
        """

        logger.info(
            "Бот успешно подключился к Discord!"
        )

        logger.info(
            "Имя бота: %s",
            self.user
        )

        logger.info(
            "Канал: %s",
            CHANNEL_ID
        )


    # =====================================================
    # ФОНОВАЯ ЗАДАЧА
    # =====================================================

    @tasks.loop(
        minutes=CHECK_INTERVAL_MINUTES
    )
    async def cartel_watcher(self):

        try:

            await self.check_flash_sale()

        except aiohttp.ClientError as error:

            logger.error(
                "Ошибка HTTP: %s",
                error
            )

        except Exception:

            logger.exception(
                "Ошибка при проверке Flash Sale"
            )


    @cartel_watcher.before_loop
    async def before_cartel_watcher(self):

        await self.wait_until_ready()


    # =====================================================
    # НЕДЕЛЬНЫЙ ПЛАНИРОВЩИК
    # =====================================================

    @tasks.loop(
        time=WEEKLY_DIGEST_TIME
    )
    async def weekly_digest_watcher(self):
        """
        Планировщик запускается в 15:00 МСК.

        Сам отчёт отправляем только во вторник.
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

        except Exception:

            logger.exception(
                "Ошибка при отправке "
                "недельного дайджеста"
            )

    @weekly_digest_watcher.before_loop
    async def before_weekly_digest_watcher(self):

        await self.wait_until_ready()


    # =====================================================
    # ПОЛУЧЕНИЕ КАНАЛА
    # =====================================================

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
                error
            )

            return None


    # =====================================================
    # ЕЖЕДНЕВНАЯ FLASH SALE
    # =====================================================

    async def check_flash_sale(self):

        logger.info(
            "Проверяем Flash Sale..."
        )

        sale = await get_latest_flash_sale()

        if sale is None:

            logger.warning(
                "Flash Sale не найдена."
            )

            return


        sale_title = sale["title"]

        sale_link = sale["link"]


        parsed_sale = parse_sale_title(
            sale_title
        )


        if parsed_sale is None:

            logger.error(
                "Не удалось разобрать заголовок: %s",
                sale_title
            )

            return


        # ---------------------------------------------
        # Сохраняем в историю
        # ---------------------------------------------

        add_sale_to_history(
            parsed_sale,
            sale_link,
            sale_title
        )


        # ---------------------------------------------
        # Проверяем, публиковали ли уже
        # ---------------------------------------------

        state = load_state()

        if (
            state.get("last_sale")
            == sale_link
        ):

            logger.info(
                "Flash Sale уже опубликована."
            )

            return


        channel = await self.get_sales_channel()

        if channel is None:
            return


        # ---------------------------------------------
        # DAILY EMBED
        # ---------------------------------------------

        embed = discord.Embed(
            title=(
                "🔥 Cartel Market — Flash Sale"
            ),
            description=(
                f"## {parsed_sale['item']}"
            ),
            url=sale_link,
            color=discord.Color.gold()
        )


        embed.add_field(
            name="💰 Цена",
            value=(
                f"**{parsed_sale['price']} "
                f"Cartel Coins**"
            ),
            inline=True
        )


        embed.add_field(
            name="📅 Дата",
            value=parsed_sale["date"],
            inline=True
        )


        embed.add_field(
            name="🔗 Источник",
            value=(
                f"[Открыть пост r/swtor]"
                f"({sale_link})"
            ),
            inline=False
        )


        embed.set_footer(
            text=(
                "SWTOR Cartel Market "
                "• Daily Flash Sale"
            )
        )

        image_url = sale.get(
            "image_url"
        )

        if image_url:
            embed.set_image(
                url=image_url
            )

        # ---------------------------------------------
        # СКАЧИВАЕМ ИЗОБРАЖЕНИЕ ЛОТА
        # ---------------------------------------------

        image_file = await download_sale_image(
            sale.get("image_url")
        )

        # ---------------------------------------------
        # ЕСЛИ КАРТИНКА ЕСТЬ
        # ---------------------------------------------

        if image_file is not None:

            embed.set_image(
                url=(
                    f"attachment://"
                    f"{image_file.filename}"
                )
            )

            await channel.send(
                embed=embed,
                file=image_file
            )


        # ---------------------------------------------
        # ЕСЛИ КАРТИНКУ ПОЛУЧИТЬ НЕ УДАЛОСЬ
        # ---------------------------------------------

        else:

            logger.warning(
                "Flash Sale отправляется "
                "без изображения."
            )

            await channel.send(
                embed=embed
            )


        state["last_sale"] = sale_link

        save_state(
            state
        )


        logger.info(
            "Flash Sale отправлена в Discord."
        )


    # =====================================================
    # НЕДЕЛЬНЫЙ ДАЙДЖЕСТ
    # =====================================================

    async def send_weekly_digest(self):

        now = datetime.now(
            MOSCOW_TIMEZONE
        )

        # ---------------------------------------------
        # Определяем период отчёта
        # ---------------------------------------------

        period_start, period_end = (
            get_digest_period(
                now.date()
            )
        )

        # Уникальный ID конкретного отчёта
        digest_id = (
            f"{period_start.isoformat()}_"
            f"{period_end.isoformat()}"
        )

        state = load_state()

        # ---------------------------------------------
        # Не отправляем один отчёт дважды
        # ---------------------------------------------

        if (
                state.get(
                    "last_weekly_digest"
                )
                == digest_id
        ):
            logger.info(
                "Недельный дайджест "
                "уже был отправлен."
            )

            return

        # ---------------------------------------------
        # Получаем скидки за 7 дней
        # ---------------------------------------------

        weekly_sales = get_sales_for_week(
            period_start,
            period_end
        )

        if not weekly_sales:
            logger.info(
                "За выбранный период "
                "Flash Sale не найдены."
            )

            return

        channel = await self.get_sales_channel()

        if channel is None:
            return

        # ---------------------------------------------
        # Создаём один большой Embed
        # ---------------------------------------------

        embed = discord.Embed(
            title=(
                "🛒 Cartel Market — "
                "скидки за неделю"
            ),
            description=(
                f"📅 **"
                f"{period_start.strftime('%d.%m.%Y')}"
                f" — "
                f"{period_end.strftime('%d.%m.%Y')}"
                f"**\n\n"
                f"За неделю найдено "
                f"**{len(weekly_sales)}** "
                f"Flash Sale."
            ),
            color=discord.Color.gold()
        )

        # ---------------------------------------------
        # Добавляем товары списком
        # ---------------------------------------------

        for index, sale in enumerate(
                weekly_sales,
                start=1
        ):
            sale_date = date.fromisoformat(
                sale["sale_date"]
            )

            embed.add_field(
                name=(
                    f"{index}. "
                    f"{sale['item']}"
                ),
                value=(
                    f"💰 **{sale['price']} "
                    f"Cartel Coins**\n"
                    f"📅 "
                    f"{sale_date.strftime('%d.%m.%Y')}\n"
                    f"🔗 "
                    f"[Открыть источник]"
                    f"({sale['link']})"
                ),
                inline=False
            )

        embed.set_footer(
            text=(
                "SWTOR Cartel Market "
                "• Weekly Digest"
            )
        )

        # ---------------------------------------------
        # Отправляем
        # ---------------------------------------------

        await channel.send(
            embed=embed
        )

        # ---------------------------------------------
        # Запоминаем отправку
        # ---------------------------------------------

        state[
            "last_weekly_digest"
        ] = digest_id

        save_state(
            state
        )

        logger.info(
            "Недельный дайджест "
            "успешно отправлен."
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
    log_handler=None
)