import os
import time
import json
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from playwright.sync_api import sync_playwright

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL", "1200"))  # 20 minutos
URLS_FILE        = "urls.json"

KEYWORDS_AVAILABLE = [
    "comprar", "compra", "comprá", "buy", "agregar al carrito",
    "seleccionar", "disponible", "en venta", "obtener entradas",
    "tickets disponibles", "comprar entrada", "comprar entradas",
    "ver entradas", "ver tickets", "adquirir", "adquirí",
    "conseguir entradas", "quiero ir", "comprar ticket",
    "elegir entradas", "elegí tu entrada", "comprá tu entrada",
    "comprá acá", "compra acá", "compra aquí",
]

KEYWORDS_SOLD_OUT = [
    "agotado", "agotadas", "sold out", "no disponible",
    "no hay entradas", "sin stock", "próximamente", "proximamente",
    "pronto disponible", "entradas proximamente", "fecha a confirmar",
    "anuncio próximamente", "stay tuned",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Manejo de URLs ─────────────────────────────────────────────────────────
def load_urls() -> dict:
    if os.path.exists(URLS_FILE):
        with open(URLS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_urls(data: dict):
    with open(URLS_FILE, "w"
