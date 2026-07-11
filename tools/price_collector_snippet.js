(async function run(options) {
  const tokenKeys = ['auth_token', 'access_token', 'token', 'jwt', 'accessToken', 'id_token'];

  function token() {
    for (const key of tokenKeys) {
      const value = localStorage.getItem(key) || sessionStorage.getItem(key);
      if (value) return { key, value };
    }
    for (const storage of [localStorage, sessionStorage]) {
      for (let i = 0; i < storage.length; i += 1) {
        const key = storage.key(i);
        if (!key) continue;
        const value = storage.getItem(key);
        if (key.toLowerCase().includes('token') && value) return { key, value };
        if (value && value.trim().startsWith('{')) {
          try {
            const parsed = JSON.parse(value);
            for (const nestedKey of tokenKeys) {
              if (parsed && parsed[nestedKey]) return { key: `${key}.${nestedKey}`, value: parsed[nestedKey] };
            }
          } catch {}
        }
      }
    }
    return null;
  }

  function endpoint(suffix) {
    return new URL(options.base.replace(/\/$/, '') + suffix, window.location.origin).toString();
  }

  async function apiGet(suffix) {
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
      const message = typeof body === 'string' ? body.slice(0, 240) : JSON.stringify(body).slice(0, 240);
      const authHint = found ? `token=${found.key}` : '未发现 token，已尝试 Cookie 登录态';
      throw new Error(`HTTP_ERROR: ${suffix}: HTTP ${response.status}: ${authHint}: ${message}`);
    }
    if (body && typeof body === 'object' && 'code' in body && 'data' in body) {
      if (body.code === 0 || body.code === 200 || body.success === true) return body.data;
      throw new Error(`UNSUPPORTED_RESPONSE: ${suffix}: ${body.message || body.reason || body.code}`);
    }
    if (typeof body === 'string') {
      throw new Error(`UNSUPPORTED_RESPONSE: ${suffix}: 接口返回了非 JSON 文本`);
    }
    return body;
  }

  function failureCode(message) {
    const text = String(message || '');
    if (/HTTP_ERROR|HTTP\s+(401|403)|unauthori[sz]ed|forbidden|token|session/i.test(text)) return 'reauth_required';
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

  function groupKeys(group) {
    return [
      group.id,
      group.group_id,
      group.groupId,
      group.key,
      group.slug,
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
    const map = new Map();
    for (const group of Array.isArray(groups) ? groups : []) {
      if (!group || typeof group !== 'object') continue;
      for (const key of groupKeys(group)) map.set(key, group);
    }
    return map;
  }

  function groupForPlan(plan, groupMap) {
    const nestedGroup = plan.group && typeof plan.group === 'object' ? plan.group : null;
    const keys = [
      plan.group_id,
      plan.groupId,
      plan.group_name,
      plan.groupName,
      plan.group_platform,
      plan.groupPlatform,
      plan.platform,
      plan.provider,
      plan.group,
      nestedGroup?.id,
      nestedGroup?.group_id,
      nestedGroup?.groupId,
      nestedGroup?.name,
      nestedGroup?.group_name,
      nestedGroup?.groupName,
      nestedGroup?.platform,
      nestedGroup?.group_platform,
      nestedGroup?.groupPlatform,
      nestedGroup?.provider,
    ].map((value) => text(value).toLowerCase()).filter(Boolean);
    for (const key of keys) {
      if (groupMap.has(key)) return groupMap.get(key);
    }
    return nestedGroup;
  }

  function baseRecord(source, recordType) {
    const siteHost = window.location.host;
    return {
      site: window.location.origin,
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

  function planRecord(source, plan, checkout, groupMap) {
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
    return withModelFields({
      ...baseRecord(source, 'plan'),
      group_id: plan.group_id ?? plan.groupId ?? group?.id ?? group?.group_id ?? group?.groupId ?? '',
      group_name: plan.group_name ?? plan.groupName ?? group?.name ?? group?.group_name ?? '',
      group_platform: plan.group_platform ?? plan.groupPlatform ?? group?.platform ?? group?.group_platform ?? group?.provider ?? '',
      plan_id: plan.id ?? '',
      plan_name: plan.name ?? '',
      price,
      original_price: number(plan.original_price),
      price_currency_hint: rate ? 'USD' : 'configured',
      pay_price_cny: cny(price, checkout),
      subscription_usd_to_cny_rate: rate,
      validity_days: plan.validity_days ?? '',
      validity_unit: plan.validity_unit ?? '',
      rate_multiplier: number(plan.rate_multiplier ?? plan.rateMultiplier ?? group?.rate_multiplier ?? group?.rateMultiplier),
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

  function groupRecord(group) {
    return withModelFields({
      ...baseRecord('/groups/available', 'group'),
      group_id: group.id ?? group.group_id ?? group.groupId ?? '',
      group_name: group.name ?? group.group_name ?? group.groupName ?? '',
      group_platform: group.platform ?? group.group_platform ?? group.groupPlatform ?? group.provider ?? '',
      rate_multiplier: number(group.rate_multiplier ?? group.rateMultiplier),
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
  const [groupsResult, checkoutResult] = await Promise.allSettled([
    apiGet('/groups/available'),
    apiGet('/payment/checkout-info'),
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

  let checkout = null;
  if (checkoutResult.status === 'fulfilled') {
    checkout = checkoutResult.value;
    for (const plan of Array.isArray(checkout.plans) ? checkout.plans : []) {
      if (plan && typeof plan === 'object') rows.push(planRecord('/payment/checkout-info', plan, checkout, groupMap));
    }
  } else {
    errors.push(checkoutResult.reason?.message || String(checkoutResult.reason || '套餐接口失败'));
  }

  if (rows.length === 0) {
    try {
      const plans = await apiGet('/payment/plans');
      for (const plan of Array.isArray(plans) ? plans : []) {
        if (plan && typeof plan === 'object') rows.push(planRecord('/payment/plans', plan, null, groupMap));
      }
    } catch (error) {
      errors.push(error.message);
    }
  }

  if (options.includeGroups) {
    if (groupsData.length) {
      for (const group of groupsData) {
        if (group && typeof group === 'object') rows.push(groupRecord(group));
      }
    } else if (groupsError) {
      errors.push(groupsError);
    }
  }

  if (rows.length === 0) {
    const errorMessage = errors.join(' | ') || '未发现价格数据';
    const errorCode = failureCode(errorMessage);
    rows.push({
      site: window.location.origin,
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

  return { tokenKey: token()?.key || 'cookie/session', rows, outputFields: options.outputFields };
})(__OPTIONS__)
