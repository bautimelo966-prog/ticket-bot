import os
import time
import json
import logging
import requests
import multiprocessing
from bs4 import BeautifulSoup
from datetime import datetime
from playwright.sync_api import sync_playwright

# ─────────────────────────────────────────────
# Configuración general
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
URLS_FILE        = "urls.json"

CHECK_INTERVAL_MOVISTAR  = 600    # 10 minutos
CHECK_INTERVAL_ALLACCESS = 300    # 5 minutos
CHECK_INTERVAL_DEFAULT   = 1200   # 20 minutos
PLAYWRIGHT_TIMEOUT       = 180    # 3 minutos

ROSALIA_URL = "https://www.movistararena.com.ar/Ticketera/38f53ef6-e155-414d-a0ba-65fe089fdf5a"
BTS_URL     = "https://www.allaccess.com.ar/event/bts"

BTS_FECHAS = [
    "https://www.allaccess.com.ar/event/bts-21-de-octubre",
    "https://www.allaccess.com.ar/event/bts-23-de-octubre",
    "https://www.allaccess.com.ar/event/bts-24-de-octubre",
]

CHROME_ARGS = [
    "--no-zygote",
    "--single-process",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

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

# ─────────────────────────────────────────────
# Persistencia
# ─────────────────────────────────────────────

def load_urls() -> dict:
    if os.path.exists(URLS_FILE):
        with open(URLS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_urls(data: dict):
    with open(URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────

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

# ─────────────────────────────────────────────
# Intervalos por sitio
# ─────────────────────────────────────────────

def get_interval(url: str) -> int:
    if "movistararena.com.ar" in url:
        return CHECK_INTERVAL_MOVISTAR
    if "allaccess.com.ar" in url:
        return CHECK_INTERVAL_ALLACCESS
    if "enigmatickets.com" in url:
        return CHECK_INTERVAL_ALLACCESS
    return CHECK_INTERVAL_DEFAULT

# ─────────────────────────────────────────────
# Multiprocessing
# ─────────────────────────────────────────────

def _worker(fn_name: str, url: str, result_queue: multiprocessing.Queue, env: dict):
    for k, v in env.items():
        os.environ[k] = v

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    try:
        if fn_name == "allaccess":
            result = _check_allaccess(url)
        elif fn_name == "bts":
            result = _check_bts(url)
        elif fn_name == "enigma":
            result = _check_enigmatickets(url)
        elif fn_name == "movistar":
            result = _check_movistar_arena(url)
        elif fn_name == "movistar_profundo":
            result = _check_movistar_profundo(url)
        else:
            result = {"status": "error", "snippet": f"Checker desconocido: {fn_name}", "fechas": {}}
        result_queue.put(result)
    except Exception as e:
        result_queue.put({"status": "error", "snippet": str(e), "fechas": {}})


def run_with_timeout(fn_name: str, url: str) -> dict:
    env = dict(os.environ)
    result_queue = multiprocessing.Queue()
    process = multiprocessing.Process(
        target=_worker,
        args=(fn_name, url, result_queue, env),
        daemon=True
    )
    process.start()
    process.join(timeout=PLAYWRIGHT_TIMEOUT)

    if process.is_alive():
        log.error(f"[TIMEOUT] Proceso killed después de {PLAYWRIGHT_TIMEOUT}s — {url}")
        process.kill()
        process.join()
        return {
            "status": "error",
            "snippet": f"Timeout: el chequeo tardó más de {PLAYWRIGHT_TIMEOUT} segundos y fue cancelado.",
            "fechas": {}
        }

    if not result_queue.empty():
        return result_queue.get_nowait()

    return {
        "status": "error",
        "snippet": "El proceso de chequeo terminó inesperadamente sin resultado.",
        "fechas": {}
    }

# ─────────────────────────────────────────────
# Login Movistar
# ─────────────────────────────────────────────

def _login_movistar(page):
    email    = os.environ.get("MOVISTAR_EMAIL", "")
    password = os.environ.get("MOVISTAR_PASSWORD", "")

    if not email or not password:
        raise Exception("Credenciales no configuradas")

    logging.info("[Movistar] Paso 1: Navegando al login...")
    page.goto("https://login.movistararena.com.ar/Account/Login", timeout=30000)
    logging.info("[Movistar] Página de login cargada")

    logging.info("[Movistar] Paso 2: Completando formulario...")
    page.fill("#inputEmail", email)
    page.fill("#inputPassword", password)

    logging.info("[Movistar] Paso 3: Haciendo click en login...")
    page.click("button.btn-login")

    logging.info("[Movistar] Paso 4: Esperando redirección...")
    page.wait_for_url("https://www.movistararena.com.ar/**", timeout=15000)
    logging.info("[Movistar] Login exitoso")

def _get_mes_texto(page) -> str:
    try:
        mes_header = page.query_selector(".mud-picker-calendar-header-transition")
        return mes_header.inner_text().strip() if mes_header else ""
    except Exception:
        return ""

def _volver_al_evento(page, url: str):
    logging.info("[Movistar-Profundo] Volviendo al evento por navegación directa...")
    page.goto(url, timeout=30000)
    page.wait_for_load_state("networkidle", timeout=20000)
    page.wait_for_timeout(1500)
    logging.info("[Movistar-Profundo] Evento recargado")

def _get_fecha_label_fila(fila) -> str:
    """
    Intenta obtener el label de fecha de una fila de evento.
    Soporta dos formatos de Movistar Arena:
    - Formato viejo: div.fecha p + div.fecha span → "19 de mayo"
    - Formato nuevo: div.hora p → "Del 01 al 05 de julio 2026"
    """
    # Formato nuevo: div.hora p
    hora_el = fila.query_selector("div.hora p")
    if hora_el:
        texto = hora_el.inner_text().strip()
        if texto:
            return texto

    # Formato viejo: div.fecha p + div.fecha span
    dia_el = fila.query_selector("div.fecha p")
    mes_el = fila.query_selector("div.fecha span")
    dia = dia_el.inner_text().strip() if dia_el else ""
    mes = mes_el.inner_text().strip() if mes_el else ""
    if dia or mes:
        return f"{dia} de {mes}".strip()

    # Fallback: texto completo de la fila truncado
    try:
        texto = fila.inner_text().strip()[:50]
        return texto if texto else "Fecha desconocida"
    except Exception:
        return "Fecha desconocida"

# ─────────────────────────────────────────────
# Checker estándar Movistar Arena
# ─────────────────────────────────────────────

def _check_movistar_arena(url: str) -> dict:
    logging.info(f"[Movistar] Iniciando chequeo: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            page = browser.new_page()
            _login_movistar(page)

            logging.info("[Movistar] Paso 5: Navegando al evento...")
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            logging.info("[Movistar] Página del evento cargada")

            fechas_estado = {}

            try:
                logging.info("[Movistar] Paso 6: Buscando botones de calendario...")
                page.wait_for_selector("button.dia-evento", timeout=8000)
                fecha_buttons = page.query_selector_all("button.dia-evento")
                logging.info(f"[Movistar] Formato calendario — {len(fecha_buttons)} fechas encontradas")

                mes_texto = _get_mes_texto(page)
                logging.info(f"[Movistar] Mes detectado: '{mes_texto}'")

                for i, btn in enumerate(fecha_buttons):
                    try:
                        logging.info(f"[Movistar] Procesando fecha {i+1}/{len(fecha_buttons)}...")
                        btn.click()
                        page.wait_for_timeout(1500)

                        dia_el = btn.query_selector("p")
                        dia    = dia_el.inner_text().strip() if dia_el else "?"
                        fecha_label = f"{dia} de {mes_texto}"

                        ticket_buttons   = page.query_selector_all("span.mud-button-label")
                        tiene_disponible = False
                        for tb in ticket_buttons:
                            texto = tb.inner_text().strip().lower()
                            if "seleccionar" in texto or "comprar" in texto:
                                tiene_disponible = True
                                break

                        fechas_estado[fecha_label] = "available" if tiene_disponible else "sold_out"
                        logging.info(f"[Movistar] {fecha_label}: {fechas_estado[fecha_label]}")

                    except Exception as ex:
                        logging.warning(f"[Movistar] Error procesando fecha {i+1}: {ex}")
                        continue

            except Exception:
                logging.info("[Movistar] Calendario no encontrado, intentando formato lista...")
                filas = page.query_selector_all("div.evento-row")
                logging.info(f"[Movistar] Formato lista — {len(filas)} filas encontradas")

                for i, fila in enumerate(filas):
                    try:
                        # FIX: usar helper que soporta ambos formatos de fecha
                        fecha_label = _get_fecha_label_fila(fila)

                        ticket_buttons   = fila.query_selector_all("span.mud-button-label")
                        tiene_disponible = False
                        for tb in ticket_buttons:
                            texto = tb.inner_text().strip().lower()
                            if "seleccionar" in texto or "comprar" in texto:
                                tiene_disponible = True
                                break

                        # Si no hay mud-button-label, buscar botón agotado/comprar genérico
                        if not ticket_buttons:
                            texto_fila = fila.inner_text().lower()
                            if "agotado" in texto_fila:
                                tiene_disponible = False
                            elif any(kw in texto_fila for kw in ["comprar", "seleccionar", "disponible"]):
                                tiene_disponible = True

                        fechas_estado[fecha_label] = "available" if tiene_disponible else "sold_out"
                        logging.info(f"[Movistar] {fecha_label}: {fechas_estado[fecha_label]}")

                    except Exception as ex:
                        logging.warning(f"[Movistar] Error procesando fila {i+1}: {ex}")
                        continue

        finally:
            browser.close()
            logging.info("[Movistar] Browser cerrado")

    disponibles = [f for f, s in fechas_estado.items() if s == "available"]
    if disponibles:
        return {
            "status": "available",
            "snippet": f"Fechas disponibles: {', '.join(disponibles)}",
            "fechas": fechas_estado
        }
    return {"status": "sold_out", "snippet": "agotado", "fechas": fechas_estado}


# ─────────────────────────────────────────────
# Checker profundo Movistar Arena (Rosalía)
# ─────────────────────────────────────────────

def _check_movistar_profundo(url: str) -> dict:
    logging.info(f"[Movistar-Profundo] Iniciando chequeo profundo: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            page = browser.new_page()
            _login_movistar(page)

            logging.info("[Movistar-Profundo] Paso 5: Navegando al evento...")
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            logging.info("[Movistar-Profundo] Página del evento cargada")

            fechas_estado = {}

            try:
                page.wait_for_selector("button.dia-evento", timeout=15000)
                fecha_buttons = page.query_selector_all("button.dia-evento")
                total_fechas  = len(fecha_buttons)
                logging.info(f"[Movistar-Profundo] {total_fechas} fechas encontradas")

                mes_texto = _get_mes_texto(page)

                dias = []
                for btn in fecha_buttons:
                    try:
                        dia_el = btn.query_selector("p")
                        dia    = dia_el.inner_text().strip() if dia_el else "?"
                        dias.append(dia)
                    except Exception:
                        dias.append("?")

            except Exception as ex:
                logging.warning(f"[Movistar-Profundo] Error buscando calendario: {ex}")
                return {"status": "error", "snippet": str(ex), "fechas": {}}

            for i in range(total_fechas):
                fecha_label = f"{dias[i]} de {mes_texto}"
                logging.info(f"[Movistar-Profundo] Procesando fecha {i+1}/{total_fechas}: {fecha_label}")

                try:
                    page.wait_for_selector("button.dia-evento", timeout=15000)
                    fecha_buttons_fresh = page.query_selector_all("button.dia-evento")

                    if i >= len(fecha_buttons_fresh):
                        logging.warning(f"[Movistar-Profundo] Índice {i} fuera de rango, saltando")
                        continue

                    fecha_buttons_fresh[i].click()
                    page.wait_for_timeout(1500)

                    btn_seleccionar = None
                    todos_los_botones = page.query_selector_all("span.mud-button-label")

                    for tb in todos_los_botones:
                        texto = tb.inner_text().strip().lower()
                        if "seleccionar" not in texto and "comprar" not in texto:
                            continue
                        try:
                            parent_text = tb.evaluate(
                                "el => { let p = el.closest('div'); return p ? p.innerText : ''; }"
                            ).lower()
                            if "diamond" in parent_text or "gold" in parent_text or "vip" in parent_text:
                                logging.info("[Movistar-Profundo] Ignorando botón VIP")
                                continue
                        except Exception:
                            pass
                        btn_seleccionar = tb
                        break

                    if not btn_seleccionar:
                        logging.info(f"[Movistar-Profundo] {fecha_label}: sin botón Seleccionar → sold_out")
                        fechas_estado[fecha_label] = "sold_out"
                        continue

                    logging.info(f"[Movistar-Profundo] Haciendo click en Seleccionar para {fecha_label}...")
                    btn_seleccionar.click()
                    page.wait_for_load_state("networkidle", timeout=15000)
                    page.wait_for_timeout(2000)

                    sectores_disponibles = page.query_selector_all("g.esSector:not(.disabled)")
                    cant = len(sectores_disponibles)
                    logging.info(f"[Movistar-Profundo] {fecha_label}: {cant} sectores disponibles en mapa")

                    if cant > 0:
                        fechas_estado[fecha_label] = "available"
                        logging.info(f"[Movistar-Profundo] ✅ {fecha_label}: HAY ENTRADAS REALES")
                    else:
                        fechas_estado[fecha_label] = "sold_out"
                        logging.info(f"[Movistar-Profundo] {fecha_label}: mapa todo gris, sin entradas")

                    _volver_al_evento(page, url)

                except Exception as ex:
                    logging.warning(f"[Movistar-Profundo] Error en fecha {i+1} ({fecha_label}): {ex}")
                    fechas_estado[fecha_label] = "unknown"
                    try:
                        _volver_al_evento(page, url)
                    except Exception:
                        pass
                    continue

        finally:
            browser.close()
            logging.info("[Movistar-Profundo] Browser cerrado")

    disponibles = [f for f, s in fechas_estado.items() if s == "available"]
    if disponibles:
        return {
            "status": "available",
            "snippet": f"Fechas con entradas reales: {', '.join(disponibles)}",
            "fechas": fechas_estado
        }
    return {"status": "sold_out", "snippet": "sin entradas reales en el mapa", "fechas": fechas_estado}


# ─────────────────────────────────────────────
# Checker AllAccess estándar
# ─────────────────────────────────────────────

def _check_allaccess(url: str) -> dict:
    logging.info(f"[AllAccess] Iniciando chequeo: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            page = browser.new_page()
            logging.info("[AllAccess] Navegando a la página...")
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            logging.info("[AllAccess] Página cargada")

            fechas_estado = {}

            try:
                page.click("div.dropdown", timeout=5000)
                page.wait_for_timeout(1000)
                logging.info("[AllAccess] Dropdown abierto")
            except Exception:
                logging.info("[AllAccess] Sin dropdown, continuando")

            items = page.query_selector_all("ul#show-dropdown li")
            logging.info(f"[AllAccess] Fechas encontradas: {len(items)}")

            for item in items:
                try:
                    clase = item.get_attribute("class") or ""
                    texto_el = item.query_selector("div")
                    texto = texto_el.inner_text().strip() if texto_el else item.inner_text().strip()
                    fecha_label = texto.split("\n")[0].strip()
                    if not fecha_label:
                        continue
                    if "agotado" in clase.lower():
                        fechas_estado[fecha_label] = "sold_out"
                    else:
                        fechas_estado[fecha_label] = "available"
                    logging.info(f"[AllAccess] {fecha_label}: {fechas_estado[fecha_label]}")
                except Exception as ex:
                    logging.warning(f"[AllAccess] Error leyendo item: {ex}")
                    continue
        finally:
            browser.close()
            logging.info("[AllAccess] Browser cerrado")

    disponibles = [f for f, s in fechas_estado.items() if s == "available"]
    if disponibles:
        return {
            "status": "available",
            "snippet": f"Fechas disponibles: {', '.join(disponibles)}",
            "fechas": fechas_estado
        }
    return {"status": "sold_out", "snippet": "agotado", "fechas": fechas_estado}


# ─────────────────────────────────────────────
# Checker BTS
# ─────────────────────────────────────────────

def _check_bts_fecha(page, fecha_url: str) -> str:
    fecha_label = fecha_url.split("/event/bts-")[-1]
    logging.info(f"[BTS] Chequeando: {fecha_label}")

    page.goto(fecha_url, timeout=30000)
    page.wait_for_load_state("networkidle", timeout=20000)
    page.wait_for_timeout(2000)

    sold_out_global = page.query_selector("div.event-status.status-soldout")
    if sold_out_global:
        logging.info(f"[BTS] {fecha_label}: agotado global")
        return "sold_out"

    try:
        page.wait_for_selector("div.selection-container", timeout=10000)
        logging.info(f"[BTS] {fecha_label}: panel de tarifas cargado")
    except Exception:
        logging.info(f"[BTS] {fecha_label}: sin panel de tarifas → unknown")
        return "unknown"

    try:
        contenido = page.inner_text("div.selection-container").lower()
        logging.info(f"[BTS] {fecha_label}: contenido panel: {contenido[:300]}")
    except Exception:
        logging.info(f"[BTS] {fecha_label}: no se pudo leer el panel → unknown")
        return "unknown"

    if "campo" not in contenido:
        logging.info(f"[BTS] {fecha_label}: Campo no aparece → sold_out")
        return "sold_out"

    lineas = contenido.split("\n")
    for idx, linea in enumerate(lineas):
        if "campo" in linea:
            contexto = " ".join(lineas[max(0, idx-2):idx+3])
            if "agotado" in contexto:
                logging.info(f"[BTS] {fecha_label}: Campo con AGOTADO → sold_out")
                return "sold_out"
            else:
                logging.info(f"[BTS] {fecha_label}: ✅ Campo sin AGOTADO → available")
                return "available"

    logging.info(f"[BTS] {fecha_label}: Campo encontrado pero no determinado → unknown")
    return "unknown"


def _check_bts(url: str) -> dict:
    logging.info(f"[BTS] Iniciando chequeo de las 3 fechas")
    fechas_estado = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            page = browser.new_page()

            for fecha_url in BTS_FECHAS:
                fecha_label = fecha_url.split("/event/bts-")[-1].replace("-", " ").title()
                try:
                    estado = _check_bts_fecha(page, fecha_url)
                    fechas_estado[fecha_label] = estado
                    logging.info(f"[BTS] {fecha_label}: {estado}")
                except Exception as ex:
                    logging.warning(f"[BTS] Error en {fecha_label}: {ex}")
                    fechas_estado[fecha_label] = "unknown"

        finally:
            browser.close()
            logging.info("[BTS] Browser cerrado")

    disponibles = [f for f, s in fechas_estado.items() if s == "available"]
    if disponibles:
        return {
            "status": "available",
            "snippet": f"Campo disponible en: {', '.join(disponibles)}",
            "fechas": fechas_estado
        }
    return {"status": "sold_out", "snippet": "Campo agotado en todas las fechas", "fechas": fechas_estado}


# ─────────────────────────────────────────────
# Checker Enigma Tickets
# ─────────────────────────────────────────────

def _check_enigmatickets(url: str) -> dict:
    logging.info(f"[Enigma] Iniciando chequeo: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            page = browser.new_page()
            logging.info("[Enigma] Navegando a la página...")
            page.goto(url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            logging.info("[Enigma] Página cargada")

            fechas_estado = {}

            filas = page.query_selector_all("div.flex.h-\\[40px\\].items-center.pl-3.pr-3.justify-between")
            logging.info(f"[Enigma] Fases encontradas: {len(filas)}")

            for fila in filas:
                try:
                    nombre_el = fila.query_selector("span.truncate")
                    nombre = nombre_el.inner_text().strip() if nombre_el else "Fase desconocida"

                    estado_el = fila.query_selector("span[data-testid='text-component']")
                    estado_texto = estado_el.inner_text().strip().lower() if estado_el else ""

                    btn_div = fila.query_selector("div.flex.justify-end div")
                    clases = btn_div.get_attribute("class") if btn_div else ""

                    logging.info(f"[Enigma] [{nombre}]: '{estado_texto}' | clases: {clases}")

                    if "agotado" in estado_texto or "sold out" in estado_texto or "bg-red" in clases:
                        fechas_estado[nombre] = "sold_out"
                    elif any(kw in estado_texto for kw in ["comprar", "disponible", "compra", "buy"]):
                        fechas_estado[nombre] = "available"
                    else:
                        fechas_estado[nombre] = "unknown"

                except Exception as ex:
                    logging.warning(f"[Enigma] Error leyendo fila: {ex}")
                    continue

            if not fechas_estado:
                logging.info("[Enigma] Sin filas, usando fallback con spans")
                todos_los_spans = page.query_selector_all("span[data-testid='text-component']")
                for span in todos_los_spans:
                    texto = span.inner_text().strip().lower()
                    if "agotado" in texto or "sold out" in texto:
                        fechas_estado["General"] = "sold_out"
                        break
                    elif any(kw in texto for kw in ["comprar", "disponible", "buy"]):
                        fechas_estado["General"] = "available"
                        break
        finally:
            browser.close()
            logging.info("[Enigma] Browser cerrado")

    disponibles = [f for f, s in fechas_estado.items() if s == "available"]
    if disponibles:
        return {
            "status": "available",
            "snippet": f"Fases disponibles: {', '.join(disponibles)}",
            "fechas": fechas_estado
        }
    return {"status": "sold_out", "snippet": "agotado", "fechas": fechas_estado}


# ─────────────────────────────────────────────
# Checkers públicos
# ─────────────────────────────────────────────

def check_allaccess(url: str) -> dict:
    return run_with_timeout("allaccess", url)

def check_bts(url: str) -> dict:
    return run_with_timeout("bts", url)

def check_enigmatickets(url: str) -> dict:
    return run_with_timeout("enigma", url)

def check_movistar_arena(url: str) -> dict:
    return run_with_timeout("movistar", url)

def check_movistar_profundo(url: str) -> dict:
    return run_with_timeout("movistar_profundo", url)

def check_url(url: str) -> dict:
    if "movistararena.com.ar" in url:
        if url == ROSALIA_URL:
            return check_movistar_profundo(url)
        return check_movistar_arena(url)
    if url == BTS_URL:
        return check_bts(url)
    if "allaccess.com.ar" in url:
        return check_allaccess(url)
    if "enigmatickets.com" in url:
        return check_enigmatickets(url)

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        text = soup.get_text(separator=" ").lower()

        for kw in KEYWORDS_SOLD_OUT:
            if kw in text:
                return {"status": "sold_out", "snippet": kw, "fechas": {}}

        for kw in KEYWORDS_AVAILABLE:
            if kw in text:
                return {"status": "available", "snippet": kw, "fechas": {}}

        return {"status": "unknown", "snippet": "", "fechas": {}}

    except requests.exceptions.HTTPError as e:
        return {"status": "error", "snippet": str(e), "fechas": {}}
    except Exception as e:
        return {"status": "error", "snippet": str(e), "fechas": {}}

# ─────────────────────────────────────────────
# Comandos Telegram
# ─────────────────────────────────────────────

def handle_command(text: str, urls: dict) -> str:
    parts = text.strip().split(maxsplit=2)
    cmd   = parts[0].lower()

    if cmd == "/add":
        if len(parts) < 3:
            return "⚠️ Uso correcto:\n<code>/add URL Nombre del evento</code>"
        url  = parts[1]
        name = parts[2]
        if not url.startswith("http"):
            return "⚠️ La URL debe empezar con http:// o https://"
        if len(urls) >= 20:
            return "⚠️ Límite de 20 URLs alcanzado."
        urls[url] = {
            "name": name,
            "last_status": "unknown",
            "last_check": 0,
            "fechas": {},
            "added": datetime.now().isoformat()
        }
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
            status_emoji = {
                "available": "🟢",
                "sold_out":  "🔴",
                "unknown":   "⚪",
                "error":     "⚠️"
            }.get(data["last_status"], "⚪")
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
            "AllAccess y Enigma: cada 5 minutos\n"
            "Movistar Arena: cada 10 minutos"
        )

    return f"❓ Comando no reconocido: {cmd}\nEscribí /help para ver los comandos."

# ─────────────────────────────────────────────
# Lógica principal de chequeo
# ─────────────────────────────────────────────

def run_check(urls: dict, notify_no_change: bool = False, force: bool = False):
    if not urls:
        return

    now           = time.time()
    urls_to_check = []

    for url, data in urls.items():
        interval   = get_interval(url)
        last_check = data.get("last_check", 0)
        if force or (now - last_check >= interval):
            urls_to_check.append(url)

    if not urls_to_check:
        return

    log.info(f"Chequeando {len(urls_to_check)} URLs...")
    changed = []
    errors  = []
    resumen = []

    for url in urls_to_check:
        data          = urls[url]
        name          = data["name"]
        fechas_prev   = data.get("fechas", {})

        log.info(f"Iniciando chequeo: {name}")
        result        = check_url(url)
        new_status    = result["status"]
        nuevas_fechas = result.get("fechas", {})

        log.info(f"  [{new_status}] {name}")

        nuevas_disponibles = []
        for fecha, estado in nuevas_fechas.items():
            if estado == "available" and fechas_prev.get(fecha) != "available":
                nuevas_disponibles.append(fecha)

        if nuevas_disponibles:
            changed.append((url, name, nuevas_disponibles))
        elif new_status == "error":
            log.warning(f"  Error en {name}: {result['snippet']}")
            errors.append((name, result["snippet"]))

        disponibles_actuales = [f for f, s in nuevas_fechas.items() if s == "available"]
        if disponibles_actuales:
            resumen.append(f"🟢 <b>{name}</b>: {', '.join(disponibles_actuales)}")
        else:
            resumen.append(f"🔴 <b>{name}</b>: sin entradas")

        urls[url]["last_status"] = new_status
        urls[url]["fechas"]      = nuevas_fechas
        urls[url]["last_check"]  = now

    save_urls(urls)

    for url, name, fechas_nuevas in changed:
        msg = (
            f"🚨 <b>¡ENTRADAS DISPONIBLES!</b>\n\n"
            f"🎫 <b>{name}</b>\n"
            f"🗓 <i>{', '.join(fechas_nuevas)}</i>\n\n"
            f"👉 <a href='{url}'>Comprá acá</a>"
        )
        send_telegram(msg)
        log.info(f"  ✅ Alerta enviada: {name} — {fechas_nuevas}")

    for name, snippet in errors:
        send_telegram(
            f"⚠️ <b>Error chequeando {name}</b>\n\n"
            f"<i>{snippet[:200]}</i>\n\n"
            f"El bot seguirá intentando en el próximo chequeo."
        )

    if notify_no_change:
        msg = "📋 <b>Estado actual:</b>\n\n" + "\n".join(resumen) if resumen else \
              "✅ Chequeo manual completado. Sin novedades por ahora."
        send_telegram(msg)

# ─────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────

def main():
    log.info("🎫 Bot de Entradas iniciado")
    send_telegram(
        "🤖 <b>Bot de Entradas iniciado</b>\n\n"
        "Estoy activo y monitoreando.\n"
        "Escribí /help para ver los comandos disponibles."
    )

    urls       = load_urls()
    last_daily = 0
    offset     = 0

    while True:
        updates = get_telegram_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg    = update.get("message", {})
            text   = msg.get("text", "")
            if text.startswith("/"):
                response = handle_command(text, urls)
                if response == "__force_check__":
                    send_telegram("🔄 Chequeando ahora...")
                    run_check(urls, notify_no_change=True, force=True)
                else:
                    send_telegram(response)

        run_check(urls)

        hora_actual = datetime.utcnow().hour - 3
        if hora_actual < 0:
            hora_actual += 24
        now = time.time()
        if hora_actual == 9 and now - last_daily >= 86400:
            total   = len(urls)
            nombres = ", ".join([data["name"] for data in urls.values()]) if urls else "ninguno"
            send_telegram(
                f"🟢 <b>Bot activo</b>\n\n"
                f"Estoy funcionando correctamente.\n"
                f"Monitoreando {total} evento(s): {nombres}"
            )
            last_daily = now

        time.sleep(2)


if __name__ == "__main__":
    multiprocessing.set_start_method("fork", force=True)
    main()
