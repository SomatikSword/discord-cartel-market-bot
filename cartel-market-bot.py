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
