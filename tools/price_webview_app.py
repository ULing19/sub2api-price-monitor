#!/usr/bin/env python
import argparse
import csv
import io
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

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
    record["name"] = site
    record["auto_refresh"] = True
    if site and not record.get("next_run"):
        record["next_run"] = next_run_at(record).isoformat()
    return record


def annotate_saved_sites(sites):
    annotated = []
    for item in sites:
        if not isinstance(item, dict):
            continue
        annotated.append(normalize_saved_site_record(item))
    return annotated


def write_saved_sites(sites):
    path = saved_sites_path()
    normalized = [normalize_saved_site_record(item) for item in sites if isinstance(item, dict)]
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_latest_rows():
    path = latest_prices_json_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("rows") if isinstance(data, dict) else []
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def write_price_snapshot(rows, summary):
    generated_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "generated_at": generated_at,
        "summary": summary,
        "rows": rows,
    }
    latest_prices_json_path().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    latest_prices_csv_path().write_text(rows_to_csv(rows), encoding="utf-8-sig")
    slug = generated_at.replace(":", "-").replace(".", "-")
    (price_history_dir() / f"prices-{slug}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
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


CONTROL_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Sub2API 价格抓取</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: #f5f7fa;
      color: #18202a;
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
    }
    .app { min-height: 100vh; display: grid; grid-template-rows: auto auto 1fr; }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 16px 18px;
      border-bottom: 1px solid #dfe5ec;
      background: #ffffff;
    }
    h1 { margin: 0; font-size: 18px; }
    .status { color: #5b6877; font-size: 12px; }
    .controls {
      display: grid;
      grid-template-columns: repeat(6, minmax(104px, 1fr));
      gap: 10px;
      align-items: end;
      padding: 14px 18px;
      border-bottom: 1px solid #dfe5ec;
      background: #ffffff;
    }
    .field-wide { grid-column: span 2; }
    label { display: grid; gap: 5px; color: #5b6877; font-size: 12px; font-weight: 650; }
    input[type="url"], input[type="text"], input[type="number"], select {
      width: 100%;
      height: 36px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      padding: 0 10px;
      color: #18202a;
      font-size: 13px;
    }
    input:focus { border-color: #2563eb; outline: 3px solid rgba(37, 99, 235, 0.12); }
    .toggle { display: flex; align-items: center; gap: 7px; height: 36px; color: #344253; }
    button {
      height: 36px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      background: #fff;
      color: #18202a;
      cursor: pointer;
      font-weight: 700;
      padding: 0 12px;
      white-space: nowrap;
    }
    button.primary { border-color: #1f6feb; background: #1f6feb; color: #fff; }
    button:disabled { cursor: not-allowed; opacity: 0.45; }
    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      padding: 14px 18px 0;
    }
    .metric {
      border: 1px solid #dfe5ec;
      border-radius: 8px;
      background: #ffffff;
      padding: 10px 12px;
    }
    .metric span { display: block; color: #64748b; font-size: 12px; }
    .metric strong { display: block; margin-top: 4px; font-size: 18px; }
    .content {
      min-height: 0;
      display: grid;
      grid-template-rows: auto 1fr 152px;
      gap: 12px;
      padding: 14px 18px 18px;
    }
    .category-strip {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px;
    }
    .category-card {
      border: 1px solid #dfe5ec;
      border-radius: 8px;
      background: #fff;
      padding: 10px 12px;
    }
    .category-card strong { display: block; font-size: 14px; }
    .category-card span { display: block; margin-top: 4px; color: #64748b; font-size: 12px; }
    .table-wrap {
      min-height: 260px;
      overflow: auto;
      border: 1px solid #dfe5ec;
      border-radius: 8px;
      background: #fff;
    }
    table { width: 100%; min-width: 1080px; border-collapse: collapse; font-size: 12px; }
    th, td { border-bottom: 1px solid #edf1f5; padding: 9px 10px; text-align: left; vertical-align: top; }
    th { position: sticky; top: 0; background: #f8fafc; color: #526174; font-weight: 800; }
    td small { display: block; color: #64748b; margin-top: 3px; line-height: 1.35; }
    .group-row td {
      position: sticky;
      top: 34px;
      background: #eef4ff;
      color: #16437e;
      font-weight: 800;
      letter-spacing: 0;
    }
    .subgroup-row td {
      background: #f8fbff;
      color: #31516f;
      font-weight: 800;
    }
    .category-badge {
      display: inline-block;
      min-width: 72px;
      border-radius: 999px;
      background: #eaf2ff;
      color: #185abc;
      padding: 2px 8px;
      font-weight: 800;
    }
    .empty { color: #778397; text-align: center; }
    .console {
      min-height: 0;
      border: 1px solid #1c2c3d;
      border-radius: 8px;
      overflow: hidden;
      background: #0c1219;
      box-shadow: 0 12px 28px rgba(15, 23, 32, 0.12);
    }
    .console-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 8px 10px;
      border-bottom: 1px solid #1d2b3a;
      background: #111b26;
      color: #d7e3f1;
      font-size: 12px;
      font-weight: 800;
    }
    .console-head span:last-child {
      color: #8fb3da;
      font-weight: 700;
    }
    .log {
      overflow: auto;
      height: calc(100% - 34px);
      color: #c9d5e2;
      padding: 10px;
      font: 12px/1.5 Consolas, "Microsoft YaHei", monospace;
      white-space: pre-wrap;
    }
    @media (max-width: 980px) {
      .controls { grid-template-columns: 1fr 1fr; }
      .summary { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="app">
    <header>
      <div>
        <h1>Sub2API 价格抓取</h1>
        <div class="status" id="status">等待登录</div>
      </div>
      <div class="status" id="rowCount">0 行</div>
    </header>

    <section class="controls">
      <label class="field-wide">
        已保存站点
        <select id="savedSiteSelect"></select>
      </label>
      <label class="field-wide">
        站点地址
        <input id="siteInput" type="url" spellcheck="false" placeholder="https://example.com" />
      </label>
      <label>
        API 路径
        <input id="apiBaseInput" type="text" spellcheck="false" value="/api/v1" />
      </label>
      <label>
        保存备注
        <input id="siteNameInput" type="text" spellcheck="false" placeholder="自动使用站点地址" readonly />
      </label>
      <label>
        间隔(小时)
        <input id="intervalHoursInput" type="number" min="0.05" step="0.25" value="3" />
      </label>
      <label class="toggle">
        <input id="includeGroupsInput" type="checkbox" checked />
        分组行
      </label>
      <button id="saveSiteBtn" type="button">保存站点</button>
      <button id="deleteSiteBtn" type="button">删除站点</button>
      <button id="openSiteBtn" type="button">WebView登录</button>
      <button id="captureBtn" type="button" class="primary">WebView抓取</button>
      <button id="updateAllBtn" type="button">更新全部</button>
      <button id="exportCsvBtn" type="button" disabled>导出 CSV</button>
      <button id="exportJsonBtn" type="button" disabled>导出 JSON</button>
    </section>

    <section class="summary">
      <div class="metric"><span>套餐</span><strong id="planCount">0</strong></div>
      <div class="metric"><span>分组</span><strong id="groupCount">0</strong></div>
      <div class="metric"><span>模型分类</span><strong id="categoryCount">0</strong></div>
      <div class="metric"><span>状态</span><strong id="stateBadge">待登录</strong></div>
    </section>

    <section class="content">
      <div class="category-strip" id="categoryStrip"></div>
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
            <tr><td colspan="10" class="empty">暂无数据</td></tr>
          </tbody>
        </table>
      </div>
      <div class="console">
        <div class="console-head">
          <span>运行控制台</span>
          <span id="consoleStatus">ready</span>
        </div>
        <div class="log" id="logBox"></div>
      </div>
    </section>
  </main>

  <script>
    const siteInput = document.querySelector('#siteInput');
    const savedSiteSelect = document.querySelector('#savedSiteSelect');
    const siteNameInput = document.querySelector('#siteNameInput');
    const intervalHoursInput = document.querySelector('#intervalHoursInput');
    const apiBaseInput = document.querySelector('#apiBaseInput');
    const includeGroupsInput = document.querySelector('#includeGroupsInput');
    const openSiteBtn = document.querySelector('#openSiteBtn');
    const captureBtn = document.querySelector('#captureBtn');
    const saveSiteBtn = document.querySelector('#saveSiteBtn');
    const deleteSiteBtn = document.querySelector('#deleteSiteBtn');
    const updateAllBtn = document.querySelector('#updateAllBtn');
    const exportCsvBtn = document.querySelector('#exportCsvBtn');
    const exportJsonBtn = document.querySelector('#exportJsonBtn');
    const resultBody = document.querySelector('#resultBody');
    const statusText = document.querySelector('#status');
    const stateBadge = document.querySelector('#stateBadge');
    const logBox = document.querySelector('#logBox');
    const consoleStatus = document.querySelector('#consoleStatus');
    const categoryStrip = document.querySelector('#categoryStrip');
    const rowCount = document.querySelector('#rowCount');
    const planCount = document.querySelector('#planCount');
    const groupCount = document.querySelector('#groupCount');
    const categoryCount = document.querySelector('#categoryCount');
    let rows = [];
    let savedSites = [];
    let latestGeneratedAt = '';
    const CATEGORY_ORDER = [
      'OpenAI',
      'Anthropic',
      'Gemini',
      'Grok',
      '其他',
      '未获取',
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
      logBox.textContent = logBox.textContent ? `${logBox.textContent}\n${line}` : line;
      logBox.scrollTop = logBox.scrollHeight;
    }

    function setState(text) {
      statusText.textContent = text;
      stateBadge.textContent = text;
      consoleStatus.textContent = text;
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

    function renderCategoryStrip() {
      const stats = new Map();
      for (const row of rows) {
        const category = row.model_category || '其他';
        const current = stats.get(category) || { plans: 0, groups: 0, min: null, sites: new Set() };
        if (row.record_type === 'plan') current.plans += 1;
        if (row.record_type === 'group') current.groups += 1;
        if (row.site_host) current.sites.add(row.site_host);
        const price = numericPrice(row);
        if (price !== null && (current.min === null || price < current.min)) current.min = price;
        stats.set(category, current);
      }
      const cards = [...stats.entries()]
        .sort((a, b) => categoryRank(a[0]) - categoryRank(b[0]) || a[0].localeCompare(b[0], 'zh-CN'))
        .map(([category, stat]) => {
          const minText = stat.min === null ? '暂无价格' : `最低 ${stat.min}`;
          return `<div class="category-card">
            <strong>${escapeHtml(category)}</strong>
            <span>${stat.sites.size} 站 · ${stat.plans} 套餐 · ${stat.groups} 分组 · ${escapeHtml(minText)}</span>
          </div>`;
        });
      categoryStrip.innerHTML = cards.length ? cards.join('') : '';
    }

    function renderSavedSites() {
      savedSiteSelect.innerHTML = '<option value="">选择已保存站点</option>' + savedSites.map((site) => {
        const interval = site.interval_minutes ? ` · ${Math.round(site.interval_minutes / 60 * 100) / 100}h` : '';
        const next = site.next_run ? ` · 下次 ${formatDateTime(site.next_run)}` : '';
        const status = site.last_status ? ` · ${site.last_status}` : '';
        const label = `${site.site}${interval}${next}${status}`;
        return `<option value="${escapeHtml(site.site)}">${escapeHtml(label)}</option>`;
      }).join('');
    }

    function render() {
      const plans = rows.filter((row) => row.record_type === 'plan');
      const groups = rows.filter((row) => row.record_type === 'group');
      const categories = new Set(rows.map((row) => row.model_category).filter(Boolean));
      planCount.textContent = String(plans.length);
      groupCount.textContent = String(groups.length);
      categoryCount.textContent = String(categories.size);
      rowCount.textContent = `${rows.length} 行`;
      exportCsvBtn.disabled = rows.length === 0;
      exportJsonBtn.disabled = rows.length === 0;
      renderCategoryStrip();

      if (!rows.length) {
        resultBody.innerHTML = '<tr><td colspan="10" class="empty">暂无数据</td></tr>';
        return;
      }

      const displayRows = [...rows].sort((a, b) => (
        categoryRank(a.model_category) - categoryRank(b.model_category)
        || String(a.model_category || '').localeCompare(String(b.model_category || ''), 'zh-CN')
        || compareRate(a, b)
        || groupLabel(a).localeCompare(groupLabel(b), 'zh-CN')
        || String(a.group_platform || '').localeCompare(String(b.group_platform || ''), 'zh-CN')
        || String(a.record_type || '').localeCompare(String(b.record_type || ''), 'zh-CN')
        || String(a.site_host || a.site || '').localeCompare(String(b.site_host || b.site || ''), 'zh-CN')
        || String(a.plan_name || '').localeCompare(String(b.plan_name || ''), 'zh-CN')
      ));
      let currentCategory = '';
      let currentCandidate = '';
      const htmlRows = [];
      for (const row of displayRows) {
        const category = row.model_category || '其他';
        if (category !== currentCategory) {
          currentCategory = category;
          currentCandidate = '';
          htmlRows.push(`<tr class="group-row"><td colspan="10">${escapeHtml(category)}</td></tr>`);
        }
        const candidate = `${rateLabel(row)}|${row.site_host || row.site || ''}|${groupLabel(row)}|${row.group_platform || ''}`;
        if (candidate !== currentCandidate) {
          currentCandidate = candidate;
          htmlRows.push(`<tr class="subgroup-row"><td colspan="10">${escapeHtml(candidateLabel(row))}</td></tr>`);
        }
        const validity = [row.validity_days, row.validity_unit].filter(Boolean).join(' ');
        htmlRows.push(`<tr>
          <td>${escapeHtml(row.record_type)}</td>
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
      siteNameInput.value = site.site || '';
      apiBaseInput.value = site.api_base || '/api/v1';
      intervalHoursInput.value = site.interval_minutes ? String(Math.round(site.interval_minutes / 60 * 100) / 100) : '3';
    }

    async function saveSite() {
      const result = await window.pywebview.api.save_site(
        siteNameInput.value,
        siteInput.value,
        apiBaseInput.value,
        intervalHoursInput.value
      );
      if (!result.ok) {
        log(result.error);
        return;
      }
      savedSites = result.saved_sites || [];
      renderSavedSites();
      savedSiteSelect.value = result.site;
      log(`已保存站点：${result.site}`);
    }

    async function deleteSite() {
      const result = await window.pywebview.api.delete_site(siteInput.value || savedSiteSelect.value);
      if (!result.ok) {
        log(result.error);
        return;
      }
      savedSites = result.saved_sites || [];
      renderSavedSites();
      log(`已删除站点：${result.site}`);
    }

    async function openSite() {
      const result = await window.pywebview.api.open_site(siteInput.value);
      if (!result.ok) {
        log(result.error);
        return;
      }
      siteInput.value = result.site;
      rows = [];
      render();
      setState('WebView 登录中');
      log(`已在 WebView 打开：${result.site}`);
      log('请在目标站点窗口完成登录，然后回到这里点击“WebView抓取”。');
    }

    async function capture() {
      captureBtn.disabled = true;
      setState('WebView 抓取中');
      log('开始从 WebView 当前登录页抓取价格接口');
      try {
        const result = await window.pywebview.api.capture_prices(apiBaseInput.value, includeGroupsInput.checked);
        if (!result.ok) {
          setState('抓取失败');
          log(result.error);
          return;
        }
        rows = result.rows || [];
        latestGeneratedAt = result.generated_at || latestGeneratedAt;
        render();
        const errorOnly = rows.length > 0 && rows.every((row) => row.record_type === 'error');
        setState(errorOnly ? '未获取到价格' : '抓取完成');
        log(`认证方式：${result.tokenKey || 'cookie/session'}`);
        log(`WebView 抓取完成：${rows.length} 行`);
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
      updateAllBtn.disabled = true;
      setState(reason === 'startup' ? '启动抓取中' : '更新全部中');
      log(reason === 'startup' ? '启动后自动抓取所有已保存站点' : '开始更新所有已保存站点');
      try {
        const result = await window.pywebview.api.update_all_prices();
        if (!result.ok) {
          setState('更新失败');
          log(result.error);
          return;
        }
        rows = result.rows || [];
        savedSites = result.saved_sites || savedSites;
        latestGeneratedAt = result.generated_at || latestGeneratedAt;
        renderSavedSites();
        render();
        setState('更新完成');
        log(`已更新 ${result.summary?.site_count || 0} 个站点，得到 ${rows.length} 行`);
      } finally {
        updateAllBtn.disabled = false;
      }
    }

    async function startAutoCheck() {
      const result = await window.pywebview.api.start_scheduler();
      if (!result.ok) {
        log(result.error);
        return;
      }
      setState('自动检查中');
      log('自动检查已启动，会按各站点设置的间隔检查所有保存的网站');
    }

    async function pollSchedulerStatus() {
      const result = await window.pywebview.api.scheduler_status();
      if (!result.ok) return;
      savedSites = result.saved_sites || savedSites;
      renderSavedSites();
      if (result.latest_generated_at && result.latest_generated_at !== latestGeneratedAt) {
        latestGeneratedAt = result.latest_generated_at;
        rows = result.rows || rows;
        render();
        log(`自动检查已刷新：${latestGeneratedAt}`);
      }
      if (result.running) {
        stateBadge.textContent = '自动检查中';
      }
    }

    async function init() {
      const state = await window.pywebview.api.initial_state();
      siteInput.value = state.site;
      savedSites = state.saved_sites || [];
      rows = state.latest_rows || [];
      latestGeneratedAt = state.latest_generated_at || '';
      renderSavedSites();
      render();
      siteNameInput.value = siteInput.value || '';
      if (state.site) {
        log('请点击“WebView登录”，在目标站点窗口完成登录。');
      } else {
        log('请输入目标站点地址，然后点击“WebView登录”。');
      }
      if (savedSites.length) {
        await updateAllSaved('startup');
      }
      await startAutoCheck();
    }

    siteInput.addEventListener('input', () => {
      siteNameInput.value = siteInput.value;
    });
    savedSiteSelect.addEventListener('change', () => {
      const site = savedSites.find((item) => item.site === savedSiteSelect.value);
      applySavedSite(site);
    });
    saveSiteBtn.addEventListener('click', saveSite);
    deleteSiteBtn.addEventListener('click', deleteSite);
    openSiteBtn.addEventListener('click', openSite);
    captureBtn.addEventListener('click', capture);
    updateAllBtn.addEventListener('click', updateAllSaved);
    exportCsvBtn.addEventListener('click', () => exportRows('csv'));
    exportJsonBtn.addEventListener('click', () => exportRows('json'));
    window.addEventListener('pywebviewready', init);
    setInterval(pollSchedulerStatus, 30000);
  </script>
</body>
</html>
"""


class PriceAppApi:
    def __init__(self, site=""):
        self.site = normalize_site(site) if str(site or "").strip() else ""
        self.rows = []
        self.browser_window = None
        self.controller_window = None
        self.update_lock = threading.Lock()
        self.scheduler_stop = threading.Event()
        self.scheduler_thread = None
        self.scheduler_message = "自动检查未启动"
        self.latest_generated_at = self._latest_generated_at()

    def initial_state(self):
        return {
            "site": self.site,
            "saved_sites": annotate_saved_sites(load_saved_sites()),
            "latest_rows": load_latest_rows(),
            "latest_generated_at": self.latest_generated_at,
        }

    def save_site(
        self,
        name,
        site,
        api_base=API_BASE,
        interval_hours=3,
        auto_refresh=True,
    ):
        try:
            normalized = normalize_site(site)
            saved = self._upsert_saved_site(
                name,
                normalized,
                api_base,
                interval_hours,
            )
            return {"ok": True, "site": normalized, "saved_sites": annotate_saved_sites(saved)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_site(self, site):
        try:
            normalized = normalize_site(site)
            saved = [item for item in load_saved_sites() if item.get("site") != normalized]
            write_saved_sites(saved)
            return {"ok": True, "site": normalized, "saved_sites": annotate_saved_sites(saved)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_site(self, site):
        try:
            self.site = normalize_site(site)
            self.rows = []
            if not self.browser_window:
                raise RuntimeError("WebView 登录窗口未初始化")
            self.browser_window.load_url(self.site)
            return {"ok": True, "site": self.site}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def capture_prices(self, api_base=API_BASE, include_groups=True):
        try:
            if not self.site:
                raise RuntimeError("请先打开目标站点")
            if not self.browser_window:
                raise RuntimeError("WebView 登录窗口未初始化")
            base = str(api_base or API_BASE).strip()
            if not base.startswith("/"):
                base = f"/{base}"
            options = {
                "base": base,
                "includeGroups": bool(include_groups),
                "outputFields": OUTPUT_FIELDS,
            }
            script = collector_js_template().replace("__OPTIONS__", json.dumps(options, ensure_ascii=False))
            result = self._evaluate_async(script, timeout=90)
            if not isinstance(result, dict):
                raise RuntimeError(f"抓取脚本返回了异常结果：{result!r}")
            if "rows" not in result:
                raise RuntimeError(result.get("message") or json.dumps(result, ensure_ascii=False))
            self.rows = result.get("rows") or []
            snapshot = write_price_snapshot(self.rows, {
                "site_count": 1,
                "success_count": 1,
                "error_count": 0,
                "mode": "manual_webview",
            })
            self.latest_generated_at = snapshot["generated_at"]
            return {"ok": True, "generated_at": self.latest_generated_at, **result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def update_all_prices(self):
        try:
            with self.update_lock:
                sites = load_saved_sites()
                if not sites:
                    raise RuntimeError("还没有保存站点")
                result = self._run_site_records(sites, only_due=False)
                return {"ok": True, **result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_scheduler(self):
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

    def scheduler_status(self):
        running = bool(self.scheduler_thread and self.scheduler_thread.is_alive())
        return {
            "ok": True,
            "running": running,
            "message": self.scheduler_message,
            "saved_sites": annotate_saved_sites(load_saved_sites()),
            "rows": load_latest_rows(),
            "latest_generated_at": self._latest_generated_at(),
        }

    def _scheduler_loop(self):
        while not self.scheduler_stop.is_set():
            try:
                with self.update_lock:
                    result = self._run_site_records(load_saved_sites(), only_due=True)
                    if result["summary"]["site_count"]:
                        self.scheduler_message = (
                            f"自动检查完成：{result['summary']['site_count']} 个站点，"
                            f"{result['summary']['success_count']} 成功，"
                            f"{result['summary']['error_count']} 失败"
                        )
            except Exception as exc:
                self.scheduler_message = f"自动检查失败：{exc}"
            self.scheduler_stop.wait(30)

    def _run_site_records(self, sites, only_due):
        due_sites = []
        for record in sites:
            if not record.get("site"):
                continue
            if only_due and not self._is_site_due(record):
                continue
            due_sites.append(record)

        if not due_sites:
            return {
                "rows": load_latest_rows(),
                "saved_sites": annotate_saved_sites(load_saved_sites()),
                "generated_at": self._latest_generated_at(),
                "summary": {
                    "site_count": 0,
                    "success_count": 0,
                    "error_count": 0,
                    "skipped_count": len(sites),
                },
            }

        fresh_rows = []
        updated_hosts = set()
        updated_records = {item.get("site"): dict(item) for item in sites}
        success_count = 0
        error_count = 0
        for record in due_sites:
            result = self._capture_site_webview(record, include_groups=True)
            rows = result.get("rows") or []
            fresh_rows.extend(rows)
            try:
                updated_hosts.add(urlparse(normalize_site(record.get("site", ""))).netloc)
            except ValueError:
                pass
            updated = self._site_status_record(record, result)
            updated_records[updated["site"]] = updated
            if result.get("ok"):
                success_count += 1
            else:
                error_count += 1

        merged_sites = list(updated_records.values())
        merged_sites.sort(key=lambda item: (item.get("name") or item.get("site") or "").lower())
        write_saved_sites(merged_sites)

        if only_due:
            previous_rows = load_latest_rows()
            all_rows = [
                row for row in previous_rows
                if (row.get("site_host") or urlparse(str(row.get("site") or "")).netloc) not in updated_hosts
            ]
            all_rows.extend(fresh_rows)
        else:
            all_rows = fresh_rows

        all_rows.sort(key=row_sort_key)
        summary = {
            "site_count": len(due_sites),
            "success_count": success_count,
            "error_count": error_count,
            "skipped_count": max(0, len(sites) - len(due_sites)),
        }
        snapshot = write_price_snapshot(all_rows, summary)
        self.rows = all_rows
        self.latest_generated_at = snapshot["generated_at"]
        return {
            "rows": all_rows,
            "saved_sites": annotate_saved_sites(merged_sites),
            "generated_at": snapshot["generated_at"],
            "summary": summary,
        }

    def _capture_site_webview(self, record, include_groups=True):
        site = normalize_site(record.get("site", ""))
        base = self._normalized_api_base(record.get("api_base", API_BASE))
        try:
            if not self.browser_window:
                raise RuntimeError("WebView 登录窗口未初始化")
            self.site = site
            self.browser_window.load_url(site)
            self._wait_webview_ready(site, timeout=45)
            options = {
                "base": base,
                "includeGroups": bool(include_groups),
                "outputFields": OUTPUT_FIELDS,
            }
            script = collector_js_template().replace("__OPTIONS__", json.dumps(options, ensure_ascii=False))
            result = self._evaluate_async(script, timeout=90)
            if not isinstance(result, dict):
                raise RuntimeError(f"WebView 抓取返回了异常结果：{result!r}")
            if "rows" not in result:
                raise RuntimeError(result.get("message") or json.dumps(result, ensure_ascii=False))
            result["ok"] = True
            return result
        except Exception as exc:
            now = datetime.now(timezone.utc).isoformat()
            return {
                "ok": False,
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
                    "error": str(exc),
                }],
                "error": str(exc),
            }

    def _wait_webview_ready(self, site, timeout=45):
        expected_origin = urlparse(site).netloc
        deadline = time.time() + timeout
        last_error = ""
        while time.time() < deadline:
            try:
                current_url = self.browser_window.get_current_url()
                current_host = urlparse(current_url).netloc
                ready = self.browser_window.evaluate_js("document.readyState")
                if current_host == expected_origin and ready in ("interactive", "complete"):
                    time.sleep(1)
                    return
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.5)
        raise RuntimeError(f"等待 WebView 加载 {site} 超时：{last_error}")

    def _record_for_site(self, site, api_base):
        normalized = normalize_site(site)
        for record in load_saved_sites():
            if record.get("site") == normalized:
                return record
        now = datetime.now(timezone.utc)
        return {
            "name": normalized,
            "site": normalized,
            "api_base": self._normalized_api_base(api_base),
            "browser_mode": "webview",
            "interval_minutes": 180,
            "auto_refresh": True,
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
        updated["name"] = updated["site"]
        updated["api_base"] = self._normalized_api_base(updated.get("api_base", API_BASE))
        updated["browser_mode"] = "webview"
        updated["auto_refresh"] = True
        updated["last_run"] = now
        updated["last_status"] = "ok" if result.get("ok") else "error"
        updated["last_error"] = "" if result.get("ok") else (result.get("error") or "")
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
            file_path.write_text(content, encoding="utf-8-sig" if fmt == "csv" else "utf-8")
            self._show_in_folder(file_path)
            return {"ok": True, "path": str(file_path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _evaluate_async(self, script, timeout):
        done = threading.Event()
        holder = {}

        def callback(result):
            holder["result"] = result
            done.set()

        immediate = self.browser_window.evaluate_js(script, callback=callback)
        if immediate not in (True, "true", None):
            return immediate
        if not done.wait(timeout):
            raise TimeoutError("抓取脚本超时，请确认站点窗口已完成登录且页面可访问")
        return holder.get("result")

    @staticmethod
    def _show_in_folder(file_path):
        if os.name == "nt":
            subprocess.Popen(["explorer", f"/select,{file_path}"])

    @staticmethod
    def _upsert_saved_site(name, site, api_base, interval_hours=3, auto_refresh=True):
        saved = load_saved_sites()
        normalized_base = str(api_base or API_BASE).strip() or API_BASE
        if not normalized_base.startswith("/"):
            normalized_base = f"/{normalized_base}"
        now = datetime.now(timezone.utc).isoformat()
        previous = next((item for item in saved if item.get("site") == site), {})
        interval = PriceAppApi._interval_minutes(interval_hours)
        record = {
            "name": site,
            "site": site,
            "api_base": normalized_base,
            "browser_mode": "webview",
            "interval_minutes": interval,
            "auto_refresh": True,
            "created_at": previous.get("created_at") or now,
            "last_run": previous.get("last_run", ""),
            "last_status": previous.get("last_status", ""),
            "last_error": previous.get("last_error", ""),
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
    parser = argparse.ArgumentParser(description="Sub2API price scraping desktop app")
    parser.add_argument("--site", default="", help="optional target site URL")
    parser.add_argument("--devtools", action="store_true", help="open control-window debug mode")
    return parser.parse_args()


def main():
    args = parse_args()
    site = normalize_site(args.site) if str(args.site or "").strip() else ""
    api = PriceAppApi(site)

    controller = webview.create_window(
        "Sub2API 价格抓取",
        html=CONTROL_HTML,
        js_api=api,
        width=980,
        height=760,
        min_size=(780, 560),
        text_select=True,
    )
    browser = webview.create_window(
        "目标站点 WebView 登录",
        url=site or BLANK_PAGE,
        width=1120,
        height=820,
        min_size=(760, 520),
        text_select=True,
    )
    api.controller_window = controller
    api.browser_window = browser

    profile_dir = output_dir() / "price-webview-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    webview.start(
        gui="edgechromium",
        debug=args.devtools,
        private_mode=False,
        storage_path=str(profile_dir),
    )


if __name__ == "__main__":
    main()
