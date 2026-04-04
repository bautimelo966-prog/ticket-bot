import os
import time
import json
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL", "1200"))
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

def load_urls() -> dict:
    if os.path.exists(URLS_FILE):
        with open(URLS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_urls(data: dict):
    with open(URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def send_telegram(text: str, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Error enviando Telegram: {e}")

def get_telegram_updates(offset: int) -> list:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=10)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        log.error(f"Error obteniendo updates: {e}")
        return []

def check_movistar_arena(url: str) -> dict:
    email    = os.environ.get("MOVISTAR_EMAIL", "")
    password = os.environ.get("MOVISTAR_PASSWORD", "")

    if not email or not password:
        return {"status": "error", "snippet": "Credenciales de Movistar Arena no configuradas"}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            page.goto("https://login.movistararena.com.ar/Account/Login", timeout=30000)
            page.fill("#inputEmail", email)
            page.fill("#inputPassword", password)
            page.click("button.btn-login")
            page.wait_for_url("https://www.movistararena.com.ar/**", timeout=15000)

            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)

            # Intentar esperar el calendario (Arjona) o los botones directos (Calamaro)
            try:
                page.wait_for_selector("button.dia-evento", timeout=8000)
                fecha_buttons = page.query_selector_all("button.dia-evento")
            except Exception:
                fecha_buttons = []

            log.info(f"Fechas encontradas: {len(fecha_buttons)}")
            disponibles = []

            if fecha_buttons:
                # Formato con calendario (ej: Arjona)
                for btn in fecha_buttons:
                    try:
                        btn.click()
                        page.wait_for_timeout(1500)
                        ticket_buttons = page.query_selector_all("span.mud-button-label")
                        textos = [tb.inner_text().strip() for tb in ticket_buttons]
                        log.info(f"Textos botones: {textos}")
                        for tb in ticket_buttons:
                            texto = tb.inner_text().strip().lower()
                            if "seleccionar" in texto or "comprar" in texto:
                                day_style = btn.get_attribute("style") or ""
                                disponibles.append(day_style)
                                break
                    except Exception as ex:
                        log.warning(f"Error en fecha: {ex}")
                        continue
            else:
                # Formato sin calendario (ej: Calamaro)
                ticket_buttons = page.query_selector_all("span.mud-button-label")
                textos = [tb.inner_text().strip() for tb in ticket_buttons]
                log.info(f"Textos botones directos: {textos}")
                for tb in ticket_buttons:
                    texto = tb.inner_text().strip().lower()
                    if "seleccionar" in texto or "comprar" in texto:
                        disponibles.append(texto)
                        break

            browser.close()

        if disponibles:
            return {"status": "available", "snippet": f"Fechas con entradas: {', '.join(disponibles)}"}
        return {"status": "sold_out", "snippet": "agotado"}

    except Exception as e:
        log.error(f"Error Playwright: {e}")
        return {"status": "error", "snippet": str(e)}

def check_url(url: str) -> dict:
    if "movistararena.com.ar" in url:
        return check_movistar_arena(url)

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        text = soup.get_text(separator=" ").lower()

        for kw in KEYWORDS_SOLD_OUT:
            if kw in text:
                return {"status": "sold_out", "snippet": kw}

        for kw in KEYWORDS_AVAILABLE:
            if kw in text:
                return {"status": "available", "snippet": kw}

        return {"status": "unknown", "snippet": ""}

    except requests.exceptions.HTTPError as e:
        return {"status": "error", "snippet": str(e)}
    except Exception as e:
        return {"status": "error", "snippet": str(e)}

def handle_command(text: str, urls: dict) -> str:
    parts = text.strip().split(maxsplit=2)
    cmd = parts[0].lower()

    if cmd == "/add":
        if len(parts) < 3:
            return "⚠️ Uso correcto:\n<code>/add URL Nombre del evento</code>"
        url  = parts[1]
        name = parts[2]
        if not url.startswith("http"):
            return "⚠️ La URL debe empezar con http:// o https://"
        if len(urls) >= 20:
            return "⚠️ Límite de 20 URLs alcanzado."
        urls[url] = {"name": name, "last_status": "unknown", "added": datetime.now().isoformat()}
        save_urls(urls)
        return f"✅ Agregado:\n<b>{name}</b>\n{url}\n\nEmpezaré a monitorearlo de inmediato."

    elif cmd == "/remove":
        if len(parts) < 2:
            return "⚠️ Uso correcto:\n<code>/remove URL</code>"
        url = parts[1]
        if url in urls:
            name = urls[url]["name"]
            del urls[url]
            save_urls(urls)
            return f"🗑️ Eliminado: <b>{name}</b>"
        return "⚠️ No encontré esa URL en la lista."

    elif cmd == "/list":
        if not urls:
            return "📋 No tenés URLs en monitoreo.\nAgregá una con /add"
        lines = ["📋 <b>URLs monitoreadas:</b>\n"]
        for i, (url, data) in enumerate(urls.items(), 1):
            status_emoji = {"available": "🟢", "sold_out": "🔴", "unknown": "⚪", "error": "⚠️"}.get(data["last_status"], "⚪")
            lines.append(f"{i}. {status_emoji} <b>{data['name']}</b>\n   <a href='{url}'>{url[:60]}...</a>")
        return "\n".join(lines)

    elif cmd == "/check":
        if not urls:
            return "📋 No tenés URLs en monitoreo."
        return "__force_check__"

    elif cmd in ("/help", "/start"):
        return (
            "🎫 <b>Bot de Entradas</b>\n\n"
            "Comandos disponibles:\n\n"
            "/add URL Nombre — Agregar URL a monitorear\n"
            "/remove URL — Eliminar una URL\n"
            "/list — Ver todas las URLs activas\n"
            "/check — Forzar chequeo ahora mismo\n"
            "/help — Ver esta ayuda\n\n"
            "El bot chequea automáticamente cada 20 minutos."
        )

    return f"❓ Comando no reconocido: {cmd}\nEscribí /help para ver los comandos."

def run_check(urls: dict, notify_no_change=False):
    if not urls:
        return

    log.info(f"Chequeando {len(urls)} URLs...")
    changed = []

    for url, data in urls.items():
        result = check_url(url)
        new_status  = result["status"]
        prev_status = data.get("last_status", "unknown")
        name        = data["name"]

        log.info(f"  [{new_status}] {name}")

        if new_status == "available" and prev_status != "available":
            changed.append((url, name, new_status, result["snippet"]))
        elif new_status == "error":
            log.warning(f"  Error en {url}: {result['snippet']}")

        urls[url]["last_status"] = new_status

    save_urls(urls)

    for url, name, status, snippet in changed:
        msg = (
            f"🚨 <b>¡ENTRADAS DISPONIBLES!</b>\n\n"
            f"🎫 <b>{name}</b>\n"
            f"🔍 Detecté: <i>{snippet}</i>\n\n"
            f"👉 <a href='{url}'>Comprá acá</a>"
        )
        send_telegram(msg)
        log.info(f"  ✅ Alerta enviada: {name}")

    if notify_no_change and not changed:
        send_telegram("✅ Chequeo manual completado. Sin novedades por ahora.")


def main():
    log.info("🎫 Bot de Entradas iniciado")
    send_telegram(
        "🤖 <b>Bot de Entradas iniciado</b>\n\n"
        "Estoy activo y monitoreando.\n"
        "Escribí /help para ver los comandos disponibles."
    )

    urls = load_urls()
    last_check = 0
    offset = 0

    while True:
        updates = get_telegram_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = msg.get("text", "")
            if text.startswith("/"):
                response = handle_command(text, urls)
                if response == "__force_check__":
                    send_telegram("🔄 Chequeando ahora...")
                    run_check(urls, notify_no_change=True)
                else:
                    send_telegram(response)

        now = time.time()
        if now - last_check >= CHECK_INTERVAL:
            run_check(urls)
            last_check = now

        time.sleep(2)


if __name__ == "__main__":
    main()
