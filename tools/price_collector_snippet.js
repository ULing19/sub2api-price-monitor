(async function run(options) {
  const accessTokenKeys = ['auth_token', 'access_token', 'token', 'jwt', 'accessToken', 'id_token'];
  const refreshTokenKeys = ['refresh_token', 'refreshToken'];
  const accessTokenNames = new Set(accessTokenKeys.map(normalizeTokenName));
  const refreshTokenNames = new Set(refreshTokenKeys.map(normalizeTokenName));
  const storageEntries = [
    { name: 'localStorage', storage: localStorage },
    { name: 'sessionStorage', storage: sessionStorage },
  ];
  let memoryAccessToken = null;
  let memoryRefreshToken = null;
  let refreshAttempt = null;

  function normalizeTokenName(value) {
    return String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  }

  function validToken(value) {
    if (typeof value !== 'string') return '';
    const normalized = value.trim();
    return normalized && normalized.length <= 65536 ? normalized : '';
  }

  function storageKeys(storage) {
    const keys = [];
    try {
      for (let i = 0; i < storage.length; i += 1) {
        const key = storage.key(i);
        if (key) keys.push(key);
      }
    } catch {}
    return keys;
  }

  function storageGet(storage, key) {
    try {
      return storage.getItem(key);
    } catch {
      return null;
    }
  }

  function storageSet(storage, key, value) {
    try {
      if (typeof storage.setItem !== 'function') return false;
      storage.setItem(key, value);
      return true;
    } catch {
      return false;
    }
  }

  function parseStoredJson(value) {
    const normalized = String(value || '').trim();
    if (!normalized.startsWith('{') && !normalized.startsWith('[')) return null;
    try {
      const parsed = JSON.parse(normalized);
      return parsed && typeof parsed === 'object' ? parsed : null;
    } catch {
      return null;
    }
  }

  function nestedToken(node, acceptedNames, path = [], depth = 0) {
    if (!node || typeof node !== 'object' || depth > 5) return null;
    const entries = Object.entries(node);
    for (const [key, value] of entries) {
      if (acceptedNames.has(normalizeTokenName(key))) {
        const tokenValue = validToken(value);
        if (tokenValue) return { path: [...path, key], value: tokenValue };
      }
    }
    for (const [key, value] of entries) {
      if (['__proto__', 'prototype', 'constructor'].includes(key)) continue;
      if (value && typeof value === 'object') {
        const found = nestedToken(value, acceptedNames, [...path, key], depth + 1);
        if (found) return found;
      }
    }
    return null;
  }

  function storedToken(directKeys, acceptedNames, kind) {
    for (const key of directKeys) {
      for (const entry of storageEntries) {
        const rawValue = storageGet(entry.storage, key);
        const parsed = parseStoredJson(rawValue);
        if (parsed) {
          const nested = nestedToken(parsed, acceptedNames);
          if (nested) {
            return {
              ...entry,
              key,
              path: nested.path,
              value: nested.value,
              label: `${key}.${nested.path.join('.')}`,
            };
          }
          continue;
        }
        const value = validToken(rawValue);
        if (value) return { ...entry, key, path: [], value, label: key };
      }
    }
    for (const entry of storageEntries) {
      for (const key of storageKeys(entry.storage)) {
        const rawValue = storageGet(entry.storage, key);
        const parsed = parseStoredJson(rawValue);
        if (parsed) {
          const nested = nestedToken(parsed, acceptedNames);
          if (nested) {
            return {
              ...entry,
              key,
              path: nested.path,
              value: nested.value,
              label: `${key}.${nested.path.join('.')}`,
            };
          }
          continue;
        }
        const normalizedKey = normalizeTokenName(key);
        const genericMatch = kind === 'access'
          ? normalizedKey.includes('token') && !normalizedKey.includes('refresh')
          : normalizedKey.includes('refreshtoken');
        const value = genericMatch ? validToken(rawValue) : '';
        if (value) return { ...entry, key, path: [], value, label: key };
      }
    }
    return null;
  }

  function accessToken() {
    return memoryAccessToken || storedToken(accessTokenKeys, accessTokenNames, 'access');
  }

  function refreshToken() {
    return memoryRefreshToken || storedToken(refreshTokenKeys, refreshTokenNames, 'refresh');
  }

  function writeOriginalLocation(location, value) {
    if (!location || !location.storage || !location.key) return false;
    if (!location.path || location.path.length === 0) {
      return storageSet(location.storage, location.key, value);
    }
    const parsed = parseStoredJson(storageGet(location.storage, location.key));
    if (!parsed) return false;
    let target = parsed;
    for (const segment of location.path.slice(0, -1)) {
      if (!target || typeof target !== 'object' || !(segment in target)) return false;
      target = target[segment];
    }
    if (!target || typeof target !== 'object') return false;
    target[location.path[location.path.length - 1]] = value;
    return storageSet(location.storage, location.key, JSON.stringify(parsed));
  }

  function token() {
    return accessToken();
  }

  function endpoint(suffix) {
    const resolved = new URL(options.base.replace(/\/$/, '') + suffix, window.location.origin);
    if (resolved.origin !== window.location.origin) {
      throw new Error(`拒绝跨域 API 地址：${suffix}`);
    }
    return resolved.toString();
  }

  function refreshResponseToken(body, acceptedNames) {
    if (!body || typeof body !== 'object') return '';
    const code = 'code' in body ? String(body.code) : '';
    if (body.success === false || (code && !['0', '200'].includes(code) && body.success !== true)) return '';
    const found = nestedToken(body, acceptedNames);
    return found ? found.value : '';
  }

  function businessCode(body) {
    if (!body || typeof body !== 'object' || Array.isArray(body)) return '';
    if ('code' in body && body.code !== null && body.code !== '') return String(body.code).trim();
    if (body.success === false && 'status' in body && body.status !== null && body.status !== '') {
      return String(body.status).trim();
    }
    return '';
  }

  function businessFailure(body) {
    if (!body || typeof body !== 'object' || Array.isArray(body)) return false;
    if (body.success === false) return true;
    const code = businessCode(body);
    return Boolean(code && !['0', '200'].includes(code) && body.success !== true);
  }

  function businessMessage(body) {
    if (!body || typeof body !== 'object') return '';
    const value = body.message ?? body.reason ?? body.error ?? '';
    return typeof value === 'string' ? value.slice(0, 240) : JSON.stringify(value).slice(0, 240);
  }

  function businessAuthCode(body) {
    const code = businessCode(body);
    return ['401', '403'].includes(code) ? code : '';
  }

  async function performSilentRefresh(rejectedAccess) {
    const refresh = refreshToken();
    if (!refresh) return false;
    const controller = new AbortController();
    const timeoutMs = Number(options.requestTimeoutMs) > 0 ? Number(options.requestTimeoutMs) : 25000;
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(endpoint('/auth/refresh'), {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ refresh_token: refresh.value }),
        signal: controller.signal,
      });
      if (!response.ok) return false;
      const rawText = await response.text();
      let body = null;
      try { body = rawText ? JSON.parse(rawText) : null; } catch { return false; }
      const nextAccessToken = refreshResponseToken(body, accessTokenNames);
      if (!nextAccessToken) return false;
      writeOriginalLocation(rejectedAccess, nextAccessToken);
      memoryAccessToken = {
        ...rejectedAccess,
        key: rejectedAccess?.key || 'refreshed-session',
        label: rejectedAccess?.label || 'refreshed-session',
        value: nextAccessToken,
      };
      const nextRefreshToken = refreshResponseToken(body, refreshTokenNames);
      if (nextRefreshToken) {
        writeOriginalLocation(refresh, nextRefreshToken);
        memoryRefreshToken = { ...refresh, value: nextRefreshToken };
      }
      return true;
    } catch {
      return false;
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  async function refreshAccessToken(rejectedAccess) {
    const current = accessToken();
    if ((!rejectedAccess && current) || (rejectedAccess && current && current.value !== rejectedAccess.value)) {
      return true;
    }
    const attemptKey = rejectedAccess?.value || '<cookie-session>';
    if (refreshAttempt && refreshAttempt.key === attemptKey) return refreshAttempt.promise;
    const promise = performSilentRefresh(rejectedAccess);
    refreshAttempt = { key: attemptKey, promise };
    return promise;
  }

  async function apiGet(suffix, allowRefresh = true) {
    const found = token();
    const headers = { Accept: 'application/json' };
    if (found) headers.Authorization = 'Bearer ' + found.value;
    const controller = new AbortController();
    const timeoutMs = Number(options.requestTimeoutMs) > 0 ? Number(options.requestTimeoutMs) : 25000;
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    let response;
    try {
      response = await fetch(endpoint(suffix), {
        headers,
        credentials: 'include',
        signal: controller.signal,
      });
    } catch (error) {
      if (error && error.name === 'AbortError') {
        throw new Error(`TIMEOUT: ${suffix}: 请求超时（${Math.round(timeoutMs / 1000)} 秒）`);
      }
      throw new Error(`NETWORK_ERROR: ${suffix}: ${error?.message || String(error)}`);
    } finally {
      window.clearTimeout(timeoutId);
    }
    const rawText = await response.text();
    let body = null;
    try { body = rawText ? JSON.parse(rawText) : null; } catch { body = rawText; }
    if (!response.ok) {
      if (allowRefresh && [401, 403].includes(response.status) && await refreshAccessToken(found)) {
        return apiGet(suffix, false);
      }
      const message = typeof body === 'string' ? body.slice(0, 240) : JSON.stringify(body).slice(0, 240);
      const authHint = found ? `token=${found.label || found.key}` : '未发现 token，已尝试 Cookie 登录态';
      throw new Error(`HTTP_ERROR: ${suffix}: HTTP ${response.status}: ${authHint}: ${message}`);
    }
    if (businessFailure(body)) {
      const authCode = businessAuthCode(body);
      if (authCode && allowRefresh && await refreshAccessToken(found)) {
        return apiGet(suffix, false);
      }
      const code = businessCode(body);
      const message = businessMessage(body) || code || '业务请求失败';
      if (authCode) {
        throw new Error(`BUSINESS_AUTH_ERROR: ${suffix}: code ${authCode}: ${message}`);
      }
      throw new Error(`BUSINESS_ERROR: ${suffix}:${code ? ` code ${code}:` : ''} ${message}`);
    }
    if (body && typeof body === 'object' && 'data' in body) {
      const code = businessCode(body);
      if (body.success === true || !code || ['0', '200'].includes(code)) return body.data;
    }
    if (typeof body === 'string') {
      throw new Error(`UNSUPPORTED_RESPONSE: ${suffix}: 接口返回了非 JSON 文本`);
    }
    return body;
  }

  function failureCode(message) {
    const text = String(message || '');
    if (/BUSINESS_AUTH_ERROR/i.test(text)) return 'reauth_required';
    if (/BUSINESS_ERROR/i.test(text)) return 'unsupported_response';
    if (/HTTP\s+(401|403)\b|unauthori[sz]ed|forbidden|(?:access|refresh|auth|id)[ _-]?token.{0,24}(?:expired|invalid|missing|revoked)|(?:expired|invalid|missing|revoked).{0,24}token|session.{0,24}(?:expired|invalid)/i.test(text)) return 'reauth_required';
    if (/TIMEOUT|超时|timeout/i.test(text)) return 'timeout';
    if (/HTTP_ERROR|HTTP\s+\d{3}/i.test(text)) return 'http_error';
    if (/UNSUPPORTED_RESPONSE/i.test(text)) return 'unsupported_response';
    if (/NETWORK_ERROR|failed to fetch|network/i.test(text)) return 'network_error';
    return 'no_price_data';
  }

  function failureLabel(code) {
    return ({
      reauth_required: '登录/会话已失效',
      timeout: '请求或页面超时',
      http_error: 'HTTP 接口错误',
      unsupported_response: '接口响应不支持',
      network_error: '网络错误',
      no_price_data: '未发现价格数据',
    })[code] || '未知错误';
  }

  function number(value) {
    if (value === null || value === undefined || value === '') return '';
    const raw = String(value).trim().replace(/,/g, '');
    const matched = raw.match(/[-+]?\d+(?:\.\d+)?/);
    if (!matched) return '';
    const n = Number(matched[0]);
    return Number.isFinite(n) ? n : '';
  }

  function optionalBoolean(value) {
    if (value === null || value === undefined || value === '') return '';
    if (typeof value === 'boolean') return value;
    if (typeof value === 'number') return value !== 0;
    const normalized = String(value).trim().toLowerCase();
    if (['true', '1', 'yes', 'on'].includes(normalized)) return true;
    if (['false', '0', 'no', 'off'].includes(normalized)) return false;
    return '';
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

  function asList(value) {
    if (!value) return [];
    if (Array.isArray(value)) return value.flatMap(asList);
    if (typeof value === 'object') {
      return Object.values(value).flatMap(asList);
    }
    return String(value).split(/[,，|;\r\n]+/).map(text).filter(Boolean);
  }

  function modelNames(record) {
    const directKeys = [
      'model',
      'models',
      'model_name',
      'model_names',
      'available_models',
      'supported_models',
      'allowed_models',
    ];
    const values = [];
    for (const key of directKeys) values.push(...asList(record[key]));

    const haystack = [
      record.name,
      record.platform,
      record.provider,
      record.description,
      record.features,
      record.group_name,
      record.group_platform,
      record.groupName,
      record.groupPlatform,
    ].map((value) => (Array.isArray(value) ? value.join(' ') : text(value))).join(' ');
    const matched = haystack.match(/\b(?:gpt|claude|gemini|deepseek|grok|llama|qwen|kimi|doubao|yi|moonshot|mistral|codex|embedding|image|tts|whisper)[a-z0-9._:-]*/gi) || [];
    values.push(...matched);

    return [...new Set(values.map(text).filter(Boolean))].slice(0, 24);
  }

  function modelCategory(record, models) {
    const haystack = [
      models.join(' '),
      record.platform,
      record.provider,
      record.group_platform,
      record.groupPlatform,
      record.name,
      record.group_name,
      record.groupName,
      record.description,
    ].map(text).join(' ').toLowerCase();
    if (/claude|anthropic/.test(haystack)) return 'Anthropic';
    if (/gemini|google/.test(haystack)) return 'Gemini';
    if (/grok|xai/.test(haystack)) return 'Grok';
    if (/gpt|openai|codex|o\d/.test(haystack)) return 'OpenAI';
    return '其他';
  }

  function groupIdKeys(group) {
    return [
      group.id,
      group.group_id,
      group.groupId,
      group.key,
      group.slug,
    ].map((value) => text(value).toLowerCase()).filter(Boolean);
  }

  function groupAliasKeys(group) {
    return [
      group.name,
      group.group_name,
      group.groupName,
      group.platform,
      group.group_platform,
      group.groupPlatform,
      group.provider,
    ].map((value) => text(value).toLowerCase()).filter(Boolean);
  }

  function buildGroupMap(groups) {
    const byId = new Map();
    const byAlias = new Map();
    for (const group of Array.isArray(groups) ? groups : []) {
      if (!group || typeof group !== 'object') continue;
      for (const key of groupIdKeys(group)) byId.set(key, group);
      for (const key of groupAliasKeys(group)) {
        if (!byAlias.has(key)) byAlias.set(key, group);
        else if (byAlias.get(key) !== group) byAlias.set(key, null);
      }
    }
    return { byId, byAlias };
  }

  function groupForPlan(plan, groupMap) {
    const nestedGroup = plan.group && typeof plan.group === 'object' ? plan.group : null;
    const idKeys = [
      plan.group_id,
      plan.groupId,
      nestedGroup?.id,
      nestedGroup?.group_id,
      nestedGroup?.groupId,
    ].map((value) => text(value).toLowerCase()).filter(Boolean);
    for (const key of idKeys) {
      if (groupMap.byId.has(key)) return groupMap.byId.get(key);
    }
    if (nestedGroup) return nestedGroup;

    const aliasKeys = [
      plan.group_name,
      plan.groupName,
      plan.group_platform,
      plan.groupPlatform,
      plan.platform,
      plan.provider,
      plan.group,
    ].map((value) => text(value).toLowerCase()).filter(Boolean);
    for (const key of aliasKeys) {
      const matched = groupMap.byAlias.get(key);
      if (matched) return matched;
    }
    return null;
  }

  function stableGroupId(group) {
    if (!group || typeof group !== 'object') return '';
    return text(group.id ?? group.group_id ?? group.groupId);
  }

  function stableGroupIdForPlan(plan, group) {
    const nestedGroup = plan.group && typeof plan.group === 'object' ? plan.group : null;
    const explicitId = text(
      plan.group_id
      ?? plan.groupId
      ?? nestedGroup?.id
      ?? nestedGroup?.group_id
      ?? nestedGroup?.groupId,
    );
    if (explicitId) return explicitId;

    // A primitive `group` value is safe only when it exactly matches the resolved group's ID.
    // This prevents same-name groups from receiving each other's user-specific rate.
    if (plan.group !== null && typeof plan.group !== 'object') {
      const primitiveGroup = text(plan.group);
      const resolvedId = stableGroupId(group);
      if (primitiveGroup && primitiveGroup === resolvedId) return primitiveGroup;
    }
    return '';
  }

  function rateValue(value) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      return number(
        value.rate_multiplier
        ?? value.rateMultiplier
        ?? value.user_rate_multiplier
        ?? value.userRateMultiplier
        ?? value.rate
        ?? value.ratio,
      );
    }
    return number(value);
  }

  function buildRateMap(payload) {
    let source = payload;
    if (source && typeof source === 'object' && !Array.isArray(source)) {
      const nested = source.rates ?? source.user_rates ?? source.userRates ?? source.data;
      if (nested && typeof nested === 'object') source = nested;
      else if (['code', 'message', 'error', 'reason', 'success'].some((key) => key in source)) {
        return { complete: false, map: new Map() };
      }
    }

    const map = new Map();
    if (Array.isArray(source)) {
      for (const item of source) {
        if (!item || typeof item !== 'object') return { complete: false, map: new Map() };
        const id = stableGroupId(item);
        const rate = rateValue(item);
        if (!id || rate === '') return { complete: false, map: new Map() };
        map.set(id, rate);
      }
      return { complete: true, map };
    }
    if (source && typeof source === 'object') {
      for (const [rawId, rawRate] of Object.entries(source)) {
        const id = stableGroupId(rawRate) || text(rawId);
        const rate = rateValue(rawRate);
        if (!id || rate === '') return { complete: false, map: new Map() };
        map.set(id, rate);
      }
      return { complete: true, map };
    }
    return { complete: false, map };
  }

  function effectiveRateFields(baseValue, groupId, rateState) {
    const baseRate = number(baseValue);
    if (rateState.complete && groupId && rateState.map.has(groupId)) {
      const userRate = rateState.map.get(groupId);
      return {
        rate_multiplier: userRate,
        base_rate_multiplier: baseRate,
        user_rate_multiplier: userRate,
        rate_data_complete: true,
        rate_source: 'user_override',
      };
    }
    return {
      rate_multiplier: baseRate,
      base_rate_multiplier: baseRate,
      user_rate_multiplier: '',
      rate_data_complete: rateState.complete,
      rate_source: rateState.complete ? 'base' : 'base_fallback_unverified',
    };
  }

  function baseRecord(source, recordType) {
    const siteHost = window.location.host;
    return {
      site: options.site || window.location.origin,
      site_host: siteHost,
      status: 'ok',
      source,
      record_type: recordType,
      fetched_at: new Date().toISOString(),
    };
  }

  function withModelFields(row, sourceRecord) {
    const names = modelNames(sourceRecord);
    return {
      ...row,
      model_category: modelCategory(sourceRecord, names),
      model_names: names.join(' | '),
    };
  }

  function planRecord(source, plan, checkout, groupMap, rateState) {
    const group = groupForPlan(plan, groupMap);
    const classificationSource = group || {
      name: plan.group_name || plan.name,
      platform: plan.group_platform,
      provider: plan.provider,
      description: plan.description,
      features: plan.features,
      models: plan.models,
    };
    const price = number(plan.price);
    const rate = number(checkout && checkout.subscription_usd_to_cny_rate);
    const effectiveRate = effectiveRateFields(
      plan.rate_multiplier ?? plan.rateMultiplier ?? group?.rate_multiplier ?? group?.rateMultiplier,
      stableGroupIdForPlan(plan, group),
      rateState,
    );
    return withModelFields({
      ...baseRecord(source, 'plan'),
      group_id: plan.group_id ?? plan.groupId ?? group?.id ?? group?.group_id ?? group?.groupId ?? '',
      group_name: plan.group_name ?? plan.groupName ?? group?.name ?? group?.group_name ?? '',
      group_platform: plan.group_platform ?? plan.groupPlatform ?? group?.platform ?? group?.group_platform ?? group?.provider ?? '',
      group_status: text(plan.group_status ?? plan.groupStatus ?? group?.status ?? group?.group_status ?? group?.groupStatus),
      is_exclusive: optionalBoolean(plan.is_exclusive ?? plan.isExclusive ?? group?.is_exclusive ?? group?.isExclusive),
      subscription_type: text(plan.subscription_type ?? plan.subscriptionType ?? group?.subscription_type ?? group?.subscriptionType),
      rpm_limit: number(plan.rpm_limit ?? plan.rpmLimit ?? group?.rpm_limit ?? group?.rpmLimit),
      plan_id: plan.id ?? '',
      plan_name: plan.name ?? '',
      price,
      original_price: number(plan.original_price),
      price_currency_hint: rate ? 'USD' : 'configured',
      pay_price_cny: cny(price, checkout),
      subscription_usd_to_cny_rate: rate,
      validity_days: plan.validity_days ?? '',
      validity_unit: plan.validity_unit ?? '',
      ...effectiveRate,
      peak_rate_enabled: plan.peak_rate_enabled ?? '',
      peak_start: plan.peak_start ?? '',
      peak_end: plan.peak_end ?? '',
      peak_rate_multiplier: number(plan.peak_rate_multiplier ?? plan.peakRateMultiplier ?? group?.peak_rate_multiplier ?? group?.peakRateMultiplier),
      daily_limit_usd: number(plan.daily_limit_usd),
      weekly_limit_usd: number(plan.weekly_limit_usd),
      monthly_limit_usd: number(plan.monthly_limit_usd),
      payment_currencies: currencies(checkout),
      features: features(plan.features),
      description: text(plan.description),
    }, classificationSource);
  }

  function groupRecord(group, rateState) {
    const effectiveRate = effectiveRateFields(
      group.rate_multiplier ?? group.rateMultiplier,
      stableGroupId(group),
      rateState,
    );
    return withModelFields({
      ...baseRecord('/groups/available', 'group'),
      group_id: group.id ?? group.group_id ?? group.groupId ?? '',
      group_name: group.name ?? group.group_name ?? group.groupName ?? '',
      group_platform: group.platform ?? group.group_platform ?? group.groupPlatform ?? group.provider ?? '',
      group_status: text(group.status ?? group.group_status ?? group.groupStatus),
      is_exclusive: optionalBoolean(group.is_exclusive ?? group.isExclusive),
      subscription_type: text(group.subscription_type ?? group.subscriptionType),
      rpm_limit: number(group.rpm_limit ?? group.rpmLimit),
      ...effectiveRate,
      peak_rate_enabled: group.peak_rate_enabled ?? '',
      peak_start: group.peak_start ?? '',
      peak_end: group.peak_end ?? '',
      peak_rate_multiplier: number(group.peak_rate_multiplier ?? group.peakRateMultiplier),
      daily_limit_usd: number(group.daily_limit_usd),
      weekly_limit_usd: number(group.weekly_limit_usd),
      monthly_limit_usd: number(group.monthly_limit_usd),
      description: text(group.description),
    }, group);
  }

  const rows = [];
  const errors = [];
  const [groupsResult, checkoutResult, ratesResult] = await Promise.allSettled([
    apiGet('/groups/available'),
    apiGet('/payment/checkout-info'),
    apiGet('/groups/rates'),
  ]);
  let groupsData = [];
  let groupsError = '';
  if (groupsResult.status === 'fulfilled') {
    groupsData = groupsResult.value;
    if (!Array.isArray(groupsData)) groupsData = [];
  } else {
    groupsError = groupsResult.reason?.message || String(groupsResult.reason || '分组接口失败');
  }
  const groupMap = buildGroupMap(groupsData);

  let rateState = { complete: false, map: new Map(), error: '' };
  if (ratesResult.status === 'fulfilled') {
    const parsedRates = buildRateMap(ratesResult.value);
    rateState = {
      ...parsedRates,
      error: parsedRates.complete ? '' : 'UNSUPPORTED_RESPONSE: /groups/rates: invalid rate data',
    };
  } else {
    rateState.error = ratesResult.reason?.message || String(ratesResult.reason || '分组倍率接口失败');
  }
  const rateErrorCode = rateState.error ? failureCode(rateState.error) : '';
  const rateOptionalUnavailable = /HTTP\s+(404|405)\b/i.test(rateState.error);

  let checkout = null;
  if (checkoutResult.status === 'fulfilled') {
    checkout = checkoutResult.value;
    for (const plan of Array.isArray(checkout.plans) ? checkout.plans : []) {
      if (plan && typeof plan === 'object') rows.push(planRecord('/payment/checkout-info', plan, checkout, groupMap, rateState));
    }
  } else {
    errors.push(checkoutResult.reason?.message || String(checkoutResult.reason || '套餐接口失败'));
  }

  if (rows.length === 0) {
    try {
      const plans = await apiGet('/payment/plans');
      for (const plan of Array.isArray(plans) ? plans : []) {
        if (plan && typeof plan === 'object') rows.push(planRecord('/payment/plans', plan, null, groupMap, rateState));
      }
    } catch (error) {
      errors.push(error.message);
    }
  }

  if (options.includeGroups) {
    if (groupsData.length) {
      for (const group of groupsData) {
        if (group && typeof group === 'object') rows.push(groupRecord(group, rateState));
      }
    } else if (groupsError) {
      errors.push(groupsError);
    }
  }

  if (rows.length === 0) {
    const errorMessage = errors.join(' | ') || '未发现价格数据';
    const errorCode = failureCode(errorMessage);
    rows.push({
      site: options.site || window.location.origin,
      site_host: window.location.host,
      status: 'no_price_found',
      source: 'none',
      record_type: 'error',
      model_category: '未获取',
      fetched_at: new Date().toISOString(),
      error: errorMessage,
      error_code: errorCode,
      status_label: failureLabel(errorCode),
    });
  } else if (errors.length) {
    const errorMessage = errors.join(' | ');
    const errorCode = failureCode(errorMessage);
    for (const row of rows) {
      row.status = 'partial';
      row.error = errorMessage;
      row.error_code = errorCode;
      row.status_label = failureLabel(errorCode);
    }
  }

  rows.sort((a, b) => `${a.site_host}|${a.model_category}|${a.group_name}|${a.plan_name}`.localeCompare(
    `${b.site_host}|${b.model_category}|${b.group_name}|${b.plan_name}`,
    'zh-CN',
  ));

  return {
    tokenKey: token()?.key || 'cookie/session',
    rows,
    outputFields: options.outputFields,
    rateData: {
      complete: rateState.complete,
      source: '/groups/rates',
      overrideCount: rateState.map.size,
      error: rateState.error,
      errorCode: rateErrorCode,
      authRequired: rateErrorCode === 'reauth_required',
      optionalUnavailable: rateOptionalUnavailable,
      partial: !rateState.complete && Boolean(rateState.error) && !rateOptionalUnavailable,
    },
  };
})(__OPTIONS__)
