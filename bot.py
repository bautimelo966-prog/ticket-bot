import os
import time
import json
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL", "180"))  # segundos
URLS_FILE        = "urls.json"

# Palabras que indican que HAY entradas disponibles
KEYWORDS_AVAILABLE = [
    "comprar", "compra", "comprá", "buy", "agregar al carrito",
    "seleccionar", "disponible", "en venta", "obtener entradas",
    "tickets disponibles", "comprar entrada", "comprar entradas",
    "ver entradas", "ver tickets", "adquirir", "adquirí",
    "conseguir entradas", "quiero ir", "comprar ticket",
    "elegir entradas", "elegí tu entrada", "comprá tu entrada",
    "comprá acá", "compra acá", "compra aquí",
]

# Palabras que indican que NO hay entradas
KEYWORDS_SOLD_OUT = [
    "agotado", "agotadas", "sold out", "no disponible",
    "no hay entradas", "sin stock", "próximamente", "proximamente",
    "pronto disponible", "entradas proximamente", "fecha a confirmar",
    "anuncio próximamente", "stay tuned",
]

# Headers para simular un navegador real
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
    """Carga las URLs guardadas. Devuelve dict {url: {"name": ..., "last_status": ...}}"""
    if os.path.exists(URLS_FILE):
        with open(URLS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_urls(data: dict):
    with open(URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── Telegram ───────────────────────────────────────────────────────────────
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

# ── Chequeo de página ──────────────────────────────────────────────────────
def check_url(url: str) -> dict:
    """
    Retorna:
      status: "available" | "sold_out" | "unknown" | "error"
      snippet: texto relevante encontrado
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Eliminar scripts y estilos para quedarnos con texto visible
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        text = soup.get_text(separator=" ").lower()

        # Buscar sold out primero (tiene prioridad)
        for kw in KEYWORDS_SOLD_OUT:
            if kw in text:
                return {"status": "sold_out", "snippet": kw}

        # Buscar disponibles
        for kw in KEYWORDS_AVAILABLE:
            if kw in text:
                return {"status": "available", "snippet": kw}

        return {"status": "unknown", "snippet": ""}

    except requests.exceptions.HTTPError as e:
        return {"status": "error", "snippet": str(e)}
    except Exception as e:
        return {"status": "error", "snippet": str(e)}

# ── Procesamiento de comandos Telegram ────────────────────────────────────
def handle_command(text: str, urls: dict) -> str:
    """Procesa comandos y retorna mensaje de respuesta."""
    parts = text.strip().split(maxsplit=2)
    cmd = parts[0].lower()

    # /add URL nombre_del_evento
    if cmd == "/add":
        if len(parts) < 3:
            return "⚠️ Uso correcto:\n<code>/add URL Nombre del evento</code>\n\nEjemplo:\n<code>/add https://ticketek.com.ar/evento/xxx Coldplay Fecha 1</code>"
        url  = parts[1]
        name = parts[2]
        if not url.startswith("http"):
            return "⚠️ La URL debe empezar con http:// o https://"
        if len(urls) >= 20:
            return "⚠️ Límite de 20 URLs alcanzado. Eliminá alguna con /remove antes de agregar."
        urls[url] = {"name": name, "last_status": "unknown", "added": datetime.now().isoformat()}
        save_urls(urls)
        return f"✅ Agregado:\n<b>{name}</b>\n{url}\n\nEmpezaré a monitorearlo de inmediato."

    # /remove URL
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

    # /list
    elif cmd == "/list":
        if not urls:
            return "📋 No tenés URLs en monitoreo.\nAgregá una con /add"
        lines = ["📋 <b>URLs monitoreadas:</b>\n"]
        for i, (url, data) in enumerate(urls.items(), 1):
            status_emoji = {"available": "🟢", "sold_out": "🔴", "unknown": "⚪", "error": "⚠️"}.get(data["last_status"], "⚪")
            lines.append(f"{i}. {status_emoji} <b>{data['name']}</b>\n   <a href='{url}'>{url[:60]}...</a>")
        return "\n".join(lines)

    # /check  (fuerza chequeo inmediato)
    elif cmd == "/check":
        if not urls:
            return "📋 No tenés URLs en monitoreo."
        return "__force_check__"

    # /help
    elif cmd in ("/help", "/start"):
        return (
            "🎫 <b>Bot de Entradas</b>\n\n"
            "Comandos disponibles:\n\n"
            "/add URL Nombre — Agregar URL a monitorear\n"
            "/remove URL — Eliminar una URL\n"
            "/list — Ver todas las URLs activas\n"
            "/check — Forzar chequeo ahora mismo\n"
            "/help — Ver esta ayuda\n\n"
            "El bot chequea automáticamente cada 3 minutos."
        )

    return f"❓ Comando no reconocido: {cmd}\nEscribí /help para ver los comandos."

# ── Loop principal ─────────────────────────────────────────────────────────
def run_check(urls: dict, notify_no_change=False):
    """Chequea todas las URLs y notifica cambios."""
    if not urls:
        return

    log.info(f"Chequeando {len(urls)} URLs...")
    changed = []

    for url, data in urls.items():
        result = check_url(url)
        new_status   = result["status"]
        prev_status  = data.get("last_status", "unknown")
        name         = data["name"]

        log.info(f"  [{new_status}] {name}")

        # Solo notificar si cambió el estado o si es la primera vez que está disponible
        if new_status == "available" and prev_status != "available":
            changed.append((url, name, new_status, result["snippet"]))

        elif new_status == "error":
            log.warning(f"  Error en {url}: {result['snippet']}")

        urls[url]["last_status"] = new_status

    save_urls(urls)

    # Enviar alertas
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
        # ── Procesar mensajes de Telegram ──────────────────────────────
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

        # ── Chequeo automático ─────────────────────────────────────────
        now = time.time()
        if now - last_check >= CHECK_INTERVAL:
            run_check(urls)
            last_check = now

        time.sleep(2)


if __name__ == "__main__":
    main()
