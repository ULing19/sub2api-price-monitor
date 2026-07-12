import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import price_webview_app as app


class FakeWindow:
    def __init__(self):
        self.destroy_count = 0

    def destroy(self):
        self.destroy_count += 1


class FakeEvent:
    def __init__(self, value=False):
        self.value = value
        self.handlers = []

    def is_set(self):
        return self.value

    def wait(self, timeout=None):
        return self.value

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class FakeBrowserWindow(FakeWindow):
    def __init__(self, closed=False):
        super().__init__()
        self.events = types.SimpleNamespace(
            initialized=FakeEvent(True),
            closing=FakeEvent(),
            closed=FakeEvent(closed),
        )
        self.load_urls = []
        self.show_count = 0
        self.restore_count = 0
        self.hide_count = 0
        self.url_probe_count = 0

    def get_current_url(self):
        self.url_probe_count += 1
        raise AssertionError("window health checks must not call get_current_url")

    def load_url(self, url):
        self.load_urls.append(url)

    def show(self):
        self.show_count += 1

    def restore(self):
        self.restore_count += 1

    def hide(self):
        self.hide_count += 1


class PriceAppLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.previous_data_root = os.environ.get("SUB2API_PRICE_APP_DATA")
        os.environ["SUB2API_PRICE_APP_DATA"] = self.temporary_directory.name
        self.apis = []

    def tearDown(self):
        for api in self.apis:
            api.shutdown(wait=True, timeout=2)
        if self.previous_data_root is None:
            os.environ.pop("SUB2API_PRICE_APP_DATA", None)
        else:
            os.environ["SUB2API_PRICE_APP_DATA"] = self.previous_data_root
        self.temporary_directory.cleanup()

    def new_api(self):
        api = app.PriceAppApi()
        self.apis.append(api)
        return api

    def test_shutdown_destroys_all_auxiliary_windows_once(self):
        api = self.new_api()
        browser = FakeWindow()
        workers = [FakeWindow(), FakeWindow()]
        api.browser_window = browser
        api.worker_windows = workers[:]
        api.start_scheduler()

        self.assertTrue(api.shutdown(wait=True, timeout=2))
        self.assertEqual(browser.destroy_count, 1)
        self.assertEqual([window.destroy_count for window in workers], [1, 1])
        self.assertTrue(api.shutdown_event.is_set())
        self.assertFalse(api.scheduler_thread.is_alive())

        self.assertTrue(api.shutdown(wait=True, timeout=2))
        self.assertEqual(browser.destroy_count, 1)
        self.assertEqual([window.destroy_count for window in workers], [1, 1])

    def test_shutdown_does_not_block_on_busy_window_locks(self):
        api = self.new_api()
        browser = FakeWindow()
        worker = FakeWindow()
        api.browser_window = browser
        api.worker_windows[0] = worker
        api.browser_lock.acquire()
        api.worker_window_lock.acquire()
        try:
            started = time.monotonic()
            with self.assertLogs(app.LOGGER, level="WARNING"):
                self.assertTrue(api.shutdown(wait=True, timeout=1))
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            api.worker_window_lock.release()
            api.browser_lock.release()
        self.assertEqual(browser.destroy_count, 1)
        self.assertEqual(worker.destroy_count, 1)

    def test_shutdown_cancels_a_blocked_update_promptly(self):
        api = self.new_api()
        entered_capture = threading.Event()
        records = [{
            "name": "blocked",
            "site": "https://blocked.example",
            "api_base": "/api/v1",
            "interval_minutes": 180,
            "next_run": datetime.now(timezone.utc).isoformat(),
        }]
        app.write_saved_sites(records)

        def blocked_capture(instance, record, include_groups=True, worker_slot=0):
            entered_capture.set()
            while True:
                instance._raise_if_shutting_down()
                time.sleep(0.02)

        api._capture_site_webview = types.MethodType(blocked_capture, api)
        result = api.start_update_all_prices("test", False)
        self.assertTrue(result["accepted"])
        self.assertTrue(entered_capture.wait(2))

        started = time.monotonic()
        self.assertTrue(api.shutdown(wait=True, timeout=2))
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(api.update_thread and api.update_thread.is_alive())
        self.assertEqual(api.update_job["status"], "cancelled")

    def test_capture_workers_are_daemon_threads(self):
        api = self.new_api()
        records = [
            {
                "name": f"site-{index}",
                "site": f"https://site-{index}.example",
                "api_base": "/api/v1",
                "interval_minutes": 180,
                "next_run": datetime.now(timezone.utc).isoformat(),
            }
            for index in range(4)
        ]
        app.write_saved_sites(records)

        def successful_capture(instance, record, include_groups=True, worker_slot=0):
            time.sleep(0.02)
            site = record["site"]
            return {
                "ok": True,
                "rows": [{
                    "site": site,
                    "site_host": app.urlparse(site).netloc,
                    "status": "ok",
                    "source": "test",
                    "record_type": "group",
                    "model_category": "OpenAI",
                    "group_id": record["name"],
                    "group_name": record["name"],
                    "rate_multiplier": 1,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }],
            }

        api._capture_site_webview = types.MethodType(successful_capture, api)
        result = api._run_site_records(records, only_due=False)

        self.assertEqual(result["summary"]["success_count"], 4)
        self.assertFalse(api.capture_threads)
        capture_threads = [
            thread for thread in threading.enumerate()
            if thread.name.startswith("site-capture-")
        ]
        self.assertTrue(all(thread.daemon for thread in capture_threads))

    def test_window_health_check_does_not_call_webview(self):
        api = self.new_api()
        window = FakeBrowserWindow()

        self.assertTrue(api._window_alive(window))
        self.assertEqual(window.url_probe_count, 0)

        window.events.closed.value = True
        self.assertFalse(api._window_alive(window))
        self.assertEqual(window.url_probe_count, 0)

    def test_relogin_reuses_precreated_window(self):
        api = self.new_api()
        browser = FakeBrowserWindow()
        api.attach_browser_window(browser)

        with mock.patch.object(app.webview, "create_window") as create_window:
            result = api._ensure_browser_window("https://example.com", visible=True)

        self.assertIs(result, browser)
        create_window.assert_not_called()
        self.assertEqual(browser.load_urls, ["https://example.com"])
        self.assertEqual(browser.show_count, 1)
        self.assertEqual(browser.restore_count, 1)

    def test_closing_login_window_hides_it_for_reuse(self):
        api = self.new_api()
        browser = FakeBrowserWindow()
        api.attach_browser_window(browser)

        self.assertFalse(api._on_browser_closing(browser))
        self.assertTrue(api.browser_cancel_event.is_set())
        self.assertIs(api.browser_window, browser)
        deadline = time.monotonic() + 1
        while browser.hide_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(browser.hide_count, 1)

    def test_missing_login_window_fails_without_dynamic_creation(self):
        api = self.new_api()
        with mock.patch.object(app.webview, "create_window") as create_window:
            with self.assertRaisesRegex(RuntimeError, "已不可用"):
                api._ensure_browser_window("https://example.com", visible=True)
        create_window.assert_not_called()

    def test_relogin_does_not_wait_forever_for_window_lock(self):
        api = self.new_api()
        api.site = "https://old.example"
        api.browser_lock.acquire()
        try:
            started = time.monotonic()
            result = api.open_site("https://new.example")
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(result["ok"])
            self.assertIn("窗口正在切换", result["error"])
            self.assertEqual(api.site, "https://old.example")
        finally:
            api.browser_lock.release()

    def test_relogin_waits_for_cancelled_credential_helper(self):
        api = self.new_api()
        browser = FakeBrowserWindow()
        api.attach_browser_window(browser)
        api.interactive_operation_lock.acquire()

        def release_helper_lock():
            time.sleep(0.05)
            api.interactive_operation_lock.release()

        threading.Thread(target=release_helper_lock, daemon=True).start()
        result = api.open_site("https://new.example")

        self.assertTrue(result["ok"])
        self.assertEqual(result["site"], "https://new.example")
        self.assertEqual(browser.load_urls, ["https://new.example"])

    def test_interactive_capture_uses_a_daemon_job(self):
        api = self.new_api()
        entered_capture = threading.Event()

        def blocked_capture(instance, api_base, include_groups, close_browser):
            entered_capture.set()
            while not instance.shutdown_event.wait(0.02):
                pass
            return {"ok": False, "shutting_down": True, "error": "应用正在退出"}

        api._capture_prices_sync = types.MethodType(blocked_capture, api)
        started_at = time.monotonic()
        result = api.start_capture_prices("/api/v1", True, True)

        self.assertTrue(result["accepted"])
        self.assertLess(time.monotonic() - started_at, 0.2)
        self.assertTrue(entered_capture.wait(1))
        self.assertTrue(api.capture_job_thread.daemon)
        self.assertTrue(api.shutdown(wait=True, timeout=2))
        self.assertEqual(api.capture_status(result["job_id"])["status"], "cancelled")

    def test_runtime_files_are_written_atomically(self):
        records = [{
            "name": "example",
            "site": "https://example.com",
            "api_base": "/api/v1",
            "interval_minutes": 180,
        }]
        app.write_saved_sites(records)
        self.assertEqual(app.load_saved_sites()[0]["site"], "https://example.com")

        payload = app.write_price_snapshot([], {"site_count": 0})
        saved_payload = json.loads(app.latest_prices_json_path().read_text(encoding="utf-8"))
        self.assertEqual(saved_payload["generated_at"], payload["generated_at"])
        self.assertFalse(list(pathlib.Path(self.temporary_directory.name).rglob("*.tmp")))

    def test_log_redaction_removes_tokens(self):
        text = app.redact_log_text(
            "Authorization: Bearer abc.def-123 and sk-abcdefghijklmnopqrstuvwxyz"
        )
        self.assertNotIn("abc.def-123", text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", text)


if __name__ == "__main__":
    unittest.main()
