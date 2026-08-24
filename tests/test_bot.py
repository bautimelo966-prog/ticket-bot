import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123")

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = MagicMock()
    requests_stub.post = MagicMock()
    requests_stub.exceptions = types.SimpleNamespace(HTTPError=RuntimeError)
    sys.modules["requests"] = requests_stub

try:
    import bs4  # noqa: F401
except ModuleNotFoundError:
    bs4_stub = types.ModuleType("bs4")
    bs4_stub.BeautifulSoup = MagicMock()
    sys.modules["bs4"] = bs4_stub

try:
    import playwright.sync_api  # noqa: F401
except ModuleNotFoundError:
    playwright_stub = types.ModuleType("playwright")
    sync_api_stub = types.ModuleType("playwright.sync_api")
    sync_api_stub.sync_playwright = MagicMock()
    playwright_stub.sync_api = sync_api_stub
    sys.modules["playwright"] = playwright_stub
    sys.modules["playwright.sync_api"] = sync_api_stub

import bot


class StatusTests(unittest.TestCase):
    def test_aggregate_never_turns_unknown_into_sold_out(self):
        self.assertEqual(
            bot.aggregate_status({"A": bot.STATUS_SOLD_OUT, "B": bot.STATUS_UNKNOWN}),
            bot.STATUS_UNKNOWN,
        )
        self.assertEqual(
            bot.aggregate_status({"A": bot.STATUS_SOLD_OUT}),
            bot.STATUS_SOLD_OUT,
        )

    def test_available_and_candidate_have_priority(self):
        self.assertEqual(
            bot.aggregate_status({"A": bot.STATUS_CANDIDATE, "B": bot.STATUS_UNKNOWN}),
            bot.STATUS_CANDIDATE,
        )
        self.assertEqual(
            bot.aggregate_status({"A": bot.STATUS_AVAILABLE, "B": bot.STATUS_BLOCKED}),
            bot.STATUS_AVAILABLE,
        )


class RoutingTests(unittest.TestCase):
    def test_individual_bts_url_uses_bts_checker(self):
        url = "https://www.allaccess.com.ar/event/bts-23-de-octubre/"
        with patch.object(bot, "check_bts", return_value={"status": "ok"}) as bts:
            with patch.object(bot, "check_allaccess") as standard:
                result = bot.check_url(url)
        self.assertEqual(result, {"status": "ok"})
        bts.assert_called_once_with(url.rstrip("/"))
        standard.assert_not_called()

    def test_normalize_url_keeps_query_and_removes_trailing_slash(self):
        self.assertEqual(
            bot.normalize_url(" HTTPS://EXAMPLE.COM/event/?x=1 "),
            "https://example.com/event?x=1",
        )

    def test_generic_checker_returns_candidate_date_for_alerting(self):
        response = MagicMock()
        response.text = "<html><body>Comprar entradas</body></html>"
        response.raise_for_status.return_value = None
        soup = MagicMock()
        soup.get_text.return_value = "Comprar entradas"
        soup.return_value = []
        with patch.object(bot.requests, "get", return_value=response):
            with patch.object(bot, "BeautifulSoup", return_value=soup):
                result = bot.check_url("https://tickets.example/show")
        self.assertEqual(result["status"], bot.STATUS_CANDIDATE)
        self.assertEqual(result["fechas"], {"General": bot.STATUS_CANDIDATE})

    def test_generic_ambiguous_page_is_unknown(self):
        response = MagicMock()
        response.text = "<html><body>Comprar entradas - agotado</body></html>"
        response.raise_for_status.return_value = None
        soup = MagicMock()
        soup.get_text.return_value = "Comprar entradas - agotado"
        soup.return_value = []
        with patch.object(bot.requests, "get", return_value=response):
            with patch.object(bot, "BeautifulSoup", return_value=soup):
                result = bot.check_url("https://tickets.example/show")
        self.assertEqual(result["status"], bot.STATUS_UNKNOWN)


class AllAccessTests(unittest.TestCase):
    def test_visible_global_sold_out_is_detected_without_dropdown(self):
        page = MagicMock()
        sold_out = MagicMock()
        sold_out.is_visible.return_value = True
        page.query_selector.side_effect = lambda selector: (
            sold_out
            if selector == "div.event-status.status-soldout"
            else None
        )

        self.assertEqual(
            bot._allaccess_global_status(page),
            bot.STATUS_SOLD_OUT,
        )

    def test_hidden_global_sold_out_template_is_ignored(self):
        page = MagicMock()
        hidden = MagicMock()
        hidden.is_visible.return_value = False
        page.query_selector.side_effect = lambda selector: (
            hidden
            if selector == "div.event-status.status-soldout"
            else None
        )

        self.assertIsNone(bot._allaccess_global_status(page))


class MovistarVerificationTests(unittest.TestCase):
    @staticmethod
    def _locator(count):
        locator = MagicMock()
        locator.count.return_value = count
        item = MagicMock()
        item.is_visible.return_value = True
        item.is_enabled.return_value = True
        locator.nth.return_value = item
        return locator

    def test_current_movistar_seat_available_class_is_confirmed(self):
        frame = MagicMock()
        frame.locator.side_effect = lambda selector: self._locator(
            1 if selector == ".asientos-vista .seat.seat-available" else 0
        )
        page = MagicMock()
        page.frames = [frame]

        count, evidence = bot._contar_inventario_real(page)

        self.assertEqual(count, 1)
        self.assertTrue(any("seat-available" in item for item in evidence))

    def test_enabled_movistar_quantity_button_is_confirmed(self):
        frame = MagicMock()
        frame.locator.side_effect = lambda selector: self._locator(
            1 if selector == bot.MOVISTAR_QUANTITY_BUTTON_SELECTOR else 0
        )
        page = MagicMock()
        page.frames = [frame]

        count, evidence = bot._contar_inventario_real(page)

        self.assertEqual(count, 1)
        self.assertTrue(any("cantidad" in item for item in evidence))

    def test_enabled_sector_without_seat_is_only_candidate(self):
        page = MagicMock()
        page.frames = []
        target = MagicMock()
        with patch.object(bot, "page_block_reason", return_value=""):
            with patch.object(bot, "_contar_inventario_real", return_value=(0, [])):
                with patch.object(bot, "_contar_sectores_disponibles", return_value=1):
                    with patch.object(
                        bot, "_available_sector_targets", return_value=[(target, "sector")]
                    ):
                        result = bot._inspect_movistar_map(page, MagicMock())
        self.assertEqual(result["status"], bot.STATUS_CANDIDATE)
        self.assertEqual(result["seat_count"], 0)

    def test_real_seat_after_opening_sector_is_confirmed(self):
        page = MagicMock()
        page.frames = []
        target = MagicMock()
        inventory = [(0, []), (2, ["2 asientos"])]
        with patch.object(bot, "page_block_reason", return_value=""):
            with patch.object(bot, "_contar_inventario_real", side_effect=inventory):
                with patch.object(bot, "_contar_sectores_disponibles", return_value=1):
                    with patch.object(
                        bot, "_available_sector_targets", return_value=[(target, "sector")]
                    ):
                        result = bot._inspect_movistar_map(page, MagicMock())
        self.assertEqual(result["status"], bot.STATUS_AVAILABLE)
        self.assertEqual(result["seat_count"], 2)


class AlertDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_urls_file = bot.URLS_FILE
        bot.URLS_FILE = os.path.join(self.temp_dir.name, "urls.json")

    def tearDown(self):
        bot.URLS_FILE = self.old_urls_file
        self.temp_dir.cleanup()

    def test_failed_telegram_alert_remains_pending_then_retries(self):
        url = "https://tickets.example/show"
        urls = {
            url: {
                "name": "Show",
                "last_status": bot.STATUS_SOLD_OUT,
                "last_check": 0,
                "fechas": {"General": bot.STATUS_SOLD_OUT},
                "last_known_fechas": {"General": bot.STATUS_SOLD_OUT},
                "pending_alerts": [],
            }
        }
        result = {
            "status": bot.STATUS_AVAILABLE,
            "snippet": "asiento confirmado",
            "fechas": {"General": bot.STATUS_AVAILABLE},
            "seat_counts": {"General": 1},
        }
        with patch.object(bot, "check_url", return_value=result):
            with patch.object(bot, "send_telegram", return_value=False):
                bot.run_check(urls, force=True)

        self.assertEqual(len(urls[url]["pending_alerts"]), 1)
        self.assertEqual(urls[url]["last_status"], bot.STATUS_AVAILABLE)

        with patch.object(bot, "send_telegram", return_value=True):
            bot.flush_pending_alerts(urls)
        self.assertEqual(urls[url]["pending_alerts"], [])

    def test_unknown_does_not_erase_last_known_state(self):
        url = "https://tickets.example/show"
        urls = {
            url: {
                "name": "Show",
                "last_status": bot.STATUS_SOLD_OUT,
                "last_check": 0,
                "fechas": {"General": bot.STATUS_SOLD_OUT},
                "last_known_fechas": {"General": bot.STATUS_SOLD_OUT},
                "pending_alerts": [],
            }
        }
        result = {
            "status": bot.STATUS_UNKNOWN,
            "snippet": "selector ausente",
            "fechas": {"General": bot.STATUS_UNKNOWN},
        }
        with patch.object(bot, "check_url", return_value=result):
            with patch.object(bot, "send_telegram", return_value=True):
                bot.run_check(urls, force=True)
        self.assertEqual(
            urls[url]["last_known_fechas"]["General"],
            bot.STATUS_SOLD_OUT,
        )

    def test_recovery_after_checker_failure_is_reported(self):
        url = "https://tickets.example/show"
        urls = {
            url: {
                "name": "Show",
                "last_status": bot.STATUS_BLOCKED,
                "last_check": 10,
                "fechas": {"General": bot.STATUS_BLOCKED},
                "last_known_fechas": {"General": bot.STATUS_SOLD_OUT},
                "pending_alerts": [],
                "consecutive_failures": 3,
            }
        }
        result = {
            "status": bot.STATUS_SOLD_OUT,
            "snippet": "mapa verificado",
            "fechas": {"General": bot.STATUS_SOLD_OUT},
        }
        sent = []
        with patch.object(bot, "check_url", return_value=result):
            with patch.object(
                bot, "send_telegram", side_effect=lambda text: sent.append(text) or True
            ):
                bot.run_check(urls, force=True)

        self.assertEqual(urls[url]["consecutive_failures"], 0)
        self.assertTrue(any("Chequeo recuperado" in text for text in sent))

    def test_candidate_does_not_suppress_later_confirmation(self):
        url = "https://www.movistararena.com.ar/Ticketera/test"
        urls = {
            url: {
                "name": "Show",
                "last_status": bot.STATUS_SOLD_OUT,
                "last_check": 0,
                "fechas": {"Fecha": bot.STATUS_SOLD_OUT},
                "last_known_fechas": {"Fecha": bot.STATUS_SOLD_OUT},
                "pending_alerts": [],
            }
        }
        candidate = {
            "status": bot.STATUS_CANDIDATE,
            "snippet": "sector",
            "fechas": {"Fecha": bot.STATUS_CANDIDATE},
            "sector_counts": {"Fecha": 1},
            "seat_counts": {"Fecha": 0},
        }
        confirmed = {
            "status": bot.STATUS_AVAILABLE,
            "snippet": "asiento",
            "fechas": {"Fecha": bot.STATUS_AVAILABLE},
            "sector_counts": {"Fecha": 1},
            "seat_counts": {"Fecha": 1},
        }
        with patch.object(bot, "send_telegram", return_value=True):
            with patch.object(bot, "check_url", return_value=candidate):
                bot.run_check(urls, force=True)
            with patch.object(bot, "check_url", return_value=confirmed):
                bot.run_check(urls, force=True)
        self.assertEqual(urls[url]["last_status"], bot.STATUS_AVAILABLE)


class CommandTests(unittest.TestCase):
    def test_add_and_remove_use_normalized_url(self):
        urls = {}
        response = bot.handle_command(
            "/add https://example.com/show/ Evento", urls
        )
        self.assertIn("Agregado", response)
        self.assertIn("https://example.com/show", urls)
        response = bot.handle_command(
            "/remove https://example.com/show/", urls
        )
        self.assertIn("Eliminado", response)
        self.assertEqual(urls, {})

    def test_add_rejects_non_http_scheme(self):
        urls = {}
        response = bot.handle_command("/add httpx://example.com/show Evento", urls)
        self.assertIn("http:// o https://", response)
        self.assertEqual(urls, {})

    def test_event_name_is_escaped_for_telegram_html(self):
        urls = {}
        response = bot.handle_command(
            "/add https://example.com/show Evento <VIP>", urls
        )
        self.assertIn("Evento &lt;VIP&gt;", response)


if __name__ == "__main__":
    unittest.main()
