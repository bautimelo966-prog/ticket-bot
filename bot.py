import os
import time
import json
import logging
import requests
import multiprocessing
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from html import escape
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from queue import Empty
from urllib.parse import urlsplit, urlunsplit
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
URLS_FILE        = os.environ.get("URLS_FILE", "urls.json")
TELEGRAM_OFFSET_FILE = os.environ.get(
    "TELEGRAM_OFFSET_FILE",
    os.path.join(os.path.dirname(os.path.abspath(URLS_FILE)), "telegram_offset.json"),
)

CHECK_INTERVAL_MOVISTAR  = int(os.environ.get("CHECK_INTERVAL_MOVISTAR", "600"))
CHECK_INTERVAL_ALLACCESS = int(os.environ.get("CHECK_INTERVAL_ALLACCESS", "300"))
CHECK_INTERVAL_DEFAULT   = int(os.environ.get("CHECK_INTERVAL_DEFAULT", "1200"))
PLAYWRIGHT_TIMEOUT       = int(os.environ.get("PLAYWRIGHT_TIMEOUT", "240"))
TELEGRAM_RETRIES         = int(os.environ.get("TELEGRAM_RETRIES", "4"))
MAX_SECTORS_TO_VERIFY    = int(os.environ.get("MAX_SECTORS_TO_VERIFY", "30"))
MAX_CONCURRENT_CHECKS    = int(os.environ.get("MAX_CONCURRENT_CHECKS", "2"))
HEALTH_FAILURE_THRESHOLD = int(os.environ.get("HEALTH_FAILURE_THRESHOLD", "3"))
UNRELIABLE_RETRY_BASE    = int(os.environ.get("UNRELIABLE_RETRY_BASE", "30"))
UNRELIABLE_RETRY_MAX     = int(os.environ.get("UNRELIABLE_RETRY_MAX", "120"))

STATUS_AVAILABLE = "available"
STATUS_CANDIDATE = "candidate"
STATUS_SOLD_OUT  = "sold_out"
STATUS_UNKNOWN   = "unknown"
STATUS_BLOCKED   = "blocked"
STATUS_ERROR     = "error"

KNOWN_STATUSES = {
    STATUS_AVAILABLE,
    STATUS_CANDIDATE,
    STATUS_SOLD_OUT,
    STATUS_UNKNOWN,
    STATUS_BLOCKED,
    STATUS_ERROR,
}

UNRELIABLE_STATUSES = {STATUS_UNKNOWN, STATUS_BLOCKED, STATUS_ERROR}


def unreliable_retry_delay(failure_count: int) -> int:
    """Calcula 30s, 60s, 120s... sin superar el máximo configurado."""
    exponent = max(0, int(failure_count) - 1)
    return min(UNRELIABLE_RETRY_BASE * (2 ** exponent), UNRELIABLE_RETRY_MAX)

BTS_URL     = "https://www.allaccess.com.ar/event/bts"

BTS_FECHAS = [
    "https://www.allaccess.com.ar/event/bts-21-de-octubre",
    "https://www.allaccess.com.ar/event/bts-23-de-octubre",
    "https://www.allaccess.com.ar/event/bts-24-de-octubre",
]

VIP_KEYWORDS = ["diamond", "gold", "silver", "vip", "platinum", "black"]

# Color computado de los polígonos disponibles en el mapa actual de Movistar.
# Se usa sólo como respaldo cuando el mapa no expone la clase ``esSector``.
MOVISTAR_AVAILABLE_FILL = "rgb(91, 203, 94)"

MOVISTAR_SECTOR_SELECTOR = "g.esSector:not(.disabled)"
MOVISTAR_PURCHASE_SIGNAL_FAILED = "purchase_signal_failed"

# Señales fuertes: estos selectores deben apuntar a inventario seleccionable,
# no simplemente a un sector habilitado del mapa general.
MOVISTAR_SEAT_SELECTORS = [
    # Clases publicadas actualmente por Movistar Arena en main.css.
    ".asientos-vista .seat.seat-available",
    ".asientos-vista .seat.seat-visionrestringida",
    "[data-seat-status='available']",
    "[data-status='available'][data-seat-id]",
    "[data-available='true'][data-seat-id]",
    ".seat.available:not(.disabled)",
    ".seat.disponible:not(.disabled)",
    ".asiento.available:not(.disabled)",
    ".asiento.disponible:not(.disabled)",
    "[aria-label*='asiento'][aria-disabled='false']",
    "[aria-label*='seat'][aria-disabled='false']",
]

# Para sectores sin asiento numerado, Movistar expone un contador +/- dentro
# del panel lateral. Un botón habilitado allí permite aumentar la cantidad.
MOVISTAR_QUANTITY_BUTTON_SELECTOR = (
    ".sidebar-mapa .contador button:not([disabled]):not(.mud-disabled)"
)

BLOCKED_MARKERS = [
    "actividad sospechosa",
    "suspicious activity",
    "access denied",
    "captcha",
    "verifica que eres humano",
    "verify you are human",
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

def normalize_url(url: str) -> str:
    """Normaliza una URL sin alterar su path significativo."""
    raw = url.strip()
    parts = urlsplit(raw)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def is_valid_http_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def load_urls() -> dict:
    if os.path.exists(URLS_FILE):
        try:
            with open(URLS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("El archivo de URLs no contiene un objeto JSON")
            return data
        except Exception as exc:
            log.exception("No se pudo cargar %s: %s", URLS_FILE, exc)
            raise
    return {}

def save_urls(data: dict):
    data_dir = os.path.dirname(os.path.abspath(URLS_FILE))
    os.makedirs(data_dir, exist_ok=True)

    temp_file = f"{URLS_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp_file, URLS_FILE)


def load_telegram_offset() -> int:
    try:
        with open(TELEGRAM_OFFSET_FILE, "r", encoding="utf-8") as f:
            value = json.load(f)
        return int(value.get("offset", 0))
    except FileNotFoundError:
        return 0
    except Exception as exc:
        log.warning("No se pudo cargar el offset de Telegram: %s", exc)
        return 0


def save_telegram_offset(offset: int):
    os.makedirs(os.path.dirname(os.path.abspath(TELEGRAM_OFFSET_FILE)), exist_ok=True)
    temp_file = f"{TELEGRAM_OFFSET_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump({"offset": int(offset)}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_file, TELEGRAM_OFFSET_FILE)


def aggregate_status(fechas: dict) -> str:
    """Combina estados sin convertir errores o ausencia de datos en agotado."""
    states = [state for state in fechas.values() if state in KNOWN_STATUSES]
    if not states:
        return STATUS_UNKNOWN
    if STATUS_AVAILABLE in states:
        return STATUS_AVAILABLE
    if STATUS_CANDIDATE in states:
        return STATUS_CANDIDATE
    if STATUS_BLOCKED in states:
        return STATUS_BLOCKED
    if STATUS_ERROR in states:
        return STATUS_ERROR
    if STATUS_UNKNOWN in states:
        return STATUS_UNKNOWN
    if all(state == STATUS_SOLD_OUT for state in states):
        return STATUS_SOLD_OUT
    return STATUS_UNKNOWN


def page_block_reason(page) -> str:
    """Devuelve la marca de bloqueo visible, si existe."""
    try:
        body = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        return ""
    return next((marker for marker in BLOCKED_MARKERS if marker in body), "")

# ─────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────

def send_telegram(text: str, parse_mode="HTML") -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }
    for attempt in range(1, TELEGRAM_RETRIES + 1):
        try:
            r = requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
            response = r.json()
            if not response.get("ok"):
                raise RuntimeError(f"Telegram respondió sin ok: {response}")
            return True
        except Exception as exc:
            log.error(
                "Error enviando Telegram (intento %s/%s): %s",
                attempt,
                TELEGRAM_RETRIES,
                exc,
            )
            if attempt < TELEGRAM_RETRIES:
                time.sleep(min(2 ** (attempt - 1), 8))
    return False


def queue_alert(data: dict, key: str, text: str):
    pending = data.setdefault("pending_alerts", [])
    if any(item.get("key") == key for item in pending):
        return
    pending.append(
        {
            "key": key,
            "text": text,
            "created": datetime.now(timezone.utc).isoformat(),
            "attempts": 0,
        }
    )


def flush_pending_alerts(urls: dict) -> bool:
    """Entrega la outbox. Una alerta solo desaparece tras confirmación de Telegram."""
    changed = False
    for data in urls.values():
        pending = data.get("pending_alerts", [])
        remaining = []
        for alert in pending:
            alert["attempts"] = int(alert.get("attempts", 0)) + 1
            if send_telegram(alert["text"]):
                log.info("Alerta confirmada por Telegram: %s", alert.get("key"))
                changed = True
            else:
                remaining.append(alert)
                changed = True
        data["pending_alerts"] = remaining
    if changed:
        save_urls(urls)
    return changed

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
        elif fn_name == "movistar_profundo":
            result = _check_movistar_profundo(url)
        elif fn_name == "movistar_simple":
            result = _check_movistar_simple(url)
        else:
            result = {
                "status": STATUS_ERROR,
                "snippet": f"Checker desconocido: {fn_name}",
                "fechas": {"General": STATUS_ERROR},
            }
        result_queue.put(result)
    except Exception as e:
        result_queue.put(
            {
                "status": STATUS_ERROR,
                "snippet": str(e),
                "fechas": {"General": STATUS_ERROR},
            }
        )


def run_with_timeout(fn_name: str, url: str) -> dict:
    env = dict(os.environ)
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_worker,
        args=(fn_name, url, result_queue, env),
        daemon=True
    )
    try:
        process.start()
        process.join(timeout=PLAYWRIGHT_TIMEOUT)

        if process.is_alive():
            log.error(
                "[TIMEOUT] Proceso cancelado después de %ss — %s",
                PLAYWRIGHT_TIMEOUT,
                url,
            )
            process.kill()
            process.join()
            return {
                "status": STATUS_ERROR,
                "snippet": (
                    "Timeout: el chequeo tardó más de "
                    f"{PLAYWRIGHT_TIMEOUT} segundos y fue cancelado."
                ),
                "fechas": {"General": STATUS_ERROR},
            }

        try:
            return result_queue.get(timeout=2)
        except Empty:
            return {
                "status": STATUS_ERROR,
                "snippet": (
                    "El proceso de chequeo terminó inesperadamente sin resultado."
                ),
                "fechas": {"General": STATUS_ERROR},
            }
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        result_queue.close()
        result_queue.join_thread()
        try:
            process.close()
        except ValueError:
            pass

# ─────────────────────────────────────────────
# Browser helper
# ─────────────────────────────────────────────

@contextmanager
def _browser_page(label: str):
    """Abre una página de Chromium y garantiza el cierre del browser."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            yield browser.new_page()
        finally:
            browser.close()
            logging.info("[%s] Browser cerrado", label)

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
    # page.fill() setea el valor de una sola vez, distinto de cómo lo hace un
    # humano tipeando. Si el formulario usa binding con debounce (frecuente
    # en Blazor), un click inmediato puede llegar antes de que el framework
    # haya registrado el valor recién puesto.
    page.wait_for_timeout(500)

    logging.info("[Movistar] Paso 3: Haciendo click en login...")
    page.click("button.btn-login")

    logging.info("[Movistar] Paso 4: Esperando redirección...")
    page.wait_for_url("https://www.movistararena.com.ar/**", timeout=15000)

    # La redirección sola no prueba que el login haya funcionado -- un login
    # fallido (credenciales vencidas, etc.) también puede terminar en una URL
    # que matchea ese patrón tan amplio. _wait_for_movistar_session confirma
    # de verdad (y reintenta el segundo click en "Iniciar sesión" si hace
    # falta, algo confirmado a mano: a veces el sitio lo pide).
    if not _wait_for_movistar_session(page):
        _log_post_click_state(page, "login fallido")
        raise Exception(
            "El login no se confirmó ni con el segundo click en 'Iniciar "
            "sesión' (¿credenciales vencidas o inválidas?)."
        )
    logging.info("[Movistar] Login exitoso")

def _get_mes_texto(page) -> str:
    try:
        mes_header = page.query_selector(".mud-picker-calendar-header-transition")
        return mes_header.inner_text().strip() if mes_header else ""
    except Exception:
        return ""

def _es_boton_vip(tb) -> bool:
    try:
        parent_text = tb.evaluate(
            "el => { let p = el.closest('div'); return p ? p.innerText : ''; }"
        ).lower()
        return any(kw in parent_text for kw in VIP_KEYWORDS)
    except Exception:
        return False

def _volver_al_evento(page, url: str):
    logging.info("[Movistar-Profundo] Volviendo al evento por navegación directa...")
    page.goto(url, timeout=30000)
    page.wait_for_load_state("networkidle", timeout=20000)
    page.wait_for_timeout(1500)
    logging.info("[Movistar-Profundo] Evento recargado")


def _contar_sectores_disponibles(page) -> int:
    """Cuenta sectores habilitados en la página o en cualquiera de sus iframes."""
    selector = MOVISTAR_SECTOR_SELECTOR

    for intento in range(3):
        diagnosticos = []
        for indice, frame in enumerate(page.frames):
            try:
                cant = len(frame.query_selector_all(selector))
            except Exception as exc:
                diagnosticos.append(
                    {"frame": indice, "error": f"selector de sectores: {exc}"}
                )
                continue
            if cant > 0:
                logging.info(
                    "[Movistar-Profundo] Detectados %s sectores por clase en frame %s.",
                    cant,
                    indice,
                )
                return cant

            try:
                diagnostico_svg = frame.locator("svg polygon").evaluate_all(
                    """poligonos => {
                        const colores = {};
                        for (const poligono of poligonos) {
                            const color = getComputedStyle(poligono).fill;
                            colores[color] = (colores[color] || 0) + 1;
                        }
                        return { total: poligonos.length, colores };
                    }"""
                )
            except Exception as exc:
                diagnosticos.append(
                    {"frame": indice, "error": f"diagnóstico SVG: {exc}"}
                )
                continue
            cant_verdes = diagnostico_svg["colores"].get(MOVISTAR_AVAILABLE_FILL, 0)
            if cant_verdes > 0:
                logging.info(
                    "[Movistar-Profundo] Detectados %s polígonos disponibles por color en frame %s.",
                    cant_verdes,
                    indice,
                )
                return cant_verdes

            diagnosticos.append(
                {
                    "frame": indice,
                    "url": frame.url[:120],
                    "poligonos": diagnostico_svg["total"],
                    "colores": diagnostico_svg["colores"],
                }
            )

        if intento == 0:
            logging.info("[Movistar-Profundo] Diagnóstico SVG por frame: %s", diagnosticos)

        if intento < 2:
            logging.info(
                "[Movistar-Profundo] Mapa sin sectores habilitados; "
                "esperando actualización (%s/2)...",
                intento + 1,
            )
            page.wait_for_timeout(3000)

    return 0


def _enabled_locator_count(locator, limit=500) -> int:
    """Cuenta elementos visibles y habilitados; evita plantillas ocultas."""
    total = min(locator.count(), limit)
    enabled = 0
    for index in range(total):
        item = locator.nth(index)
        try:
            if item.is_visible() and item.is_enabled():
                enabled += 1
        except Exception:
            continue
    return enabled


def _contar_inventario_real(page) -> tuple[int, list[str]]:
    """
    Busca evidencia fuerte de inventario seleccionable.

    Un sector habilitado no cuenta como asiento. Solo se confirman elementos
    explícitamente marcados como asientos disponibles o controles de cantidad
    activos dentro de un bloque de compra.
    """
    total = 0
    evidence = []

    for frame_index, frame in enumerate(page.frames):
        for selector in MOVISTAR_SEAT_SELECTORS:
            try:
                count = _enabled_locator_count(frame.locator(selector))
            except Exception:
                count = 0
            if count:
                total += count
                evidence.append(f"frame {frame_index}: {count} por {selector}")

        try:
            quantity_buttons = frame.locator(MOVISTAR_QUANTITY_BUTTON_SELECTOR)
            count = _enabled_locator_count(quantity_buttons, limit=100)
        except Exception:
            count = 0
        if count:
            total += count
            evidence.append(
                f"frame {frame_index}: {count} botón(es) de cantidad habilitado(s)"
            )

        try:
            quantity_controls = frame.locator(
                "select:not([disabled]), input[type='number']:not([disabled])"
            )
            for index in range(min(quantity_controls.count(), 100)):
                control = quantity_controls.nth(index)
                if not control.is_visible() or not control.is_enabled():
                    continue
                info = control.evaluate(
                    """el => {
                        const parent = el.closest('div, section, article, form') || el.parentElement;
                        const text = (parent?.innerText || '').toLowerCase();
                        const options = el.tagName === 'SELECT'
                            ? Array.from(el.options).map(o => ({value: o.value, disabled: o.disabled}))
                            : [];
                        return {
                            text,
                            max: el.getAttribute('max'),
                            options,
                        };
                    }"""
                )
                context_text = info.get("text", "")
                purchase_context = any(
                    word in context_text
                    for word in ("entrada", "ticket", "cantidad", "quantity", "asiento")
                )
                if not purchase_context:
                    continue

                has_quantity = False
                if info.get("options"):
                    has_quantity = any(
                        not option.get("disabled")
                        and str(option.get("value", "")).strip().isdigit()
                        and int(option["value"]) > 0
                        for option in info["options"]
                    )
                else:
                    max_value = str(info.get("max") or "").strip()
                    has_quantity = max_value.isdigit() and int(max_value) > 0

                if has_quantity:
                    total += 1
                    evidence.append(
                        f"frame {frame_index}: control de cantidad comprable"
                    )
        except Exception as exc:
            logging.debug("No se pudieron inspeccionar cantidades: %s", exc)

    return total, evidence


def _available_sector_targets(page) -> list:
    """Devuelve locators de sectores candidatos, incluyendo el respaldo por color."""
    targets = []
    seen = set()
    for frame in page.frames:
        try:
            primary = frame.locator(MOVISTAR_SECTOR_SELECTOR)
            for index in range(min(primary.count(), 200)):
                item = primary.nth(index)
                key = (frame.url, "class", index)
                targets.append((item, key))
                seen.add(key)
        except Exception:
            pass

        try:
            polygons = frame.locator("svg polygon")
            for index in range(min(polygons.count(), 500)):
                item = polygons.nth(index)
                color = item.evaluate("el => getComputedStyle(el).fill")
                key = (frame.url, "polygon", index)
                if color == MOVISTAR_AVAILABLE_FILL and key not in seen:
                    targets.append((item, key))
        except Exception:
            pass
    return targets


def _inspect_movistar_map(page, reopen_map) -> dict:
    """
    Distingue sector candidato de inventario confirmado.

    Para evitar que un falso positivo quede grabado como disponible, abre cada
    sector candidato (hasta el límite configurado) y vuelve a buscar evidencia
    fuerte de asiento/cantidad. Si no puede confirmarla, conserva candidate.
    """
    blocked = page_block_reason(page)
    if blocked:
        return {
            "status": STATUS_BLOCKED,
            "candidate_count": 0,
            "seat_count": 0,
            "evidence": [blocked],
        }

    direct_count, direct_evidence = _contar_inventario_real(page)
    if direct_count > 0:
        return {
            "status": STATUS_AVAILABLE,
            "candidate_count": _contar_sectores_disponibles(page),
            "seat_count": direct_count,
            "evidence": direct_evidence,
        }

    candidate_count = _contar_sectores_disponibles(page)
    if candidate_count <= 0:
        # Para llegar hasta acá ya se hizo click en un botón de compra que no
        # decía "agotado" en su fila. Si ahora no aparece ningún sector, no
        # hay que volver a buscar "agotado" en el texto de toda la página:
        # eso puede matchear con OTRA fecha del mismo listado (falso agotado)
        # o con un mapa que todavía no terminó de cargar. Sin sectores ni
        # evidencia de haber llegado al mapa real, el estado es incierto.
        return {
            "status": STATUS_UNKNOWN,
            "candidate_count": 0,
            "seat_count": 0,
            "evidence": ["sin sectores habilitados tras entrar; no se confirma agotado"],
        }

    sectors_to_check = min(candidate_count, MAX_SECTORS_TO_VERIFY)
    diagnostics = []
    current_page = page

    for sector_index in range(sectors_to_check):
        owns_page = sector_index > 0
        if owns_page:
            try:
                current_page = reopen_map()
            except Exception as exc:
                diagnostics.append(f"no se reabrió sector {sector_index}: {exc}")
                continue

        try:
            targets = _available_sector_targets(current_page)
            if sector_index >= len(targets):
                diagnostics.append(
                    f"sector {sector_index}: locator ausente ({len(targets)} detectados)"
                )
                continue
            target, _ = targets[sector_index]
            target.scroll_into_view_if_needed(timeout=3000)
            target.click(force=True, timeout=5000)
            current_page.wait_for_timeout(1500)

            blocked = page_block_reason(current_page)
            if blocked:
                diagnostics.append(f"sector {sector_index}: bloqueo {blocked}")
                continue

            seat_count, evidence = _contar_inventario_real(current_page)
            diagnostics.extend(
                f"sector {sector_index}: {item}" for item in evidence
            )
            if seat_count > 0:
                return {
                    "status": STATUS_AVAILABLE,
                    "candidate_count": candidate_count,
                    "seat_count": seat_count,
                    "evidence": diagnostics,
                }
        except Exception as exc:
            diagnostics.append(f"sector {sector_index}: {exc}")
        finally:
            if owns_page:
                try:
                    current_page.close()
                except Exception:
                    pass

    return {
        "status": STATUS_CANDIDATE,
        "candidate_count": candidate_count,
        "seat_count": 0,
        "evidence": diagnostics[:20],
    }

# ─────────────────────────────────────────────
# Checker profundo Movistar Arena (todas las URLs)
# ─────────────────────────────────────────────

def _process_movistar_labels(
    page,
    url: str,
    labels: list,
    enter_fn,
    fechas_estado: dict,
    sector_counts: dict,
    seat_counts: dict,
    evidence_by_date: dict,
    purchase_signals: dict,
):
    """Recorre cada función/fecha, entra a su mapa y confirma inventario.

    Común a los tres formatos de listado de Movistar (calendario, listado
    de shows y evento-row); solo cambia ``enter_fn``, que comparte la firma
    ``(page, url, index)`` en los tres casos.
    """
    for index, label in enumerate(labels):
        try:
            enter_status = enter_fn(page, url, index)
            purchase_signals[label] = enter_status in (
                "entered",
                MOVISTAR_PURCHASE_SIGNAL_FAILED,
            )
            if enter_status != "entered":
                fechas_estado[label] = (
                    STATUS_UNKNOWN
                    if enter_status == MOVISTAR_PURCHASE_SIGNAL_FAILED
                    else enter_status
                )
                sector_counts[label] = 0
                seat_counts[label] = 0
                if enter_status == MOVISTAR_PURCHASE_SIGNAL_FAILED:
                    evidence_by_date[label] = [
                        "Comprar/Seleccionar visible; el mapa no abrió"
                    ]
                continue

            inspect = _inspect_movistar_map(
                page,
                lambda idx=index: _new_map_page(
                    page.context,
                    lambda extra: enter_fn(extra, url, idx),
                ),
            )
            fechas_estado[label] = inspect["status"]
            sector_counts[label] = inspect["candidate_count"]
            seat_counts[label] = inspect["seat_count"]
            evidence_by_date[label] = inspect["evidence"]
            logging.info(
                "[Movistar-Profundo] %s: %s | sectores=%s | "
                "inventario=%s | evidencia=%s",
                label,
                inspect["status"],
                inspect["candidate_count"],
                inspect["seat_count"],
                inspect["evidence"][:5],
            )
        except Exception as exc:
            logging.exception(
                "[Movistar-Profundo] Error verificando %s", label
            )
            purchase_signals.setdefault(label, False)
            fechas_estado[label] = STATUS_UNKNOWN
            evidence_by_date[label] = [str(exc)]


def _find_movistar_purchase_button(root):
    buttons = root.query_selector_all("span.mud-button-label")
    for button in buttons:
        text = button.inner_text().strip().lower()
        if "seleccionar" not in text and "comprar" not in text:
            continue
        if _es_boton_vip(button):
            continue
        try:
            disabled = button.evaluate(
                """el => {
                    const btn = el.closest('button');
                    return !btn || btn.disabled || btn.getAttribute('aria-disabled') === 'true'
                        || btn.classList.contains('btn-disabled');
                }"""
            )
            if disabled:
                continue
        except Exception:
            continue
        return button
    return None


def _has_explicit_sold_out(root) -> bool:
    try:
        text = root.inner_text().lower()
    except Exception:
        try:
            text = root.locator("body").inner_text(timeout=3000).lower()
        except Exception:
            return False
    return any(
        marker in text
        for marker in ("agotado", "sin entradas", "no hay entradas", "sold out")
    )


def _wait_after_navigation(page):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    page.wait_for_timeout(1500)


def _wait_for_index(page, selector: str, index: int, attempts: int = 12, wait_ms: int = 1000) -> list:
    """Reintenta hasta que ``selector`` tenga al menos ``index + 1`` elementos.

    En páginas que renderizan filas de forma asincrónica (Blazor), que la red
    esté quieta no garantiza que el DOM ya terminó de pintar todas las filas.
    Logueado, el render de cada fila puede tardar bastante más que sin login
    (probablemente trae datos de precio/disponibilidad por fila); sin este
    reintento con margen amplio, reabrir el evento para la fecha N (N>0)
    encuentra menos filas de las esperadas y tira un IndexError.
    """
    items = page.query_selector_all(selector)
    for _ in range(attempts):
        if index < len(items):
            break
        page.wait_for_timeout(wait_ms)
        items = page.query_selector_all(selector)
    return items


def _log_post_click_state(page, label: str):
    """Diagnóstico: qué quedó en pantalla justo después de clickear comprar.

    Sin esto, cuando el mapa no aparece no hay forma de saber si el click
    navegó a otra URL, abrió un modal, o no cambió nada -- hay que
    reproducirlo a mano para verlo. Queda en INFO porque es liviano y solo
    importa cuando después no se detecta ningún sector.
    """
    try:
        snippet = page.locator("body").inner_text(timeout=3000)[:400]
    except Exception as exc:
        snippet = f"<no se pudo leer body: {exc}>"
    try:
        title = page.title()
    except Exception:
        title = "<sin título>"
    logging.info(
        "[Movistar-Profundo] %s post-click → url=%s title=%s texto=%r",
        label,
        page.url,
        title,
        snippet,
    )


def _wait_for_movistar_session(page, attempts: int = 8, wait_ms: int = 1000) -> bool:
    """Espera a que la página confirme la sesión logueada tras recargar.

    El shell inicial de esta app (antes de que termine de verificar la
    sesión contra el server) siempre arranca mostrando el menú de "Iniciar
    sesión" de un visitante anónimo. Cada vez que el bot vuelve a cargar la
    página del evento (una vez por fecha, o en el login inicial), corre el
    riesgo de leerla en ese estado transitorio y confundirlo con una sesión
    realmente perdida. Devuelve False si después de esperar sigue sin
    confirmarse.
    """
    def _confirmada() -> bool:
        try:
            body_text = page.locator("body").inner_text(timeout=3000).lower()
        except Exception:
            return False
        return "iniciá sesión o creá tu cuenta" not in body_text

    intento_click_hecho = False
    for _ in range(attempts):
        if _confirmada():
            return True
        if not intento_click_hecho:
            # Confirmado a mano: a veces el sitio necesita un segundo click
            # en "Iniciar sesión" del header para terminar de reconocer una
            # sesión que en realidad ya está autenticada.
            intento_click_hecho = True
            try:
                page.click('text="Iniciar sesión"', timeout=3000)
                page.wait_for_timeout(1000)
                continue
            except Exception:
                pass
        page.wait_for_timeout(wait_ms)
    return _confirmada()


def _enter_calendar_map(page, url: str, date_index: int) -> str:
    page.goto(url, timeout=30000)
    _wait_after_navigation(page)
    if page_block_reason(page):
        return STATUS_BLOCKED
    if not _wait_for_movistar_session(page):
        logging.warning(
            "[Movistar-Profundo] La sesión no se confirmó tras recargar "
            "(sigue mostrando 'Iniciar sesión')."
        )
    page.wait_for_selector("button.dia-evento", timeout=15000)
    dates = _wait_for_index(page, "button.dia-evento", date_index)
    if date_index >= len(dates):
        raise IndexError(f"Fecha {date_index} fuera de rango")
    dates[date_index].click()
    page.wait_for_timeout(1000)
    button = _find_movistar_purchase_button(page)
    if not button:
        return STATUS_SOLD_OUT if _has_explicit_sold_out(page) else STATUS_UNKNOWN
    try:
        button.click()
        _wait_after_navigation(page)
    except Exception as exc:
        logging.warning(
            "[Movistar-Profundo] Apareció Comprar/Seleccionar, "
            "pero no se abrió el mapa: %s",
            exc,
        )
        return MOVISTAR_PURCHASE_SIGNAL_FAILED
    return "entered"


def _enter_row_map(page, url: str, row_index: int, selector: str) -> str:
    page.goto(url, timeout=30000)
    _wait_after_navigation(page)
    if page_block_reason(page):
        return STATUS_BLOCKED
    if not _wait_for_movistar_session(page):
        logging.warning(
            "[Movistar-Profundo] La sesión no se confirmó tras recargar "
            "(sigue mostrando 'Iniciar sesión')."
        )
    rows = _wait_for_index(page, selector, row_index)
    if row_index >= len(rows):
        raise IndexError(f"Fila {row_index} fuera de rango")
    button = _find_movistar_purchase_button(rows[row_index])
    if not button:
        return (
            STATUS_SOLD_OUT
            if _has_explicit_sold_out(rows[row_index])
            else STATUS_UNKNOWN
        )
    try:
        button.click()
        _wait_after_navigation(page)
    except Exception as exc:
        logging.warning(
            "[Movistar-Profundo] Apareció Comprar/Seleccionar, "
            "pero no se abrió el mapa: %s",
            exc,
        )
        return MOVISTAR_PURCHASE_SIGNAL_FAILED
    return "entered"


def _enter_list_map(page, url: str, row_index: int) -> str:
    return _enter_row_map(
        page,
        url,
        row_index,
        "div.shows-listado div.show",
    )


def _enter_event_row_map(page, url: str, row_index: int) -> str:
    """Abre una función del formato público actual ``div.evento-row``."""
    return _enter_row_map(page, url, row_index, "div.evento-row")


def _movistar_row_label(row, index: int) -> str:
    try:
        day_el = row.query_selector("div.fecha p")
        month_el = row.query_selector("div.fecha span")
        day = day_el.inner_text().strip() if day_el else ""
        month = month_el.inner_text().strip() if month_el else ""
        return f"{day} de {month}".strip() or f"Fecha {index + 1}"
    except Exception:
        return f"Fecha {index + 1}"


def _new_map_page(context, enter_map):
    extra_page = context.new_page()
    try:
        result = enter_map(extra_page)
        if result != "entered":
            raise RuntimeError(f"No se pudo volver a abrir el mapa: {result}")
        return extra_page
    except Exception:
        extra_page.close()
        raise


def _check_movistar_profundo(url: str) -> dict:
    logging.info("[Movistar-Profundo] Iniciando chequeo profundo: %s", url)
    fechas_estado = {}
    sector_counts = {}
    seat_counts = {}
    evidence_by_date = {}
    purchase_signals = {}

    with _browser_page("Movistar-Profundo") as page:
        _login_movistar(page)
        page.goto(url, timeout=30000)
        _wait_after_navigation(page)

        blocked = page_block_reason(page)
        if blocked:
            return {
                "status": STATUS_BLOCKED,
                "snippet": blocked,
                "fechas": {"General": STATUS_BLOCKED},
                "sector_counts": {},
                "seat_counts": {},
                "purchase_signals": {},
            }

        calendar_dates = page.query_selector_all("button.dia-evento")
        rows = page.query_selector_all("div.shows-listado div.show")
        event_rows = page.query_selector_all("div.evento-row")

        if calendar_dates:
            month = _get_mes_texto(page)
            labels = []
            for index, date_button in enumerate(calendar_dates):
                try:
                    day = date_button.query_selector("p")
                    day_text = day.inner_text().strip() if day else str(index + 1)
                except Exception:
                    day_text = str(index + 1)
                labels.append(f"{day_text} de {month}".strip())

            _process_movistar_labels(
                page, url, labels, _enter_calendar_map,
                fechas_estado, sector_counts, seat_counts,
                evidence_by_date, purchase_signals,
            )

        elif rows:
            labels = [
                _movistar_row_label(row, index)
                for index, row in enumerate(rows)
            ]
            _process_movistar_labels(
                page, url, labels, _enter_list_map,
                fechas_estado, sector_counts, seat_counts,
                evidence_by_date, purchase_signals,
            )
        elif event_rows:
            labels = [
                _movistar_row_label(row, index)
                for index, row in enumerate(event_rows)
            ]
            _process_movistar_labels(
                page, url, labels, _enter_event_row_map,
                fechas_estado, sector_counts, seat_counts,
                evidence_by_date, purchase_signals,
            )
        else:
            fechas_estado["General"] = STATUS_UNKNOWN
            evidence_by_date["General"] = [
                "No apareció calendario ni listado de funciones conocido"
            ]
            purchase_signals["General"] = False

    status = aggregate_status(fechas_estado)
    snippets = {
        STATUS_AVAILABLE: "asientos o cantidad seleccionable confirmados",
        STATUS_CANDIDATE: "sectores habilitados; asiento aún no confirmado",
        STATUS_SOLD_OUT: "mapa verificado sin inventario",
        STATUS_UNKNOWN: "no se pudo determinar con seguridad",
        STATUS_BLOCKED: "el sitio bloqueó o desafió el chequeo",
    }
    return {
        "status": status,
        "snippet": snippets.get(status, status),
        "fechas": fechas_estado,
        "sector_counts": sector_counts,
        "seat_counts": seat_counts,
        "evidence": evidence_by_date,
        "purchase_signals": purchase_signals,
    }


# ─────────────────────────────────────────────
# Checker AllAccess estándar
# ─────────────────────────────────────────────

def _visible_element(page, selector: str):
    """Devuelve el primer elemento visible para un selector, si existe."""
    try:
        element = page.query_selector(selector)
        if element and element.is_visible():
            return element
    except Exception as exc:
        logging.debug(
            "[AllAccess] No se pudo inspeccionar el selector %s: %s",
            selector,
            exc,
        )
    return None


def _allaccess_global_status(page) -> str | None:
    """
    Lee estados explícitos publicados en la cabecera del evento.

    Los eventos agotados de una sola función no muestran ``#show-dropdown``.
    AllAccess publica en cambio ``event-status status-soldout``; si no se
    consulta antes del desplegable, un agotado confirmado termina como
    ``unknown`` y se pierde la línea base necesaria para detectar una futura
    liberación.
    """
    sold_out_selectors = (
        "div.event-status.status-soldout",
        ".event-status.soldout",
        "[data-status='soldout']",
        "[data-status='sold-out']",
    )
    for selector in sold_out_selectors:
        if _visible_element(page, selector):
            logging.info(
                "[AllAccess] Agotado global confirmado por selector: %s",
                selector,
            )
            return STATUS_SOLD_OUT
    return None


def _allaccess_show_item_status(item, signal: str) -> str:
    """Clasifica una función del desplegable sin depender de su texto."""
    if any(word in signal for word in ("agotado", "sold out", "disabled")):
        return STATUS_SOLD_OUT

    # En el flujo actual, una función comprable puede mostrar únicamente la
    # fecha. La señal estable es el enlace ``a.show`` con el id de la función.
    try:
        if item.query_selector("a.show"):
            return STATUS_CANDIDATE
    except Exception as exc:
        logging.debug("[AllAccess] No se pudo inspeccionar a.show: %s", exc)

    if any(
        word in signal
        for word in ("comprar", "seleccionar", "disponible", "available")
    ):
        return STATUS_CANDIDATE
    return STATUS_UNKNOWN


def _check_allaccess(url: str) -> dict:
    logging.info(f"[AllAccess] Iniciando chequeo: {url}")
    fechas_estado = {}
    with _browser_page("AllAccess") as page:
        logging.info("[AllAccess] Navegando a la página...")
        page.goto(url, timeout=30000)
        _wait_after_navigation(page)
        logging.info("[AllAccess] Página cargada")

        blocked = page_block_reason(page)
        if blocked:
            return {
                "status": STATUS_BLOCKED,
                "snippet": blocked,
                "fechas": {"General": STATUS_BLOCKED},
            }

        global_status = _allaccess_global_status(page)
        if global_status:
            return {
                "status": global_status,
                "snippet": "agotado global confirmado",
                "fechas": {"General": global_status},
            }

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
                signal = f"{clase} {texto}".lower()
                # El listado es una señal rápida, no prueba de inventario final.
                fechas_estado[fecha_label] = _allaccess_show_item_status(
                    item,
                    signal,
                )
                logging.info(f"[AllAccess] {fecha_label}: {fechas_estado[fecha_label]}")
            except Exception as ex:
                logging.warning(f"[AllAccess] Error leyendo item: {ex}")
                continue

        # Respaldo para páginas de una sola función que publican el botón
        # directo sin construir el desplegable.
        if not fechas_estado and _visible_element(page, "#buyButton"):
            fechas_estado["General"] = STATUS_CANDIDATE
            logging.info(
                "[AllAccess] Botón Ver entradas habilitado sin dropdown"
            )

    if not fechas_estado:
        fechas_estado["General"] = STATUS_UNKNOWN
    status = aggregate_status(fechas_estado)
    return {
        "status": status,
        "snippet": (
            "señal de compra detectada; falta confirmar inventario"
            if status == STATUS_CANDIDATE
            else status
        ),
        "fechas": fechas_estado,
    }


# ─────────────────────────────────────────────
# Checker BTS
# ─────────────────────────────────────────────

def _check_bts_fecha(page, fecha_url: str) -> str:
    fecha_label = fecha_url.split("/event/bts-")[-1]
    logging.info(f"[BTS] Chequeando: {fecha_label}")

    page.goto(fecha_url, timeout=30000)
    _wait_after_navigation(page)

    if page_block_reason(page):
        return STATUS_BLOCKED

    sold_out_global = page.query_selector("div.event-status.status-soldout")
    if sold_out_global:
        logging.info(f"[BTS] {fecha_label}: agotado global")
        return STATUS_SOLD_OUT

    try:
        page.wait_for_selector("div.selection-container", timeout=30000)  # aumentado de 10000 a 30000
        logging.info(f"[BTS] {fecha_label}: panel de tarifas cargado")
    except Exception:
        logging.info(f"[BTS] {fecha_label}: sin panel de tarifas → unknown")
        return STATUS_UNKNOWN

    try:
        contenido = page.inner_text("div.selection-container").lower()
        logging.info(f"[BTS] {fecha_label}: contenido panel: {contenido[:300]}")
    except Exception:
        logging.info(f"[BTS] {fecha_label}: no se pudo leer el panel → unknown")
        return STATUS_UNKNOWN

    if "campo" not in contenido:
        # "Campo" puede no aparecer por agotado real o porque el sitio cambió
        # de texto/estructura; solo se confirma agotado con marca explícita.
        if "agotado" in contenido or "sold out" in contenido:
            logging.info(f"[BTS] {fecha_label}: Campo no aparece, agotado confirmado en el panel")
            return STATUS_SOLD_OUT
        logging.info(f"[BTS] {fecha_label}: Campo no aparece y sin marca explícita → unknown")
        return STATUS_UNKNOWN

    lineas = contenido.split("\n")
    for idx, linea in enumerate(lineas):
        if "campo" in linea:
            contexto = " ".join(lineas[max(0, idx-2):idx+3])
            if "agotado" in contexto:
                logging.info(f"[BTS] {fecha_label}: Campo con AGOTADO → sold_out")
                return STATUS_SOLD_OUT
            else:
                logging.info(f"[BTS] {fecha_label}: Campo sin AGOTADO → candidate")
                return STATUS_CANDIDATE

    logging.info(f"[BTS] {fecha_label}: Campo encontrado pero no determinado → unknown")
    return STATUS_UNKNOWN


def _check_bts(url: str) -> dict:
    normalized = normalize_url(url)
    urls_to_check = BTS_FECHAS if normalized == normalize_url(BTS_URL) else [normalized]
    logging.info("[BTS] Iniciando chequeo de %s fecha(s)", len(urls_to_check))
    fechas_estado = {}

    with _browser_page("BTS") as page:
        for fecha_url in urls_to_check:
            fecha_label = fecha_url.split("/event/bts-")[-1].replace("-", " ").title()
            try:
                estado = _check_bts_fecha(page, fecha_url)
                fechas_estado[fecha_label] = estado
                logging.info(f"[BTS] {fecha_label}: {estado}")
            except Exception as ex:
                logging.warning(f"[BTS] Error en {fecha_label}: {ex}")
                fechas_estado[fecha_label] = STATUS_UNKNOWN

    status = aggregate_status(fechas_estado)
    return {
        "status": status,
        "snippet": (
            "Campo aparece sin marca de agotado; revisar compra"
            if status == STATUS_CANDIDATE
            else status
        ),
        "fechas": fechas_estado,
    }


# ─────────────────────────────────────────────
# Checker Enigma Tickets
# ─────────────────────────────────────────────

def _check_enigmatickets(url: str) -> dict:
    logging.info(f"[Enigma] Iniciando chequeo: {url}")
    fechas_estado = {}
    with _browser_page("Enigma") as page:
        logging.info("[Enigma] Navegando a la página...")
        page.goto(url, timeout=30000)
        _wait_after_navigation(page)
        logging.info("[Enigma] Página cargada")

        blocked = page_block_reason(page)
        if blocked:
            return {
                "status": STATUS_BLOCKED,
                "snippet": blocked,
                "fechas": {"General": STATUS_BLOCKED},
            }

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
                    fechas_estado[nombre] = STATUS_SOLD_OUT
                elif any(kw in estado_texto for kw in ["comprar", "disponible", "compra", "buy"]):
                    fechas_estado[nombre] = STATUS_CANDIDATE
                else:
                    fechas_estado[nombre] = STATUS_UNKNOWN

            except Exception as ex:
                logging.warning(f"[Enigma] Error leyendo fila: {ex}")
                continue

        if not fechas_estado:
            logging.info("[Enigma] Sin filas, usando fallback con spans")
            todos_los_spans = page.query_selector_all("span[data-testid='text-component']")
            for span in todos_los_spans:
                texto = span.inner_text().strip().lower()
                if "agotado" in texto or "sold out" in texto:
                    fechas_estado["General"] = STATUS_SOLD_OUT
                    break
                elif any(kw in texto for kw in ["comprar", "disponible", "buy"]):
                    fechas_estado["General"] = STATUS_CANDIDATE
                    break

    if not fechas_estado:
        fechas_estado["General"] = STATUS_UNKNOWN
    status = aggregate_status(fechas_estado)
    return {
        "status": status,
        "snippet": (
            "señal de compra detectada; falta confirmar inventario"
            if status == STATUS_CANDIDATE
            else status
        ),
        "fechas": fechas_estado,
    }


# ─────────────────────────────────────────────
# Checker simple Movistar Arena (por defecto)
# ─────────────────────────────────────────────

def _clasificar_fila_simple(root, label: str, fechas_estado: dict, purchase_signals: dict):
    """Clasifica una fila/fecha sin clickear nada, solo leyendo su botón."""
    try:
        button = _find_movistar_purchase_button(root)
    except Exception:
        button = None
    if button:
        fechas_estado[label] = STATUS_CANDIDATE
        purchase_signals[label] = True
        return
    fechas_estado[label] = (
        STATUS_SOLD_OUT if _has_explicit_sold_out(root) else STATUS_UNKNOWN
    )
    purchase_signals[label] = False


def _check_movistar_simple(url: str) -> dict:
    """
    Chequeo liviano: se loguea (algunas URLs, como /Ticketera/..., redirigen
    directo al login y no muestran nada sin sesión) pero NO abre el mapa de
    asientos. Solo mira si el botón de cada fecha dice "Comprar"/"Seleccionar"
    (señal de compra) o "Agotado" (explícito).

    No confirma un asiento real -- eso es lo que hacía fallar al chequeo
    profundo por depender de repetidas recargas logueadas para abrir el
    mapa. Acá solo se recarga una vez por fecha en el peor caso (formato
    calendario), no una vez por fecha Y otra vez para abrir su mapa. Para
    eventos puntuales que necesiten la confirmación de asiento real, está
    el modo profundo (opt-in con /profundo).
    """
    logging.info("[Movistar-Simple] Iniciando chequeo: %s", url)
    fechas_estado = {}
    purchase_signals = {}

    with _browser_page("Movistar-Simple") as page:
        _login_movistar(page)
        page.goto(url, timeout=30000)
        _wait_after_navigation(page)

        blocked = page_block_reason(page)
        if blocked:
            return {
                "status": STATUS_BLOCKED,
                "snippet": blocked,
                "fechas": {"General": STATUS_BLOCKED},
                "purchase_signals": {},
            }

        calendar_dates = page.query_selector_all("button.dia-evento")
        rows = page.query_selector_all("div.shows-listado div.show")
        event_rows = page.query_selector_all("div.evento-row")

        if rows:
            for index, row in enumerate(rows):
                label = _movistar_row_label(row, index)
                _clasificar_fila_simple(row, label, fechas_estado, purchase_signals)
        elif event_rows:
            for index, row in enumerate(event_rows):
                label = _movistar_row_label(row, index)
                _clasificar_fila_simple(row, label, fechas_estado, purchase_signals)
        elif calendar_dates:
            # Este formato no muestra el estado en el día del calendario --
            # hay que clickearlo para que aparezca el botón de compra. No
            # necesita login, pero sí volver a cargar la página por cada
            # fecha (el click reemplaza el contenido), igual que el profundo.
            month = _get_mes_texto(page)
            labels = []
            for index, date_button in enumerate(calendar_dates):
                try:
                    day = date_button.query_selector("p")
                    day_text = day.inner_text().strip() if day else str(index + 1)
                except Exception:
                    day_text = str(index + 1)
                labels.append(f"{day_text} de {month}".strip())

            for index, label in enumerate(labels):
                try:
                    page.goto(url, timeout=30000)
                    _wait_after_navigation(page)
                    if not _wait_for_movistar_session(page):
                        logging.warning(
                            "[Movistar-Simple] La sesión no se confirmó tras "
                            "recargar para %s.",
                            label,
                        )
                    dates_fresh = _wait_for_index(page, "button.dia-evento", index)
                    if index >= len(dates_fresh):
                        fechas_estado[label] = STATUS_UNKNOWN
                        purchase_signals[label] = False
                        continue
                    dates_fresh[index].click()
                    page.wait_for_timeout(1000)
                    _clasificar_fila_simple(page, label, fechas_estado, purchase_signals)
                except Exception as exc:
                    logging.warning(
                        "[Movistar-Simple] Error verificando %s: %s", label, exc
                    )
                    fechas_estado[label] = STATUS_UNKNOWN
                    purchase_signals[label] = False
        else:
            fechas_estado["General"] = STATUS_UNKNOWN
            purchase_signals["General"] = False

    if not fechas_estado:
        fechas_estado["General"] = STATUS_UNKNOWN
    status = aggregate_status(fechas_estado)
    return {
        "status": status,
        "snippet": (
            "apareció Comprar/Seleccionar; falta confirmar asiento (chequeo simple)"
            if status == STATUS_CANDIDATE
            else status
        ),
        "fechas": fechas_estado,
        "purchase_signals": purchase_signals,
        # Marca el origen para que run_check redacte la alerta distinto: acá
        # "candidate" significa que el sitio público ya muestra Comprar, una
        # señal más fuerte que la del profundo (sector habilitado sin poder
        # confirmar asiento), que sigue mereciendo el tono más cauteloso.
        "checker": "movistar_simple",
    }


# ─────────────────────────────────────────────
# Checkers públicos
# ─────────────────────────────────────────────

def check_allaccess(url: str) -> dict:
    return run_with_timeout("allaccess", url)

def check_bts(url: str) -> dict:
    return run_with_timeout("bts", url)

def check_enigmatickets(url: str) -> dict:
    return run_with_timeout("enigma", url)

def check_movistar_profundo(url: str) -> dict:
    return run_with_timeout("movistar_profundo", url)

def check_movistar_simple(url: str) -> dict:
    return run_with_timeout("movistar_simple", url)

def check_url(url: str, deep: bool = False) -> dict:
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    host = parts.netloc
    path = parts.path.lower()

    if host.endswith("movistararena.com.ar"):
        if deep:
            return check_movistar_profundo(normalized)
        return check_movistar_simple(normalized)
    if host.endswith("allaccess.com.ar") and path.startswith("/event/bts"):
        return check_bts(normalized)
    if host.endswith("allaccess.com.ar"):
        return check_allaccess(normalized)
    if host.endswith("enigmatickets.com"):
        return check_enigmatickets(normalized)

    try:
        r = requests.get(normalized, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        text = soup.get_text(separator=" ").lower()

        blocked = next((kw for kw in BLOCKED_MARKERS if kw in text), "")
        if blocked:
            return {
                "status": STATUS_BLOCKED,
                "snippet": blocked,
                "fechas": {"General": STATUS_BLOCKED},
            }

        sold_hits = [kw for kw in KEYWORDS_SOLD_OUT if kw in text]
        available_hits = [kw for kw in KEYWORDS_AVAILABLE if kw in text]
        if available_hits and not sold_hits:
            return {
                "status": STATUS_CANDIDATE,
                "snippet": available_hits[0],
                "fechas": {"General": STATUS_CANDIDATE},
            }
        if sold_hits and not available_hits:
            return {
                "status": STATUS_SOLD_OUT,
                "snippet": sold_hits[0],
                "fechas": {"General": STATUS_SOLD_OUT},
            }
        return {
            "status": STATUS_UNKNOWN,
            "snippet": "señales ambiguas" if sold_hits and available_hits else "",
            "fechas": {"General": STATUS_UNKNOWN},
        }

    except requests.exceptions.HTTPError as e:
        return {
            "status": STATUS_ERROR,
            "snippet": str(e),
            "fechas": {"General": STATUS_ERROR},
        }
    except Exception as e:
        return {
            "status": STATUS_ERROR,
            "snippet": str(e),
            "fechas": {"General": STATUS_ERROR},
        }

# ─────────────────────────────────────────────
# Comandos Telegram
# ─────────────────────────────────────────────

def handle_command(text: str, urls: dict) -> str:
    parts = text.strip().split(maxsplit=2)
    cmd   = parts[0].lower()

    if cmd == "/add":
        if len(parts) < 3:
            return "⚠️ Uso correcto:\n<code>/add URL Nombre del evento</code>"
        url  = normalize_url(parts[1])
        name = parts[2]
        if not is_valid_http_url(url):
            return "⚠️ La URL debe empezar con http:// o https://"
        if len(urls) >= 20:
            return "⚠️ Límite de 20 URLs alcanzado."
        urls[url] = {
            "name": name,
            "last_status": STATUS_UNKNOWN,
            "last_check": 0,
            "fechas": {},
            "last_known_fechas": {},
            "pending_alerts": [],
            "consecutive_failures": 0,
            "next_retry_at": 0,
            "health_alert_active": False,
            "purchase_signals": {},
            "deep_check": False,
            "added": datetime.now().isoformat()
        }
        save_urls(urls)
        return (
            f"✅ Agregado:\n<b>{escape(name)}</b>\n{escape(url)}\n\n"
            "Empezaré a monitorearlo de inmediato."
        )

    elif cmd == "/remove":
        if len(parts) < 2:
            return "⚠️ Uso correcto:\n<code>/remove URL</code>"
        url = normalize_url(parts[1])
        if url in urls:
            name = urls[url]["name"]
            del urls[url]
            save_urls(urls)
            return f"🗑️ Eliminado: <b>{escape(str(name))}</b>"
        return "⚠️ No encontré esa URL en la lista."

    elif cmd == "/list":
        if not urls:
            return "📋 No tenés URLs en monitoreo.\nAgregá una con /add"
        lines = ["📋 <b>URLs monitoreadas:</b>\n"]
        for i, (url, data) in enumerate(urls.items(), 1):
            status_emoji = {
                STATUS_AVAILABLE: "🚨",
                STATUS_CANDIDATE: "🟡",
                STATUS_SOLD_OUT:  "🔴",
                STATUS_UNKNOWN:   "⚪",
                STATUS_BLOCKED:   "🛑",
                STATUS_ERROR:     "⚠️",
            }.get(data.get("last_status"), "⚪")
            profundo = " 🔬" if data.get("deep_check") else ""
            lines.append(
                f"{i}. {status_emoji} <b>{escape(str(data['name']))}</b>{profundo}\n"
                f"   <a href='{escape(url, quote=True)}'>{escape(url[:60])}...</a>"
            )
        return "\n".join(lines)

    elif cmd == "/status":
        if not urls:
            return "📋 No tenés URLs en monitoreo."
        lines = ["🩺 <b>Estado detallado:</b>\n"]
        for index, (url, data) in enumerate(urls.items(), 1):
            last_check = float(data.get("last_check", 0) or 0)
            if last_check:
                checked_at = datetime.fromtimestamp(
                    last_check, tz=timezone.utc
                ).astimezone(timezone(timedelta(hours=-3)))
                checked_text = checked_at.strftime("%d/%m %H:%M")
            else:
                checked_text = "nunca"
            status = data.get("last_status", STATUS_UNKNOWN)
            snippet = str(data.get("last_snippet", "") or "")[:120]
            pending = len(data.get("pending_alerts", []))
            failures = int(data.get("consecutive_failures", 0) or 0)
            detail = f" — {snippet}" if snippet else ""
            pending_text = f" — {pending} alerta(s) pendiente(s)" if pending else ""
            failure_text = (
                f" — {failures} fallo(s) consecutivo(s)" if failures else ""
            )
            lines.append(
                f"{index}. <b>{escape(str(data['name']))}</b>: "
                f"<code>{escape(str(status))}</code>\n"
                f"   Último chequeo: {checked_text}"
                f"{escape(detail)}{escape(pending_text)}{escape(failure_text)}"
            )
        return "\n".join(lines)

    elif cmd == "/check":
        if not urls:
            return "📋 No tenés URLs en monitoreo."
        return "__force_check__"

    elif cmd == "/profundo":
        if len(parts) < 2:
            return (
                "⚠️ Uso correcto:\n<code>/profundo URL on</code> o "
                "<code>/profundo URL off</code>\n\n"
                "Por defecto los eventos de Movistar usan el chequeo simple "
                "(solo mira si aparece Comprar/Seleccionar). Activá el "
                "profundo para una fecha puntual si el simple te da "
                "señales que después no se confirman."
            )
        args = parts[2].split() if len(parts) > 2 else []
        url = normalize_url(parts[1])
        if url not in urls:
            return "⚠️ No encontré esa URL en la lista."
        if "movistararena.com.ar" not in url:
            return "⚠️ El chequeo profundo solo aplica a eventos de Movistar Arena."
        activar = args[0].lower() if args else "on"
        if activar not in ("on", "off"):
            return "⚠️ Usá <code>on</code> u <code>off</code>."
        urls[url]["deep_check"] = activar == "on"
        save_urls(urls)
        estado = "activado" if activar == "on" else "desactivado"
        return f"🔧 Chequeo profundo {estado} para <b>{escape(str(urls[url]['name']))}</b>."

    elif cmd in ("/help", "/start"):
        return (
            "🎫 <b>Bot de Entradas</b>\n\n"
            "Comandos disponibles:\n\n"
            "/add URL Nombre — Agregar URL a monitorear\n"
            "/remove URL — Eliminar una URL\n"
            "/list — Ver todas las URLs activas\n"
            "/check — Forzar chequeo ahora mismo\n"
            "/status — Ver el último resultado y hora por evento\n"
            "/profundo URL on|off — Activar/desactivar la verificación "
            "de asiento real para un evento de Movistar puntual\n"
            "/help — Ver esta ayuda\n\n"
            "AllAccess y Enigma: cada 5 minutos\n"
            "Movistar Arena: cada 10 minutos\n\n"
            "Movistar Arena por defecto usa el chequeo simple (avisa "
            "apenas aparece Comprar/Seleccionar, sin confirmar asiento)."
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
        last_check = float(data.get("last_check", 0) or 0)
        retry_at   = float(data.get("next_retry_at", 0) or 0)
        retry_due  = bool(retry_at and now >= retry_at)
        normal_due = not retry_at and now - last_check >= interval
        if force or retry_due or normal_due:
            urls_to_check.append(url)

    if not urls_to_check:
        return

    log.info(f"Chequeando {len(urls_to_check)} URLs...")
    resumen = []

    results = {}
    workers = max(1, min(MAX_CONCURRENT_CHECKS, len(urls_to_check)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(check_url, url, urls[url].get("deep_check", False)): url
            for url in urls_to_check
        }
        for future in as_completed(futures):
            checked_url = futures[future]
            try:
                results[checked_url] = future.result()
            except Exception as exc:
                log.exception("Fallo no controlado chequeando %s", checked_url)
                results[checked_url] = {
                    "status": STATUS_ERROR,
                    "snippet": str(exc),
                    "fechas": {"General": STATUS_ERROR},
                }

    for url in urls_to_check:
        data          = urls[url]
        name          = data["name"]
        observed_prev = data.get("fechas", {})
        known_prev    = data.get("last_known_fechas", observed_prev.copy())
        purchase_signals_prev = data.get("purchase_signals", {})

        log.info(f"Iniciando chequeo: {name}")
        result        = results[url]
        new_status    = result.get("status", STATUS_ERROR)
        if new_status not in KNOWN_STATUSES:
            new_status = STATUS_ERROR
        nuevas_fechas = result.get("fechas", {})
        if not nuevas_fechas:
            nuevas_fechas = {"General": STATUS_UNKNOWN}

        log.info(f"  [{new_status}] {name}")

        confirmed_dates = []
        candidate_dates = []
        for fecha, estado in nuevas_fechas.items():
            if estado == STATUS_AVAILABLE and known_prev.get(fecha) != STATUS_AVAILABLE:
                confirmed_dates.append(fecha)
            elif (
                estado == STATUS_CANDIDATE
                and observed_prev.get(fecha) != STATUS_CANDIDATE
                and known_prev.get(fecha)
                not in (STATUS_AVAILABLE, STATUS_CANDIDATE)
            ):
                candidate_dates.append(fecha)

        sector_counts_prev = data.get("sector_counts", {})
        sector_counts_new  = result.get("sector_counts", {})
        for fecha, cant in sector_counts_new.items():
            prev_cant = sector_counts_prev.get(fecha, 0)
            if (
                cant > prev_cant
                and nuevas_fechas.get(fecha) == STATUS_CANDIDATE
                and fecha not in candidate_dates
            ):
                candidate_dates.append(fecha)
                logging.info(
                    "  📈 %s — %s: sectores %s → %s",
                    name,
                    fecha,
                    prev_cant,
                    cant,
                )

        purchase_signals_new = result.get("purchase_signals", {})
        if not isinstance(purchase_signals_new, dict):
            purchase_signals_new = {}
        for fecha, detected in purchase_signals_new.items():
            if (
                detected
                and not purchase_signals_prev.get(fecha, False)
                and nuevas_fechas.get(fecha) in UNRELIABLE_STATUSES
                and known_prev.get(fecha) != STATUS_AVAILABLE
                and fecha not in candidate_dates
            ):
                candidate_dates.append(fecha)
                logging.info(
                    "  🟡 %s — %s: apareció Comprar/Seleccionar",
                    name,
                    fecha,
                )

        if confirmed_dates:
            confirmed_text = ", ".join(escape(str(date)) for date in confirmed_dates)
            queue_alert(
                data,
                "confirmed:" + "|".join(sorted(confirmed_dates)),
                (
                    "🚨 <b>¡ENTRADAS CONFIRMADAS!</b>\n\n"
                    f"🎫 <b>{escape(str(name))}</b>\n"
                    f"🗓 <i>{confirmed_text}</i>\n\n"
                    "✅ El bot encontró asiento o cantidad seleccionable.\n\n"
                    f"👉 <a href='{escape(url, quote=True)}'>Comprá acá</a>"
                ),
            )

        if candidate_dates:
            candidate_text = ", ".join(
                escape(str(date)) for date in candidate_dates
            )
            if result.get("checker") == "movistar_simple":
                # El chequeo simple solo llega a "candidate" cuando el sitio
                # público ya muestra Comprar/Seleccionar en vez de Agotado --
                # la misma señal que antes alcanzaba para conseguir entradas,
                # así que acá sí se avisa con tono de urgencia.
                candidate_msg = (
                    "🚨 <b>¡HAY DISPONIBLE!</b>\n\n"
                    f"🎫 <b>{escape(str(name))}</b>\n"
                    f"🗓 <i>{candidate_text}</i>\n\n"
                    "Apareció \"Comprar\"/\"Seleccionar\" donde antes decía "
                    "Agotado.\n\n"
                    f"👉 <a href='{escape(url, quote=True)}'>Comprá acá</a>"
                )
            else:
                candidate_msg = (
                    "🟡 <b>POSIBLE LIBERACIÓN</b>\n\n"
                    f"🎫 <b>{escape(str(name))}</b>\n"
                    f"🗓 <i>{candidate_text}</i>\n\n"
                    "Apareció una señal de compra o un sector habilitado, "
                    "pero todavía no pude confirmar un asiento seleccionable.\n\n"
                    f"👉 <a href='{escape(url, quote=True)}'>Revisá ahora</a>"
                )
            queue_alert(
                data,
                "candidate:" + "|".join(sorted(candidate_dates)),
                candidate_msg,
            )

        previous_check = float(data.get("last_check", 0) or 0)
        health_alert_active = bool(data.get("health_alert_active", False))
        data["health_alert_active"] = health_alert_active
        checked_at = time.time()
        if new_status in UNRELIABLE_STATUSES:
            data["consecutive_failures"] = int(
                data.get("consecutive_failures", 0)
            ) + 1
            retry_delay = unreliable_retry_delay(data["consecutive_failures"])
            data["next_retry_at"] = checked_at + retry_delay
            logging.warning(
                "  Reintento rápido de %s en %ss (fallo %s)",
                name,
                retry_delay,
                data["consecutive_failures"],
            )
        else:
            data["consecutive_failures"] = 0
            data["next_retry_at"] = 0

        if (
            new_status in UNRELIABLE_STATUSES
            and data["consecutive_failures"] >= HEALTH_FAILURE_THRESHOLD
            and not health_alert_active
        ):
            label = {
                STATUS_UNKNOWN: "Resultado indeterminado",
                STATUS_BLOCKED: "Sitio bloqueando el chequeo",
                STATUS_ERROR: "Error técnico",
            }[new_status]
            snippet = str(result.get("snippet", ""))[:300]
            queue_alert(
                data,
                "health:unreliable",
                (
                    f"⚠️ <b>Problema persistente: {escape(str(name))}</b>\n\n"
                    f"{escape(label)} durante "
                    f"{data['consecutive_failures']} chequeos seguidos.\n"
                    f"<i>{escape(snippet or 'Sin detalle adicional')}</i>\n\n"
                    "No se interpretó como agotado. Seguiré haciendo "
                    "reintentos rápidos; conviene revisar manualmente."
                ),
            )
            data["health_alert_active"] = True

        if (
            previous_check
            and new_status not in UNRELIABLE_STATUSES
            and health_alert_active
        ):
            queue_alert(
                data,
                f"recovered:{new_status}",
                (
                    f"✅ <b>Chequeo recuperado: {escape(str(name))}</b>\n\n"
                    f"Estado actual: <code>{escape(new_status)}</code>."
                ),
            )
            data["health_alert_active"] = False

        emoji = {
            STATUS_AVAILABLE: "🚨",
            STATUS_CANDIDATE: "🟡",
            STATUS_SOLD_OUT: "🔴",
            STATUS_UNKNOWN: "⚪",
            STATUS_BLOCKED: "🛑",
            STATUS_ERROR: "⚠️",
        }[new_status]
        dates_for_status = [
            date
            for date, state in nuevas_fechas.items()
            if state in (STATUS_AVAILABLE, STATUS_CANDIDATE)
        ]
        detail = (
            ", ".join(str(date) for date in dates_for_status)
            if dates_for_status
            else str(result.get("snippet", "") or "")
        )
        resumen.append(
            f"{emoji} <b>{escape(str(name))}</b>: {escape(new_status)}"
            + (f" — {escape(detail[:250])}" if detail else "")
        )

        known_next = dict(known_prev)
        for fecha, estado in nuevas_fechas.items():
            if estado not in UNRELIABLE_STATUSES:
                known_next[fecha] = estado

        purchase_signals_next = dict(purchase_signals_prev)
        for fecha, detected in purchase_signals_new.items():
            estado = nuevas_fechas.get(fecha, STATUS_UNKNOWN)
            if detected:
                purchase_signals_next[fecha] = True
            elif estado not in UNRELIABLE_STATUSES:
                purchase_signals_next[fecha] = False

        sector_counts_next = dict(sector_counts_prev)
        for fecha, count in sector_counts_new.items():
            estado = nuevas_fechas.get(fecha, STATUS_UNKNOWN)
            if estado not in UNRELIABLE_STATUSES:
                sector_counts_next[fecha] = count

        urls[url]["last_status"]   = new_status
        urls[url]["fechas"]        = nuevas_fechas
        urls[url]["last_known_fechas"] = known_next
        urls[url]["last_check"]    = checked_at
        urls[url]["last_snippet"]  = result.get("snippet", "")
        urls[url]["sector_counts"] = sector_counts_next
        urls[url]["seat_counts"]   = result.get("seat_counts", {})
        urls[url]["last_evidence"] = result.get("evidence", {})
        urls[url]["purchase_signals"] = purchase_signals_next
        if new_status not in UNRELIABLE_STATUSES:
            urls[url]["last_reliable_check"] = now

    save_urls(urls)
    flush_pending_alerts(urls)

    if notify_no_change:
        msg = "📋 <b>Estado actual:</b>\n\n" + "\n".join(resumen) if resumen else \
              "✅ Chequeo manual completado. Sin novedades por ahora."
        send_telegram(msg)

# ─────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────

def main():
    log.info("🎫 Bot de Entradas iniciado")
    urls       = load_urls()
    last_daily = 0
    offset     = load_telegram_offset()

    send_telegram(
        "🤖 <b>Bot de Entradas iniciado</b>\n\n"
        f"Estoy activo y monitoreando {len(urls)} evento(s).\n"
        "Escribí /status para ver la salud de cada chequeo."
    )

    while True:
        flush_pending_alerts(urls)
        updates = get_telegram_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg    = update.get("message", {})
            text   = msg.get("text", "")
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if chat_id != str(TELEGRAM_CHAT_ID):
                log.warning("Comando ignorado desde chat no autorizado: %s", chat_id)
                save_telegram_offset(offset)
                continue
            if text.startswith("/"):
                response = handle_command(text, urls)
                if response == "__force_check__":
                    send_telegram("🔄 Chequeando ahora...")
                    run_check(urls, notify_no_change=True, force=True)
                else:
                    send_telegram(response)
            save_telegram_offset(offset)

        run_check(urls)

        argentina_now = datetime.now(timezone(timedelta(hours=-3)))
        hora_actual = argentina_now.hour
        now = time.time()
        if hora_actual == 9 and now - last_daily >= 86400:
            total   = len(urls)
            nombres = (
                ", ".join(escape(str(data["name"])) for data in urls.values())
                if urls
                else "ninguno"
            )
            unhealthy = [
                data["name"]
                for data in urls.values()
                if data.get("last_status") in UNRELIABLE_STATUSES
            ]
            health_text = (
                f"\n⚠️ Con problemas: "
                f"{', '.join(escape(str(name)) for name in unhealthy)}"
                if unhealthy
                else "\n✅ Todos con último resultado interpretable."
            )
            send_telegram(
                f"🟢 <b>Bot activo</b>\n\n"
                f"Monitoreando {total} evento(s): {nombres}"
                f"{health_text}"
            )
            last_daily = now

        time.sleep(2)


if __name__ == "__main__":
    main()
