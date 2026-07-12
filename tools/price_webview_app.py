#!/usr/bin/env python
import argparse
import ctypes
import csv
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import pathlib
import queue
import random
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import ProxyHandler, Request, build_opener

import psutil
import webview


API_BASE = "/api/v1"
BLANK_PAGE = "about:blank"
MODEL_CATEGORY_ORDER = {
    "OpenAI": 0,
    "Anthropic": 1,
    "Gemini": 2,
    "Grok": 3,
    "其他": 4,
    "未获取": 5,
}
OUTPUT_FIELDS = [
    "site",
    "site_host",
    "status",
    "source",
    "record_type",
    "model_category",
    "model_names",
    "group_id",
    "group_name",
    "group_platform",
    "plan_id",
    "plan_name",
    "price",
    "original_price",
    "price_currency_hint",
    "pay_price_cny",
    "subscription_usd_to_cny_rate",
    "validity_days",
    "validity_unit",
    "rate_multiplier",
    "peak_rate_enabled",
    "peak_start",
    "peak_end",
    "peak_rate_multiplier",
    "daily_limit_usd",
    "weekly_limit_usd",
    "monthly_limit_usd",
    "payment_currencies",
    "features",
    "description",
    "fetched_at",
    "error",
]


APP_NAME = "Sub2APIPriceMonitor"
APP_VERSION = "1.0.0"
APP_WINDOW_TITLE = "Sub2API 中转站比价"
LOGGER = logging.getLogger(APP_NAME)
SHUTDOWN_JOIN_TIMEOUT_SECONDS = 1.0


AUTH_ERROR_PATTERNS = (
    r"\b(?:http\s*)?(?:401|403)\b",
    r"\bunauthori[sz]ed\b",
    r"\bforbidden\b",
    r"\b(?:access|refresh|auth|id)[ _-]?token\b.{0,24}\b(?:expired|invalid|missing|revoked)\b",
    r"\b(?:expired|invalid|missing|revoked)\b.{0,24}\btoken\b",
    r"\bsession\b.{0,24}\b(?:expired|invalid)\b",
    r"登录(?:状态|态)?(?:已)?(?:失效|过期)",
    r"(?:请|需要|必须).{0,8}登录",
    r"未登录",
    r"认证(?:失败|失效|过期)",
    r"授权(?:失败|失效|过期)",
    r"会话(?:失效|过期)",
)


def requires_reauthorization(error, current_url="", has_password_input=False):
    if has_password_input:
        return True
    url = str(current_url or "").lower()
    if re.search(r"(?:^|[/#?&=_-])(?:login|signin|sign-in|auth|authorize)(?:$|[/#?&=_-])", url):
        return True
    text = str(error or "").lower()
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in AUTH_ERROR_PATTERNS)


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
ERROR_ALREADY_EXISTS = 183
APP_MUTEX_NAME = f"Local\\{APP_NAME}.SingleInstance"
_APP_MUTEX_HANDLE = None
MAX_SITE_WORKERS = 1
WINDOW_LOCK_TIMEOUT_SECONDS = 0.5
WINDOW_INITIALIZE_TIMEOUT_SECONDS = 15
INTERACTIVE_RELOGIN_WAIT_SECONDS = 5
BROWSER_HOST_START_TIMEOUT_SECONDS = 30
BROWSER_IPC_TIMEOUT_SECONDS = 12


class AppShutdownRequested(RuntimeError):
    pass

ERROR_LABELS = {
    "reauth_required": "登录/会话已失效",
    "cloudflare_challenge": "Cloudflare 验证未完成",
    "timeout": "请求或页面超时",
    "http_error": "HTTP 接口错误",
    "unsupported_response": "接口响应不支持",
    "no_price_data": "未发现价格数据",
    "network_error": "网络错误",
    "window_closed": "登录窗口已关闭",
    "unknown_error": "未知错误",
}

CLOUDFLARE_PATTERNS = (
    r"cloudflare",
    r"challenges\.cloudflare\.com",
    r"cdn-cgi/challenge-platform",
    r"just a moment",
    r"checking your browser",
    r"security verification",
    r"正在进行安全验证",
    r"安全服务防护",
    r"人机验证",
)

TIMEOUT_PATTERNS = (
    r"\btimeout\b",
    r"timed out",
    r"AbortError",
    r"超时",
    r"等待 WebView",
)

NETWORK_ERROR_PATTERNS = (
    r"failed to fetch",
    r"networkerror",
    r"ERR_",
    r"connection",
    r"DNS",
    r"网络",
)

REAUTH_ELIGIBLE_ERROR_CODES = {
    "reauth_required",
    "cloudflare_challenge",
    "http_error",
    "unsupported_response",
    "no_price_data",
}


class _CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTEW)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


if os.name == "nt":
    _advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    _cred_write = _advapi32.CredWriteW
    _cred_write.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    _cred_write.restype = wintypes.BOOL
    _cred_read = _advapi32.CredReadW
    _cred_read.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
    ]
    _cred_read.restype = wintypes.BOOL
    _cred_delete = _advapi32.CredDeleteW
    _cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _cred_delete.restype = wintypes.BOOL
    _cred_free = _advapi32.CredFree
    _cred_free.argtypes = [ctypes.c_void_p]
    _cred_free.restype = None
else:
    _cred_write = None
    _cred_read = None
    _cred_delete = None
    _cred_free = None


def credential_target(site):
    normalized = normalize_site(site)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    return f"{APP_NAME}:{safe_host(normalized)[:80]}:{digest}"


def read_site_credentials(site):
    if not _cred_read:
        return None
    normalized = normalize_site(site)
    pointer = ctypes.POINTER(_CREDENTIALW)()
    if not _cred_read(credential_target(normalized), CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return None
        raise OSError(error, "读取 Windows 凭据失败")
    try:
        credential = pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        payload = json.loads(blob.decode("utf-8"))
        if normalize_site(payload.get("site", "")) != normalized:
            return None
        return {
            "site": normalized,
            "username": str(payload.get("username") or credential.UserName or ""),
            "password": str(payload.get("password") or ""),
        }
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    finally:
        _cred_free(pointer)


def write_site_credentials(site, username, password):
    if not _cred_write:
        raise RuntimeError("保存密码仅支持 Windows 凭据管理器")
    normalized = normalize_site(site)
    username = str(username or "").strip()[:512]
    password = str(password or "")
    if not password:
        raise ValueError("密码不能为空")
    payload = json.dumps({
        "site": normalized,
        "username": username,
        "password": password,
    }, ensure_ascii=False).encode("utf-8")
    if len(payload) > 2500:
        raise ValueError("登录凭据过长，无法保存到 Windows 凭据管理器")
    blob = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    credential = _CREDENTIALW()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = credential_target(normalized)
    credential.Comment = f"{APP_NAME} saved login for {normalized}"
    credential.CredentialBlobSize = len(payload)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = username
    if not _cred_write(ctypes.byref(credential), 0):
        error = ctypes.get_last_error()
        raise OSError(error, "写入 Windows 凭据失败")
    return True


def delete_site_credentials(site):
    if not _cred_delete:
        return False
    if _cred_delete(credential_target(site), CRED_TYPE_GENERIC, 0):
        return True
    error = ctypes.get_last_error()
    if error == ERROR_NOT_FOUND:
        return False
    raise OSError(error, "删除 Windows 凭据失败")


def has_site_credentials(site):
    credential = read_site_credentials(site)
    return bool(credential and credential.get("password"))


def focus_existing_instance():
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    find_window = user32.FindWindowW
    find_window.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    find_window.restype = wintypes.HWND
    show_window = user32.ShowWindow
    show_window.argtypes = [wintypes.HWND, ctypes.c_int]
    show_window.restype = wintypes.BOOL
    set_foreground_window = user32.SetForegroundWindow
    set_foreground_window.argtypes = [wintypes.HWND]
    set_foreground_window.restype = wintypes.BOOL
    handle = find_window(None, APP_WINDOW_TITLE)
    if not handle:
        return False
    show_window(handle, 9)
    set_foreground_window(handle)
    return True


def acquire_single_instance():
    global _APP_MUTEX_HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create_mutex.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_mutex(None, False, APP_MUTEX_NAME)
    if not handle:
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        close_handle(handle)
        if not focus_existing_instance():
            ctypes.windll.user32.MessageBoxW(
                None,
                "Sub2API 中转站比价已经在运行。",
                APP_WINDOW_TITLE,
                0x40,
            )
        return False
    _APP_MUTEX_HANDLE = handle
    return True


def release_single_instance():
    global _APP_MUTEX_HANDLE
    if os.name == "nt" and _APP_MUTEX_HANDLE:
        ctypes.windll.kernel32.CloseHandle(_APP_MUTEX_HANDLE)
        _APP_MUTEX_HANDLE = None


def bundled_root():
    if getattr(sys, "frozen", False):
        return pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(sys.executable).parent))
    return pathlib.Path(__file__).resolve().parent


def repo_root():
    return pathlib.Path(__file__).resolve().parents[1]


def app_data_root():
    override = os.getenv("SUB2API_PRICE_APP_DATA")
    if override:
        return pathlib.Path(override)
    if getattr(sys, "frozen", False):
        local_app_data = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
        if local_app_data:
            return pathlib.Path(local_app_data) / APP_NAME
    return repo_root() / "output"


def normalize_site(raw):
    value = str(raw or "").strip()
    if not value:
        raise ValueError("站点地址不能为空")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")


def safe_host(site):
    host = urlparse(site).netloc
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in host)
    return cleaned or "unknown-site"


def timestamp_slug():
    return datetime.now(timezone.utc).isoformat().replace(":", "-").replace(".", "-")


def output_dir():
    path = app_data_root()
    path.mkdir(parents=True, exist_ok=True)
    return path


def webview_profile_dir():
    return output_dir() / "price-webview-profile"


def log_path():
    path = output_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path / "app.log"


def redact_log_text(value):
    text = str(value or "")
    text = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/]+=*",
        r"\1<redacted>",
        text,
    )
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-<redacted>", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact_log_text(super().format(record))


def configure_logging():
    if any(getattr(handler, "sub2api_handler", False) for handler in LOGGER.handlers):
        return
    handler = RotatingFileHandler(
        log_path(),
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.sub2api_handler = True
    handler.setFormatter(RedactingFormatter(
        "%(asctime)s %(levelname)s %(threadName)s %(message)s"
    ))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


def atomic_write_text(path, content, encoding="utf-8"):
    destination = pathlib.Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        with temporary.open("w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(destination))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def windows_path_aliases(path):
    absolute = os.path.abspath(os.fspath(path))
    aliases = {os.path.normcase(os.path.normpath(absolute))}
    if os.name != "nt":
        return aliases
    kernel32 = ctypes.windll.kernel32
    for function_name in ("GetLongPathNameW", "GetShortPathNameW"):
        function = getattr(kernel32, function_name)
        function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        function.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = function(absolute, buffer, len(buffer))
        if 0 < length < len(buffer):
            aliases.add(os.path.normcase(os.path.normpath(buffer.value)))
    return aliases


def cleanup_webview_processes(profile_dir, reason):
    if os.name != "nt":
        return 0
    profile_tokens = windows_path_aliases(profile_dir)
    matches = []
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            if str(process.info.get("name") or "").lower() != "msedgewebview2.exe":
                continue
            command_line = " ".join(process.info.get("cmdline") or [])
            normalized_command_line = os.path.normcase(command_line)
            if not any(token in normalized_command_line for token in profile_tokens):
                continue
            matches.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    if not matches:
        return 0

    LOGGER.info("Cleaning %s app-owned WebView2 processes during %s", len(matches), reason)
    for process in matches:
        try:
            process.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    _, alive = psutil.wait_procs(matches, timeout=1.0)
    for process in alive:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    if alive:
        psutil.wait_procs(alive, timeout=0.5)
    return len(matches)


def default_output_path(site, fmt):
    return output_dir() / f"group-prices-{safe_host(site)}-{timestamp_slug()}.{fmt}"


def saved_sites_path():
    return output_dir() / "price-sites.json"


def price_history_dir():
    path = output_dir() / "price-history"
    path.mkdir(parents=True, exist_ok=True)
    return path


def latest_prices_json_path():
    return output_dir() / "price-latest.json"


def latest_prices_csv_path():
    return output_dir() / "price-latest.csv"


def load_saved_sites():
    path = saved_sites_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        LOGGER.exception("Failed to load saved sites from %s", path)
        return []
    return []


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def interval_minutes(record):
    interval = max(1, int(record.get("interval_minutes") or 180))
    return interval


def jittered_interval_minutes(record):
    return max(1, round(interval_minutes(record) * random.uniform(0.9, 1.1)))


def next_run_at(record, from_time=None):
    stored = parse_datetime(record.get("next_run"))
    if stored:
        return stored
    last = parse_datetime(record.get("last_run"))
    if last:
        return last + timedelta(minutes=jittered_interval_minutes(record))
    return from_time or datetime.now(timezone.utc)


def schedule_next_run(record, from_time=None):
    base = from_time or datetime.now(timezone.utc)
    return base + timedelta(minutes=jittered_interval_minutes(record))


def normalize_saved_site_record(item):
    record = dict(item)
    site = str(record.get("site") or "")
    record["name"] = str(record.get("name") or site)
    record["auto_refresh"] = True
    record["remember_credentials"] = bool(record.get("remember_credentials", True))
    record["auto_login"] = bool(record.get("auto_login", True)) and record["remember_credentials"]
    record["reauth_required"] = bool(
        record.get("reauth_required") or record.get("last_status") == "reauth_required"
    )
    if record.get("last_status") == "ok":
        record["last_status_label"] = record.get("last_status_label") or "正常"
    elif record.get("last_error_code"):
        record["last_status_label"] = record.get("last_status_label") or ERROR_LABELS.get(
            record.get("last_error_code"),
            ERROR_LABELS["unknown_error"],
        )
    if site and not record.get("next_run"):
        record["next_run"] = next_run_at(record).isoformat()
    return record


def annotate_saved_sites(sites):
    annotated = []
    for item in sites:
        if not isinstance(item, dict):
            continue
        record = normalize_saved_site_record(item)
        try:
            record["credentials_saved"] = has_site_credentials(record.get("site", ""))
        except Exception:
            record["credentials_saved"] = False
        annotated.append(record)
    return annotated


def write_saved_sites(sites):
    path = saved_sites_path()
    normalized = [normalize_saved_site_record(item) for item in sites if isinstance(item, dict)]
    atomic_write_text(
        path,
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
    )


def load_latest_rows():
    path = latest_prices_json_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("rows") if isinstance(data, dict) else []
        return rows if isinstance(rows, list) else []
    except Exception:
        LOGGER.exception("Failed to load latest prices from %s", path)
        return []


def write_price_snapshot(rows, summary):
    generated_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "generated_at": generated_at,
        "summary": summary,
        "rows": rows,
    }
    json_content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    csv_content = rows_to_csv(rows)
    slug = generated_at.replace(":", "-").replace(".", "-")
    atomic_write_text(price_history_dir() / f"prices-{slug}.json", json_content)
    atomic_write_text(latest_prices_csv_path(), csv_content, encoding="utf-8-sig")
    atomic_write_text(latest_prices_json_path(), json_content)
    return payload


def collector_js_template():
    return (bundled_root() / "price_collector_snippet.js").read_text(encoding="utf-8")


def rows_to_csv(rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in OUTPUT_FIELDS})
    return buffer.getvalue()


def row_group_label(row):
    return str(row.get("group_name") or row.get("group_id") or "未分组")


def row_rate(row):
    value = row.get("rate_multiplier")
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value or "").replace(",", ""))
    if not match:
        return float("inf")
    try:
        return float(match.group(0))
    except ValueError:
        return float("inf")


def row_sort_key(row):
    category = str(row.get("model_category") or "其他")
    return (
        MODEL_CATEGORY_ORDER.get(category, len(MODEL_CATEGORY_ORDER)),
        category,
        row_rate(row),
        row.get("site_host", ""),
        row_group_label(row),
        row.get("group_platform", ""),
        row.get("record_type", ""),
        row.get("plan_name", ""),
    )


def row_price(row):
    for key in ("pay_price_cny", "price"):
        try:
            value = float(str(row.get(key) or "").replace(",", ""))
            if value >= 0:
                return value
        except ValueError:
            continue
    return float("inf")


def row_identity_key(row):
    site = row.get("site_host") or urlparse(str(row.get("site") or "")).netloc or row.get("site") or ""
    return tuple(
        str(value or "").strip().lower()
        for value in (
            site,
            row.get("record_type"),
            row.get("model_category"),
            row.get("group_id") or row.get("group_name"),
            row.get("group_platform"),
            row.get("plan_id") or row.get("plan_name") or row.get("description"),
        )
    )


def row_timestamp(row):
    parsed = parse_datetime(row.get("fetched_at"))
    return parsed or datetime.min.replace(tzinfo=timezone.utc)


def merge_price_rows(previous_rows, fresh_rows):
    merged = {}
    for row in list(previous_rows or []) + list(fresh_rows or []):
        if not isinstance(row, dict):
            continue
        key = row_identity_key(row)
        current = merged.get(key)
        if current is None or row_timestamp(row) >= row_timestamp(current):
            merged[key] = row
    rows = list(merged.values())
    rows.sort(key=lambda row: (row_price(row), row_rate(row), row_sort_key(row)))
    return rows


def replace_price_rows(previous_rows, fresh_rows, refreshed_sites):
    refreshed_hosts = set()
    for site in refreshed_sites or []:
        try:
            host = urlparse(normalize_site(site)).netloc.lower()
        except (TypeError, ValueError):
            host = ""
        if host:
            refreshed_hosts.add(host)

    retained_rows = []
    for row in previous_rows or []:
        if not isinstance(row, dict):
            continue
        host = str(
            row.get("site_host")
            or urlparse(str(row.get("site") or "")).netloc
            or ""
        ).lower()
        if host not in refreshed_hosts:
            retained_rows.append(row)
    return merge_price_rows(retained_rows, fresh_rows)


COLLECTOR_JS = r"""
(async function run(options) {
  const tokenKeys = ['auth_token', 'access_token'];

  function token() {
    for (const key of tokenKeys) {
      const value = localStorage.getItem(key) || sessionStorage.getItem(key);
      if (value) return { key, value };
    }
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key && key.toLowerCase().includes('token')) {
        const value = localStorage.getItem(key);
        if (value) return { key, value };
      }
    }
    return null;
  }

  function endpoint(suffix) {
    return new URL(options.base.replace(/\/$/, '') + suffix, window.location.origin).toString();
  }

  async function apiGet(suffix) {
    const found = token();
    if (!found) throw new Error('未检测到 auth_token，请先在站点窗口登录');
    const response = await fetch(endpoint(suffix), {
      headers: {
        Accept: 'application/json',
        Authorization: 'Bearer ' + found.value,
      },
      credentials: 'include',
    });
    const rawText = await response.text();
    let body = null;
    try { body = rawText ? JSON.parse(rawText) : null; } catch { body = rawText; }
    if (!response.ok) {
      const message = typeof body === 'string' ? body.slice(0, 240) : JSON.stringify(body).slice(0, 240);
      throw new Error(`${suffix}: HTTP ${response.status}: ${message}`);
    }
    if (body && typeof body === 'object' && 'code' in body && 'data' in body) {
      if (body.code === 0 || body.code === 200 || body.success === true) return body.data;
      throw new Error(`${suffix}: ${body.message || body.reason || body.code}`);
    }
    return body;
  }

  function number(value) {
    if (value === null || value === undefined || value === '') return '';
    const n = Number(value);
    return Number.isFinite(n) ? n : '';
  }

  function text(value) {
    return String(value ?? '').replace(/\s+/g, ' ').trim();
  }

  function features(value) {
    if (!value) return '';
    if (Array.isArray(value)) return value.map(text).filter(Boolean).join(' | ');
    if (typeof value === 'string') {
      const trimmed = value.trim();
      if (trimmed.startsWith('[')) {
        try {
          const parsed = JSON.parse(trimmed);
          if (Array.isArray(parsed)) return features(parsed);
        } catch {}
      }
      return trimmed.split(/[\r\n;]+/).map(text).filter(Boolean).join(' | ');
    }
    return text(value);
  }

  function currencies(checkout) {
    const methods = checkout && checkout.methods;
    if (!methods || typeof methods !== 'object') return '';
    const values = new Set();
    for (const method of Object.values(methods)) {
      if (!method || typeof method !== 'object') continue;
      const currency = String(method.currency || 'CNY').trim().toUpperCase();
      if (/^[A-Z]{3}$/.test(currency)) values.add(currency);
    }
    return [...values].sort().join(',');
  }

  function cny(price, checkout) {
    const p = Number(price);
    const rate = Number(checkout && checkout.subscription_usd_to_cny_rate);
    if (!Number.isFinite(p) || !Number.isFinite(rate) || rate <= 0) return '';
    return Math.round(p * rate * 100) / 100;
  }

  function baseRecord(source, recordType) {
    return {
      site: window.location.origin,
      status: 'ok',
      source,
      record_type: recordType,
      fetched_at: new Date().toISOString(),
    };
  }

  function planRecord(source, plan, checkout) {
    const price = number(plan.price);
    const rate = number(checkout && checkout.subscription_usd_to_cny_rate);
    return {
      ...baseRecord(source, 'plan'),
      group_id: plan.group_id ?? '',
      group_name: plan.group_name ?? '',
      group_platform: plan.group_platform ?? '',
      plan_id: plan.id ?? '',
      plan_name: plan.name ?? '',
      price,
      original_price: number(plan.original_price),
      price_currency_hint: rate ? 'USD' : 'configured',
      pay_price_cny: cny(price, checkout),
      subscription_usd_to_cny_rate: rate,
      validity_days: plan.validity_days ?? '',
      validity_unit: plan.validity_unit ?? '',
      rate_multiplier: number(plan.rate_multiplier),
      peak_rate_enabled: plan.peak_rate_enabled ?? '',
      peak_start: plan.peak_start ?? '',
      peak_end: plan.peak_end ?? '',
      peak_rate_multiplier: number(plan.peak_rate_multiplier),
      daily_limit_usd: number(plan.daily_limit_usd),
      weekly_limit_usd: number(plan.weekly_limit_usd),
      monthly_limit_usd: number(plan.monthly_limit_usd),
      payment_currencies: currencies(checkout),
      features: features(plan.features),
      description: text(plan.description),
    };
  }

  function groupRecord(group) {
    return {
      ...baseRecord('/groups/available', 'group'),
      group_id: group.id ?? '',
      group_name: group.name ?? '',
      group_platform: group.platform ?? '',
      rate_multiplier: number(group.rate_multiplier),
      peak_rate_enabled: group.peak_rate_enabled ?? '',
      peak_start: group.peak_start ?? '',
      peak_end: group.peak_end ?? '',
      peak_rate_multiplier: number(group.peak_rate_multiplier),
      daily_limit_usd: number(group.daily_limit_usd),
      weekly_limit_usd: number(group.weekly_limit_usd),
      monthly_limit_usd: number(group.monthly_limit_usd),
      description: text(group.description),
    };
  }

  const rows = [];
  const errors = [];
  let checkout = null;
  try {
    checkout = await apiGet('/payment/checkout-info');
    for (const plan of Array.isArray(checkout.plans) ? checkout.plans : []) {
      if (plan && typeof plan === 'object') rows.push(planRecord('/payment/checkout-info', plan, checkout));
    }
  } catch (error) {
    errors.push(error.message);
  }

  if (rows.length === 0) {
    try {
      const plans = await apiGet('/payment/plans');
      for (const plan of Array.isArray(plans) ? plans : []) {
        if (plan && typeof plan === 'object') rows.push(planRecord('/payment/plans', plan, null));
      }
    } catch (error) {
      errors.push(error.message);
    }
  }

  if (options.includeGroups) {
    try {
      const groups = await apiGet('/groups/available');
      for (const group of Array.isArray(groups) ? groups : []) {
        if (group && typeof group === 'object') rows.push(groupRecord(group));
      }
    } catch (error) {
      errors.push(error.message);
    }
  }

  if (rows.length === 0) {
    rows.push({
      site: window.location.origin,
      status: 'no_price_found',
      source: 'none',
      record_type: 'error',
      fetched_at: new Date().toISOString(),
      error: errors.join(' | '),
    });
  } else if (errors.length) {
    for (const row of rows) {
      row.status = 'partial';
      row.error = errors.join(' | ');
    }
  }

  return { tokenKey: token()?.key || '', rows, outputFields: options.outputFields };
})(__OPTIONS__)
"""


CREDENTIAL_HELPER_JS = r"""
(function applyCredentials(credentials) {
  const isVisible = (element) => {
    if (!element || element.disabled || element.readOnly) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const passwordInputs = [...document.querySelectorAll('input[type="password"]')].filter(isVisible);
  if (passwordInputs.length !== 1) {
    return { loginForm: passwordInputs.length > 0, ambiguous: passwordInputs.length > 1 };
  }

  const passwordInput = passwordInputs[0];
  const form = passwordInput.form || passwordInput.closest('form');
  const scope = form || document;
  const candidates = [...scope.querySelectorAll('input')].filter((input) => (
    input !== passwordInput
    && isVisible(input)
    && ['text', 'email', 'tel', ''].includes(String(input.type || '').toLowerCase())
  ));
  const usernameInput = candidates.find((input) => /^(username|email)$/i.test(input.autocomplete || ''))
    || candidates.find((input) => input.type === 'email')
    || candidates.find((input) => /user|account|login|email|mail|phone|mobile/i.test(
      `${input.name || ''} ${input.id || ''} ${input.placeholder || ''}`
    ))
    || candidates[0]
    || null;
  const loginUrl = /(?:^|[/#?&=_-])(?:login|signin|sign-in|auth|authorize)(?:$|[/#?&=_-])/i.test(location.href);
  const passwordMode = String(passwordInput.autocomplete || '').toLowerCase();
  const likelyLogin = loginUrl || Boolean(usernameInput) || passwordMode === 'current-password';
  if (!likelyLogin || passwordMode === 'new-password') {
    return { loginForm: true, ambiguous: true };
  }

  const setValue = (input, value) => {
    if (!input || !value || input.value) return false;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    if (setter) setter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  };

  let filled = false;
  if (credentials) {
    filled = setValue(usernameInput, credentials.username || '') || filled;
    filled = setValue(passwordInput, credentials.password || '') || filled;
  }

  const save = () => {
    const password = passwordInput.value || '';
    if (!password) return;
    const username = usernameInput?.value || '';
    try {
      const api = window.pywebview && window.pywebview.api;
      if (api && typeof api.save_credentials === 'function') {
        Promise.resolve(api.save_credentials(username, password)).catch(() => {});
      }
    } catch {}
  };

  if (!passwordInput.dataset.sub2apiCredentialHook) {
    passwordInput.dataset.sub2apiCredentialHook = '1';
    passwordInput.addEventListener('change', save);
    passwordInput.addEventListener('blur', save);
    passwordInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') save();
    });
  }
  if (form && !form.dataset.sub2apiCredentialHook) {
    form.dataset.sub2apiCredentialHook = '1';
    form.addEventListener('submit', save, true);
  }

  const captchaSelector = [
    'iframe[src*="captcha" i]',
    'iframe[src*="challenge" i]',
    'iframe[src*="turnstile" i]',
    '[class*="captcha" i]',
    '[id*="captcha" i]',
    '[class*="turnstile" i]',
    '[id*="turnstile" i]',
  ].join(',');
  const hasCaptcha = [...document.querySelectorAll(captchaSelector)].some(isVisible);
  const hasOtp = [...scope.querySelectorAll('input')].some((input) => (
    isVisible(input)
    && (
      String(input.autocomplete || '').toLowerCase() === 'one-time-code'
      || /^(otp|totp|code|verification.?code|verify.?code)$/i.test(input.name || input.id || '')
      || /验证码|动态码|一次性密码/i.test(input.placeholder || '')
    )
  ));
  const requiredUnchecked = [...scope.querySelectorAll('input[type="checkbox"][required]')]
    .some((input) => isVisible(input) && !input.checked);
  const buttons = [...scope.querySelectorAll('button, input[type="submit"]')].filter(isVisible);
  const submitButtons = buttons.filter((button) => {
    const type = String(button.type || '').toLowerCase();
    const label = String(button.innerText || button.value || button.getAttribute('aria-label') || '').trim();
    return type === 'submit' || /^(登录|登錄|log\s*in|sign\s*in|继续|繼續|continue)$/i.test(label);
  });
  const submitButton = submitButtons.length === 1 ? submitButtons[0] : null;
  const canAutoSubmit = Boolean(
    credentials?.autoLogin
    && credentials?.allowAutoLogin
    && passwordInput.value
    && (!usernameInput || usernameInput.value)
    && !hasCaptcha
    && !hasOtp
    && !requiredUnchecked
    && submitButton
    && !window.__sub2apiAutoLoginPending
  );
  let autoSubmitted = false;
  if (canAutoSubmit) {
    window.__sub2apiAutoLoginPending = true;
    autoSubmitted = true;
    save();
    window.setTimeout(() => {
      try {
        if (form?.requestSubmit) form.requestSubmit(submitButton);
        else submitButton.click();
      } catch {
        submitButton.click();
      }
    }, 350);
  }

  return {
    loginForm: true,
    ambiguous: false,
    username: usernameInput?.value || '',
    password: passwordInput.value || '',
    filled,
    autoSubmitted,
    blockedByChallenge: hasCaptcha || hasOtp || requiredUnchecked,
  };
})(__CREDENTIALS__)
"""


CONTROL_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Sub2API 中转站比价</title>
  <style>
    * { box-sizing: border-box; }
    :root {
      color-scheme: light;
      --bg: #f4f5f7;
      --panel: #ffffff;
      --panel-muted: #fafafa;
      --border: #e2e4e9;
      --border-strong: #cfd3da;
      --text: #18181b;
      --muted: #6b7280;
      --accent: #f97316;
      --accent-soft: #fff1e8;
      --blue: #2563eb;
      --green: #15803d;
      --red: #b91c1c;
    }
    html, body { width: 100%; height: 100%; }
    body {
      margin: 0;
      overflow: hidden;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
    }
    button, input, select { font: inherit; }
    button {
      height: 36px;
      border: 1px solid var(--border-strong);
      border-radius: 7px;
      background: var(--panel);
      color: var(--text);
      cursor: pointer;
      font-weight: 650;
      padding: 0 12px;
      white-space: nowrap;
    }
    button:hover:not(:disabled) { border-color: #aeb4bd; background: #f8f8f9; }
    button:disabled { cursor: not-allowed; opacity: 0.45; }
    button.primary { border-color: var(--accent); background: var(--accent); color: #fff; }
    button.primary:hover:not(:disabled) { border-color: #ea580c; background: #ea580c; }
    button.danger { color: var(--red); }
    input[type="url"], input[type="text"], input[type="number"], input[type="search"], select {
      width: 100%;
      height: 36px;
      border: 1px solid var(--border-strong);
      border-radius: 7px;
      padding: 0 10px;
      background: #fff;
      color: var(--text);
      font-size: 13px;
    }
    input:focus, select:focus { border-color: var(--blue); outline: 3px solid rgba(37, 99, 235, 0.11); }
    label { display: grid; gap: 6px; color: #525866; font-size: 12px; font-weight: 650; }
    [hidden] { display: none !important; }
    .app-shell { height: 100vh; display: grid; grid-template-columns: 218px minmax(0, 1fr); }
    .sidebar {
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 18px;
      padding: 18px 14px 14px;
      border-right: 1px solid var(--border);
      background: #fbfbfc;
    }
    .brand { display: flex; align-items: center; gap: 10px; padding: 0 4px; }
    .brand-mark {
      display: grid;
      place-items: center;
      width: 38px;
      height: 38px;
      border-radius: 8px;
      background: var(--accent);
      color: #fff;
      font-weight: 800;
      letter-spacing: 0;
    }
    .brand-copy { min-width: 0; }
    .brand-copy strong { display: block; font-size: 15px; }
    .brand-copy span { display: block; margin-top: 2px; color: var(--muted); font-size: 11px; }
    .side-label { padding: 0 5px; color: #8a909b; font-size: 11px; font-weight: 750; text-transform: uppercase; }
    .side-nav { display: grid; gap: 5px; }
    .nav-item {
      width: 100%;
      display: flex;
      align-items: center;
      justify-content: flex-start;
      gap: 10px;
      height: 40px;
      border-color: transparent;
      background: transparent;
      color: #4b5563;
      text-align: left;
    }
    .nav-item.active { border-color: #fed7aa; background: var(--accent-soft); color: #c2410c; }
    .nav-icon { width: 22px; color: currentColor; text-align: center; font-weight: 800; }
    .site-picker { display: grid; gap: 8px; }
    .site-count { color: var(--muted); font-size: 11px; }
    .side-spacer { flex: 1; }
    .side-status {
      padding: 11px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
    }
    .side-status span { display: block; color: var(--muted); font-size: 11px; }
    .side-status strong { display: block; margin-top: 4px; font-size: 12px; overflow-wrap: anywhere; }
    .workspace { min-width: 0; min-height: 0; display: grid; grid-template-rows: 72px minmax(0, 1fr); }
    .topbar {
      min-width: 0;
      display: grid;
      grid-template-columns: minmax(150px, 0.7fr) minmax(340px, 1.4fr) auto;
      align-items: center;
      gap: 16px;
      padding: 12px 18px;
      border-bottom: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.96);
    }
    .page-heading { min-width: 0; }
    .page-heading h1 { margin: 0; font-size: 18px; letter-spacing: 0; }
    .page-heading span { display: block; margin-top: 4px; color: var(--muted); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .tabs {
      min-width: 0;
      display: grid;
      grid-template-columns: repeat(5, minmax(58px, 1fr));
      gap: 4px;
      padding: 5px;
      border: 1px solid #e8e8eb;
      border-radius: 8px;
      background: #f2f2f4;
    }
    .tab-button { height: 36px; border-color: transparent; background: transparent; padding: 0 8px; font-size: 12px; }
    .tab-button.active { border-color: var(--border); background: #fff; color: #111827; box-shadow: 0 2px 7px rgba(17, 24, 39, 0.08); }
    .top-actions { display: flex; align-items: center; justify-content: flex-end; gap: 7px; }
    .top-actions button { padding: 0 10px; }
    .view-host { min-width: 0; min-height: 0; overflow: hidden; }
    .view { width: 100%; height: 100%; min-width: 0; min-height: 0; }
    .price-view {
      display: grid;
      grid-template-rows: auto auto auto minmax(260px, 1fr) auto;
      gap: 12px;
      padding: 16px 18px 14px;
      overflow: hidden;
    }
    .summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .metric { border: 1px solid var(--border); border-radius: 8px; background: var(--panel); padding: 11px 13px; }
    .metric span { display: block; color: var(--muted); font-size: 11px; }
    .metric strong { display: block; margin-top: 4px; font-size: 18px; letter-spacing: 0; }
    .metric:last-child strong { font-size: 13px; line-height: 1.45; }
    .category-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .category-card { min-width: 0; border: 1px solid var(--border); border-radius: 8px; background: var(--panel); padding: 9px 11px; }
    .category-card strong { display: block; font-size: 13px; }
    .category-card span { display: block; margin-top: 4px; color: var(--muted); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .filter-bar {
      display: grid;
      grid-template-columns: minmax(190px, 1.7fr) minmax(130px, 1fr) minmax(105px, 0.7fr) minmax(100px, 0.7fr) auto;
      gap: 9px;
      align-items: end;
      padding: 10px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel);
    }
    .toggle { display: flex; align-items: center; gap: 7px; min-height: 36px; color: #4b5563; }
    .table-wrap { min-height: 260px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; background: var(--panel); }
    table { width: 100%; min-width: 1040px; border-collapse: collapse; font-size: 12px; }
    th, td { border-bottom: 1px solid #eceef1; padding: 9px 10px; text-align: left; vertical-align: top; }
    th { position: sticky; top: 0; z-index: 2; background: #f7f7f8; color: #555d69; font-weight: 750; }
    tbody tr:hover td { background: #fafafa; }
    td small { display: block; color: var(--muted); margin-top: 3px; line-height: 1.35; }
    .subgroup-row td { background: #fff7ed; color: #9a3412; font-weight: 750; }
    .category-badge { display: inline-block; min-width: 68px; border-radius: 6px; background: #eef2ff; color: #3730a3; padding: 2px 7px; font-weight: 750; }
    .empty { color: #818794; text-align: center; padding: 34px 10px; }
    .pager { display: flex; align-items: center; justify-content: flex-end; gap: 8px; min-height: 32px; color: var(--muted); font-size: 12px; }
    .pager button { width: 34px; height: 32px; padding: 0; font-size: 18px; }
    .site-view { padding: 18px; overflow: auto; }
    .section-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 14px; margin-bottom: 16px; }
    .section-head h2 { margin: 0; font-size: 18px; }
    .section-head p { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
    .settings-layout { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(270px, 0.75fr); gap: 14px; align-items: start; }
    .tool-panel { border: 1px solid var(--border); border-radius: 8px; background: var(--panel); padding: 16px; }
    .tool-panel h3 { margin: 0 0 14px; font-size: 14px; }
    .settings-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .field-wide { grid-column: 1 / -1; }
    .toggle-list { display: grid; gap: 8px; margin-bottom: 14px; }
    .action-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .action-grid .wide { grid-column: 1 / -1; }
    .logs-view { padding: 18px; }
    .console { height: 100%; min-height: 0; border: 1px solid #202833; border-radius: 8px; overflow: hidden; background: #0d1117; }
    .console-head { display: flex; align-items: center; justify-content: space-between; height: 40px; padding: 0 12px; border-bottom: 1px solid #252d38; background: #151b23; color: #e5e7eb; font-size: 12px; font-weight: 750; }
    .console-head span:last-child { color: #93c5fd; }
    .log { height: calc(100% - 40px); overflow: auto; padding: 12px; color: #c9d1d9; font: 12px/1.55 Consolas, "Microsoft YaHei", monospace; white-space: pre-wrap; }
    @media (max-width: 900px) {
      .app-shell { grid-template-columns: 178px minmax(0, 1fr); }
      .workspace { grid-template-rows: 116px minmax(0, 1fr); }
      .topbar { grid-template-columns: minmax(130px, 1fr) auto; grid-template-rows: auto auto; gap: 8px 12px; }
      .tabs { grid-column: 1 / -1; grid-row: 2; }
      .top-actions { grid-column: 2; grid-row: 1; }
      .category-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .filter-bar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .price-view { grid-template-rows: auto auto auto 360px auto; overflow: auto; }
      .settings-layout { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">S2</div>
        <div class="brand-copy">
          <strong>Sub2API Monitor</strong>
          <span>中转站当前价格</span>
        </div>
      </div>

      <div>
        <div class="side-label">工作区</div>
        <nav class="side-nav" aria-label="工作区导航">
          <button class="nav-item active" type="button" data-view="prices"><span class="nav-icon">¥</span>当前价格</button>
          <button class="nav-item" type="button" data-view="sites"><span class="nav-icon">S</span>站点与授权</button>
          <button class="nav-item" type="button" data-view="logs"><span class="nav-icon">›_</span>运行日志</button>
        </nav>
      </div>

      <div class="site-picker">
        <div class="side-label">当前站点</div>
        <select id="savedSiteSelect" aria-label="选择已保存站点"></select>
        <span class="site-count" id="siteCountLabel">0 个已保存站点</span>
      </div>

      <div class="side-spacer"></div>
      <div class="side-status">
        <span>运行状态 · v__APP_VERSION__</span>
        <strong id="status">等待登录</strong>
      </div>
    </aside>

    <main class="workspace">
      <header class="topbar">
        <div class="page-heading">
          <h1 id="viewTitle">当前价格</h1>
          <span id="snapshotTime">尚未生成价格快照</span>
        </div>
        <div class="tabs" id="categoryTabs"></div>
        <div class="top-actions">
          <button id="updateAllBtn" type="button" title="更新所有站点">↻ 更新全部</button>
          <button id="exportCsvBtn" type="button" title="导出 CSV" disabled>↓ CSV</button>
          <button id="exportJsonBtn" type="button" title="导出 JSON" disabled>↓ JSON</button>
        </div>
      </header>

      <div class="view-host">
        <section class="view price-view" id="pricesView">
          <div class="summary">
            <div class="metric"><span>当前套餐</span><strong id="planCount">0</strong></div>
            <div class="metric"><span>当前分组</span><strong id="groupCount">0</strong></div>
            <div class="metric"><span>模型分类</span><strong id="categoryCount">0</strong></div>
            <div class="metric"><span>状态</span><strong id="stateBadge">待更新</strong></div>
          </div>

          <div class="category-strip" id="categoryStrip"></div>

          <div class="filter-bar">
            <label>
              搜索
              <input id="filterInput" type="search" spellcheck="false" placeholder="站点、分组、套餐、平台" />
            </label>
            <label>
              站点
              <select id="siteFilterSelect"></select>
            </label>
            <label>
              类型
              <select id="typeFilterSelect">
                <option value="">全部</option>
                <option value="plan">套餐</option>
                <option value="group">分组</option>
                <option value="error">错误</option>
              </select>
            </label>
            <label>
              最高倍率
              <input id="maxRateInput" type="number" min="0" step="0.01" placeholder="不限" />
            </label>
            <label class="toggle">
              <input id="priceOnlyInput" type="checkbox" />
              只看价格/倍率
            </label>
          </div>

          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>类型</th>
                  <th>站点</th>
                  <th>模型分类</th>
                  <th>分组</th>
                  <th>平台</th>
                  <th>套餐</th>
                  <th>价格</th>
                  <th>CNY</th>
                  <th>有效期</th>
                  <th>倍率</th>
                </tr>
              </thead>
              <tbody id="resultBody">
                <tr><td colspan="10" class="empty">暂无当前价格</td></tr>
              </tbody>
            </table>
          </div>

          <div class="pager" id="pager">
            <span id="rowCount">0 行</span>
            <button id="prevPageBtn" type="button" aria-label="上一页" title="上一页">&lsaquo;</button>
            <span id="pageStatus">1 / 1</span>
            <button id="nextPageBtn" type="button" aria-label="下一页" title="下一页">&rsaquo;</button>
          </div>
        </section>

        <section class="view site-view" id="sitesView" hidden>
          <div class="section-head">
            <div>
              <h2>站点与授权</h2>
              <p>保存站点配置，登录后抓取当前套餐和分组倍率。</p>
            </div>
          </div>

          <div class="settings-layout">
            <section class="tool-panel">
              <h3>站点配置</h3>
              <div class="settings-grid">
                <label class="field-wide">
                  站点地址
                  <input id="siteInput" type="url" spellcheck="false" placeholder="https://example.com" />
                </label>
                <label>
                  保存备注
                  <input id="siteNameInput" type="text" spellcheck="false" placeholder="默认使用站点地址" />
                </label>
                <label>
                  API 路径
                  <input id="apiBaseInput" type="text" spellcheck="false" value="/api/v1" />
                </label>
                <label>
                  更新间隔（小时）
                  <input id="intervalHoursInput" type="number" min="0.05" step="0.25" value="3" />
                </label>
              </div>
            </section>

            <section class="tool-panel">
              <h3>授权与操作</h3>
              <div class="toggle-list">
                <label class="toggle"><input id="includeGroupsInput" type="checkbox" checked />包含分组倍率</label>
                <label class="toggle"><input id="rememberCredentialsInput" type="checkbox" checked />保存密码到 Windows 凭据管理器</label>
                <label class="toggle"><input id="autoLoginInput" type="checkbox" checked />允许自动填写与登录</label>
              </div>
              <div class="action-grid">
                <button id="saveSiteBtn" type="button">保存站点</button>
                <button id="clearCredentialsBtn" type="button">清除密码</button>
                <button id="openSiteBtn" type="button" class="wide">打开 WebView 登录</button>
                <button id="refreshLoginBtn" type="button">手动刷新验证页</button>
                <button id="hideLoginBtn" type="button">暂时跳过本站</button>
                <button id="captureBtn" type="button" class="primary wide">抓取当前价格</button>
                <button id="deleteSiteBtn" type="button" class="danger wide">删除站点</button>
              </div>
            </section>
          </div>
        </section>

        <section class="view logs-view" id="logsView" hidden>
          <div class="console">
            <div class="console-head">
              <span>运行日志</span>
              <span id="consoleStatus">ready</span>
            </div>
            <div class="log" id="logBox"></div>
          </div>
        </section>
      </div>
    </main>
  </div>

  <script>
    const siteInput = document.querySelector('#siteInput');
    const savedSiteSelect = document.querySelector('#savedSiteSelect');
    const siteNameInput = document.querySelector('#siteNameInput');
    const intervalHoursInput = document.querySelector('#intervalHoursInput');
    const apiBaseInput = document.querySelector('#apiBaseInput');
    const includeGroupsInput = document.querySelector('#includeGroupsInput');
    const rememberCredentialsInput = document.querySelector('#rememberCredentialsInput');
    const autoLoginInput = document.querySelector('#autoLoginInput');
    const openSiteBtn = document.querySelector('#openSiteBtn');
    const refreshLoginBtn = document.querySelector('#refreshLoginBtn');
    const hideLoginBtn = document.querySelector('#hideLoginBtn');
    const captureBtn = document.querySelector('#captureBtn');
    const saveSiteBtn = document.querySelector('#saveSiteBtn');
    const deleteSiteBtn = document.querySelector('#deleteSiteBtn');
    const clearCredentialsBtn = document.querySelector('#clearCredentialsBtn');
    const updateAllBtn = document.querySelector('#updateAllBtn');
    const exportCsvBtn = document.querySelector('#exportCsvBtn');
    const exportJsonBtn = document.querySelector('#exportJsonBtn');
    const resultBody = document.querySelector('#resultBody');
    const statusText = document.querySelector('#status');
    const stateBadge = document.querySelector('#stateBadge');
    const logBox = document.querySelector('#logBox');
    const consoleStatus = document.querySelector('#consoleStatus');
    const categoryStrip = document.querySelector('#categoryStrip');
    const categoryTabs = document.querySelector('#categoryTabs');
    const filterInput = document.querySelector('#filterInput');
    const siteFilterSelect = document.querySelector('#siteFilterSelect');
    const typeFilterSelect = document.querySelector('#typeFilterSelect');
    const maxRateInput = document.querySelector('#maxRateInput');
    const priceOnlyInput = document.querySelector('#priceOnlyInput');
    const rowCount = document.querySelector('#rowCount');
    const planCount = document.querySelector('#planCount');
    const groupCount = document.querySelector('#groupCount');
    const categoryCount = document.querySelector('#categoryCount');
    const pager = document.querySelector('#pager');
    const prevPageBtn = document.querySelector('#prevPageBtn');
    const nextPageBtn = document.querySelector('#nextPageBtn');
    const pageStatus = document.querySelector('#pageStatus');
    const viewTitle = document.querySelector('#viewTitle');
    const snapshotTime = document.querySelector('#snapshotTime');
    const siteCountLabel = document.querySelector('#siteCountLabel');
    const pricesView = document.querySelector('#pricesView');
    const sitesView = document.querySelector('#sitesView');
    const logsView = document.querySelector('#logsView');
    const navItems = [...document.querySelectorAll('.nav-item[data-view]')];
    let rows = [];
    let savedSites = [];
    let latestGeneratedAt = '';
    let stateRevision = 0;
    let activeCategory = '';
    let currentPage = 0;
    let renderDebounceTimer = null;
    let statusPollTimer = null;
    let statusPollBusy = false;
    let lastHandledUpdateId = 0;
    const logLines = [];
    const PAGE_SIZE = 200;
    const MAX_LOG_LINES = 240;
    let loginAutoCaptureTimer = null;
    let loginAutoCaptureBusy = false;
    let loginAutoCaptureMode = '';
    let loginOpenBusy = false;
    let lastAutoCaptureError = '';
    let reauthQueue = [];
    let reauthActiveSite = '';
    let updateRunning = false;
    let observedUpdateId = 0;
    const deferredReauthSites = new Set();
    let noteTouched = false;
    let lastAutoNote = '';
    let activeView = 'prices';
    const CATEGORY_ORDER = [
      'OpenAI',
      'Anthropic',
      'Gemini',
      'Grok',
    ];

    function escapeHtml(value) {
      return String(value ?? '').replace(/\s+/g, ' ').trim()
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function log(message) {
      const line = `[${new Date().toLocaleTimeString()}] ${message}`;
      logLines.push(line);
      if (logLines.length > MAX_LOG_LINES) logLines.splice(0, logLines.length - MAX_LOG_LINES);
      logBox.textContent = logLines.join('\n');
      logBox.scrollTop = logBox.scrollHeight;
    }

    function scheduleRender(resetPage = true) {
      if (resetPage) currentPage = 0;
      if (renderDebounceTimer) clearTimeout(renderDebounceTimer);
      renderDebounceTimer = setTimeout(() => {
        renderDebounceTimer = null;
        render();
      }, 120);
    }

    function setState(text) {
      statusText.textContent = text;
      stateBadge.textContent = text;
      consoleStatus.textContent = text;
    }

    function setView(view) {
      const views = { prices: pricesView, sites: sitesView, logs: logsView };
      const titles = { prices: '当前价格', sites: '站点与授权', logs: '运行日志' };
      activeView = views[view] ? view : 'prices';
      for (const [name, element] of Object.entries(views)) {
        element.hidden = name !== activeView;
      }
      for (const button of navItems) {
        button.classList.toggle('active', button.dataset.view === activeView);
      }
      viewTitle.textContent = titles[activeView];
      if (activeView === 'prices') render();
    }

    function categoryRank(category) {
      const index = CATEGORY_ORDER.indexOf(category || '');
      return index >= 0 ? index : CATEGORY_ORDER.length;
    }

    function numericPrice(row) {
      const value = row.pay_price_cny || row.price;
      const n = Number(value);
      return Number.isFinite(n) ? n : null;
    }

    function numericRate(row) {
      const matched = String(row.rate_multiplier ?? '').replace(/,/g, '').match(/[-+]?\d+(?:\.\d+)?/);
      const n = matched ? Number(matched[0]) : Number.POSITIVE_INFINITY;
      return Number.isFinite(n) ? n : Number.POSITIVE_INFINITY;
    }

    function compareRate(a, b) {
      const left = numericRate(a);
      const right = numericRate(b);
      if (left === right) return 0;
      if (!Number.isFinite(left)) return 1;
      if (!Number.isFinite(right)) return -1;
      return left - right;
    }

    function groupLabel(row) {
      return row.group_name || row.group_id || '未分组';
    }

    function rateLabel(row) {
      const rate = numericRate(row);
      return Number.isFinite(rate) ? String(rate) : '无倍率';
    }

    function candidateLabel(row) {
      const site = row.site_host || row.site || '未知站点';
      const platform = row.group_platform ? ` · ${row.group_platform}` : '';
      return `倍率 ${rateLabel(row)} · ${site} · ${groupLabel(row)}${platform}`;
    }

    function formatDateTime(value) {
      if (!value) return '';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return '';
      return date.toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      });
    }

    function priceSortValue(row) {
      const price = numericPrice(row);
      return price === null ? Number.POSITIVE_INFINITY : price;
    }

    function recordTypeLabel(type) {
      if (type === 'plan') return '套餐';
      if (type === 'group') return '分组';
      if (type === 'error') return '错误';
      return type || '';
    }

    function rowSiteValue(row) {
      return row.site_host || row.site || '';
    }

    function rowHasPriceOrRate(row) {
      return numericPrice(row) !== null || Number.isFinite(numericRate(row));
    }

    function rowSearchText(row) {
      return [
        row.site,
        row.site_host,
        row.record_type,
        row.model_category,
        row.model_names,
        row.group_id,
        row.group_name,
        row.group_platform,
        row.plan_id,
        row.plan_name,
        row.price,
        row.pay_price_cny,
        row.rate_multiplier,
        row.description,
        row.error,
      ].map((value) => String(value ?? '').toLowerCase()).join(' ');
    }

    function compareDisplayRows(a, b) {
      if (activeCategory) {
        return compareRate(a, b)
          || priceSortValue(a) - priceSortValue(b)
          || rowSiteValue(a).localeCompare(rowSiteValue(b), 'zh-CN')
          || groupLabel(a).localeCompare(groupLabel(b), 'zh-CN')
          || String(a.group_platform || '').localeCompare(String(b.group_platform || ''), 'zh-CN')
          || String(a.record_type || '').localeCompare(String(b.record_type || ''), 'zh-CN')
          || String(a.plan_name || '').localeCompare(String(b.plan_name || ''), 'zh-CN');
      }
      return priceSortValue(a) - priceSortValue(b)
        || compareRate(a, b)
        || categoryRank(a.model_category) - categoryRank(b.model_category)
        || String(a.model_category || '').localeCompare(String(b.model_category || ''), 'zh-CN')
        || rowSiteValue(a).localeCompare(rowSiteValue(b), 'zh-CN')
        || groupLabel(a).localeCompare(groupLabel(b), 'zh-CN')
        || String(a.group_platform || '').localeCompare(String(b.group_platform || ''), 'zh-CN')
        || String(a.record_type || '').localeCompare(String(b.record_type || ''), 'zh-CN')
        || String(a.plan_name || '').localeCompare(String(b.plan_name || ''), 'zh-CN');
    }

    function getFilteredRows() {
      const query = filterInput.value.trim().toLowerCase();
      const selectedSite = siteFilterSelect.value;
      const selectedType = typeFilterSelect.value;
      const maxRate = maxRateInput.value === '' ? null : Number(maxRateInput.value);
      return rows.filter((row) => {
        if (activeCategory && row.model_category !== activeCategory) return false;
        if (selectedSite && rowSiteValue(row) !== selectedSite) return false;
        if (selectedType && row.record_type !== selectedType) return false;
        if (priceOnlyInput.checked && !rowHasPriceOrRate(row)) return false;
        if (Number.isFinite(maxRate)) {
          const rate = numericRate(row);
          if (!Number.isFinite(rate) || rate > maxRate) return false;
        }
        if (query && !rowSearchText(row).includes(query)) return false;
        return true;
      }).sort(compareDisplayRows);
    }

    function renderCategoryTabs() {
      const tabs = [
        { value: '', label: '全部', count: rows.length },
        ...CATEGORY_ORDER.map((category) => ({
          value: category,
          label: category,
          count: rows.filter((row) => row.model_category === category).length,
        })),
      ];
      categoryTabs.innerHTML = tabs.map((tab) => {
        const active = tab.value === activeCategory ? ' active' : '';
        return `<button class="tab-button${active}" type="button" data-category="${escapeHtml(tab.value)}">${escapeHtml(tab.label)} ${tab.count}</button>`;
      }).join('');
      for (const button of categoryTabs.querySelectorAll('button')) {
        button.addEventListener('click', () => {
          activeCategory = button.dataset.category || '';
          currentPage = 0;
          if (activeView === 'prices') render();
          else setView('prices');
        });
      }
    }

    function renderSiteFilterOptions() {
      const selected = siteFilterSelect.value;
      const sites = [...new Set(rows.map(rowSiteValue).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b, 'zh-CN'));
      siteFilterSelect.innerHTML = '<option value="">全部站点</option>' + sites.map((site) => (
        `<option value="${escapeHtml(site)}">${escapeHtml(site)}</option>`
      )).join('');
      if (sites.includes(selected)) siteFilterSelect.value = selected;
    }

    function renderCategoryStrip(sourceRows) {
      const stats = new Map();
      for (const row of sourceRows) {
        const category = row.model_category || '其他';
        if (!CATEGORY_ORDER.includes(category)) continue;
        const current = stats.get(category) || { plans: 0, groups: 0, minPrice: null, minRate: null, sites: new Set() };
        if (row.record_type === 'plan') current.plans += 1;
        if (row.record_type === 'group') current.groups += 1;
        if (row.site_host) current.sites.add(row.site_host);
        const price = numericPrice(row);
        if (price !== null && (current.minPrice === null || price < current.minPrice)) current.minPrice = price;
        const rate = numericRate(row);
        if (Number.isFinite(rate) && (current.minRate === null || rate < current.minRate)) current.minRate = rate;
        stats.set(category, current);
      }
      const cards = [...stats.entries()]
        .sort((a, b) => categoryRank(a[0]) - categoryRank(b[0]) || a[0].localeCompare(b[0], 'zh-CN'))
        .map(([category, stat]) => {
          const priceText = stat.minPrice === null ? '暂无价格' : `最低价 ${stat.minPrice}`;
          const rateText = stat.minRate === null ? '暂无倍率' : `最低倍率 ${stat.minRate}`;
          return `<div class="category-card">
            <strong>${escapeHtml(category)}</strong>
            <span>${stat.sites.size} 站 · ${stat.plans} 套餐 · ${stat.groups} 分组 · ${escapeHtml(rateText)} · ${escapeHtml(priceText)}</span>
          </div>`;
        });
      categoryStrip.innerHTML = cards.length ? cards.join('') : '';
    }

    function renderSavedSites() {
      const selected = savedSiteSelect.value;
      savedSiteSelect.innerHTML = '<option value="">选择已保存站点</option>' + savedSites.map((site) => {
        const interval = site.interval_minutes ? ` · ${Math.round(site.interval_minutes / 60 * 100) / 100}h` : '';
        const next = site.next_run ? ` · 下次 ${formatDateTime(site.next_run)}` : '';
        const statusLabels = {
          ok: '正常',
          error: '失败',
          reauth_required: '需重新授权',
          cloudflare_challenge: 'Cloudflare 验证未完成',
          timeout: '请求或页面超时',
          http_error: 'HTTP 接口错误',
          unsupported_response: '接口响应不支持',
          no_price_data: '未发现价格数据',
          network_error: '网络错误',
          window_closed: '登录窗口已关闭',
          unknown_error: '未知错误',
        };
        const statusText = site.last_status_label || statusLabels[site.last_status] || site.last_status;
        const status = statusText ? ` · ${statusText}` : '';
        const credential = site.credentials_saved ? ' · 已保存密码' : '';
        const name = site.name && site.name !== site.site ? `${site.name} · ${site.site}` : site.site;
        const label = `${name}${interval}${next}${status}${credential}`;
        return `<option value="${escapeHtml(site.site)}">${escapeHtml(label)}</option>`;
      }).join('');
      if (savedSites.some((site) => site.site === selected)) savedSiteSelect.value = selected;
      siteCountLabel.textContent = `${savedSites.length} 个已保存站点`;
    }

    function render() {
      renderCategoryTabs();
      renderSiteFilterOptions();
      const displayRows = getFilteredRows();
      const pageCount = Math.max(1, Math.ceil(displayRows.length / PAGE_SIZE));
      currentPage = Math.min(currentPage, pageCount - 1);
      const pageStart = currentPage * PAGE_SIZE;
      const pageRows = displayRows.slice(pageStart, pageStart + PAGE_SIZE);
      const plans = displayRows.filter((row) => row.record_type === 'plan');
      const groups = displayRows.filter((row) => row.record_type === 'group');
      const categories = new Set(displayRows.map((row) => row.model_category).filter((category) => CATEGORY_ORDER.includes(category)));
      planCount.textContent = String(plans.length);
      groupCount.textContent = String(groups.length);
      categoryCount.textContent = String(categories.size);
      rowCount.textContent = `${displayRows.length}/${rows.length} 行 · ${currentPage + 1}/${pageCount} 页`;
      snapshotTime.textContent = latestGeneratedAt
        ? `当前快照 ${formatDateTime(latestGeneratedAt)}`
        : '尚未生成价格快照';
      exportCsvBtn.disabled = rows.length === 0;
      exportJsonBtn.disabled = rows.length === 0;
      renderCategoryStrip(displayRows);
      pager.hidden = displayRows.length <= PAGE_SIZE;
      prevPageBtn.disabled = currentPage <= 0;
      nextPageBtn.disabled = currentPage >= pageCount - 1;
      pageStatus.textContent = `${currentPage + 1} / ${pageCount}`;

      if (!rows.length) {
        resultBody.innerHTML = '<tr><td colspan="10" class="empty">暂无数据</td></tr>';
        return;
      }

      if (!displayRows.length) {
        resultBody.innerHTML = '<tr><td colspan="10" class="empty">没有符合筛选条件的数据</td></tr>';
        return;
      }

      let currentCandidate = '';
      const htmlRows = [];
      for (const row of pageRows) {
        const category = row.model_category || '其他';
        const candidate = `${rateLabel(row)}|${row.site_host || row.site || ''}|${groupLabel(row)}|${row.group_platform || ''}`;
        if (activeCategory && candidate !== currentCandidate) {
          currentCandidate = candidate;
          htmlRows.push(`<tr class="subgroup-row"><td colspan="10">${escapeHtml(candidateLabel(row))}</td></tr>`);
        }
        const validity = [row.validity_days, row.validity_unit].filter(Boolean).join(' ');
        htmlRows.push(`<tr>
          <td>${escapeHtml(recordTypeLabel(row.record_type))}</td>
          <td>${escapeHtml(row.site_host || row.site)}</td>
          <td><span class="category-badge">${escapeHtml(category)}</span><small>${escapeHtml(row.model_names)}</small></td>
          <td>${escapeHtml(row.group_name || row.group_id)}</td>
          <td>${escapeHtml(row.group_platform)}</td>
          <td>${escapeHtml(row.plan_name || row.description)}</td>
          <td>${escapeHtml(row.price)}</td>
          <td>${escapeHtml(row.pay_price_cny)}</td>
          <td>${escapeHtml(validity)}</td>
          <td>${escapeHtml(row.rate_multiplier)}</td>
        </tr>`);
      }
      resultBody.innerHTML = htmlRows.join('');
    }

    function applySavedSite(site) {
      if (!site) return;
      siteInput.value = site.site || '';
      siteNameInput.value = site.name || site.site || '';
      lastAutoNote = siteNameInput.value;
      noteTouched = false;
      apiBaseInput.value = site.api_base || '/api/v1';
      intervalHoursInput.value = site.interval_minutes ? String(Math.round(site.interval_minutes / 60 * 100) / 100) : '3';
      rememberCredentialsInput.checked = site.remember_credentials !== false;
      autoLoginInput.checked = site.auto_login !== false && rememberCredentialsInput.checked;
    }

    function syncDefaultNote(force = false) {
      const site = siteInput.value.trim();
      if (!site) return;
      if (force || !noteTouched || !siteNameInput.value.trim() || siteNameInput.value === lastAutoNote) {
        siteNameInput.value = site;
        lastAutoNote = site;
        noteTouched = false;
      }
    }

    async function saveSite() {
      const result = await window.pywebview.api.save_site(
        siteNameInput.value,
        siteInput.value,
        apiBaseInput.value,
        intervalHoursInput.value,
        rememberCredentialsInput.checked,
        autoLoginInput.checked
      );
      if (!result.ok) {
        log(result.error);
        return;
      }
      savedSites = result.saved_sites || [];
      stateRevision = Number(result.revision) || stateRevision;
      renderSavedSites();
      savedSiteSelect.value = result.site;
      const saved = savedSites.find((item) => item.site === result.site);
      if (saved) applySavedSite(saved);
      log(`已保存站点：${result.site}`);
    }

    async function deleteSite() {
      const result = await window.pywebview.api.delete_site(siteInput.value || savedSiteSelect.value);
      if (!result.ok) {
        log(result.error);
        return;
      }
      savedSites = result.saved_sites || [];
      stateRevision = Number(result.revision) || stateRevision;
      reauthQueue = reauthQueue.filter((item) => item.site !== result.site);
      if (reauthActiveSite === result.site) {
        stopLoginAutoCapture();
        reauthActiveSite = '';
        setTimeout(startNextReauth, 0);
      }
      renderSavedSites();
      log(result.credentials_deleted
        ? `已删除站点及其保存密码：${result.site}`
        : `已删除站点：${result.site}`);
    }

    async function clearCredentials() {
      const site = siteInput.value || savedSiteSelect.value;
      const result = await window.pywebview.api.clear_credentials(site);
      if (!result.ok) {
        log(result.error);
        return;
      }
      savedSites = result.saved_sites || savedSites;
      stateRevision = Number(result.revision) || stateRevision;
      renderSavedSites();
      savedSiteSelect.value = result.site;
      log(result.deleted ? `已清除站点密码：${result.site}` : `该站点没有已保存密码：${result.site}`);
    }

    function stopLoginAutoCapture() {
      if (loginAutoCaptureTimer) {
        clearInterval(loginAutoCaptureTimer);
        loginAutoCaptureTimer = null;
      }
      loginAutoCaptureBusy = false;
      loginAutoCaptureMode = '';
      lastAutoCaptureError = '';
    }

    function reauthLabel(record) {
      return record?.name && record.name !== record.site
        ? `${record.name} (${record.site})`
        : record?.site || '未知站点';
    }

    async function requestLoginWindow(site) {
      if (loginOpenBusy) {
        return { ok: false, busy: true, error: '登录窗口正在打开，请稍候。' };
      }
      loginOpenBusy = true;
      openSiteBtn.disabled = true;
      let timeoutId = null;
      try {
        const timeout = new Promise((resolve) => {
          timeoutId = setTimeout(() => resolve({
            ok: false,
            timeout: true,
            error: '打开登录窗口超时，请再次点击 WebView登录。',
          }), 20000);
        });
        return await Promise.race([
          window.pywebview.api.open_site(site),
          timeout,
        ]);
      } catch (error) {
        return { ok: false, error: String(error?.message || error || '打开登录窗口失败') };
      } finally {
        if (timeoutId) clearTimeout(timeoutId);
        loginOpenBusy = false;
        openSiteBtn.disabled = false;
      }
    }

    function finishActiveReauth() {
      if (!reauthActiveSite) return;
      const completed = reauthActiveSite;
      reauthActiveSite = '';
      log(`重新授权完成：${completed}`);
      setTimeout(startNextReauth, 500);
    }

    function enqueueReauthSites(sites) {
      for (const record of sites || []) {
        if (!record?.site || record.site === reauthActiveSite) continue;
        if (deferredReauthSites.has(record.site)) continue;
        if (reauthQueue.some((item) => item.site === record.site)) continue;
        reauthQueue.push(record);
        log(`检测到站点授权失效：${reauthLabel(record)}`);
      }
      if (!reauthActiveSite && !loginAutoCaptureTimer && reauthQueue.length) {
        setTimeout(startNextReauth, 0);
      }
    }

    async function startNextReauth() {
      if (updateRunning || reauthActiveSite || loginAutoCaptureTimer || !reauthQueue.length) return;
      const record = reauthQueue.shift();
      reauthActiveSite = record.site;
      const saved = savedSites.find((item) => item.site === record.site) || record;
      applySavedSite(saved);
      savedSiteSelect.value = record.site;
      setState('需要重新授权');
      log(`正在打开重新授权窗口：${reauthLabel(record)}`);
      const result = await requestLoginWindow(record.site);
      if (!result.ok) {
        log(`重新授权窗口打开失败：${result.error}`);
        reauthActiveSite = '';
        if (result.update_running) {
          reauthQueue.unshift(record);
          setTimeout(startNextReauth, 1000);
          return;
        }
        if (result.timeout) {
          reauthQueue.unshift(record);
          setState('重新授权窗口超时');
          return;
        }
        setTimeout(startNextReauth, 500);
        return;
      }
      siteInput.value = result.site;
      setState('等待重新授权');
      log('请在 WebView 中重新登录；授权恢复后会自动抓取并继续处理下一个站点。');
      startLoginAutoCapture('reauth');
    }

    async function tryLoginAutoCapture() {
      if (loginAutoCaptureBusy) return;
      loginAutoCaptureBusy = true;
      try {
        const mode = loginAutoCaptureMode;
        const ok = await capture({ auto: true });
        if (ok) {
          stopLoginAutoCapture();
          if (mode === 'reauth') {
            finishActiveReauth();
          } else {
            log('已自动抓取价格并收起 WebView 登录窗口');
            setTimeout(startNextReauth, 500);
          }
        }
      } finally {
        loginAutoCaptureBusy = false;
      }
    }

    function startLoginAutoCapture(mode = 'login') {
      stopLoginAutoCapture();
      loginAutoCaptureMode = mode;
      log(mode === 'reauth'
        ? '已启动重新授权检测：登录恢复后会自动抓取。'
        : '已启动登录后自动抓取：完成登录后会自动抓取价格并收起 WebView 窗口。');
      loginAutoCaptureTimer = setInterval(tryLoginAutoCapture, 5000);
      setTimeout(tryLoginAutoCapture, 1500);
    }

    async function openSite() {
      const result = await requestLoginWindow(siteInput.value);
      if (!result.ok) {
        log(result.error);
        return;
      }
      siteInput.value = result.site;
      deferredReauthSites.delete(result.site);
      render();
      setState('WebView 登录中');
      log(`已在 WebView 打开：${result.site}`);
      log('请在目标站点窗口完成登录；登录态可用后应用会自动抓取并收起该窗口。');
      startLoginAutoCapture(reauthActiveSite === result.site ? 'reauth' : 'login');
    }

    async function refreshLoginWebView() {
      stopLoginAutoCapture();
      refreshLoginBtn.disabled = true;
      setState('正在刷新登录页');
      try {
        const result = await window.pywebview.api.refresh_login_webview();
        if (!result.ok) {
          setState('刷新失败');
          log(result.error);
          return;
        }
        siteInput.value = result.site;
        setState('等待人工验证');
        log(`已手动刷新验证页：${result.site}`);
        log('请完成验证；完成后点击“WebView抓取”，应用不会自动刷新此页面。');
      } finally {
        refreshLoginBtn.disabled = false;
      }
    }

    async function hideLoginWebView() {
      stopLoginAutoCapture();
      hideLoginBtn.disabled = true;
      try {
        const result = await window.pywebview.api.hide_login_webview();
        if (!result.ok) {
          log(result.error);
          return;
        }
        const skippedSite = result.site || reauthActiveSite || siteInput.value;
        if (skippedSite) {
          deferredReauthSites.add(skippedSite);
          reauthQueue = reauthQueue.filter((item) => item.site !== skippedSite);
          log(`本轮已暂时跳过：${skippedSite}`);
        }
        if (reauthActiveSite) {
          reauthActiveSite = '';
          setTimeout(startNextReauth, 500);
        }
        setState('已暂时跳过本站');
        log('登录窗口已收起；本站在本轮更新中不会再次自动弹出。');
      } finally {
        hideLoginBtn.disabled = false;
      }
    }

    async function capture(options = {}) {
      const auto = Boolean(options.auto);
      if (!auto) stopLoginAutoCapture();
      captureBtn.disabled = true;
      setState(auto ? '等待登录完成' : 'WebView 抓取中');
      if (!auto) log('开始从 WebView 当前登录页抓取价格接口');
      try {
        const started = await window.pywebview.api.start_capture_prices(
          apiBaseInput.value,
          includeGroupsInput.checked,
          true
        );
        let result = started;
        if (started.ok) {
          const jobId = started.job_id;
          while (true) {
            await new Promise((resolve) => setTimeout(resolve, 250));
            const job = await window.pywebview.api.capture_status(jobId);
            if (!job.ok) {
              result = job;
              break;
            }
            if (['completed', 'failed', 'cancelled'].includes(job.status)) {
              result = job.result || { ok: false, error: job.message || '抓取任务未返回结果' };
              break;
            }
          }
        }
        if (!result.ok) {
          if (auto) {
            if (['cloudflare_challenge', 'timeout', 'network_error'].includes(result.error_code)) {
              stopLoginAutoCapture();
              setState('自动检测已暂停');
              log(`${result.status_label || '页面异常'}：自动检测已暂停。`);
              log('请手动点击“手动刷新验证页”“WebView抓取”或“暂时跳过本站”。');
              return false;
            }
            if (result.error_code === 'window_closed') {
              stopLoginAutoCapture();
              const skippedSite = reauthActiveSite || siteInput.value;
              if (skippedSite) deferredReauthSites.add(skippedSite);
              await window.pywebview.api.hide_login_webview();
              if (reauthActiveSite) {
                reauthQueue = reauthQueue.filter((item) => item.site !== reauthActiveSite);
                reauthActiveSite = '';
              }
              setState(result.status_label || '登录窗口已关闭');
              log(`${result.status_label || '自动抓取已暂停'}：本站在本轮已暂时跳过。`);
              setTimeout(startNextReauth, 500);
              return false;
            }
            setState('等待登录完成');
            if (result.error && result.error !== lastAutoCaptureError) {
              lastAutoCaptureError = result.error;
              log(`自动抓取等待中：${result.error}`);
            }
          } else {
            setState('抓取失败');
            log(result.error);
          }
          return false;
        }
        rows = result.rows || [];
        savedSites = result.saved_sites || savedSites;
        latestGeneratedAt = result.generated_at || latestGeneratedAt;
        stateRevision = Number(result.revision) || stateRevision;
        currentPage = 0;
        renderSavedSites();
        setView('prices');
        const errorOnly = rows.length > 0 && rows.every((row) => row.record_type === 'error');
        setState(errorOnly ? '未获取到价格' : '抓取完成');
        log(`认证方式：${result.tokenKey || 'cookie/session'}`);
        log(`WebView 抓取完成：${rows.length} 行`);
        if (!auto && reauthActiveSite) {
          finishActiveReauth();
        } else if (!auto) {
          setTimeout(startNextReauth, 500);
        }
        return true;
      } finally {
        captureBtn.disabled = false;
      }
    }

    async function exportRows(format) {
      const result = await window.pywebview.api.export_results(format);
      if (result.cancelled) return;
      if (!result.ok) {
        log(result.error);
        return;
      }
      log(`已导出：${result.path}`);
    }

    async function updateAllSaved(reason = 'manual') {
      setState(reason === 'startup' ? '启动抓取中' : '更新全部中');
      log(reason === 'startup' ? '启动后自动抓取所有已保存站点' : '开始更新所有已保存站点');
      const result = await window.pywebview.api.start_update_all_prices(reason, false);
      if (!result.ok) {
        setState('更新失败');
        log(result.error);
        return;
      }
      if (result.busy) {
        if (result.login_active) {
          setState('等待人工处理');
          log('登录/验证窗口仍在处理，请先抓取成功或点击“暂时跳过本站”。');
        } else {
          setState('更新已在运行');
          log('已有更新任务正在运行，本次操作不会重复启动。');
        }
      } else if (result.accepted) {
        updateAllBtn.disabled = true;
        rows = [];
        latestGeneratedAt = '';
        currentPage = 0;
        render();
        snapshotTime.textContent = '正在重建当前价格快照';
        setState('后台更新中');
      }
      scheduleStatusPoll(250);
    }

    async function startAutoCheck() {
      const result = await window.pywebview.api.start_scheduler();
      if (!result.ok) {
        log(result.error);
        return;
      }
      if (!reauthActiveSite && !reauthQueue.length) {
        setState('自动检查中');
      }
      log('自动检查已启动，会按各站点设置的间隔检查所有保存的网站');
    }

    function scheduleStatusPoll(delay = 8000) {
      if (statusPollTimer) clearTimeout(statusPollTimer);
      statusPollTimer = setTimeout(pollSchedulerStatus, delay);
    }

    function applyRuntimeStatus(result) {
      savedSites = result.saved_sites || savedSites;
      renderSavedSites();
      if (result.rows_changed) {
        stateRevision = Number(result.revision) || stateRevision;
        latestGeneratedAt = result.latest_generated_at || latestGeneratedAt;
        rows = result.rows || [];
        currentPage = 0;
        render();
      } else if (result.revision) {
        stateRevision = Number(result.revision) || stateRevision;
      }

      const update = result.update || {};
      const updating = update.status === 'running';
      const updateId = Number(update.id) || 0;
      if (updating && updateId && updateId !== observedUpdateId) {
        observedUpdateId = updateId;
        deferredReauthSites.clear();
      }
      updateRunning = updating;
      updateAllBtn.disabled = updating;
      if (updating) {
        const completed = Number(update.completed_sites) || 0;
        const total = Number(update.total_sites) || 0;
        setState(total ? `更新中 ${completed}/${total}` : '后台更新中');
      } else if (update.id && update.id > lastHandledUpdateId && ['completed', 'failed'].includes(update.status)) {
        lastHandledUpdateId = update.id;
        if (update.status === 'completed') {
          const summary = update.summary || {};
          const reauthSites = result.reauth_sites || [];
          setState(reauthSites.length ? '需要重新授权' : '更新完成');
          log(`已更新 ${summary.site_count || 0} 个站点，${summary.success_count || 0} 成功，${summary.error_count || 0} 失败，当前 ${rows.length} 行`);
          if (reauthSites.length) {
            log(`${reauthSites.length} 个站点登录态已失效，将逐个打开 WebView 重新授权。`);
          }
        } else {
          setState('更新失败');
          log(update.error || update.message || '后台更新失败');
        }
      }
      if (!updating) {
        enqueueReauthSites(result.reauth_sites || []);
      }
      if (!updating && result.running && !reauthActiveSite && !reauthQueue.length && !update.id) {
        stateBadge.textContent = '自动检查中';
      }
    }

    async function pollSchedulerStatus() {
      if (statusPollBusy) return;
      statusPollBusy = true;
      let delay = 8000;
      try {
        const result = await window.pywebview.api.scheduler_status(stateRevision);
        if (!result.ok) return;
        applyRuntimeStatus(result);
        delay = result.update?.status === 'running' ? 1000 : 8000;
      } catch (error) {
        delay = 3000;
      } finally {
        statusPollBusy = false;
        scheduleStatusPoll(delay);
      }
    }

    async function init() {
      const state = await window.pywebview.api.initial_state();
      siteInput.value = state.site;
      savedSites = state.saved_sites || [];
      rows = state.latest_rows || [];
      latestGeneratedAt = state.latest_generated_at || '';
      stateRevision = Number(state.revision) || 0;
      renderSavedSites();
      setView('prices');
      const currentSite = savedSites.find((item) => item.site === state.site);
      if (currentSite) {
        applySavedSite(currentSite);
        savedSiteSelect.value = currentSite.site;
      } else if (siteInput.value) {
        syncDefaultNote(true);
      }
      if (state.site) {
        log('请点击“WebView登录”，在目标站点窗口完成登录。');
      } else {
        log('请输入目标站点地址，然后点击“WebView登录”。');
      }
      await startAutoCheck();
      if (savedSites.length) {
        await updateAllSaved('startup');
      }
      scheduleStatusPoll(500);
    }

    siteInput.addEventListener('input', () => {
      syncDefaultNote();
    });
    siteNameInput.addEventListener('input', () => {
      noteTouched = true;
    });
    savedSiteSelect.addEventListener('change', () => {
      const site = savedSites.find((item) => item.site === savedSiteSelect.value);
      applySavedSite(site);
      if (site) setView('sites');
    });
    for (const button of navItems) {
      button.addEventListener('click', () => setView(button.dataset.view));
    }
    filterInput.addEventListener('input', () => scheduleRender(true));
    siteFilterSelect.addEventListener('change', () => { currentPage = 0; render(); });
    typeFilterSelect.addEventListener('change', () => { currentPage = 0; render(); });
    maxRateInput.addEventListener('input', () => scheduleRender(true));
    priceOnlyInput.addEventListener('change', () => { currentPage = 0; render(); });
    prevPageBtn.addEventListener('click', () => {
      if (currentPage > 0) {
        currentPage -= 1;
        render();
      }
    });
    nextPageBtn.addEventListener('click', () => {
      currentPage += 1;
      render();
    });
    rememberCredentialsInput.addEventListener('change', () => {
      if (!rememberCredentialsInput.checked) autoLoginInput.checked = false;
    });
    autoLoginInput.addEventListener('change', () => {
      if (autoLoginInput.checked) rememberCredentialsInput.checked = true;
    });
    saveSiteBtn.addEventListener('click', saveSite);
    deleteSiteBtn.addEventListener('click', deleteSite);
    clearCredentialsBtn.addEventListener('click', clearCredentials);
    openSiteBtn.addEventListener('click', openSite);
    refreshLoginBtn.addEventListener('click', refreshLoginWebView);
    hideLoginBtn.addEventListener('click', hideLoginWebView);
    captureBtn.addEventListener('click', capture);
    updateAllBtn.addEventListener('click', () => {
      setView('prices');
      updateAllSaved('manual');
    });
    exportCsvBtn.addEventListener('click', () => exportRows('csv'));
    exportJsonBtn.addEventListener('click', () => exportRows('json'));
    window.addEventListener('pywebviewready', init);
  </script>
</body>
</html>
"""


class BrowserCredentialBridge:
    def __init__(self, api):
        self.api = api

    def save_credentials(self, username="", password=""):
        return self.api.save_browser_credentials(username, password)


class RemoteWindowEvent:
    def __init__(self, manager=None, initialized=False):
        self.manager = manager
        self.initialized = initialized

    def is_set(self):
        return False

    def wait(self, timeout=None):
        if not self.initialized:
            return False
        try:
            self.manager.ensure_started(timeout=timeout)
            return True
        except Exception:
            return False

    def __iadd__(self, handler):
        return self


class RemoteWindow:
    def __init__(self, manager, name):
        self.manager = manager
        self.name = name
        self.events = type("RemoteWindowEvents", (), {
            "initialized": RemoteWindowEvent(manager, initialized=True),
            "closing": RemoteWindowEvent(),
            "closed": RemoteWindowEvent(),
        })()

    def load_url(self, url):
        self.manager.call("load_url", self.name, {"url": url})

    def get_current_url(self):
        return self.manager.call("get_current_url", self.name) or ""

    def evaluate_js(self, script, callback=None):
        if callback is None:
            return self.manager.call(
                "evaluate_js",
                self.name,
                {"script": script, "timeout": BROWSER_IPC_TIMEOUT_SECONDS},
                timeout=BROWSER_IPC_TIMEOUT_SECONDS + 3,
            )

        def invoke():
            try:
                result = self.manager.call(
                    "evaluate_js",
                    self.name,
                    {"script": script, "timeout": 65},
                    timeout=70,
                )
            except Exception as exc:
                result = {"message": str(exc), "error_code": "webview_host_error"}
            callback(result)

        threading.Thread(
            target=invoke,
            name=f"remote-js-{self.name}",
            daemon=True,
        ).start()
        return True

    def show(self):
        self.manager.call("show", self.name)

    def restore(self):
        self.manager.call("restore", self.name)

    def hide(self):
        self.manager.call("hide", self.name)

    def destroy(self):
        return None


class BrowserProcessManager:
    def __init__(self, storage_path):
        self.storage_path = pathlib.Path(storage_path)
        self.token = secrets.token_urlsafe(32)
        self.port = self._free_port()
        self.process = None
        self.ready_pid = None
        self.opener = build_opener(ProxyHandler({}))
        self.lock = threading.RLock()
        self.generation = 0

    @staticmethod
    def _free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def window(self, name):
        return RemoteWindow(self, name)

    def _command(self):
        executable = pathlib.Path(sys.executable)
        command = [str(executable)]
        if not getattr(sys, "frozen", False):
            command.append(str(pathlib.Path(__file__).resolve()))
        command.extend([
            "--browser-host",
            "--browser-host-port", str(self.port),
            "--browser-host-token", self.token,
            "--browser-host-storage", str(self.storage_path),
        ])
        return command

    def ensure_started(self, timeout=None):
        timeout = BROWSER_HOST_START_TIMEOUT_SECONDS if timeout is None else float(timeout)
        with self.lock:
            if self.process and self.process.poll() is None:
                if self.ready_pid == self.process.pid:
                    return
            else:
                self._terminate_locked()
                self.storage_path.mkdir(parents=True, exist_ok=True)
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                self.process = subprocess.Popen(
                    self._command(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
                self.ready_pid = None
                self.generation += 1
                LOGGER.info("Started isolated WebView host generation=%s pid=%s", self.generation, self.process.pid)
            process = self.process

        deadline = time.monotonic() + max(1.0, timeout)
        last_error = ""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"WebView 子进程启动失败，退出码 {process.returncode}")
            try:
                self._request({"command": "ping"}, timeout=1)
                with self.lock:
                    if self.process is process and process.poll() is None:
                        self.ready_pid = process.pid
                return
            except Exception as exc:
                last_error = str(exc)
                time.sleep(0.2)
        self.mark_failed("startup_timeout")
        raise RuntimeError(f"WebView 子进程启动超时：{last_error}")

    def call(self, command, window=None, payload=None, timeout=BROWSER_IPC_TIMEOUT_SECONDS):
        self.ensure_started()
        request_payload = {
            "command": command,
            "window": window or "",
            "payload": payload or {},
        }
        started = time.monotonic()
        try:
            result = self._request(request_payload, timeout=timeout)
            LOGGER.debug(
                "WebView IPC command=%s window=%s duration=%.3f",
                command,
                window or "",
                time.monotonic() - started,
            )
            return result
        except Exception as exc:
            LOGGER.warning(
                "WebView IPC failed command=%s window=%s duration=%.3f error=%s",
                command,
                window or "",
                time.monotonic() - started,
                exc,
            )
            self.mark_failed(f"{command}_failed")
            raise RuntimeError(f"WebView 子进程无响应，已自动重启：{exc}") from exc

    def _request(self, payload, timeout):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"http://127.0.0.1:{self.port}/command",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Sub2API-Token": self.token,
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=max(0.2, float(timeout))) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "WebView 子进程调用失败")
        return result.get("result")

    def mark_failed(self, reason):
        with self.lock:
            LOGGER.warning("Recycling isolated WebView host: %s", reason)
            self._terminate_locked()

    def _terminate_locked(self):
        process = self.process
        self.process = None
        self.ready_pid = None
        if not process:
            return
        try:
            parent = psutil.Process(process.pid)
            owned = parent.children(recursive=True)
            for child in reversed(owned):
                try:
                    child.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            try:
                parent.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            _, alive = psutil.wait_procs([*owned, parent], timeout=2)
            for item in alive:
                try:
                    item.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except psutil.NoSuchProcess:
            pass

    def shutdown(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                try:
                    self._request({"command": "shutdown"}, timeout=2)
                except Exception:
                    pass
            self._terminate_locked()


class BrowserHostCredentialBridge:
    def __init__(self):
        self.site = ""

    def save_credentials(self, username="", password=""):
        try:
            if not self.site:
                return {"ok": False, "error": "当前没有选择站点"}
            write_site_credentials(self.site, username, password)
            return {"ok": True, "site": self.site}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


class BrowserHostRuntime:
    def __init__(self, port, token, storage_path):
        self.port = int(port)
        self.token = token
        self.storage_path = pathlib.Path(storage_path)
        self.credential_bridge = BrowserHostCredentialBridge()
        self.windows = {}
        self.server = None
        self.shutting_down = threading.Event()
        self.login_cancelled = threading.Event()

    def attach_windows(self, login, workers):
        self.windows["login"] = login
        for slot, worker in enumerate(workers):
            self.windows[f"worker-{slot}"] = worker
        login.events.closing += self._on_login_closing

    def _on_login_closing(self, window=None):
        if self.shutting_down.is_set():
            return None
        self.login_cancelled.set()
        threading.Thread(target=self.windows["login"].hide, daemon=True).start()
        return False

    def execute(self, request):
        command = request.get("command")
        if command == "ping":
            return {"version": APP_VERSION}
        if command == "shutdown":
            threading.Thread(target=self.stop, daemon=True).start()
            return True
        name = request.get("window") or ""
        window = self.windows.get(name)
        if not window:
            raise RuntimeError(f"未知 WebView 窗口：{name}")
        payload = request.get("payload") or {}
        if command == "load_url":
            url = str(payload.get("url") or BLANK_PAGE)
            if name == "login":
                self.credential_bridge.site = normalize_site(url) if url != BLANK_PAGE else ""
                self.login_cancelled.clear()
            window.load_url(url)
            return True
        if command == "get_current_url":
            return window.get_current_url() or ""
        if command == "show":
            window.show()
            return True
        if command == "restore":
            window.restore()
            return True
        if command == "hide":
            if name == "login":
                self.login_cancelled.set()
            window.hide()
            return True
        if command == "evaluate_js":
            script = str(payload.get("script") or "")
            timeout = max(1.0, float(payload.get("timeout") or BROWSER_IPC_TIMEOUT_SECONDS))
            done = threading.Event()
            holder = {}

            def callback(result):
                holder["result"] = result
                done.set()

            immediate = window.evaluate_js(script, callback=callback)
            if immediate not in (True, "true", None):
                return immediate
            if not done.wait(timeout):
                raise TimeoutError("WebView JavaScript 执行超时")
            return holder.get("result")
        raise RuntimeError(f"未知 WebView 命令：{command}")

    def serve(self):
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/command" or self.headers.get("X-Sub2API-Token") != runtime.token:
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    request = json.loads(self.rfile.read(length).decode("utf-8"))
                    response = {"ok": True, "result": runtime.execute(request)}
                except Exception as exc:
                    response = {"ok": False, "error": str(exc)}
                body = json.dumps(response, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        class Server(ThreadingHTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", self.port), Handler)
        self.server.serve_forever(poll_interval=0.2)

    def stop(self):
        if self.shutting_down.is_set():
            return
        self.shutting_down.set()
        if self.server:
            self.server.shutdown()
        for window in list(self.windows.values()):
            try:
                window.destroy()
            except Exception:
                pass


def run_browser_host(args):
    runtime = BrowserHostRuntime(
        args.browser_host_port,
        args.browser_host_token,
        args.browser_host_storage,
    )
    login = webview.create_window(
        "目标站点 WebView 登录",
        url=BLANK_PAGE,
        js_api=runtime.credential_bridge,
        width=1120,
        height=820,
        min_size=(760, 520),
        hidden=True,
        focus=False,
        text_select=True,
    )
    workers = [
        webview.create_window(
            f"后台价格抓取 {slot + 1}",
            url=BLANK_PAGE,
            width=960,
            height=720,
            min_size=(640, 480),
            hidden=True,
            focus=False,
            text_select=False,
        )
        for slot in range(MAX_SITE_WORKERS)
    ]
    runtime.attach_windows(login, workers)
    webview.start(
        runtime.serve,
        gui="edgechromium",
        private_mode=False,
        storage_path=str(runtime.storage_path),
    )
    return 0


class PriceAppApi:
    def __init__(self, site=""):
        self.site = normalize_site(site) if str(site or "").strip() else ""
        self.rows = load_latest_rows()
        self.browser_window = None
        self.worker_windows = [None] * MAX_SITE_WORKERS
        self.controller_window = None
        self.credential_bridge = BrowserCredentialBridge(self)
        self.auto_login_attempts = {}
        self.browser_lock = threading.Lock()
        self.worker_window_lock = threading.Lock()
        self.interactive_operation_lock = threading.Lock()
        self.worker_operation_locks = [threading.Lock() for _ in range(MAX_SITE_WORKERS)]
        self.data_lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.job_lock = threading.RLock()
        self.capture_job_lock = threading.RLock()
        self.update_lock = threading.Lock()
        self.shutdown_lock = threading.RLock()
        self.capture_threads_lock = threading.Lock()
        self.scheduler_stop = threading.Event()
        self.scheduler_thread = None
        self.scheduler_message = "自动检查未启动"
        self.browser_cancel_event = threading.Event()
        self.login_session_active = threading.Event()
        self.shutdown_event = threading.Event()
        self.shutdown_complete = threading.Event()
        self.shutdown_thread = None
        self.capture_threads = set()
        self.browser_operation_id = 0
        self.latest_generated_at = self._latest_generated_at()
        self.cached_saved_sites = annotate_saved_sites(load_saved_sites())
        self.state_revision = 1
        self.update_thread = None
        self.update_job_sequence = 0
        self.update_job = self._idle_update_job()
        self.capture_job_thread = None
        self.capture_job_sequence = 0
        self.capture_job = self._idle_capture_job()

    def attach_controller_window(self, window):
        self.controller_window = window
        if window:
            window.events.closing += self._on_controller_closing
            window.events.closed += self._on_controller_closed
        return window

    def attach_browser_window(self, window):
        self.browser_window = window
        if window:
            window.events.closing += self._on_browser_closing
            window.events.closed += self._on_browser_closed
        return window

    def attach_worker_window(self, window, slot=0):
        self.worker_windows[slot] = window
        if window:
            window.events.closed += (
                lambda closed_window=None, worker_slot=slot: self._on_worker_closed(
                    worker_slot, closed_window
                )
            )
        return window

    def _on_browser_closing(self, window=None):
        if self.shutdown_event.is_set():
            return None
        if window is None or window is self.browser_window:
            self.browser_cancel_event.set()
            threading.Thread(
                target=self._hide_browser_window,
                args=(self.browser_operation_id,),
                name="hide-login-window",
                daemon=True,
            ).start()
        return False

    def _on_browser_closed(self, window=None):
        if window is None or window is self.browser_window:
            self.browser_cancel_event.set()
            self.browser_window = None

    def _on_worker_closed(self, slot, window=None):
        if window is None or window is self.worker_windows[slot]:
            self.worker_windows[slot] = None

    def _on_controller_closing(self, window=None):
        self.request_shutdown("main_window_closing")

    def _on_controller_closed(self, window=None):
        if window is None or window is self.controller_window:
            self.controller_window = None
        self.request_shutdown("main_window_closed")

    def _raise_if_shutting_down(self):
        if self.shutdown_event.is_set():
            raise AppShutdownRequested("APP_SHUTDOWN: 应用正在退出")

    def _signal_shutdown(self, reason):
        with self.shutdown_lock:
            first_request = not self.shutdown_event.is_set()
            self.shutdown_event.set()
            self.scheduler_stop.set()
            self.browser_cancel_event.set()
            self.scheduler_message = "应用正在退出"
        if first_request:
            LOGGER.info("Shutdown requested: %s", reason)
            with self.job_lock:
                if self.update_job.get("status") == "running":
                    self.update_job.update({
                        "status": "cancelling",
                        "message": "应用正在退出，正在取消更新",
                        "current_site": "",
                    })
            with self.capture_job_lock:
                if self.capture_job.get("status") == "running":
                    self.capture_job.update({
                        "status": "cancelling",
                        "message": "应用正在退出，正在取消抓取",
                    })
        return first_request

    def request_shutdown(self, reason="requested"):
        self._signal_shutdown(reason)
        with self.shutdown_lock:
            if self.shutdown_thread and self.shutdown_thread.is_alive():
                return
            if self.shutdown_complete.is_set():
                return
            self.shutdown_thread = threading.Thread(
                target=self._shutdown_sequence,
                name="app-shutdown",
                daemon=True,
            )
            self.shutdown_thread.start()

    def _shutdown_sequence(self):
        try:
            self._destroy_auxiliary_windows()
            self._join_background_threads(SHUTDOWN_JOIN_TIMEOUT_SECONDS)
            self._destroy_auxiliary_windows()
        except Exception:
            LOGGER.exception("Unexpected error during shutdown")
        finally:
            self.shutdown_complete.set()
            LOGGER.info("Shutdown cleanup completed")

    def _destroy_auxiliary_windows(self):
        browser_lock_acquired = self.browser_lock.acquire(timeout=0.05)
        try:
            browser = self.browser_window
            self.browser_window = None
        finally:
            if browser_lock_acquired:
                self.browser_lock.release()
        if not browser_lock_acquired:
            LOGGER.warning("Browser window lock was busy during shutdown; using fallback cleanup")

        worker_lock_acquired = self.worker_window_lock.acquire(timeout=0.05)
        try:
            workers = [window for window in self.worker_windows if window]
            self.worker_windows = [None] * MAX_SITE_WORKERS
        finally:
            if worker_lock_acquired:
                self.worker_window_lock.release()
        if not worker_lock_acquired:
            LOGGER.warning("Worker window lock was busy during shutdown; using fallback cleanup")
        for window in [browser, *workers]:
            if not window:
                continue
            try:
                window.destroy()
            except Exception:
                LOGGER.exception("Failed to destroy auxiliary WebView window")

    def _join_background_threads(self, timeout):
        deadline = time.monotonic() + max(0.0, float(timeout))
        current = threading.current_thread()
        with self.capture_threads_lock:
            capture_threads = list(self.capture_threads)
        threads = [
            self.scheduler_thread,
            self.update_thread,
            self.capture_job_thread,
            *capture_threads,
        ]
        seen = set()
        for thread in threads:
            if not thread or thread is current or id(thread) in seen:
                continue
            seen.add(id(thread))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        lingering = [
            thread.name
            for thread in threads
            if thread and thread is not current and thread.is_alive()
        ]
        if lingering:
            LOGGER.warning("Daemon threads still stopping at exit: %s", ", ".join(lingering))

    def shutdown(self, wait=True, timeout=SHUTDOWN_JOIN_TIMEOUT_SECONDS):
        self.request_shutdown("main_finally")
        thread = self.shutdown_thread
        if wait and thread and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)) + 0.25)
        if wait and not self.shutdown_complete.is_set():
            LOGGER.warning("Shutdown cleanup exceeded %.1f seconds", float(timeout))
        return self.shutdown_complete.is_set()

    @staticmethod
    def _window_alive(window):
        if not window:
            return False
        try:
            closed_event = getattr(getattr(window, "events", None), "closed", None)
            return not (closed_event and closed_event.is_set())
        except Exception:
            return True

    def _browser_alive(self):
        if self._window_alive(self.browser_window):
            return True
        self.browser_window = None
        return False

    def _worker_alive(self, slot):
        if self._window_alive(self.worker_windows[slot]):
            return True
        self.worker_windows[slot] = None
        return False

    @staticmethod
    def _wait_window_initialized(window, timeout=WINDOW_INITIALIZE_TIMEOUT_SECONDS):
        initialized_event = getattr(getattr(window, "events", None), "initialized", None)
        if initialized_event and not initialized_event.wait(timeout):
            raise RuntimeError("WebView 窗口初始化超时，请重启应用")

    def _ensure_browser_window(self, url=None, visible=False):
        self._raise_if_shutting_down()
        if not self.browser_lock.acquire(timeout=WINDOW_LOCK_TIMEOUT_SECONDS):
            raise RuntimeError("WebView 窗口正在切换，请稍后重试")
        try:
            self._raise_if_shutting_down()
            if not self._browser_alive():
                raise RuntimeError("登录 WebView 已不可用，请重启应用")
            window = self.browser_window
        finally:
            self.browser_lock.release()

        try:
            self._wait_window_initialized(window)
            if url:
                self.browser_cancel_event.clear()
                self.browser_operation_id += 1
                LOGGER.info(
                    "Login WebView navigation operation=%s host=%s",
                    self.browser_operation_id,
                    urlparse(url).netloc,
                )
                window.load_url(url)
                self._raise_if_shutting_down()
            if visible:
                try:
                    window.show()
                except Exception:
                    pass
                try:
                    window.restore()
                except Exception:
                    pass
            return window
        except Exception:
            if window is self.browser_window:
                self.browser_cancel_event.set()
                self.browser_window = None
            raise

    def _ensure_worker_window(self, slot, url=None):
        self._raise_if_shutting_down()
        if not self.worker_window_lock.acquire(timeout=WINDOW_LOCK_TIMEOUT_SECONDS):
            raise RuntimeError("后台 WebView 正在切换，请稍后重试")
        try:
            self._raise_if_shutting_down()
            if not self._worker_alive(slot):
                raise RuntimeError(f"后台 WebView {slot + 1} 已不可用，请重启应用")
            window = self.worker_windows[slot]
        finally:
            self.worker_window_lock.release()
        self._wait_window_initialized(window)
        try:
            if url:
                LOGGER.info("Worker WebView navigation slot=%s host=%s", slot + 1, urlparse(url).netloc)
                window.load_url(url)
                self._raise_if_shutting_down()
            return window
        except Exception:
            if window is self.worker_windows[slot]:
                self.worker_windows[slot] = None
            raise

    def _hide_browser_window(self, operation_id=None):
        if operation_id is not None and operation_id != self.browser_operation_id:
            return
        if not self.browser_window:
            return
        try:
            self.browser_window.hide()
        except Exception:
            self.browser_window = None

    @staticmethod
    def _idle_update_job():
        return {
            "id": 0,
            "status": "idle",
            "reason": "",
            "message": "",
            "started_at": "",
            "completed_at": "",
            "completed_sites": 0,
            "total_sites": 0,
            "current_site": "",
            "summary": {},
            "error": "",
        }

    @staticmethod
    def _idle_capture_job():
        return {
            "id": 0,
            "status": "idle",
            "message": "",
            "started_at": "",
            "completed_at": "",
            "result": {},
        }

    def _cache_state(self, rows=None, saved_sites=None, generated_at=None):
        with self.state_lock:
            if rows is not None:
                self.rows = rows
            if saved_sites is not None:
                self.cached_saved_sites = saved_sites
            if generated_at is not None:
                self.latest_generated_at = generated_at
            self.state_revision += 1
            return self.state_revision

    def _mark_credentials_cached(self, site, present):
        normalized = normalize_site(site)
        with self.state_lock:
            changed = False
            updated_sites = []
            for item in self.cached_saved_sites:
                updated = dict(item)
                if updated.get("site") == normalized:
                    value = bool(present)
                    changed = changed or updated.get("credentials_saved") != value
                    updated["credentials_saved"] = value
                updated_sites.append(updated)
            if changed:
                self.cached_saved_sites = updated_sites
                self.state_revision += 1
            return self.state_revision

    def _state_snapshot(self, known_revision=None):
        try:
            known = int(known_revision)
        except (TypeError, ValueError):
            known = -1
        with self.state_lock:
            revision = self.state_revision
            saved_sites = [dict(item) for item in self.cached_saved_sites]
            rows = [dict(item) for item in self.rows] if known != revision else []
            generated_at = self.latest_generated_at
        with self.job_lock:
            update = dict(self.update_job)
            update["summary"] = dict(update.get("summary") or {})
        return {
            "revision": revision,
            "rows_changed": known != revision,
            "rows": rows,
            "saved_sites": saved_sites,
            "latest_generated_at": generated_at,
            "update": update,
        }

    def initial_state(self):
        snapshot = self._state_snapshot()
        return {
            "site": self.site,
            "saved_sites": snapshot["saved_sites"],
            "reauth_sites": self._reauth_sites(snapshot["saved_sites"]),
            "latest_rows": snapshot["rows"],
            "latest_generated_at": snapshot["latest_generated_at"],
            "revision": snapshot["revision"],
            "update": snapshot["update"],
        }

    def save_site(
        self,
        name,
        site,
        api_base=API_BASE,
        interval_hours=3,
        remember_credentials=True,
        auto_login=True,
        auto_refresh=True,
    ):
        try:
            normalized = normalize_site(site)
            with self.data_lock:
                if not remember_credentials:
                    delete_site_credentials(normalized)
                    self.auto_login_attempts.pop(normalized, None)
                saved = self._upsert_saved_site(
                    name,
                    normalized,
                    api_base,
                    interval_hours,
                    remember_credentials,
                    auto_login,
                )
                annotated = annotate_saved_sites(saved)
                revision = self._cache_state(saved_sites=annotated)
            return {
                "ok": True,
                "site": normalized,
                "saved_sites": annotated,
                "revision": revision,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_site(self, site):
        try:
            normalized = normalize_site(site)
            with self.data_lock:
                credentials_deleted = delete_site_credentials(normalized)
                saved = [item for item in load_saved_sites() if item.get("site") != normalized]
                write_saved_sites(saved)
                self.auto_login_attempts.pop(normalized, None)
                annotated = annotate_saved_sites(saved)
                revision = self._cache_state(saved_sites=annotated)
            return {
                "ok": True,
                "site": normalized,
                "credentials_deleted": credentials_deleted,
                "saved_sites": annotated,
                "revision": revision,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def clear_credentials(self, site):
        try:
            normalized = normalize_site(site)
            with self.data_lock:
                deleted = delete_site_credentials(normalized)
                self.auto_login_attempts.pop(normalized, None)
                revision = self._mark_credentials_cached(normalized, False)
            return {
                "ok": True,
                "site": normalized,
                "deleted": deleted,
                "saved_sites": self._state_snapshot(revision)["saved_sites"],
                "revision": revision,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def save_browser_credentials(self, username="", password=""):
        try:
            if not self.site:
                raise RuntimeError("当前没有选择站点")
            current_url = self.browser_window.get_current_url() if self.browser_window else ""
            if not self._same_site_host(self.site, current_url):
                raise RuntimeError("当前登录页与所选站点不一致，已拒绝保存密码")
            record = self._record_for_site(self.site, API_BASE)
            if record.get("remember_credentials") is False:
                return {"ok": True, "site": self.site, "ignored": True}
            write_site_credentials(self.site, username, password)
            revision = self._mark_credentials_cached(self.site, True)
            return {"ok": True, "site": self.site, "revision": revision}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_site(self, site):
        if self.shutdown_event.is_set():
            return {"ok": False, "shutting_down": True, "error": "应用正在退出"}
        if not self.interactive_operation_lock.acquire(timeout=INTERACTIVE_RELOGIN_WAIT_SECONDS):
            return {"ok": False, "busy": True, "error": "登录窗口正在抓取，请稍后再试"}
        try:
            self._raise_if_shutting_down()
            with self.job_lock:
                if self.update_thread and self.update_thread.is_alive():
                    return {
                        "ok": False,
                        "busy": True,
                        "update_running": True,
                        "error": "后台更新仍在运行，请等待完成后再打开登录窗口",
                    }
                self.login_session_active.set()
            normalized = normalize_site(site)
            self._ensure_browser_window(normalized, visible=True)
            self.site = normalized
            self._start_credential_helper(normalized)
            return {"ok": True, "site": normalized}
        except Exception as exc:
            self.login_session_active.clear()
            error = str(exc)
            failure = self._classify_capture_failure(error, self.browser_window)
            return {
                "ok": False,
                "error": error,
                "auth_required": failure["auth_required"],
                "error_code": failure["error_code"],
                "status_label": failure["status_label"],
            }
        finally:
            self.interactive_operation_lock.release()

    def refresh_login_webview(self):
        if self.shutdown_event.is_set():
            return {"ok": False, "shutting_down": True, "error": "应用正在退出"}
        if not self.site:
            return {"ok": False, "error": "请先选择并打开目标站点"}
        self.browser_cancel_event.set()
        if not self.interactive_operation_lock.acquire(timeout=INTERACTIVE_RELOGIN_WAIT_SECONDS):
            return {"ok": False, "busy": True, "error": "登录窗口仍在执行操作，请稍后再试"}
        try:
            self._raise_if_shutting_down()
            self.login_session_active.set()
            window = self._ensure_browser_window(self.site, visible=True)
            self._start_credential_helper(self.site)
            return {"ok": True, "site": self.site, "operation_id": self.browser_operation_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            self.interactive_operation_lock.release()

    def hide_login_webview(self):
        if self.shutdown_event.is_set():
            return {"ok": False, "shutting_down": True, "error": "应用正在退出"}
        self.browser_cancel_event.set()
        self.login_session_active.clear()
        if not self._browser_alive():
            return {"ok": True, "hidden": False, "site": self.site}
        threading.Thread(
            target=self._hide_browser_window,
            args=(self.browser_operation_id,),
            name="hide-login-window",
            daemon=True,
        ).start()
        return {"ok": True, "hidden": True, "site": self.site}

    def start_capture_prices(self, api_base=API_BASE, include_groups=True, close_browser=True):
        if self.shutdown_event.is_set():
            return {"ok": False, "shutting_down": True, "error": "应用正在退出"}
        with self.capture_job_lock:
            if self.capture_job_thread and self.capture_job_thread.is_alive():
                return {
                    "ok": True,
                    "accepted": False,
                    "busy": True,
                    "job_id": self.capture_job.get("id", 0),
                    "status": self.capture_job.get("status", "running"),
                }
            self.capture_job_sequence += 1
            job_id = self.capture_job_sequence
            self.capture_job = {
                "id": job_id,
                "status": "running",
                "message": "WebView 抓取中",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": "",
                "result": {},
            }
            self.capture_job_thread = threading.Thread(
                target=self._run_capture_job,
                args=(job_id, api_base, bool(include_groups), bool(close_browser)),
                name=f"interactive-capture-{job_id}",
                daemon=True,
            )
            self.capture_job_thread.start()
            return {
                "ok": True,
                "accepted": True,
                "busy": False,
                "job_id": job_id,
                "status": "running",
            }

    def _run_capture_job(self, job_id, api_base, include_groups, close_browser):
        try:
            result = self._capture_prices_sync(api_base, include_groups, close_browser)
        except Exception as exc:
            LOGGER.exception("Interactive capture job %s failed", job_id)
            result = {"ok": False, "error": str(exc)}
        with self.capture_job_lock:
            if self.capture_job.get("id") == job_id:
                cancelled = self.shutdown_event.is_set()
                self.capture_job.update({
                    "status": "cancelled" if cancelled else "completed" if result.get("ok") else "failed",
                    "message": "应用退出，抓取已取消" if cancelled else "抓取完成" if result.get("ok") else "抓取失败",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "result": result,
                })
            if self.capture_job_thread is threading.current_thread():
                self.capture_job_thread = None

    def capture_status(self, job_id=0):
        try:
            requested_id = int(job_id or 0)
        except (TypeError, ValueError):
            requested_id = 0
        with self.capture_job_lock:
            job = dict(self.capture_job)
            job["result"] = dict(job.get("result") or {})
        if requested_id and job.get("id") != requested_id:
            return {"ok": False, "error": "抓取任务不存在或已过期"}
        return {"ok": True, **job}

    def _capture_prices_sync(self, api_base=API_BASE, include_groups=True, close_browser=True):
        if self.shutdown_event.is_set():
            return {"ok": False, "shutting_down": True, "error": "应用正在退出"}
        if not self.interactive_operation_lock.acquire(blocking=False):
            return {"ok": False, "busy": True, "error": "WebView 正在执行其他操作"}
        try:
            self._raise_if_shutting_down()
            if not self.site:
                raise RuntimeError("请先打开目标站点")
            if self.browser_cancel_event.is_set() and not self._browser_alive():
                raise RuntimeError("WINDOW_CLOSED: 登录窗口已关闭，请重新点击 WebView 登录")
            if self._browser_alive():
                self._ensure_browser_window(visible=False)
            else:
                self._ensure_browser_window(self.site, visible=False)
            window = self.browser_window
            credential_state = self._prepare_login_credentials(self.site, window, allow_save=True)
            if credential_state.get("autoSubmitted"):
                self._wait_for_auto_login(self.site, window, allow_save=True)
            base = str(api_base or API_BASE).strip()
            if not base.startswith("/"):
                base = f"/{base}"
            options = {
                "base": base,
                "includeGroups": bool(include_groups),
                "outputFields": OUTPUT_FIELDS,
                "requestTimeoutMs": 25000,
            }
            script = collector_js_template().replace("__OPTIONS__", json.dumps(options, ensure_ascii=False))
            result = self._evaluate_async(window, script, timeout=60)
            self._raise_if_shutting_down()
            if not isinstance(result, dict):
                raise RuntimeError(f"抓取脚本返回了异常结果：{result!r}")
            if "rows" not in result:
                raise RuntimeError(result.get("message") or json.dumps(result, ensure_ascii=False))
            fresh_rows = result.get("rows") or []
            if not self._has_price_rows(fresh_rows):
                error_code = self._rows_error_code(fresh_rows)
                message = self._rows_error_message(fresh_rows) or "还没有获取到价格，请确认已完成登录"
                raise RuntimeError(f"{error_code.upper()}: {message}" if error_code else message)
            self.auto_login_attempts.pop(self.site, None)
            with self.data_lock:
                merged_rows = replace_price_rows(load_latest_rows(), fresh_rows, [self.site])
                snapshot = write_price_snapshot(merged_rows, {
                    "site_count": 1,
                    "success_count": 1,
                    "error_count": 0,
                    "mode": "manual_webview",
                })
                record = next((item for item in load_saved_sites() if item.get("site") == self.site), None)
                if record:
                    saved_sites = self._update_site_status(record, {"ok": True, "rows": fresh_rows})
                else:
                    saved_sites = [dict(item) for item in self.cached_saved_sites]
                revision = self._cache_state(
                    rows=merged_rows,
                    saved_sites=saved_sites,
                    generated_at=snapshot["generated_at"],
                )
            if close_browser:
                self._hide_browser_window()
                self.login_session_active.clear()
            return {
                "ok": True,
                **result,
                "generated_at": snapshot["generated_at"],
                "rows": merged_rows,
                "saved_sites": saved_sites,
                "revision": revision,
            }
        except Exception as exc:
            error = str(exc)
            failure = self._classify_capture_failure(error, self.browser_window)
            return {
                "ok": False,
                "error": error,
                "auth_required": failure["auth_required"],
                "error_code": failure["error_code"],
                "status_label": failure["status_label"],
            }
        finally:
            self.interactive_operation_lock.release()

    def update_all_prices(self):
        return self.start_update_all_prices("manual", False)

    def start_update_all_prices(self, reason="manual", only_due=False):
        try:
            self._raise_if_shutting_down()
            with self.job_lock:
                if self.login_session_active.is_set():
                    return {
                        "ok": True,
                        "accepted": False,
                        "busy": True,
                        "login_active": True,
                        "update": dict(self.update_job),
                    }
                if self.update_thread and self.update_thread.is_alive():
                    return {
                        "ok": True,
                        "accepted": False,
                        "busy": True,
                        "update": dict(self.update_job),
                    }
                with self.data_lock:
                    sites = load_saved_sites()
                if not sites:
                    raise RuntimeError("还没有保存站点")
                due_sites = [
                    item for item in sites
                    if item.get("site") and (not only_due or self._is_site_due(item))
                ]
                if only_due and not due_sites:
                    return {
                        "ok": True,
                        "accepted": False,
                        "busy": False,
                        "due": False,
                        "update": dict(self.update_job),
                    }
                self.update_job_sequence += 1
                job_id = self.update_job_sequence
                self.update_job = {
                    "id": job_id,
                    "status": "running",
                    "reason": str(reason or "manual"),
                    "message": "准备更新站点",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "completed_at": "",
                    "completed_sites": 0,
                    "total_sites": len(due_sites),
                    "current_site": "",
                    "summary": {},
                    "error": "",
                }
                self.update_thread = threading.Thread(
                    target=self._run_update_job,
                    args=(job_id, sites, bool(only_due)),
                    name=f"price-update-{job_id}",
                    daemon=True,
                )
                self.update_thread.start()
                return {
                    "ok": True,
                    "accepted": True,
                    "busy": False,
                    "update": dict(self.update_job),
                }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _run_update_job(self, job_id, sites, only_due):
        try:
            with self.update_lock:
                self._raise_if_shutting_down()
                result = self._run_site_records(
                    sites,
                    only_due=only_due,
                    progress_callback=lambda completed, total, site: self._update_job_progress(
                        job_id, completed, total, site
                    ),
                )
            summary = result.get("summary") or {}
            message = (
                f"更新完成：{summary.get('site_count', 0)} 个站点，"
                f"{summary.get('success_count', 0)} 成功，"
                f"{summary.get('error_count', 0)} 失败"
            )
            with self.job_lock:
                if self.update_job.get("id") == job_id:
                    self.update_job.update({
                        "status": "completed",
                        "message": message,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "completed_sites": summary.get("site_count", 0),
                        "current_site": "",
                        "summary": summary,
                        "error": "",
                    })
            self.scheduler_message = message
        except AppShutdownRequested:
            with self.job_lock:
                if self.update_job.get("id") == job_id:
                    self.update_job.update({
                        "status": "cancelled",
                        "message": "应用退出，更新已取消",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "current_site": "",
                        "error": "",
                    })
        except Exception as exc:
            error = str(exc)
            LOGGER.exception("Price update job %s failed", job_id)
            with self.job_lock:
                if self.update_job.get("id") == job_id:
                    self.update_job.update({
                        "status": "failed",
                        "message": f"更新失败：{error}",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "current_site": "",
                        "error": error,
                    })
            self.scheduler_message = f"自动检查失败：{error}"
        finally:
            with self.job_lock:
                if self.update_thread is threading.current_thread():
                    self.update_thread = None

    def _update_job_progress(self, job_id, completed, total, site):
        with self.job_lock:
            if self.update_job.get("id") != job_id:
                return
            self.update_job.update({
                "completed_sites": int(completed),
                "total_sites": int(total),
                "current_site": str(site or ""),
                "message": f"正在更新 {completed}/{total}：{site}",
            })

    def start_scheduler(self):
        if self.shutdown_event.is_set():
            return {"ok": False, "running": False, "shutting_down": True, "error": "应用正在退出"}
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            return {"ok": True, "running": True}
        self.scheduler_stop.clear()
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.scheduler_thread.start()
        self.scheduler_message = "自动检查中"
        return {"ok": True, "running": True}

    def stop_scheduler(self):
        self.scheduler_stop.set()
        self.scheduler_message = "自动检查已停止"
        return {"ok": True, "running": False}

    def scheduler_status(self, known_revision=None):
        running = bool(self.scheduler_thread and self.scheduler_thread.is_alive())
        snapshot = self._state_snapshot(known_revision)
        return {
            "ok": True,
            "running": running,
            "message": self.scheduler_message,
            **snapshot,
            "reauth_sites": self._reauth_sites(snapshot["saved_sites"]),
        }

    def _scheduler_loop(self):
        while not self.shutdown_event.is_set() and not self.scheduler_stop.wait(30):
            result = self.start_update_all_prices("scheduler", True)
            if result.get("accepted"):
                self.scheduler_message = "后台自动检查中"
            elif not result.get("ok"):
                self.scheduler_message = f"自动检查失败：{result.get('error', '未知错误')}"

    def _run_capture_tasks(self, records, worker_count):
        tasks = queue.Queue()
        results = queue.Queue()
        for index, record in enumerate(records):
            tasks.put((index, record))

        def worker(slot):
            current = threading.current_thread()
            try:
                while not self.shutdown_event.is_set():
                    try:
                        index, record = tasks.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        self._raise_if_shutting_down()
                        result = self._capture_site_webview(record, True, slot)
                        results.put((index, record, result, None))
                    except Exception as exc:
                        results.put((index, record, None, exc))
                        if isinstance(exc, AppShutdownRequested):
                            return
                    finally:
                        tasks.task_done()
            finally:
                with self.capture_threads_lock:
                    self.capture_threads.discard(current)

        threads = [
            threading.Thread(
                target=worker,
                args=(slot,),
                name=f"site-capture-{slot + 1}",
                daemon=True,
            )
            for slot in range(worker_count)
        ]
        with self.capture_threads_lock:
            self.capture_threads.update(threads)
        for thread in threads:
            thread.start()

        completed = 0
        try:
            while completed < len(records):
                self._raise_if_shutting_down()
                try:
                    _, record, result, error = results.get(timeout=0.2)
                except queue.Empty:
                    if not any(thread.is_alive() for thread in threads):
                        raise RuntimeError("后台抓取线程意外结束")
                    continue
                if error:
                    raise error
                completed += 1
                yield record, result
        finally:
            for thread in threads:
                thread.join(0.5)

    def _run_site_records(self, sites, only_due, progress_callback=None):
        self._raise_if_shutting_down()
        due_sites = []
        for record in sites:
            if not record.get("site"):
                continue
            if only_due and not self._is_site_due(record):
                continue
            due_sites.append(record)

        if not due_sites:
            snapshot = self._state_snapshot(self.state_revision)
            saved_sites = snapshot["saved_sites"]
            return {
                "rows": [dict(item) for item in self.rows],
                "saved_sites": saved_sites,
                "reauth_sites": self._reauth_sites(saved_sites),
                "generated_at": self.latest_generated_at,
                "summary": {
                    "site_count": 0,
                    "success_count": 0,
                    "error_count": 0,
                    "reauth_count": len(self._reauth_sites(saved_sites)),
                    "skipped_count": len(sites),
                },
            }

        fresh_rows = []
        captured_updates = {}
        success_count = 0
        error_count = 0
        worker_count = min(MAX_SITE_WORKERS, len(due_sites))
        if progress_callback:
            progress_callback(0, len(due_sites), due_sites[0].get("site", ""))
        completed = 0
        for record, result in self._run_capture_tasks(due_sites, worker_count):
            self._raise_if_shutting_down()
            rows = result.get("rows") or []
            fresh_rows.extend(rows)
            updated = self._site_status_record(record, result)
            captured_updates[updated["site"]] = updated
            if result.get("ok"):
                success_count += 1
            else:
                error_count += 1
            completed += 1
            if progress_callback:
                progress_callback(completed, len(due_sites), record.get("site", ""))

        self._raise_if_shutting_down()
        with self.data_lock:
            current_records = load_saved_sites()
            status_fields = (
                "last_run",
                "last_status",
                "last_error",
                "last_error_code",
                "last_status_label",
                "reauth_required",
                "reauth_requested_at",
                "last_row_count",
                "last_plan_count",
                "next_run",
                "updated_at",
            )
            merged_sites = []
            for current in current_records:
                site = current.get("site")
                status_update = captured_updates.get(site)
                if status_update:
                    merged = dict(current)
                    for field in status_fields:
                        merged[field] = status_update.get(field)
                    merged_sites.append(merged)
                else:
                    merged_sites.append(current)
            merged_sites.sort(key=lambda item: (item.get("name") or item.get("site") or "").lower())
            write_saved_sites(merged_sites)
            annotated_sites = annotate_saved_sites(merged_sites)
            reauth_sites = self._reauth_sites(annotated_sites)
            summary = {
                "site_count": len(due_sites),
                "success_count": success_count,
                "error_count": error_count,
                "reauth_count": len(reauth_sites),
                "skipped_count": max(0, len(sites) - len(due_sites)),
            }
            refreshed_sites = [record.get("site", "") for record in due_sites]
            all_rows = replace_price_rows(load_latest_rows(), fresh_rows, refreshed_sites)
            snapshot = write_price_snapshot(all_rows, summary)
            self._cache_state(
                rows=all_rows,
                saved_sites=annotated_sites,
                generated_at=snapshot["generated_at"],
            )
        return {
            "rows": all_rows,
            "saved_sites": annotated_sites,
            "reauth_sites": reauth_sites,
            "generated_at": snapshot["generated_at"],
            "summary": summary,
        }

    def _capture_site_webview(self, record, include_groups=True, worker_slot=0):
        self._raise_if_shutting_down()
        site = normalize_site(record.get("site", ""))
        base = self._normalized_api_base(record.get("api_base", API_BASE))
        LOGGER.info("Site capture start slot=%s host=%s", worker_slot + 1, urlparse(site).netloc)
        with self.worker_operation_locks[worker_slot]:
            window = None
            try:
                self._raise_if_shutting_down()
                window = self._ensure_worker_window(worker_slot, site)
                self._wait_webview_ready(window, site, timeout=30)
                credential_state = self._prepare_login_credentials(site, window, allow_save=False)
                if credential_state.get("autoSubmitted"):
                    self._wait_for_auto_login(site, window, allow_save=False)
                options = {
                    "base": base,
                    "includeGroups": bool(include_groups),
                    "outputFields": OUTPUT_FIELDS,
                    "requestTimeoutMs": 25000,
                }
                script = collector_js_template().replace("__OPTIONS__", json.dumps(options, ensure_ascii=False))
                result = self._evaluate_async(window, script, timeout=60)
                self._raise_if_shutting_down()
                if not isinstance(result, dict):
                    raise RuntimeError(f"WebView 抓取返回了异常结果：{result!r}")
                if "rows" not in result:
                    raise RuntimeError(result.get("message") or json.dumps(result, ensure_ascii=False))
                if not self._has_price_rows(result.get("rows") or []):
                    error_code = self._rows_error_code(result.get("rows") or [])
                    message = self._rows_error_message(result.get("rows") or []) or "未获取到价格"
                    raise RuntimeError(f"{error_code.upper()}: {message}" if error_code else message)
                self.auto_login_attempts.pop(site, None)
                result["ok"] = True
                LOGGER.info("Site capture completed slot=%s host=%s", worker_slot + 1, urlparse(site).netloc)
                return result
            except AppShutdownRequested:
                raise
            except Exception as exc:
                now = datetime.now(timezone.utc).isoformat()
                error = str(exc)
                LOGGER.warning(
                    "Site capture failed slot=%s host=%s error=%s",
                    worker_slot + 1,
                    urlparse(site).netloc,
                    error,
                )
                failure = self._classify_capture_failure(error, window)
                return {
                    "ok": False,
                    "auth_required": failure["auth_required"],
                    "error_code": failure["error_code"],
                    "status_label": failure["status_label"],
                    "tokenKey": "",
                    "rows": [{
                        "site": site,
                        "site_host": urlparse(site).netloc,
                        "status": "error",
                        "source": "webview",
                        "record_type": "error",
                        "model_category": "未获取",
                        "model_names": "",
                        "fetched_at": now,
                        "error": error,
                        "error_code": failure["error_code"],
                        "status_label": failure["status_label"],
                    }],
                    "error": error,
                }

    @staticmethod
    def _same_site_host(site, current_url):
        try:
            expected = urlparse(normalize_site(site))
            current = urlparse(str(current_url or ""))
            return (
                expected.scheme in ("http", "https")
                and current.scheme in ("http", "https")
                and expected.netloc.lower() == current.netloc.lower()
            )
        except ValueError:
            return False

    def _start_credential_helper(self, site):
        if self.shutdown_event.is_set():
            return
        normalized = normalize_site(site)

        def worker():
            if not self.interactive_operation_lock.acquire(timeout=5):
                return
            try:
                self._raise_if_shutting_down()
                window = self.browser_window
                self._wait_webview_ready(window, normalized, timeout=30)
                if self.site == normalized:
                    self._prepare_login_credentials(normalized, window, allow_save=True)
            except Exception:
                return
            finally:
                self.interactive_operation_lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def _prepare_login_credentials(self, site, window=None, allow_save=False):
        try:
            self._raise_if_shutting_down()
            normalized = normalize_site(site)
            window = window or self.browser_window
            if not window:
                return {"loginForm": False}
            if window is self.browser_window and self.site != normalized:
                return {"loginForm": False}
            current_url = window.get_current_url() or ""
            if not self._same_site_host(normalized, current_url):
                return {"loginForm": False}
            record = self._record_for_site(normalized, API_BASE)
            if record.get("remember_credentials") is False:
                return {"loginForm": False}
            credential = read_site_credentials(normalized)
            if credential:
                last_attempt = float(self.auto_login_attempts.get(normalized) or 0)
                credential = {
                    **credential,
                    "autoLogin": bool(record.get("auto_login", True)),
                    "allowAutoLogin": time.time() - last_attempt >= 120,
                }
            script = CREDENTIAL_HELPER_JS.replace(
                "__CREDENTIALS__",
                json.dumps(credential, ensure_ascii=False) if credential else "null",
            )
            result = window.evaluate_js(script)
            if isinstance(result, dict) and result.get("autoSubmitted"):
                self.auto_login_attempts[normalized] = time.time()
            if (
                allow_save
                and isinstance(result, dict)
                and result.get("loginForm")
                and not result.get("ambiguous")
                and result.get("password")
            ):
                write_site_credentials(
                    normalized,
                    result.get("username") or "",
                    result.get("password") or "",
                )
                self._mark_credentials_cached(normalized, True)
            return result if isinstance(result, dict) else {"loginForm": False}
        except AppShutdownRequested:
            raise
        except Exception:
            return {"loginForm": False}

    def _wait_for_auto_login(self, site, window, allow_save=False, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._raise_if_shutting_down()
            if window is self.browser_window and self.browser_cancel_event.is_set():
                raise RuntimeError("WINDOW_CLOSED: 登录窗口已关闭")
            if self._detect_cloudflare_challenge(window):
                raise RuntimeError("CLOUDFLARE_CHALLENGE: Cloudflare 验证未完成或当前环境不兼容")
            time.sleep(0.5)
            state = self._prepare_login_credentials(site, window, allow_save=allow_save)
            if not state.get("loginForm"):
                return True
            if state.get("blockedByChallenge"):
                return False
        return False

    def _wait_webview_ready(self, window, site, timeout=45):
        self._raise_if_shutting_down()
        if not window:
            raise RuntimeError("WebView 窗口不可用")
        expected_origin = urlparse(site).netloc
        deadline = time.time() + timeout
        last_error = ""
        while time.time() < deadline:
            self._raise_if_shutting_down()
            if window is self.browser_window and self.browser_cancel_event.is_set():
                raise RuntimeError("WINDOW_CLOSED: 登录窗口已关闭")
            try:
                current_url = window.get_current_url()
                current_host = urlparse(current_url).netloc
                ready = window.evaluate_js("document.readyState")
                if self._detect_cloudflare_challenge(window):
                    raise RuntimeError("CLOUDFLARE_CHALLENGE: Cloudflare 验证未完成或当前环境不兼容")
                if current_host == expected_origin and ready in ("interactive", "complete"):
                    time.sleep(1)
                    return
            except Exception as exc:
                last_error = str(exc)
                if "CLOUDFLARE_CHALLENGE" in last_error or "WINDOW_CLOSED" in last_error:
                    raise
            time.sleep(0.5)
        raise RuntimeError(f"等待 WebView 加载 {site} 超时：{last_error}")

    def _record_for_site(self, site, api_base):
        normalized = normalize_site(site)
        with self.state_lock:
            cached = [dict(item) for item in self.cached_saved_sites]
        for record in cached:
            if record.get("site") == normalized:
                record.pop("credentials_saved", None)
                return record
        now = datetime.now(timezone.utc)
        return {
            "name": normalized,
            "site": normalized,
            "api_base": self._normalized_api_base(api_base),
            "browser_mode": "webview",
            "interval_minutes": 180,
            "auto_refresh": True,
            "remember_credentials": True,
            "auto_login": True,
            "next_run": now.isoformat(),
        }

    def _update_site_status(self, record, result):
        sites = load_saved_sites()
        updated = self._site_status_record(record, result)
        merged = [item for item in sites if item.get("site") != updated.get("site")]
        merged.append(updated)
        merged.sort(key=lambda item: (item.get("name") or item.get("site") or "").lower())
        write_saved_sites(merged)
        return annotate_saved_sites(merged)

    def _site_status_record(self, record, result):
        updated = dict(record)
        rows = result.get("rows") or []
        now = datetime.now(timezone.utc).isoformat()
        updated["site"] = normalize_site(updated.get("site", ""))
        updated["name"] = str(updated.get("name") or updated["site"])
        updated["api_base"] = self._normalized_api_base(updated.get("api_base", API_BASE))
        updated["browser_mode"] = "webview"
        updated["auto_refresh"] = True
        updated["last_run"] = now
        auth_required = bool(result.get("auth_required"))
        error_code = result.get("error_code") or ("reauth_required" if auth_required else "")
        if not result.get("ok") and not error_code:
            error_code = self._classify_error_text(result.get("error") or "")
        status_label = result.get("status_label") or self._error_label(error_code)
        updated["last_status"] = (
            "ok" if result.get("ok") else "reauth_required" if auth_required else error_code or "error"
        )
        updated["reauth_required"] = auth_required
        updated["reauth_requested_at"] = now if auth_required else ""
        updated["last_error"] = "" if result.get("ok") else (result.get("error") or "")
        updated["last_error_code"] = "" if result.get("ok") else error_code
        updated["last_status_label"] = "正常" if result.get("ok") else status_label
        updated["last_row_count"] = len(rows)
        updated["last_plan_count"] = len([row for row in rows if row.get("record_type") == "plan"])
        updated["next_run"] = schedule_next_run(updated, datetime.fromisoformat(now)).isoformat()
        updated["updated_at"] = now
        return updated

    def _is_site_due(self, record):
        next_run = parse_datetime(record.get("next_run"))
        if not next_run:
            return True
        return datetime.now(timezone.utc) >= next_run

    @staticmethod
    def _normalized_api_base(api_base):
        base = str(api_base or API_BASE).strip() or API_BASE
        return base if base.startswith("/") else f"/{base}"

    @staticmethod
    def _interval_minutes(interval_hours):
        try:
            hours = float(interval_hours)
        except (TypeError, ValueError):
            hours = 3.0
        return max(1, int(round(hours * 60)))

    @staticmethod
    def _has_price_rows(rows):
        return any(
            isinstance(row, dict)
            and row.get("record_type") in ("plan", "group")
            and row.get("status") != "error"
            for row in rows or []
        )

    @staticmethod
    def _rows_error_message(rows):
        messages = [
            str(row.get("error") or "").strip()
            for row in rows or []
            if isinstance(row, dict) and row.get("error")
        ]
        return " | ".join(message for message in messages if message)

    @staticmethod
    def _rows_error_code(rows):
        for row in rows or []:
            if isinstance(row, dict) and row.get("error_code"):
                return str(row.get("error_code") or "").strip()
        return ""

    @staticmethod
    def _error_label(code):
        return ERROR_LABELS.get(code or "", ERROR_LABELS["unknown_error"])

    @staticmethod
    def _looks_like_cloudflare(text):
        value = str(text or "")
        return any(re.search(pattern, value, re.IGNORECASE) for pattern in CLOUDFLARE_PATTERNS)

    @staticmethod
    def _classify_error_text(error, current_url=""):
        text = f"{error or ''} {current_url or ''}"
        if "WINDOW_CLOSED" in text:
            return "window_closed"
        if PriceAppApi._looks_like_cloudflare(text):
            return "cloudflare_challenge"
        if "REAUTH_REQUIRED" in text:
            return "reauth_required"
        if "TIMEOUT" in text:
            return "timeout"
        if "HTTP_ERROR" in text:
            return "http_error"
        if "UNSUPPORTED_RESPONSE" in text:
            return "unsupported_response"
        if "NETWORK_ERROR" in text:
            return "network_error"
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in TIMEOUT_PATTERNS):
            return "timeout"
        if re.search(r"\bHTTP\s+\d{3}\b", text, re.IGNORECASE):
            return "http_error"
        if "rows" in text and "result" in text:
            return "unsupported_response"
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in NETWORK_ERROR_PATTERNS):
            return "network_error"
        if re.search(r"未获取到价格|未发现价格|no_price|no price|no_price_found|no pricing", text, re.IGNORECASE):
            return "no_price_data"
        return "unknown_error"

    def _classify_capture_failure(self, error, window=None):
        if "WebView 子进程无响应" in str(error or ""):
            return {
                "error_code": "timeout",
                "status_label": self._error_label("timeout"),
                "auth_required": False,
            }
        current_url = ""
        if window:
            try:
                current_url = window.get_current_url() or ""
            except Exception:
                current_url = ""
        auth_required = self._current_page_requires_reauthorization(error, window)
        code = "reauth_required" if auth_required else self._classify_error_text(error, current_url)
        reauth_required = code in REAUTH_ELIGIBLE_ERROR_CODES
        return {
            "error_code": code,
            "status_label": self._error_label(code),
            "auth_required": reauth_required,
        }

    def _detect_cloudflare_challenge(self, window):
        if not window:
            return False
        try:
            current_url = window.get_current_url() or ""
        except Exception:
            current_url = ""
        if self._looks_like_cloudflare(current_url):
            return True
        try:
            marker = window.evaluate_js(
                "(() => {"
                "const title = document.title || '';"
                "const text = document.body ? document.body.innerText.slice(0, 2000) : '';"
                "return [location.href, title, text].join('\\n');"
                "})()"
            )
        except Exception:
            marker = ""
        return self._looks_like_cloudflare(marker)

    def _current_page_requires_reauthorization(self, error, window=None):
        current_url = ""
        has_password_input = False
        window = window or self.browser_window
        if window:
            try:
                current_url = window.get_current_url() or ""
            except Exception:
                current_url = ""
            try:
                has_password_input = bool(window.evaluate_js(
                    "(() => {"
                    "const visible = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e); "
                    "return !e.disabled && r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden'; };"
                    "const p=[...document.querySelectorAll('input[type=\"password\"]')].filter(visible);"
                    "if(p.length!==1 || String(p[0].autocomplete||'').toLowerCase()==='new-password') return false;"
                    "const scope=p[0].form||document;"
                    "const user=[...scope.querySelectorAll('input')].some(i => i!==p[0] && visible(i) "
                    "&& ['text','email','tel',''].includes(String(i.type||'').toLowerCase()));"
                    "return user || String(p[0].autocomplete||'').toLowerCase()==='current-password' "
                    "|| /(?:^|[/#?&=_-])(?:login|signin|sign-in|auth|authorize)(?:$|[/#?&=_-])/i.test(location.href);"
                    "})()"
                ))
            except Exception:
                has_password_input = False
        return requires_reauthorization(error, current_url, has_password_input)

    @staticmethod
    def _reauth_sites(sites):
        return [
            item for item in sites or []
            if isinstance(item, dict)
            and (item.get("reauth_required") or item.get("last_status") == "reauth_required")
            and item.get("site")
        ]

    @staticmethod
    def _latest_generated_at():
        path = latest_prices_json_path()
        if not path.exists():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("generated_at") or "")
        except Exception:
            return ""

    def export_results(self, fmt):
        try:
            fmt = "json" if fmt == "json" else "csv"
            if not self.rows:
                return {"ok": False, "error": "没有可导出的数据"}

            site_for_name = self.site or next(
                (row.get("site") for row in self.rows if row.get("site")),
                "unknown-site",
            )
            default_path = default_output_path(site_for_name, fmt)
            selected = self.controller_window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(output_dir()),
                save_filename=default_path.name,
                file_types=(f"{fmt.upper()} files (*.{fmt})", "All files (*.*)"),
            )
            if not selected:
                return {"ok": False, "cancelled": True}

            file_path = pathlib.Path(selected[0] if isinstance(selected, (list, tuple)) else selected)
            content = (
                json.dumps(
                    {"generated_at": datetime.now(timezone.utc).isoformat(), "rows": self.rows},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
                if fmt == "json"
                else rows_to_csv(self.rows)
            )
            atomic_write_text(
                file_path,
                content,
                encoding="utf-8-sig" if fmt == "csv" else "utf-8",
            )
            self._show_in_folder(file_path)
            return {"ok": True, "path": str(file_path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _evaluate_async(self, window, script, timeout):
        self._raise_if_shutting_down()
        done = threading.Event()
        holder = {}

        def callback(result):
            holder["result"] = result
            done.set()

        immediate = window.evaluate_js(script, callback=callback)
        if immediate not in (True, "true", None):
            return immediate
        deadline = time.time() + timeout
        while not done.is_set() and time.time() < deadline:
            self._raise_if_shutting_down()
            if window is self.browser_window and self.browser_cancel_event.is_set():
                raise RuntimeError("WINDOW_CLOSED: 登录窗口已关闭")
            if self._detect_cloudflare_challenge(window):
                raise RuntimeError("CLOUDFLARE_CHALLENGE: Cloudflare 验证未完成或当前环境不兼容")
            done.wait(0.5)
        if not done.is_set():
            raise TimeoutError("抓取脚本超时，请确认站点窗口已完成登录且页面可访问")
        return holder.get("result")

    @staticmethod
    def _show_in_folder(file_path):
        if os.name == "nt":
            subprocess.Popen(["explorer", f"/select,{file_path}"])

    @staticmethod
    def _upsert_saved_site(
        name,
        site,
        api_base,
        interval_hours=3,
        remember_credentials=True,
        auto_login=True,
        auto_refresh=True,
    ):
        saved = load_saved_sites()
        normalized_base = str(api_base or API_BASE).strip() or API_BASE
        if not normalized_base.startswith("/"):
            normalized_base = f"/{normalized_base}"
        now = datetime.now(timezone.utc).isoformat()
        previous = next((item for item in saved if item.get("site") == site), {})
        interval = PriceAppApi._interval_minutes(interval_hours)
        label = str(name or "").strip() or site
        record = {
            "name": label,
            "site": site,
            "api_base": normalized_base,
            "browser_mode": "webview",
            "interval_minutes": interval,
            "auto_refresh": True,
            "remember_credentials": bool(remember_credentials),
            "auto_login": bool(auto_login) and bool(remember_credentials),
            "created_at": previous.get("created_at") or now,
            "last_run": previous.get("last_run", ""),
            "last_status": previous.get("last_status", ""),
            "last_error": previous.get("last_error", ""),
            "last_error_code": previous.get("last_error_code", ""),
            "last_status_label": previous.get("last_status_label", ""),
            "reauth_required": previous.get("reauth_required", False),
            "reauth_requested_at": previous.get("reauth_requested_at", ""),
            "last_row_count": previous.get("last_row_count", 0),
            "last_plan_count": previous.get("last_plan_count", 0),
            "next_run": previous.get("next_run") or datetime.now(timezone.utc).isoformat(),
            "updated_at": now,
        }
        if previous.get("interval_minutes") != interval:
            record["next_run"] = datetime.now(timezone.utc).isoformat()
        merged = [item for item in saved if item.get("site") != site]
        merged.append(record)
        merged.sort(key=lambda item: (item.get("name") or item.get("site") or "").lower())
        write_saved_sites(merged)
        return merged


def parse_args():
    parser = argparse.ArgumentParser(description="Sub2API relay price comparison desktop app")
    parser.add_argument("--site", default="", help="optional target site URL")
    parser.add_argument("--devtools", action="store_true", help="open control-window debug mode")
    parser.add_argument("--browser-host", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--browser-host-port", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--browser-host-token", default="", help=argparse.SUPPRESS)
    parser.add_argument("--browser-host-storage", default="", help=argparse.SUPPRESS)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.browser_host:
        return run_browser_host(args)
    if not acquire_single_instance():
        return 0
    api = None
    browser_manager = None
    profile_dir = None
    try:
        configure_logging()
        site = normalize_site(args.site) if str(args.site or "").strip() else ""
        api = PriceAppApi(site)
        profile_dir = webview_profile_dir()
        profile_dir.mkdir(parents=True, exist_ok=True)
        cleanup_webview_processes(profile_dir, "startup")
        browser_manager = BrowserProcessManager(profile_dir)
        browser_manager.ensure_started()
        api.attach_browser_window(browser_manager.window("login"))
        for slot in range(MAX_SITE_WORKERS):
            api.attach_worker_window(browser_manager.window(f"worker-{slot}"), slot)
        LOGGER.info(
            "Starting %s %s frozen=%s data_dir=%s",
            APP_NAME,
            APP_VERSION,
            bool(getattr(sys, "frozen", False)),
            output_dir(),
        )
        controller = webview.create_window(
            APP_WINDOW_TITLE,
            html=CONTROL_HTML.replace("__APP_VERSION__", APP_VERSION),
            js_api=api,
            width=1180,
            height=820,
            min_size=(860, 620),
            text_select=True,
        )
        api.attach_controller_window(controller)

        webview.start(
            gui="edgechromium",
            debug=args.devtools,
            private_mode=True,
        )
        return 0
    except Exception as exc:
        LOGGER.exception("Fatal application error")
        if os.name == "nt":
            try:
                diagnostic_path = str(log_path())
            except Exception:
                diagnostic_path = "本地日志目录不可用"
            ctypes.windll.user32.MessageBoxW(
                None,
                f"应用发生错误，无法继续运行。\n\n{exc}\n\n日志：{diagnostic_path}",
                APP_WINDOW_TITLE,
                0x10,
            )
        return 1
    finally:
        if api:
            api.shutdown(wait=True)
        if browser_manager:
            browser_manager.shutdown()
        if profile_dir:
            cleanup_webview_processes(profile_dir, "shutdown")
        release_single_instance()
        LOGGER.info("Application process exiting")


if __name__ == "__main__":
    exit_code = main()
    os._exit(int(exit_code or 0))
