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

    def test_log_redaction_removes_tokens(self):
        text = app.redact_log_text(
            "Authorization: Bearer abc.def-123 and sk-abcdefghijklmnopqrstuvwxyz"
        )
        self.assertNotIn("abc.def-123", text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", text)


if __name__ == "__main__":
    unittest.main()
