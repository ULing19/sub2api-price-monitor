import json
import os
import pathlib
import subprocess
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


class FakeCancellableBrowserWindow(FakeBrowserWindow):
    def __init__(self, closed=False):
        super().__init__(closed=closed)
        self.remote_cancelled = False

    def is_cancelled(self):
        return self.remote_cancelled


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

    def test_webview_host_timeout_is_classified_without_reopening_window(self):
        api = self.new_api()
        window = FakeBrowserWindow()

        failure = api._classify_capture_failure(
            "WebView 子进程无响应，已自动重启",
            window,
        )

        self.assertEqual(failure["error_code"], "timeout")
        self.assertFalse(failure["auth_required"])
        self.assertEqual(window.url_probe_count, 0)

    def test_capture_failure_only_reauthorizes_explicit_auth_errors(self):
        api = self.new_api()

        for error in (
            "HTTP_ERROR: /groups/available: HTTP 401: unauthorized",
            "HTTP_ERROR: /groups/available: HTTP 403: forbidden",
            "UNSUPPORTED_RESPONSE: access token expired",
        ):
            with self.subTest(error=error):
                failure = api._classify_capture_failure(error)
                self.assertEqual(failure["error_code"], "reauth_required")
                self.assertTrue(failure["auth_required"])

        for status in (500, 503):
            with self.subTest(status=status):
                failure = api._classify_capture_failure(
                    f"HTTP_ERROR: /groups/rates: HTTP {status}: upstream unavailable"
                )
                self.assertEqual(failure["error_code"], "http_error")
                self.assertFalse(failure["auth_required"])

    def test_partial_auth_success_keeps_site_in_reauthorization_state(self):
        api = self.new_api()
        record = {
            "name": "partial-auth",
            "site": "https://partial-auth.example",
            "api_base": "/api/v1",
            "interval_minutes": 180,
            "last_run": "2026-07-01T00:00:00+00:00",
            "last_status": "reauth_required",
            "health_status": "needs_auth",
            "reauth_required": True,
            "consecutive_failures": 1,
        }
        result = {
            "ok": True,
            "auth_required": True,
            "partial_auth": True,
            "error_code": "reauth_required",
            "status_label": "登录/会话已失效",
            "rateData": {
                "complete": False,
                "error": "HTTP_ERROR: /groups/rates: HTTP 401: unauthorized",
            },
            "rows": [{
                "site": record["site"],
                "site_host": "partial-auth.example",
                "record_type": "group",
                "group_id": "primary",
                "rate_multiplier": 1,
                "rate_data_complete": False,
            }],
        }

        updated = api._site_status_record(record, result)

        self.assertTrue(updated["reauth_required"])
        self.assertEqual(updated["health_status"], "needs_auth")
        self.assertEqual(updated["last_status"], "reauth_required")

    def test_site_connection_is_read_only_for_success_partial_and_auth(self):
        site = "https://connection-test.example"
        site_record = {
            "name": "connection-test",
            "site": site,
            "api_base": "/api/v1",
            "interval_minutes": 180,
            "include_groups": True,
            "auto_refresh": True,
        }
        baseline_row = {
            "site": site,
            "site_host": "connection-test.example",
            "status": "ok",
            "source": "/groups/available",
            "record_type": "group",
            "model_category": "OpenAI",
            "group_id": "baseline",
            "group_name": "Baseline",
            "rate_multiplier": 1,
            "fetched_at": "2026-07-01T00:00:00+00:00",
        }
        existing_change = {
            "id": "existing-change",
            "change_type": "rate_changed",
            "site": site,
            "site_key": site,
            "site_host": "connection-test.example",
            "item_label": "Baseline",
            "old_value": 1.1,
            "new_value": 1,
            "detected_at": "2026-07-01T00:00:00+00:00",
            "acknowledged": False,
        }
        app.write_saved_sites([site_record])
        app.write_price_snapshot(
            [baseline_row],
            {"site_count": 1, "success_count": 1, "error_count": 0},
        )
        app.write_price_change_baselines({site: [baseline_row]})
        app.write_price_changes([existing_change])

        api = self.new_api()
        tracked_paths = (
            app.saved_sites_path(),
            app.latest_prices_json_path(),
            app.latest_prices_csv_path(),
            app.price_change_baselines_path(),
            app.price_changes_path(),
        )

        def persistence_fingerprint():
            return {
                path.name: path.read_bytes() if path.exists() else None
                for path in tracked_paths
            }

        cases = (
            (
                "success",
                {
                    "ok": True,
                    "rows": [
                        {
                            **baseline_row,
                            "group_id": "live",
                            "group_name": "Live",
                            "rate_multiplier": 0.8,
                            "rate_source": "user_override",
                        },
                        {
                            "site": site,
                            "site_host": "connection-test.example",
                            "status": "ok",
                            "source": "/payment/plans",
                            "record_type": "plan",
                            "model_category": "OpenAI",
                            "plan_id": "starter",
                            "plan_name": "Starter",
                            "pay_price_cny": 10,
                        },
                    ],
                    "rateData": {
                        "complete": True,
                        "optionalUnavailable": False,
                        "partial": False,
                    },
                },
                {
                    "ok": True,
                    "healthy": True,
                    "partial": False,
                    "auth_required": False,
                    "plan_count": 1,
                    "group_count": 1,
                    "user_rate_count": 1,
                },
            ),
            (
                "partial",
                {
                    "ok": True,
                    "rows": [{
                        **baseline_row,
                        "status": "partial",
                        "error": "NETWORK_ERROR: /groups/rates unavailable",
                        "error_code": "network_error",
                        "rate_data_complete": False,
                    }],
                    "rateData": {
                        "complete": False,
                        "error": "NETWORK_ERROR: /groups/rates unavailable",
                        "errorCode": "network_error",
                        "optionalUnavailable": False,
                        "partial": True,
                    },
                },
                {
                    "ok": True,
                    "healthy": False,
                    "partial": True,
                    "auth_required": False,
                    "error_code": "network_error",
                },
            ),
            (
                "auth",
                {
                    "ok": True,
                    "rows": [{
                        **baseline_row,
                        "status": "partial",
                        "error": "REAUTH_REQUIRED: token expired",
                        "error_code": "reauth_required",
                        "rate_data_complete": False,
                    }],
                    "rateData": {
                        "complete": False,
                        "error": "REAUTH_REQUIRED: token expired",
                        "errorCode": "reauth_required",
                        "authRequired": True,
                        "optionalUnavailable": False,
                        "partial": True,
                    },
                },
                {
                    "ok": True,
                    "healthy": False,
                    "partial": True,
                    "auth_required": True,
                    "error_code": "reauth_required",
                },
            ),
        )

        for name, capture_result, expected in cases:
            with self.subTest(name=name):
                before_files = persistence_fingerprint()
                before_revision = api.state_revision
                before_rows = [dict(row) for row in api.rows]
                before_sites = [dict(record) for record in api.cached_saved_sites]
                with mock.patch.object(
                    api,
                    "_capture_site_webview",
                    return_value=capture_result,
                ):
                    result = api.test_site_connection(site, "/api/v1", True)

                for key, value in expected.items():
                    self.assertEqual(result[key], value, key)
                self.assertEqual(persistence_fingerprint(), before_files)
                self.assertEqual(api.state_revision, before_revision)
                self.assertEqual(api.rows, before_rows)
                self.assertEqual(api.cached_saved_sites, before_sites)
                self.assertFalse(api.connection_test_active.is_set())

    def test_site_connection_returns_busy_for_conflicting_operations(self):
        site = "https://connection-busy.example"
        app.write_saved_sites([{
            "name": "connection-busy",
            "site": site,
            "api_base": "/api/v1",
            "interval_minutes": 180,
        }])
        api = self.new_api()
        before_revision = api.state_revision

        active_thread = mock.Mock()
        active_thread.is_alive.return_value = True
        with mock.patch.object(api, "_capture_site_webview") as capture:
            with mock.patch.object(api, "update_thread", active_thread):
                update_result = api.test_site_connection(site)
            with mock.patch.object(api, "capture_job_thread", active_thread):
                capture_result = api.test_site_connection(site)
            api.login_session_active.set()
            try:
                login_result = api.test_site_connection(site)
            finally:
                api.login_session_active.clear()

        for result, message in (
            (update_result, "后台更新"),
            (capture_result, "WebView 抓取"),
            (login_result, "登录窗口"),
        ):
            self.assertFalse(result["ok"])
            self.assertTrue(result["busy"])
            self.assertIn(message, result["error"])
        capture.assert_not_called()
        self.assertEqual(api.state_revision, before_revision)
        self.assertFalse(api.connection_test_active.is_set())

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

    def test_stale_hide_does_not_hide_a_new_login_operation(self):
        api = self.new_api()
        browser = FakeBrowserWindow()
        api.attach_browser_window(browser)
        api.browser_operation_id = 2

        api._hide_browser_window(operation_id=1)

        self.assertEqual(browser.hide_count, 0)

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

    def test_login_window_does_not_open_during_background_update(self):
        api = self.new_api()
        browser = FakeBrowserWindow()
        api.attach_browser_window(browser)
        api.update_thread = mock.Mock()
        api.update_thread.is_alive.return_value = True

        result = api.open_site("https://new.example")

        self.assertFalse(result["ok"])
        self.assertTrue(result["update_running"])
        self.assertEqual(browser.load_urls, [])
        self.assertFalse(api.login_session_active.is_set())
        api.update_thread = None

    def test_background_update_waits_for_manual_login_action(self):
        api = self.new_api()
        api.login_session_active.set()

        result = api.start_update_all_prices("manual", False)

        self.assertTrue(result["ok"])
        self.assertTrue(result["busy"])
        self.assertTrue(result["login_active"])
        self.assertFalse(result["accepted"])

    def test_skip_login_clears_active_session(self):
        api = self.new_api()
        api.site = "https://current.example"
        browser = FakeBrowserWindow()
        api.attach_browser_window(browser)
        api.login_session_active.set()

        result = api.hide_login_webview()

        self.assertTrue(result["ok"])
        self.assertFalse(api.login_session_active.is_set())

    def test_refresh_login_webview_reloads_current_site(self):
        api = self.new_api()
        api.site = "https://current.example"
        browser = FakeBrowserWindow()
        api.attach_browser_window(browser)

        with mock.patch.object(api, "_start_credential_helper") as credential_helper:
            result = api.refresh_login_webview()

        self.assertTrue(result["ok"])
        self.assertEqual(browser.load_urls, ["https://current.example"])
        self.assertEqual(browser.show_count, 1)
        self.assertFalse(api.browser_cancel_event.is_set())
        credential_helper.assert_called_once_with("https://current.example")

    def test_refresh_login_webview_failure_clears_active_session(self):
        api = self.new_api()
        api.site = "https://refresh-failure.example"
        api.login_session_active.set()

        with mock.patch.object(
            api,
            "_ensure_browser_window",
            side_effect=RuntimeError("login window unavailable"),
        ):
            result = api.refresh_login_webview()

        self.assertFalse(result["ok"])
        self.assertIn("unavailable", result["error"])
        self.assertFalse(api.login_session_active.is_set())

    def test_hide_login_webview_cancels_without_waiting_for_helper(self):
        api = self.new_api()
        api.site = "https://current.example"
        browser = FakeBrowserWindow()
        api.attach_browser_window(browser)
        api.interactive_operation_lock.acquire()
        try:
            started = time.monotonic()
            result = api.hide_login_webview()
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            api.interactive_operation_lock.release()

        self.assertTrue(result["ok"])
        self.assertTrue(result["hidden"])
        self.assertTrue(api.browser_cancel_event.is_set())
        deadline = time.monotonic() + 1
        while browser.hide_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(browser.hide_count, 1)

    def test_interactive_capture_uses_a_daemon_job(self):
        api = self.new_api()
        api.site = "https://capture.example"
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

    def test_interactive_capture_rejects_background_update_and_new_login_site(self):
        api = self.new_api()
        api.site = "https://capture-running.example"
        entered_capture = threading.Event()
        release_capture = threading.Event()

        def blocked_capture(instance, api_base, include_groups, close_browser):
            entered_capture.set()
            release_capture.wait(2)
            return {"ok": False, "cancelled": True, "error": "cancelled by test"}

        api._capture_prices_sync = types.MethodType(blocked_capture, api)
        started = api.start_capture_prices("/api/v1", True, True)
        self.assertTrue(started["accepted"])
        self.assertTrue(entered_capture.wait(1))
        capture_thread = api.capture_job_thread

        update = api.start_update_all_prices("manual", False)
        opened = api.open_site("https://other.example")

        self.assertTrue(update["ok"])
        self.assertTrue(update["busy"])
        self.assertTrue(update["capture_active"])
        self.assertFalse(update["accepted"])
        self.assertFalse(opened["ok"])
        self.assertTrue(opened["busy"])
        self.assertTrue(opened["capture_running"])

        release_capture.set()
        capture_thread.join(2)
        self.assertFalse(capture_thread.is_alive())

    def test_hide_or_remote_cancel_discards_inflight_capture_result(self):
        for cancellation_mode in ("hide", "remote"):
            with self.subTest(cancellation_mode=cancellation_mode):
                site = f"https://cancel-{cancellation_mode}.example"
                record = {
                    "name": cancellation_mode,
                    "site": site,
                    "api_base": "/api/v1",
                    "interval_minutes": 180,
                    "last_status": "reauth_required",
                    "health_status": "needs_auth",
                    "reauth_required": True,
                    "consecutive_failures": 1,
                }
                baseline_rows = [{
                    "site": site,
                    "site_host": app.urlparse(site).netloc,
                    "status": "ok",
                    "source": "test",
                    "record_type": "group",
                    "model_category": "OpenAI",
                    "group_id": "old",
                    "group_name": "Old",
                    "rate_multiplier": 1,
                }]
                app.write_saved_sites([record])
                app.write_price_snapshot(
                    baseline_rows,
                    {"site_count": 1, "success_count": 1, "error_count": 0},
                )
                latest_before = app.latest_prices_json_path().read_bytes()
                sites_before = app.saved_sites_path().read_bytes()

                api = self.new_api()
                api.site = site
                api.browser_operation_id = 10
                api.login_session_active.set()
                browser = FakeCancellableBrowserWindow()
                api.attach_browser_window(browser)
                entered_evaluate = threading.Event()
                release_evaluate = threading.Event()

                def delayed_result(window, script, timeout=60):
                    entered_evaluate.set()
                    release_evaluate.wait(2)
                    return {
                        "rows": [{
                            "site": site,
                            "site_host": app.urlparse(site).netloc,
                            "status": "ok",
                            "source": "test",
                            "record_type": "group",
                            "model_category": "OpenAI",
                            "group_id": "new",
                            "group_name": "New",
                            "rate_multiplier": 2,
                        }],
                    }

                with mock.patch.object(
                    api,
                    "_prepare_login_credentials",
                    return_value={"loginForm": False},
                ), mock.patch.object(api, "_evaluate_async", side_effect=delayed_result):
                    started = api.start_capture_prices("/api/v1", True, True)
                    self.assertTrue(started["accepted"])
                    self.assertTrue(entered_evaluate.wait(1))
                    capture_thread = api.capture_job_thread
                    if cancellation_mode == "hide":
                        hidden = api.hide_login_webview()
                        self.assertTrue(hidden["ok"])
                    else:
                        browser.remote_cancelled = True
                    release_evaluate.set()
                    capture_thread.join(2)

                self.assertFalse(capture_thread.is_alive())
                status = api.capture_status(started["job_id"])
                self.assertEqual(status["status"], "cancelled")
                self.assertTrue(status["result"]["cancelled"])
                self.assertEqual(status["result"]["error_code"], "window_closed")
                self.assertEqual(app.latest_prices_json_path().read_bytes(), latest_before)
                self.assertEqual(app.saved_sites_path().read_bytes(), sites_before)
                self.assertEqual(app.load_latest_rows()[0]["group_id"], "old")
                self.assertTrue(app.load_saved_sites()[0]["reauth_required"])

    def test_stale_browser_operation_result_is_not_committed(self):
        site = "https://stale-operation.example"
        record = {
            "name": "stale",
            "site": site,
            "api_base": "/api/v1",
            "interval_minutes": 180,
            "last_status": "reauth_required",
            "health_status": "needs_auth",
            "reauth_required": True,
            "consecutive_failures": 1,
        }
        app.write_saved_sites([record])
        app.write_price_snapshot([], {
            "site_count": 0,
            "success_count": 0,
            "error_count": 0,
        })
        latest_before = app.latest_prices_json_path().read_bytes()
        sites_before = app.saved_sites_path().read_bytes()
        api = self.new_api()
        api.site = site
        api.browser_operation_id = 20
        browser = FakeCancellableBrowserWindow()
        api.attach_browser_window(browser)
        entered_evaluate = threading.Event()
        release_evaluate = threading.Event()

        def delayed_result(window, script, timeout=60):
            entered_evaluate.set()
            release_evaluate.wait(2)
            return {
                "rows": [{
                    "site": site,
                    "site_host": app.urlparse(site).netloc,
                    "status": "ok",
                    "source": "test",
                    "record_type": "group",
                    "model_category": "OpenAI",
                    "group_id": "stale-result",
                    "group_name": "Stale result",
                    "rate_multiplier": 1,
                }],
            }

        with mock.patch.object(
            api,
            "_prepare_login_credentials",
            return_value={"loginForm": False},
        ), mock.patch.object(api, "_evaluate_async", side_effect=delayed_result):
            started = api.start_capture_prices("/api/v1", True, True)
            self.assertTrue(started["accepted"])
            self.assertTrue(entered_evaluate.wait(1))
            capture_thread = api.capture_job_thread
            api.browser_operation_id = 21
            release_evaluate.set()
            capture_thread.join(2)

        self.assertFalse(capture_thread.is_alive())
        status = api.capture_status(started["job_id"])
        self.assertEqual(status["status"], "cancelled")
        self.assertTrue(status["result"]["cancelled"])
        self.assertEqual(app.latest_prices_json_path().read_bytes(), latest_before)
        self.assertEqual(app.saved_sites_path().read_bytes(), sites_before)

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

    def test_refresh_replaces_all_old_rows_for_the_updated_site(self):
        previous = [
            {"site": "https://a.example", "site_host": "a.example", "record_type": "group", "group_id": "old"},
            {"site": "https://a.example", "site_host": "a.example", "record_type": "plan", "plan_id": "gone"},
            {"site": "https://b.example", "site_host": "b.example", "record_type": "group", "group_id": "keep"},
        ]
        fresh = [
            {"site": "https://a.example", "site_host": "a.example", "record_type": "group", "group_id": "current"},
        ]

        rows = app.replace_price_rows(previous, fresh, ["https://a.example"])

        self.assertEqual(
            {(row["site_host"], row.get("group_id") or row.get("plan_id")) for row in rows},
            {("a.example", "current"), ("b.example", "keep")},
        )

    def test_failed_refresh_replaces_stale_prices_with_current_error(self):
        previous = [{
            "site": "https://a.example",
            "site_host": "a.example",
            "record_type": "group",
            "group_id": "stale-price",
            "rate_multiplier": 0.1,
        }]
        fresh = [{
            "site": "https://a.example",
            "site_host": "a.example",
            "record_type": "error",
            "error": "current failure",
        }]

        rows = app.replace_price_rows(previous, fresh, ["https://a.example"])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["record_type"], "error")
        self.assertEqual(rows[0]["error"], "current failure")

    def test_detect_price_changes_covers_price_rate_details_add_and_remove(self):
        previous = [
            {
                "site": "https://a.example",
                "site_host": "a.example",
                "record_type": "group",
                "model_category": "OpenAI",
                "group_id": "rate",
                "group_name": "rate",
                "rate_multiplier": 1,
            },
            {
                "site": "https://a.example",
                "site_host": "a.example",
                "record_type": "plan",
                "model_category": "OpenAI",
                "plan_id": "price",
                "plan_name": "price",
                "price": 10,
            },
            {
                "site": "https://a.example",
                "site_host": "a.example",
                "record_type": "group",
                "model_category": "OpenAI",
                "group_id": "details",
                "group_name": "details",
                "description": "old description",
                "validity_days": 30,
            },
            {
                "site": "https://a.example",
                "site_host": "a.example",
                "record_type": "group",
                "model_category": "OpenAI",
                "group_id": "removed",
                "group_name": "removed",
            },
        ]
        current = [
            {
                **previous[0],
                "rate_multiplier": 1.25,
            },
            {
                **previous[1],
                "price": 8,
            },
            {
                **previous[2],
                "description": "new description",
                "validity_days": 60,
            },
            {
                "site": "https://a.example",
                "site_host": "a.example",
                "record_type": "group",
                "model_category": "OpenAI",
                "group_id": "added",
                "group_name": "added",
            },
        ]

        changes = app.detect_price_changes(previous, current)
        by_type = {change["change_type"]: change for change in changes}

        self.assertTrue({
            "rate_changed",
            "price_changed",
            "details_changed",
            "added",
            "removed",
        }.issubset(by_type))
        self.assertEqual(by_type["rate_changed"]["change_percent"], 25.0)
        self.assertEqual(by_type["price_changed"]["change_percent"], -20.0)
        self.assertEqual(
            by_type["details_changed"]["old_value"]["description"],
            "old description",
        )
        self.assertEqual(
            by_type["details_changed"]["new_value"]["description"],
            "new description",
        )
        self.assertEqual(by_type["details_changed"]["old_value"]["validity_days"], 30)
        self.assertEqual(by_type["details_changed"]["new_value"]["validity_days"], 60)

    def test_zero_boundary_price_and_rate_changes_are_high_severity(self):
        cases = (
            ("rate_changed", "rate_multiplier", "group", 0, 1, "increase"),
            ("rate_changed", "rate_multiplier", "group", 1, 0, "decrease"),
            ("price_changed", "price", "plan", 0, 10, "increase"),
            ("price_changed", "price", "plan", 10, 0, "decrease"),
        )
        for change_type, field, record_type, old_value, new_value, direction in cases:
            with self.subTest(
                change_type=change_type,
                old_value=old_value,
                new_value=new_value,
            ):
                row = {
                    "site": "https://a.example",
                    "site_host": "a.example",
                    "record_type": record_type,
                    "model_category": "OpenAI",
                    "group_id": "primary",
                    "group_name": "primary",
                    field: old_value,
                }
                if record_type == "plan":
                    row.update({"plan_id": "monthly", "plan_name": "Monthly"})

                changes = app.detect_price_changes(
                    [row],
                    [{**row, field: new_value}],
                )
                change = next(
                    item for item in changes if item["change_type"] == change_type
                )

                self.assertEqual(change["source_field"], field)
                self.assertEqual(change["old_value"], old_value)
                self.assertEqual(change["new_value"], new_value)
                self.assertEqual(change["direction"], direction)
                self.assertEqual(change["severity"], "high")

    def test_first_snapshot_establishes_baseline_without_changes(self):
        rows = [{
            "site": "https://a.example",
            "site_host": "a.example",
            "record_type": "group",
            "model_category": "OpenAI",
            "group_id": "baseline",
            "group_name": "baseline",
            "rate_multiplier": 1,
        }]

        snapshot = app.write_price_snapshot(rows, {"site_count": 1})

        self.assertEqual(snapshot["changes"], [])
        self.assertEqual(app.load_price_changes(), [])

    def test_recovery_compares_with_last_successful_baseline(self):
        healthy = [{
            "site": "https://a.example",
            "site_host": "a.example",
            "record_type": "group",
            "model_category": "OpenAI",
            "group_id": "primary",
            "group_name": "primary",
            "rate_multiplier": 1,
        }]
        failed = [{
            "site": "https://a.example",
            "site_host": "a.example",
            "record_type": "error",
            "model_category": "未获取",
            "error": "temporary failure",
            "status_label": "网络错误",
        }]
        recovered = [{**healthy[0], "rate_multiplier": 1.5}]

        app.write_price_snapshot(healthy, {"site_count": 1, "success_count": 1})
        failed_snapshot = app.write_price_snapshot(
            failed,
            {"site_count": 1, "success_count": 0, "error_count": 1},
        )
        recovered_snapshot = app.write_price_snapshot(
            recovered,
            {"site_count": 1, "success_count": 1, "error_count": 0},
        )

        self.assertIn("site_error", {
            change["change_type"] for change in failed_snapshot["changes"]
        })
        recovered_types = {
            change["change_type"] for change in recovered_snapshot["changes"]
        }
        self.assertIn("rate_changed", recovered_types)
        self.assertNotIn("added", recovered_types)

    def test_incomplete_user_rate_keeps_last_verified_rate_baseline(self):
        verified = {
            "site": "https://a.example",
            "site_host": "a.example",
            "record_type": "group",
            "model_category": "OpenAI",
            "group_id": "primary",
            "group_name": "primary",
            "rate_multiplier": 0.8,
            "base_rate_multiplier": 1.0,
            "user_rate_multiplier": 0.8,
            "rate_source": "user_override",
            "rate_data_complete": True,
        }
        incomplete = {
            **verified,
            "rate_multiplier": 1.0,
            "user_rate_multiplier": None,
            "rate_source": "base",
            "rate_data_complete": False,
        }
        recovered = {
            **verified,
            "rate_multiplier": 0.9,
            "user_rate_multiplier": 0.9,
        }

        app.write_price_snapshot([verified], {"site_count": 1, "success_count": 1})
        incomplete_snapshot = app.write_price_snapshot(
            [incomplete],
            {"site_count": 1, "success_count": 1},
        )

        self.assertNotIn(
            "rate_changed",
            {change["change_type"] for change in incomplete_snapshot["changes"]},
        )
        baseline = app.load_price_change_baselines()["https://a.example"][0]
        self.assertEqual(baseline["rate_multiplier"], 0.8)
        self.assertEqual(baseline["user_rate_multiplier"], 0.8)
        self.assertEqual(baseline["rate_source"], "user_override")
        self.assertTrue(baseline["rate_data_complete"])

        recovered_snapshot = app.write_price_snapshot(
            [recovered],
            {"site_count": 1, "success_count": 1},
        )
        rate_change = next(
            change
            for change in recovered_snapshot["changes"]
            if change["change_type"] == "rate_changed"
        )
        self.assertEqual(rate_change["old_value"], 0.8)
        self.assertEqual(rate_change["new_value"], 0.9)
        self.assertEqual(rate_change["change_percent"], 12.5)

    def test_incomplete_plan_user_rate_keeps_last_verified_rate_baseline(self):
        verified = {
            "site": "https://a.example",
            "site_host": "a.example",
            "record_type": "plan",
            "model_category": "OpenAI",
            "group_id": "primary",
            "group_name": "primary",
            "plan_id": "monthly",
            "plan_name": "Monthly",
            "rate_multiplier": 0.8,
            "base_rate_multiplier": 1.0,
            "user_rate_multiplier": 0.8,
            "rate_source": "user_override",
            "rate_data_complete": True,
        }
        incomplete = {
            **verified,
            "rate_multiplier": 1.0,
            "user_rate_multiplier": "",
            "rate_source": "base_fallback_unverified",
            "rate_data_complete": False,
        }
        recovered = {
            **verified,
            "rate_multiplier": 0.9,
            "user_rate_multiplier": 0.9,
        }

        app.write_price_snapshot([verified], {"site_count": 1, "success_count": 1})
        incomplete_snapshot = app.write_price_snapshot(
            [incomplete],
            {"site_count": 1, "success_count": 1},
        )

        self.assertNotIn(
            "rate_changed",
            {change["change_type"] for change in incomplete_snapshot["changes"]},
        )
        baseline = app.load_price_change_baselines()["https://a.example"][0]
        self.assertEqual(baseline["rate_multiplier"], 0.8)
        self.assertEqual(baseline["user_rate_multiplier"], 0.8)
        self.assertEqual(baseline["rate_source"], "user_override")
        self.assertTrue(baseline["rate_data_complete"])

        recovered_snapshot = app.write_price_snapshot(
            [recovered],
            {"site_count": 1, "success_count": 1},
        )
        rate_change = next(
            change
            for change in recovered_snapshot["changes"]
            if change["change_type"] == "rate_changed"
        )
        self.assertEqual(rate_change["old_value"], 0.8)
        self.assertEqual(rate_change["new_value"], 0.9)
        self.assertEqual(rate_change["change_percent"], 12.5)

    def test_group_metadata_changes_emit_specific_events(self):
        previous = [{
            "site": "https://a.example",
            "site_host": "a.example",
            "record_type": "group",
            "model_category": "OpenAI",
            "group_id": "primary",
            "group_name": "primary",
            "group_platform": "openai",
            "group_status": "active",
            "is_exclusive": False,
            "subscription_type": "standard",
            "rpm_limit": 60,
        }]
        current = [{
            **previous[0],
            "group_platform": "anthropic",
            "group_status": "disabled",
            "is_exclusive": True,
            "subscription_type": "premium",
            "rpm_limit": 120,
        }]

        changes = app.detect_price_changes(previous, current)
        by_type = {change["change_type"]: change for change in changes}
        expected = {
            "status_changed": {
                "source_field": "group_status",
                "old_value": "active",
                "new_value": "disabled",
                "direction": "changed",
                "severity": "medium",
            },
            "is_exclusive_changed": {
                "source_field": "is_exclusive",
                "old_value": False,
                "new_value": True,
                "direction": "changed",
                "severity": "medium",
            },
            "subscription_type_changed": {
                "source_field": "subscription_type",
                "old_value": "standard",
                "new_value": "premium",
                "direction": "changed",
                "severity": "low",
            },
            "rpm_limit_changed": {
                "source_field": "rpm_limit",
                "old_value": 60,
                "new_value": 120,
                "direction": "increase",
                "severity": "medium",
            },
            "platform_changed": {
                "source_field": "group_platform",
                "old_value": "openai",
                "new_value": "anthropic",
                "direction": "changed",
                "severity": "low",
            },
        }

        self.assertTrue(expected.keys() <= by_type.keys())
        self.assertNotIn("details_changed", by_type)
        for change_type, fields in expected.items():
            with self.subTest(change_type=change_type):
                change = by_type[change_type]
                for field, value in fields.items():
                    self.assertEqual(change[field], value)
        self.assertEqual(by_type["rpm_limit_changed"]["change_percent"], 100.0)

    def test_site_health_tracks_consecutive_failures_auth_and_recovery(self):
        api = self.new_api()
        healthy = {
            "name": "primary",
            "site": "https://a.example",
            "api_base": "/api/v1",
            "interval_minutes": 180,
            "auto_refresh": True,
            "last_run": "2026-07-01T00:00:00+00:00",
            "last_status": "ok",
            "health_status": "ok",
            "consecutive_failures": 0,
        }
        failure = {
            "ok": False,
            "error": "temporary failure",
            "error_code": "network_error",
            "status_label": "network error",
            "rows": [{
                "site": healthy["site"],
                "record_type": "error",
                "error": "temporary failure",
            }],
        }

        first = api._site_status_record(healthy, failure)
        second = api._site_status_record(first, failure)
        third = api._site_status_record(second, failure)

        self.assertEqual((first["consecutive_failures"], first["health_status"]), (1, "warning"))
        self.assertEqual((second["consecutive_failures"], second["health_status"]), (2, "warning"))
        self.assertEqual((third["consecutive_failures"], third["health_status"]), (3, "failed"))
        self.assertIsNone(app.site_status_transition_change(healthy, first))
        self.assertIsNone(app.site_status_transition_change(first, second))
        unhealthy = app.site_status_transition_change(second, third)
        self.assertEqual(unhealthy["change_type"], "site_unhealthy")
        self.assertEqual(unhealthy["direction"], "degraded")
        self.assertEqual(unhealthy["severity"], "high")

        needs_auth = api._site_status_record(healthy, {
            **failure,
            "auth_required": True,
            "error_code": "reauth_required",
        })
        auth_change = app.site_status_transition_change(healthy, needs_auth)
        self.assertEqual(needs_auth["health_status"], "needs_auth")
        self.assertTrue(needs_auth["reauth_required"])
        self.assertEqual(auth_change["new_value"], "needs_auth")

        recovered = api._site_status_record(third, {
            "ok": True,
            "rows": [{
                "site": healthy["site"],
                "record_type": "group",
                "group_id": "primary",
                "rate_multiplier": 1,
            }],
        })
        recovery = app.site_status_transition_change(third, recovered)
        self.assertEqual(recovered["consecutive_failures"], 0)
        self.assertEqual(recovered["health_status"], "ok")
        self.assertEqual(recovered["last_error"], "")
        self.assertEqual(recovery["change_type"], "site_recovered")
        self.assertEqual(recovery["direction"], "recovered")
        self.assertEqual(recovery["severity"], "low")

    def test_site_status_separates_core_and_enrichment_and_caps_history(self):
        api = self.new_api()
        site = "https://partial.example"
        record = {
            "name": "partial",
            "site": site,
            "api_base": "/api/v1",
            "interval_minutes": 180,
            "check_history": [
                {"checked_at": f"2026-07-01T00:{index:02d}:00+00:00", "status": "ok"}
                for index in range(app.SITE_CHECK_HISTORY_LIMIT)
            ],
        }
        updated = api._site_status_record(record, {
            "ok": True,
            "rows": [{
                "site": site,
                "record_type": "plan",
                "status": "partial",
                "error": "checkout fallback",
                "error_code": "http_error",
            }],
            "rateData": {
                "complete": False,
                "partial": True,
                "error": "rates unavailable",
                "errorCode": "http_error",
            },
        })

        self.assertEqual(updated["last_core_status"], "partial")
        self.assertEqual(updated["last_enrichment_status"], "degraded")
        self.assertEqual(updated["last_enrichment_error"], "rates unavailable")
        self.assertEqual(len(updated["check_history"]), app.SITE_CHECK_HISTORY_LIMIT)
        self.assertEqual(updated["check_history"][0]["status"], "partial")

    def test_first_error_snapshot_records_site_error(self):
        snapshot = app.write_price_snapshot([{
            "site": "https://a.example",
            "site_host": "a.example",
            "record_type": "error",
            "error": "initial failure",
            "status_label": "network error",
        }], {
            "site_count": 1,
            "success_count": 0,
            "error_count": 1,
        })

        error_change = next(
            change for change in snapshot["changes"]
            if change["change_type"] == "site_error"
        )
        self.assertIsNone(error_change["old_value"])
        self.assertEqual(error_change["new_value"], "initial failure")
        self.assertEqual(error_change["source_field"], "site_health")
        self.assertEqual(error_change["direction"], "degraded")
        self.assertEqual(error_change["severity"], "high")

    def test_acknowledged_changes_are_persisted(self):
        app.write_price_changes([
            {
                "id": "one",
                "change_type": "rate_changed",
                "acknowledged": False,
            },
            {
                "id": "two",
                "change_type": "added",
                "acknowledged": False,
            },
        ])

        app.acknowledge_price_changes(["one"])
        persisted = {change["id"]: change for change in app.load_price_changes()}

        self.assertTrue(persisted["one"]["acknowledged"])
        self.assertFalse(persisted["two"]["acknowledged"])

    def test_auto_refresh_false_survives_save_reload_and_status_update(self):
        api = self.new_api()
        result = api.save_site(
            "disabled",
            "https://disabled.example",
            auto_refresh=False,
        )

        self.assertTrue(result["ok"])
        saved = app.load_saved_sites()[0]
        self.assertFalse(saved["auto_refresh"])

        annotated = api._update_site_status(saved, {"ok": True, "rows": []})
        reloaded = app.load_saved_sites()[0]
        self.assertFalse(reloaded["auto_refresh"])
        self.assertFalse(annotated[0]["auto_refresh"])

        api._cache_state(saved_sites=annotated)
        state = api.initial_state()
        self.assertFalse(state["saved_sites"][0]["auto_refresh"])

    def test_automatic_updates_skip_disabled_sites_but_manual_update_includes_them(self):
        records = [
            {
                "name": "enabled",
                "site": "https://enabled.example",
                "api_base": "/api/v1",
                "interval_minutes": 180,
                "next_run": "2000-01-01T00:00:00+00:00",
                "auto_refresh": True,
            },
            {
                "name": "disabled",
                "site": "https://disabled.example",
                "api_base": "/api/v1",
                "interval_minutes": 180,
                "next_run": "2000-01-01T00:00:00+00:00",
                "auto_refresh": False,
            },
        ]

        def run_update(reason, only_due):
            app.write_saved_sites(records)
            api = self.new_api()
            captured = []

            def successful_capture(instance, record, include_groups=True, worker_slot=0):
                captured.append(record["site"])
                return {
                    "ok": True,
                    "rows": [{
                        "site": record["site"],
                        "site_host": app.urlparse(record["site"]).netloc,
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
            result = api.start_update_all_prices(reason, only_due)
            self.assertTrue(result["accepted"])
            deadline = time.monotonic() + 3
            while api.update_thread and api.update_thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(api.update_thread and api.update_thread.is_alive())
            self.assertEqual(api.update_job["status"], "completed")
            return set(captured)

        enabled = {"https://enabled.example"}
        both = {"https://enabled.example", "https://disabled.example"}
        self.assertEqual(run_update("startup", False), enabled)
        self.assertEqual(run_update("scheduler", True), enabled)
        self.assertEqual(run_update("manual", False), both)

    def test_include_groups_is_persisted_and_defaults_to_true_for_legacy_sites(self):
        api = self.new_api()
        with mock.patch.object(app, "has_site_credentials", return_value=False):
            result = api.save_site(
                name="plans-only",
                site="https://plans-only.example",
                api_base="/api/v1",
                interval_hours=3,
                remember_credentials=True,
                auto_login=True,
                auto_refresh=True,
                include_groups=False,
            )

        self.assertTrue(result["ok"])
        saved = app.load_saved_sites()[0]
        self.assertFalse(saved["include_groups"])
        self.assertFalse(app.normalize_saved_site_record(saved)["include_groups"])

        legacy = app.normalize_saved_site_record({
            "name": "legacy",
            "site": "https://legacy-groups.example",
            "interval_minutes": 180,
        })
        self.assertTrue(legacy["include_groups"])

    def test_background_capture_honors_saved_include_groups_setting(self):
        record = {
            "name": "plans-only",
            "site": "https://plans-only.example",
            "api_base": "/api/v1",
            "interval_minutes": 180,
            "auto_refresh": True,
            "include_groups": False,
        }
        app.write_saved_sites([record])
        api = self.new_api()
        captured_include_groups = []

        def successful_capture(instance, saved, include_groups=True, worker_slot=0):
            captured_include_groups.append(include_groups)
            return {
                "ok": True,
                "rows": [{
                    "site": saved["site"],
                    "site_host": app.urlparse(saved["site"]).netloc,
                    "status": "ok",
                    "source": "test",
                    "record_type": "plan",
                    "model_category": "OpenAI",
                    "group_id": "primary",
                    "plan_id": "monthly",
                    "plan_name": "Monthly",
                    "rate_multiplier": 1,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }],
            }

        api._capture_site_webview = types.MethodType(successful_capture, api)
        with mock.patch.object(app, "has_site_credentials", return_value=False):
            result = api._run_site_records(app.load_saved_sites(), only_due=False)

        self.assertEqual(result["summary"]["success_count"], 1)
        self.assertEqual(captured_include_groups, [False])

    def test_start_site_check_targets_only_requested_disabled_site(self):
        target = "https://disabled.example"
        app.write_saved_sites([
            {
                "name": "enabled",
                "site": "https://enabled.example",
                "api_base": "/api/v1",
                "interval_minutes": 180,
                "auto_refresh": True,
            },
            {
                "name": "disabled",
                "site": target,
                "api_base": "/api/v1",
                "interval_minutes": 180,
                "auto_refresh": False,
            },
        ])
        api = self.new_api()
        captured = []

        def successful_capture(instance, record, include_groups=True, worker_slot=0):
            captured.append(record["site"])
            return {
                "ok": True,
                "rows": [{
                    "site": record["site"],
                    "site_host": app.urlparse(record["site"]).netloc,
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
        result = api.start_site_check(target)
        self.assertTrue(result["accepted"])
        deadline = time.monotonic() + 3
        while api.update_thread and api.update_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertFalse(api.update_thread and api.update_thread.is_alive())
        self.assertEqual(captured, [target])
        self.assertEqual(api.update_job["status"], "completed")
        self.assertEqual(api.update_job["requested_site"], target)
        self.assertEqual(api.update_job["summary"]["site_count"], 1)

    def test_delete_site_removes_saved_rows_changes_and_baseline(self):
        removed_site = "https://remove.example"
        kept_site = "https://keep.example"
        app.write_saved_sites([
            {"name": "remove", "site": removed_site, "interval_minutes": 180},
            {"name": "keep", "site": kept_site, "interval_minutes": 180},
        ])
        app.write_price_snapshot([
            {
                "site": removed_site,
                "site_host": "remove.example",
                "record_type": "group",
                "group_id": "remove",
                "rate_multiplier": 1,
            },
            {
                "site": kept_site,
                "site_host": "keep.example",
                "record_type": "group",
                "group_id": "keep",
                "rate_multiplier": 1,
            },
        ], {"site_count": 2, "success_count": 2})
        app.write_price_changes([
            {"id": "remove-change", "site": removed_site, "change_type": "rate_changed"},
            {"id": "keep-change", "site": kept_site, "change_type": "rate_changed"},
        ])
        api = self.new_api()

        with mock.patch.object(app, "delete_site_credentials", return_value=True):
            result = api.delete_site(removed_site)

        self.assertTrue(result["ok"])
        self.assertEqual([site["site"] for site in app.load_saved_sites()], [kept_site])
        self.assertEqual([row["site"] for row in app.load_latest_rows()], [kept_site])
        self.assertEqual([change["id"] for change in app.load_price_changes()], ["keep-change"])
        self.assertNotIn(removed_site, app.load_price_change_baselines())

    def test_delete_site_discards_an_inflight_capture_result(self):
        removed_site = "https://delete-during-update.example"
        app.write_saved_sites([{
            "name": "remove",
            "site": removed_site,
            "api_base": "/api/v1",
            "interval_minutes": 180,
            "auto_refresh": True,
        }])
        api = self.new_api()
        capture_started = threading.Event()
        release_capture = threading.Event()

        def delayed_capture(instance, record, include_groups=True, worker_slot=0):
            capture_started.set()
            release_capture.wait(3)
            return {
                "ok": True,
                "rows": [{
                    "site": removed_site,
                    "site_host": "delete-during-update.example",
                    "record_type": "group",
                    "group_id": "primary",
                    "group_name": "primary",
                    "rate_multiplier": 1,
                    "rate_data_complete": True,
                }],
            }

        api._capture_site_webview = types.MethodType(delayed_capture, api)
        started = api.start_site_check(removed_site)
        self.assertTrue(started["accepted"])
        self.assertTrue(capture_started.wait(2))
        try:
            with mock.patch.object(app, "delete_site_credentials", return_value=True):
                deleted = api.delete_site(removed_site)
            self.assertTrue(deleted["ok"])
        finally:
            release_capture.set()

        deadline = time.monotonic() + 4
        while api.update_thread and api.update_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertFalse(api.update_thread and api.update_thread.is_alive())
        self.assertEqual(app.load_saved_sites(), [])
        self.assertNotIn(removed_site, {
            app._change_site_key(row) for row in app.load_latest_rows()
        })
        self.assertNotIn(removed_site, app.load_price_change_baselines())
        self.assertNotIn(removed_site, {
            app._change_site_key(change) for change in app.load_price_changes()
        })

    def test_delete_site_discards_an_inflight_interactive_capture_result(self):
        removed_site = "https://delete-during-interactive.example"
        app.write_saved_sites([{
            "name": "remove",
            "site": removed_site,
            "api_base": "/api/v1",
            "interval_minutes": 180,
            "auto_refresh": True,
        }])
        api = self.new_api()
        browser = FakeCancellableBrowserWindow()
        api.attach_browser_window(browser)
        api.site = removed_site
        capture_started = threading.Event()
        release_capture = threading.Event()
        result_holder = {}

        def delayed_evaluate(window, script, timeout):
            capture_started.set()
            release_capture.wait(3)
            return {
                "ok": True,
                "rows": [{
                    "site": removed_site,
                    "site_host": "delete-during-interactive.example",
                    "record_type": "group",
                    "group_id": "primary",
                    "group_name": "primary",
                    "rate_multiplier": 1,
                    "rate_data_complete": True,
                }],
            }

        def capture_worker():
            result_holder["result"] = api._capture_prices_sync(close_browser=True)

        with (
            mock.patch.object(api, "_prepare_login_credentials", return_value={}),
            mock.patch.object(api, "_evaluate_async", side_effect=delayed_evaluate),
            mock.patch.object(app, "delete_site_credentials", return_value=True),
        ):
            worker = threading.Thread(target=capture_worker, daemon=True)
            worker.start()
            self.assertTrue(capture_started.wait(2))
            deleted = api.delete_site(removed_site)
            self.assertTrue(deleted["ok"])
            release_capture.set()
            worker.join(4)

        self.assertFalse(worker.is_alive())
        result = result_holder["result"]
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["discarded"])
        self.assertEqual(app.load_saved_sites(), [])
        self.assertNotIn(removed_site, {
            app._change_site_key(row) for row in app.load_latest_rows()
        })
        self.assertNotIn(removed_site, app.load_price_change_baselines())
        for history_file in app.price_history_dir().glob("prices-*.json"):
            payload = json.loads(history_file.read_text(encoding="utf-8"))
            self.assertNotIn(removed_site, {
                app._change_site_key(row)
                for row in payload.get("rows") or []
                if isinstance(row, dict)
            })

    def test_browser_host_late_credential_save_respects_persisted_opt_out(self):
        site = "https://credential-opt-out.example"
        app.write_saved_sites([{
            "name": "credentials",
            "site": site,
            "remember_credentials": True,
            "auto_login": True,
        }])
        api = self.new_api()
        bridge = app.BrowserHostCredentialBridge()
        bridge.site = site

        with mock.patch.object(app, "delete_site_credentials", return_value=True):
            cleared = api.clear_credentials(site)

        self.assertTrue(cleared["ok"])
        persisted = app.load_saved_sites()[0]
        self.assertFalse(persisted["remember_credentials"])
        self.assertFalse(persisted["auto_login"])
        with mock.patch.object(app, "write_site_credentials") as write_credentials:
            ignored = bridge.save_credentials("user@example.com", "late-secret")
        self.assertTrue(ignored["ok"])
        self.assertTrue(ignored["ignored"])
        write_credentials.assert_not_called()

        with (
            mock.patch.object(
                app,
                "load_saved_sites",
                side_effect=[
                    [{"site": site, "remember_credentials": True}],
                    [],
                ],
            ),
            mock.patch.object(app, "write_site_credentials") as late_write,
            mock.patch.object(app, "delete_site_credentials", return_value=True) as rollback,
        ):
            raced = bridge.save_credentials("user@example.com", "raced-secret")

        self.assertTrue(raced["ok"])
        self.assertTrue(raced["ignored"])
        late_write.assert_called_once_with(site, "user@example.com", "raced-secret")
        rollback.assert_called_once_with(site)

    def test_credential_opt_out_is_persisted_before_secret_deletion(self):
        site = "https://credential-order.example"
        base_record = {
            "name": "credentials",
            "site": site,
            "api_base": "/api/v1",
            "interval_minutes": 180,
            "remember_credentials": True,
            "auto_login": True,
        }
        app.write_saved_sites([base_record])
        api = self.new_api()
        observed_states = []

        def delete_after_persistence(target):
            record = next(
                (item for item in app.load_saved_sites() if item.get("site") == target),
                None,
            )
            observed_states.append(
                None if record is None else bool(record.get("remember_credentials", True))
            )
            return True

        with (
            mock.patch.object(app, "delete_site_credentials", side_effect=delete_after_persistence),
            mock.patch.object(app, "has_site_credentials", return_value=False),
        ):
            saved = api.save_site(
                "credentials",
                site,
                remember_credentials=False,
                auto_login=True,
            )
            self.assertTrue(saved["ok"])

            app.write_saved_sites([base_record])
            cleared = api.clear_credentials(site)
            self.assertTrue(cleared["ok"])

            app.write_saved_sites([base_record])
            deleted = api.delete_site(site)
            self.assertTrue(deleted["ok"])

        self.assertEqual(observed_states, [False, False, None])

    def test_delete_site_removes_it_from_retained_history_snapshots(self):
        removed_site = "https://remove-history.example"
        kept_site = "https://keep-history.example"
        app.write_saved_sites([
            {"name": "remove", "site": removed_site, "interval_minutes": 180},
            {"name": "keep", "site": kept_site, "interval_minutes": 180},
        ])
        rows = [
            {
                "site": removed_site,
                "site_host": "remove-history.example",
                "record_type": "group",
                "group_id": "remove",
                "rate_multiplier": 1,
            },
            {
                "site": kept_site,
                "site_host": "keep-history.example",
                "record_type": "group",
                "group_id": "keep",
                "rate_multiplier": 1,
            },
        ]
        app.write_price_snapshot(rows, {"site_count": 2, "success_count": 2})
        time.sleep(0.002)
        app.write_price_snapshot(
            [{**rows[0], "rate_multiplier": 1.2}, rows[1]],
            {"site_count": 2, "success_count": 2},
        )

        with mock.patch.object(app, "delete_site_credentials", return_value=True):
            result = self.new_api().delete_site(removed_site)

        self.assertTrue(result["ok"])
        history_files = sorted(app.price_history_dir().glob("prices-*.json"))
        self.assertTrue(history_files)
        for history_file in history_files:
            with self.subTest(history_file=history_file.name):
                payload = json.loads(history_file.read_text(encoding="utf-8"))
                self.assertNotIn(removed_site, {
                    app._change_site_key(row)
                    for row in payload.get("rows") or []
                    if isinstance(row, dict)
                })
                self.assertNotIn(removed_site, {
                    app._change_site_key(change)
                    for change in payload.get("changes") or []
                    if isinstance(change, dict)
                })

    def test_smtp_settings_store_password_only_through_credential_helper(self):
        api = self.new_api()
        with (
            mock.patch.object(app, "write_smtp_password", return_value=True) as write_password,
            mock.patch.object(app, "has_smtp_password", return_value=True),
        ):
            result = api.save_smtp_settings(
                enabled=True,
                host="smtp.example.com",
                port=587,
                security="starttls",
                username="sender@example.com",
                from_address="sender@example.com",
                recipients="one@example.com; two@example.com",
                password="smtp-secret-value",
            )

        self.assertTrue(result["ok"])
        write_password.assert_called_once()
        self.assertEqual(write_password.call_args.args[:2], (
            "smtp-secret-value",
            "sender@example.com",
        ))
        self.assertEqual(
            app.smtp_credential_identity(write_password.call_args.kwargs["settings"]),
            {
                "host": "smtp.example.com",
                "port": 587,
                "security": "starttls",
                "username": "sender@example.com",
            },
        )
        raw_settings = app.notification_settings_path().read_text(encoding="utf-8")
        self.assertNotIn("smtp-secret-value", raw_settings)
        self.assertNotIn('"password"', raw_settings)
        self.assertNotIn("password", result["smtp"])
        self.assertTrue(result["smtp"]["has_password"])

    def test_smtp_identity_change_requires_a_new_matching_password(self):
        old_settings = {
            **app.default_smtp_settings(),
            "enabled": True,
            "host": "old.smtp.example",
            "port": 587,
            "security": "starttls",
            "username": "old-sender@example.com",
            "from_address": "old-sender@example.com",
            "recipients": ["receiver@example.com"],
        }
        app.write_smtp_settings(old_settings)
        api = self.new_api()
        stored_credential = {
            "password": "old-secret",
            "host": old_settings["host"],
            "port": old_settings["port"],
            "security": old_settings["security"],
            "username": old_settings["username"],
        }

        with (
            mock.patch.object(app, "read_credential_payload", return_value=stored_credential),
            mock.patch.object(app, "write_smtp_password") as write_password,
        ):
            result = api.save_smtp_settings(
                enabled=True,
                host="new.smtp.example",
                port=587,
                security="starttls",
                username="new-sender@example.com",
                from_address="new-sender@example.com",
                recipients="receiver@example.com",
                password="",
            )

        self.assertFalse(result["ok"])
        write_password.assert_not_called()
        persisted = app.load_smtp_settings()
        self.assertEqual(persisted["host"], old_settings["host"])
        self.assertEqual(persisted["username"], old_settings["username"])

    def test_smtp_save_disables_candidate_when_written_password_does_not_match(self):
        api = self.new_api()
        with (
            mock.patch.object(app, "write_smtp_password", return_value=True),
            mock.patch.object(app, "has_smtp_password", return_value=False),
        ):
            result = api.save_smtp_settings(
                enabled=True,
                host="smtp.example.com",
                port=587,
                security="starttls",
                username="sender@example.com",
                from_address="sender@example.com",
                recipients="receiver@example.com",
                password="smtp-secret",
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["smtp"]["enabled"])
        self.assertFalse(result["smtp"]["has_password"])
        self.assertIn("自动停用", result["smtp"]["last_error"])
        self.assertFalse(app.load_smtp_settings()["enabled"])

    def test_smtp_save_and_clear_are_one_serialized_state_transition(self):
        api = self.new_api()
        password_written = threading.Event()
        release_save = threading.Event()
        delete_called = threading.Event()
        secret_state = {"present": False}
        results = {}

        def delayed_write(password, username="", settings=None):
            secret_state["present"] = True
            password_written.set()
            release_save.wait(3)
            return True

        def delete_password():
            delete_called.set()
            secret_state["present"] = False
            return True

        def has_password(settings=None):
            return secret_state["present"]

        def save_worker():
            results["save"] = api.save_smtp_settings(
                enabled=True,
                host="smtp.example.com",
                port=587,
                security="starttls",
                username="sender@example.com",
                from_address="sender@example.com",
                recipients="receiver@example.com",
                password="smtp-secret",
            )

        def clear_worker():
            results["clear"] = api.clear_smtp_password()

        with (
            mock.patch.object(app, "write_smtp_password", side_effect=delayed_write),
            mock.patch.object(app, "delete_smtp_password", side_effect=delete_password),
            mock.patch.object(app, "has_smtp_password", side_effect=has_password),
        ):
            save_thread = threading.Thread(target=save_worker, daemon=True)
            clear_thread = threading.Thread(target=clear_worker, daemon=True)
            save_thread.start()
            self.assertTrue(password_written.wait(2))
            clear_thread.start()
            self.assertFalse(delete_called.wait(0.1))
            release_save.set()
            save_thread.join(4)
            clear_thread.join(4)

        self.assertFalse(save_thread.is_alive())
        self.assertFalse(clear_thread.is_alive())
        self.assertTrue(results["save"]["ok"])
        self.assertTrue(results["clear"]["ok"])
        self.assertTrue(delete_called.is_set())
        self.assertFalse(secret_state["present"])
        persisted = app.load_smtp_settings()
        self.assertFalse(persisted["enabled"])
        self.assertIn("已清除", persisted["last_error"])

    def test_smtp_password_wrappers_use_generic_credential_helpers(self):
        with mock.patch.object(
            app,
            "read_credential_payload",
            return_value={"password": "stored-secret"},
        ) as read_payload:
            self.assertEqual(app.read_smtp_password(), "stored-secret")
        read_payload.assert_called_once_with(app.SMTP_CREDENTIAL_TARGET)

        with mock.patch.object(app, "write_credential_payload", return_value=True) as write_payload:
            self.assertTrue(app.write_smtp_password("new-secret", "sender@example.com"))
        write_payload.assert_called_once_with(
            app.SMTP_CREDENTIAL_TARGET,
            {
                "password": "new-secret",
                "host": "",
                "port": 587,
                "security": "starttls",
                "username": "sender@example.com",
            },
            username="sender@example.com",
            comment=f"{app.APP_NAME} SMTP password",
        )

        with mock.patch.object(app, "delete_credential_payload", return_value=True) as delete_payload:
            self.assertTrue(app.delete_smtp_password())
        delete_payload.assert_called_once_with(app.SMTP_CREDENTIAL_TARGET)

    def test_send_smtp_message_uses_smtp_ssl(self):
        settings = {
            **app.default_smtp_settings(),
            "host": "smtp.example.com",
            "port": 465,
            "security": "ssl",
            "username": "sender@example.com",
            "from_address": "sender@example.com",
            "recipients": ["receiver@example.com"],
        }
        client = mock.Mock()
        context = object()
        with (
            mock.patch.object(app.ssl, "create_default_context", return_value=context),
            mock.patch.object(app.smtplib, "SMTP_SSL", return_value=client) as smtp_ssl,
            mock.patch.object(app.smtplib, "SMTP") as smtp_plain,
        ):
            subject = app.send_smtp_message(
                settings,
                "Price changed",
                "Details",
                password="smtp-secret",
            )

        smtp_ssl.assert_called_once_with(
            "smtp.example.com",
            465,
            timeout=app.NOTIFICATION_TIMEOUT_SECONDS,
            context=context,
        )
        smtp_plain.assert_not_called()
        client.ehlo.assert_called_once_with()
        client.starttls.assert_not_called()
        client.login.assert_called_once_with("sender@example.com", "smtp-secret")
        client.send_message.assert_called_once()
        client.quit.assert_called_once_with()
        self.assertEqual(subject, "[Sub2API Monitor] Price changed")

    def test_send_smtp_message_uses_starttls(self):
        settings = {
            **app.default_smtp_settings(),
            "host": "smtp.example.com",
            "port": 587,
            "security": "starttls",
            "username": "sender@example.com",
            "from_address": "sender@example.com",
            "recipients": ["receiver@example.com"],
        }
        client = mock.Mock()
        context = object()
        with (
            mock.patch.object(app.ssl, "create_default_context", return_value=context),
            mock.patch.object(app.smtplib, "SMTP", return_value=client) as smtp_plain,
            mock.patch.object(app.smtplib, "SMTP_SSL") as smtp_ssl,
        ):
            app.send_smtp_message(
                settings,
                "Price changed",
                "Details",
                password="smtp-secret",
            )

        smtp_plain.assert_called_once_with(
            "smtp.example.com",
            587,
            timeout=app.NOTIFICATION_TIMEOUT_SECONDS,
        )
        smtp_ssl.assert_not_called()
        self.assertEqual(client.ehlo.call_count, 2)
        client.starttls.assert_called_once_with(context=context)
        client.login.assert_called_once_with("sender@example.com", "smtp-secret")
        client.send_message.assert_called_once()
        client.quit.assert_called_once_with()

    def test_build_smtp_change_message_summarizes_a_single_event(self):
        subject, body = app.build_smtp_change_message([{
            "site": "https://single.example",
            "site_host": "single.example",
            "change_type": "rate_changed",
            "direction": "increase",
            "severity": "medium",
            "old_value": 1,
            "new_value": 1.25,
            "message": "Primary 倍率 1 -> 1.25",
        }])

        self.assertIn("single.example", subject)
        self.assertIn("倍率变化", subject)
        self.assertIn("上涨", subject)
        self.assertIn("站点：single.example", body)
        self.assertIn("变化：倍率变化（上涨）", body)
        self.assertIn("旧值：1", body)
        self.assertIn("新值：1.25", body)

    def test_build_smtp_change_message_groups_multiple_directions(self):
        changes = [
            {
                "site_host": "increase.example",
                "change_type": "rate_changed",
                "direction": "increase",
                "severity": "high",
                "old_value": 1,
                "new_value": 2,
            },
            {
                "site_host": "decrease.example",
                "change_type": "price_changed",
                "direction": "decrease",
                "severity": "medium",
                "old_value": 20,
                "new_value": 15,
            },
            {
                "site_host": "added.example",
                "change_type": "added",
                "direction": "added",
                "severity": "low",
                "new_value": {"group_name": "new"},
            },
            {
                "site_host": "removed.example",
                "change_type": "removed",
                "direction": "removed",
                "severity": "high",
                "old_value": {"group_name": "old"},
            },
            {
                "site_host": "failed.example",
                "change_type": "site_unhealthy",
                "direction": "degraded",
                "severity": "high",
                "old_value": "warning",
                "new_value": "failed",
            },
            {
                "site_host": "recovered.example",
                "change_type": "site_recovered",
                "direction": "recovered",
                "severity": "low",
                "old_value": "failed",
                "new_value": "ok",
            },
        ]

        subject, body = app.build_smtp_change_message(changes)

        self.assertIn("6 条监控变化", subject)
        self.assertIn("3 条高优先级", subject)
        headings = (
            "上涨（1）",
            "下降（1）",
            "新增（1）",
            "移除（1）",
            "故障与恶化（1）",
            "恢复（1）",
        )
        positions = [body.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        for site in (
            "increase.example",
            "decrease.example",
            "added.example",
            "removed.example",
            "failed.example",
            "recovered.example",
        ):
            self.assertIn(site, body)

    def test_smtp_send_filters_events_before_building_message(self):
        settings = {
            **app.default_smtp_settings(),
            "enabled": True,
            "host": "smtp.example.com",
            "from_address": "sender@example.com",
            "recipients": ["receiver@example.com"],
            "min_severity": "medium",
            "notify_changes": True,
            "notify_site_errors": False,
            "notify_recoveries": True,
        }
        changes = [
            {
                "site_host": "low.example",
                "change_type": "rate_changed",
                "direction": "increase",
                "severity": "low",
            },
            {
                "site_host": "status.example",
                "change_type": "status_changed",
                "direction": "changed",
                "severity": "medium",
                "old_value": "active",
                "new_value": "disabled",
            },
            {
                "site_host": "error.example",
                "change_type": "site_error",
                "direction": "degraded",
                "severity": "high",
            },
            {
                "site_host": "recovered.example",
                "change_type": "site_recovered",
                "direction": "recovered",
                "severity": "high",
                "old_value": "failed",
                "new_value": "ok",
            },
        ]
        api = self.new_api()

        with (
            mock.patch.object(app, "load_smtp_settings", return_value=dict(settings)),
            mock.patch.object(
                app,
                "send_smtp_message",
                return_value="[Sub2API Monitor] filtered",
            ) as send_message,
            mock.patch.object(app, "update_smtp_delivery_state") as update_state,
            mock.patch.object(app, "append_notification_log") as append_log,
        ):
            api._send_change_notification(changes)

        _, subject, body = send_message.call_args.args
        self.assertIn("2 条监控变化", subject)
        self.assertIn("status.example", body)
        self.assertIn("recovered.example", body)
        self.assertNotIn("low.example", body)
        self.assertNotIn("error.example", body)
        update_state.assert_called_once_with(last_sent_at=mock.ANY, last_error="")
        self.assertEqual(append_log.call_args.args[0]["event_count"], 2)

    def test_notification_filtering_and_failure_log_redaction(self):
        settings = {
            **app.default_smtp_settings(),
            "enabled": True,
            "host": "smtp.example.com",
            "from_address": "sender@example.com",
            "recipients": ["receiver@example.com"],
            "min_severity": "medium",
            "notify_changes": True,
            "notify_site_errors": False,
            "notify_recoveries": True,
        }
        self.assertFalse(app.PriceAppApi._notification_change_allowed(
            {"change_type": "rate_changed", "severity": "low"},
            settings,
        ))
        self.assertTrue(app.PriceAppApi._notification_change_allowed(
            {"change_type": "details_changed", "severity": "medium"},
            settings,
        ))
        self.assertFalse(app.PriceAppApi._notification_change_allowed(
            {"change_type": "site_error", "severity": "high"},
            settings,
        ))
        self.assertTrue(app.PriceAppApi._notification_change_allowed(
            {"change_type": "site_recovered", "severity": "high"},
            settings,
        ))

        api = self.new_api()
        error = "Authorization: Bearer abc.def-123 and sk-abcdefghijklmnopqrstuvwxyz"
        with (
            mock.patch.object(app, "load_smtp_settings", return_value=dict(settings)),
            mock.patch.object(app, "send_smtp_message", side_effect=RuntimeError(error)),
            mock.patch.object(app, "write_smtp_settings") as write_settings,
            mock.patch.object(app, "append_notification_log") as append_log,
        ):
            api._send_change_notification([{
                "change_type": "details_changed",
                "severity": "medium",
                "site_host": "a.example",
                "message": "metadata changed",
            }])

        persisted_settings = write_settings.call_args.args[0]
        log_entry = append_log.call_args.args[0]
        for value in (persisted_settings["last_error"], log_entry["error"]):
            self.assertNotIn("abc.def-123", value)
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", value)
            self.assertIn("<redacted>", value)
        self.assertEqual(log_entry["target"], "re***@example.com")

    def test_smtp_delivery_status_update_does_not_overwrite_new_settings(self):
        initial = {
            **app.default_smtp_settings(),
            "enabled": True,
            "host": "old.smtp.example",
            "port": 587,
            "security": "starttls",
            "username": "",
            "from_address": "old@example.com",
            "recipients": ["old-target@example.com"],
            "updated_at": "2026-07-01T00:00:00+00:00",
        }
        app.write_smtp_settings(initial)
        api = self.new_api()
        send_started = threading.Event()
        release_send = threading.Event()

        def delayed_send(settings, subject, body, password=None):
            send_started.set()
            release_send.wait(3)
            return "[Sub2API Monitor] delayed"

        change = {
            "change_type": "rate_changed",
            "severity": "high",
            "site": "https://a.example",
            "site_host": "a.example",
            "message": "rate changed",
        }
        with (
            mock.patch.object(app, "send_smtp_message", side_effect=delayed_send),
            mock.patch.object(app, "has_smtp_password", return_value=False),
        ):
            worker = threading.Thread(
                target=api._send_change_notification,
                args=([change],),
                daemon=True,
            )
            worker.start()
            self.assertTrue(send_started.wait(2))
            newer = {
                **initial,
                "host": "new.smtp.example",
                "from_address": "new@example.com",
                "recipients": ["new-target@example.com"],
                "updated_at": "2026-07-13T00:00:00+00:00",
            }
            app.write_smtp_settings(newer)
            release_send.set()
            worker.join(4)

        self.assertFalse(worker.is_alive())
        persisted = app.load_smtp_settings()
        self.assertEqual(persisted["host"], newer["host"])
        self.assertEqual(persisted["from_address"], newer["from_address"])
        self.assertEqual(persisted["recipients"], newer["recipients"])
        self.assertEqual(persisted["updated_at"], newer["updated_at"])
        self.assertTrue(persisted["last_sent_at"])
        self.assertEqual(persisted["last_error"], "")

    def test_legacy_1_1_json_loads_with_new_defaults(self):
        legacy_site = {
            "name": "legacy",
            "site": "https://legacy.example",
            "api_base": "/api/v1",
            "interval_minutes": 180,
            "last_run": "2026-01-01T00:00:00+00:00",
            "last_status": "ok",
        }
        legacy_row = {
            "site": "https://legacy.example",
            "site_host": "legacy.example",
            "record_type": "group",
            "model_category": "OpenAI",
            "group_id": "legacy",
            "group_name": "legacy",
            "rate_multiplier": 1,
        }
        legacy_change = {
            "change_type": "rate_changed",
            "site": "https://legacy.example",
            "old_value": 1,
            "new_value": 1.1,
            "change_percent": 10,
            "detected_at": "2026-01-01T00:00:00+00:00",
        }
        app.atomic_write_text(
            app.saved_sites_path(),
            json.dumps([legacy_site], ensure_ascii=False),
        )
        app.atomic_write_text(
            app.latest_prices_json_path(),
            json.dumps({"generated_at": "2026-01-01T00:00:00+00:00", "rows": [legacy_row]}),
        )
        app.atomic_write_text(
            app.price_changes_path(),
            json.dumps({"changes": [legacy_change]}),
        )

        with mock.patch.object(app, "has_site_credentials", return_value=False):
            normalized_site = app.annotate_saved_sites(app.load_saved_sites())[0]
        self.assertTrue(normalized_site["auto_refresh"])
        self.assertTrue(normalized_site["remember_credentials"])
        self.assertTrue(normalized_site["auto_login"])
        self.assertEqual(normalized_site["health_status"], "ok")
        self.assertEqual(normalized_site["consecutive_failures"], 0)
        self.assertEqual(normalized_site["last_exclusive_count"], 0)
        self.assertEqual(normalized_site["last_user_rate_count"], 0)
        self.assertEqual(app.load_latest_rows(), [legacy_row])

        normalized_change = app.load_price_changes()[0]
        self.assertTrue(normalized_change["id"])
        self.assertFalse(normalized_change["acknowledged"])
        self.assertEqual(normalized_change["source_field"], "")
        self.assertEqual(normalized_change["direction"], "increase")
        self.assertEqual(normalized_change["severity"], "medium")

        detected = app.detect_price_changes([legacy_row], [{**legacy_row, "rate_multiplier": 1.1}])
        self.assertIn("rate_changed", {change["change_type"] for change in detected})

    def test_control_html_wires_change_history_and_monitor_controls(self):
        html = app.CONTROL_HTML
        save_site_block = html.split("async function saveSite()", 1)[1].split(
            "async function deleteSite()",
            1,
        )[0]
        runtime_block = html.split("function applyRuntimeStatus(result)", 1)[1].split(
            "async function pollSchedulerStatus()",
            1,
        )[0]
        render_changes_block = html.split("function renderChanges()", 1)[1].split(
            "function applySavedSite(site)",
            1,
        )[0]
        update_all_block = html.split("async function updateAllSaved(reason = 'manual')", 1)[1].split(
            "async function startAutoCheck()",
            1,
        )[0]
        init_block = html.split("async function init()", 1)[1].split(
            "siteInput.addEventListener",
            1,
        )[0]

        self.assertIn("autoRefreshInput.checked", save_site_block)
        self.assertIn("state.changes", init_block)
        self.assertIn("result.changes", runtime_block)
        self.assertIn("window.pywebview.api.acknowledge_changes", html)
        self.assertIn("const hasPercent = item.change_percent !== null", render_changes_block)
        self.assertIn("hasPercent && Number.isFinite", render_changes_block)
        self.assertIn("result.message || '没有需要更新的站点'", update_all_block)
        for element_id in (
            "changeSearchInput",
            "changeSiteFilterSelect",
            "changeTypeFilterSelect",
            "unreadOnlyInput",
            "acknowledgeChangesBtn",
        ):
            self.assertIn(f"{element_id}.addEventListener", html)

    def test_control_html_wires_site_probe_and_stale_state_guards(self):
        html = app.CONTROL_HTML
        revision_block = html.split("function advanceStateRevision(value)", 1)[1].split(
            "function setView(view)",
            1,
        )[0]
        runtime_block = html.split("function applyRuntimeStatus(result)", 1)[1].split(
            "async function pollSchedulerStatus()",
            1,
        )[0]
        stop_capture_block = html.split("function stopLoginAutoCapture()", 1)[1].split(
            "function reauthLabel(record)",
            1,
        )[0]
        start_capture_block = html.split("function startLoginAutoCapture(mode = 'login')", 1)[1].split(
            "async function openSite()",
            1,
        )[0]

        for element_id in (
            "siteMonitorSearchInput",
            "siteHealthFilterSelect",
            "siteEnabledFilterSelect",
            "testConnectionBtn",
            "connectionTestResult",
            "siteRecentChanges",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("siteMonitorSearchInput.addEventListener('input', renderSiteMonitor)", html)
        self.assertIn("siteHealthFilterSelect.addEventListener('change', renderSiteMonitor)", html)
        self.assertIn("testConnectionBtn.addEventListener('click', testSiteConnection)", html)
        self.assertIn("window.pywebview.api.test_site_connection", html)
        self.assertIn("siteRecentChanges.innerHTML", html)

        self.assertIn("if (next > stateRevision) stateRevision = next", revision_block)
        self.assertIn("incomingRevision && incomingRevision < stateRevision", revision_block)
        self.assertIn("scheduleStatusPoll(0)", revision_block)
        self.assertIn("incomingRevision >= stateRevision", runtime_block)
        self.assertIn("if (stateFresh)", runtime_block)
        self.assertIn("if (updateId > observedUpdateId)", runtime_block)
        self.assertNotIn("if (updating && updateId", runtime_block)

        self.assertIn("clearTimeout(loginAutoCaptureKickoffTimer)", stop_capture_block)
        self.assertIn("loginAutoCaptureKickoffTimer = null", stop_capture_block)
        self.assertIn("loginAutoCaptureKickoffTimer = setTimeout", start_capture_block)
        self.assertIn("tryLoginAutoCapture(generation)", start_capture_block)

    def test_control_html_mutation_revision_guard_executes(self):
        html = app.CONTROL_HTML
        helper_block = html.split("function advanceStateRevision(value)", 1)[1].split(
            "function setView(view)",
            1,
        )[0]
        script = f"""
let stateRevision = 7;
const scheduled = [];
function scheduleStatusPoll(delay) {{ scheduled.push(delay); }}
function advanceStateRevision(value){helper_block}
const stale = acceptMutationResult({{ revision: 6 }});
const current = acceptMutationResult({{ revision: 7 }});
const newer = acceptMutationResult({{ revision: 8 }});
process.stdout.write(JSON.stringify({{ stale, current, newer, stateRevision, scheduled }}));
"""
        completed = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertFalse(result["stale"])
        self.assertTrue(result["current"])
        self.assertTrue(result["newer"])
        self.assertEqual(result["stateRevision"], 8)
        self.assertEqual(result["scheduled"], [0])

    def test_reauth_queue_reconciles_an_active_site_no_longer_required(self):
        html = app.CONTROL_HTML
        helper_block = html.split("function enqueueReauthSites(sites)", 1)[1].split(
            "async function startNextReauth()",
            1,
        )[0]
        script = f"""
let reauthActiveSite = 'https://disabled.example';
let reauthActiveRecord = {{ site: reauthActiveSite }};
let reauthQueue = [{{ site: reauthActiveSite }}];
let loginAutoCaptureTimer = 1;
const deferredReauthSites = new Set();
let stopped = 0;
let hidden = 0;
let started = 0;
function stopLoginAutoCapture() {{ stopped += 1; loginAutoCaptureTimer = null; }}
function startNextReauth() {{ started += 1; }}
function log() {{}}
function reauthLabel(record) {{ return record.site; }}
const window = {{ pywebview: {{ api: {{ hide_login_webview: async () => {{ hidden += 1; }} }} }} }};
function enqueueReauthSites(sites){helper_block}
enqueueReauthSites([]);
setTimeout(() => process.stdout.write(JSON.stringify({{
  reauthActiveSite,
  reauthActiveRecord,
  queueLength: reauthQueue.length,
  stopped,
  hidden,
  started,
}})), 20);
"""
        completed = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["reauthActiveSite"], "")
        self.assertIsNone(result["reauthActiveRecord"])
        self.assertEqual(result["queueLength"], 0)
        self.assertEqual(result["stopped"], 1)
        self.assertEqual(result["hidden"], 1)
        self.assertEqual(result["started"], 1)

    def test_disabled_sites_are_not_queued_for_automatic_reauthorization(self):
        enabled = {
            "site": "https://enabled.example",
            "auto_refresh": True,
            "reauth_required": True,
        }
        disabled = {
            "site": "https://disabled.example",
            "auto_refresh": False,
            "last_status": "reauth_required",
        }
        self.assertEqual(app.PriceAppApi._reauth_sites([disabled, enabled]), [enabled])

    def test_capture_normalization_redacts_persisted_error_fields(self):
        normalized = app.PriceAppApi._normalize_capture_result({
            "ok": True,
            "partial": True,
            "error_code": "http_error",
            "error": "access_token=top-secret",
            "rateData": {
                "partial": True,
                "error": '{"refresh_token":"rate-secret"}',
            },
            "rows": [{
                "site": "https://redaction.example",
                "record_type": "group",
                "group_id": "primary",
                "rate_multiplier": 1,
                "error": "Cookie: sessionid=row-secret",
            }],
        })
        serialized = json.dumps(normalized, ensure_ascii=False)
        for secret in ("top-secret", "rate-secret", "row-secret"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(normalized["error_code"], "http_error")
        self.assertIn("<redacted>", serialized)

    def test_smtp_test_failure_returns_revision_for_frontend_ordering(self):
        api = self.new_api()
        with mock.patch.object(
            app,
            "validate_smtp_settings",
            side_effect=ValueError("password=missing-secret"),
        ):
            result = api.test_smtp_notification()
        self.assertFalse(result["ok"])
        self.assertGreater(result["revision"], 0)
        self.assertNotIn("missing-secret", json.dumps(result, ensure_ascii=False))

    def test_log_redaction_removes_tokens(self):
        text = app.redact_log_text(
            "\n".join((
                "Authorization: Bearer abc.def-123 and sk-abcdefghijklmnopqrstuvwxyz",
                "https://user:url-secret@example.com/path?access_token=query-secret",
                '{"refresh_token":"json-secret","api_key":"api-secret"}',
                '{"accessToken":"camel-access-secret"}',
                '{"refreshToken":"camel-refresh-secret"}',
                '{"clientSecret":"camel-client-secret"}',
                '{"password":"top secret with spaces"}',
                "Cookie: sessionid=cookie-secret",
                "Set-Cookie: auth_token=set-cookie-secret; HttpOnly",
            ))
        )
        for secret in (
            "abc.def-123",
            "sk-abcdefghijklmnopqrstuvwxyz",
            "url-secret",
            "query-secret",
            "json-secret",
            "api-secret",
            "camel-access-secret",
            "camel-refresh-secret",
            "camel-client-secret",
            "top secret with spaces",
            "cookie-secret",
            "set-cookie-secret",
        ):
            self.assertNotIn(secret, text)
        self.assertIn("<redacted>", text)


if __name__ == "__main__":
    unittest.main()
