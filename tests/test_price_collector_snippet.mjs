import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const COLLECTOR_PATH = path.resolve(TEST_DIR, '..', 'tools', 'price_collector_snippet.js');
const COLLECTOR_SOURCE = readFileSync(COLLECTOR_PATH, 'utf8');

function storage(values = {}) {
  const entries = new Map(Object.entries(values).map(([key, value]) => [key, String(value)]));
  return {
    get length() {
      return entries.size;
    },
    getItem(key) {
      return entries.has(key) ? entries.get(key) : null;
    },
    key(index) {
      return [...entries.keys()][index] ?? null;
    },
    setItem(key, value) {
      entries.set(String(key), String(value));
    },
  };
}

function apiResponse(data, status = 200) {
  return {
    status,
    body: status >= 200 && status < 300
      ? { code: 0, data }
      : { message: String(data) },
  };
}

async function runCollector(routes, extraOptions = {}, environment = {}) {
  const requestedPaths = [];
  const requests = [];
  const local = storage(environment.localStorage);
  const session = storage(environment.sessionStorage);
  const fetch = async (url, options = {}) => {
    const pathname = new URL(url).pathname;
    requestedPaths.push(pathname);
    const request = {
      url: String(url),
      pathname,
      method: String(options.method || 'GET').toUpperCase(),
      headers: options.headers || {},
      body: options.body,
    };
    requests.push(request);
    const configuredRoute = routes[pathname];
    const route = typeof configuredRoute === 'function'
      ? await configuredRoute(request)
      : configuredRoute;
    if (route instanceof Error) throw route;
    const response = route ?? apiResponse('not found', 404);
    return {
      ok: response.status >= 200 && response.status < 300,
      status: response.status,
      async text() {
        return JSON.stringify(response.body);
      },
    };
  };

  const context = {
    __OPTIONS__: {
      base: '/api/v1',
      includeGroups: true,
      outputFields: [],
      requestTimeoutMs: 1000,
      ...extraOptions,
    },
    AbortController,
    URL,
    fetch,
    localStorage: local,
    sessionStorage: session,
    window: {
      clearTimeout,
      location: {
        host: 'monitor.example',
        origin: 'https://monitor.example',
      },
      setTimeout,
    },
  };
  const script = new vm.Script(COLLECTOR_SOURCE, { filename: COLLECTOR_PATH });
  const result = await script.runInNewContext(context);
  return { requestedPaths, requests, result, localStorage: local, sessionStorage: session };
}

test('collector source parses as a standalone WebView expression', () => {
  assert.doesNotThrow(() => new vm.Script(COLLECTOR_SOURCE, { filename: COLLECTOR_PATH }));
});

test('user rates merge by stable ID and preserve richer group metadata', async () => {
  const groups = [
    {
      id: 'group-a',
      name: 'Shared name',
      platform: 'openai',
      rate_multiplier: 1,
      status: 'active',
      is_exclusive: true,
      subscription_type: 'monthly',
      rpm_limit: 60,
    },
    {
      id: 'group-b',
      name: 'Shared name',
      platform: 'openai',
      rate_multiplier: 2,
      status: 'paused',
      is_exclusive: 'false',
      subscription_type: 'quota',
      rpm_limit: '120',
    },
    {
      id: 'group-c',
      name: 'Base only',
      platform: 'anthropic',
      rate_multiplier: 3,
    },
  ];
  const checkout = {
    plans: [
      { id: 'plan-a', name: 'Plan A', group_id: 'group-a', rate_multiplier: 1, price: 10 },
      { id: 'plan-b', name: 'Plan B', group_id: 'group-b', rate_multiplier: 2, price: 20 },
      { id: 'ambiguous', name: 'Ambiguous', group_name: 'Shared name', rate_multiplier: 9, price: 30 },
    ],
  };
  const { requestedPaths, result } = await runCollector({
    '/api/v1/groups/available': apiResponse(groups),
    '/api/v1/groups/rates': apiResponse({ 'group-a': 1.25, 'group-b': 2.5 }),
    '/api/v1/payment/checkout-info': apiResponse(checkout),
  });

  assert.ok(requestedPaths.includes('/api/v1/groups/rates'));
  assert.deepEqual({ ...result.rateData }, {
    complete: true,
    source: '/groups/rates',
    overrideCount: 2,
    error: '',
    errorCode: '',
    authRequired: false,
    optionalUnavailable: false,
    partial: false,
  });

  const groupRows = result.rows.filter((row) => row.record_type === 'group');
  const byGroupId = Object.fromEntries(groupRows.map((row) => [row.group_id, row]));
  assert.equal(byGroupId['group-a'].rate_multiplier, 1.25);
  assert.equal(byGroupId['group-a'].base_rate_multiplier, 1);
  assert.equal(byGroupId['group-a'].user_rate_multiplier, 1.25);
  assert.equal(byGroupId['group-a'].rate_source, 'user_override');
  assert.equal(byGroupId['group-a'].rate_data_complete, true);
  assert.equal(byGroupId['group-a'].group_status, 'active');
  assert.equal(byGroupId['group-a'].is_exclusive, true);
  assert.equal(byGroupId['group-a'].subscription_type, 'monthly');
  assert.equal(byGroupId['group-a'].rpm_limit, 60);

  assert.equal(byGroupId['group-b'].rate_multiplier, 2.5);
  assert.equal(byGroupId['group-b'].base_rate_multiplier, 2);
  assert.equal(byGroupId['group-b'].is_exclusive, false);
  assert.equal(byGroupId['group-b'].rpm_limit, 120);
  assert.equal(byGroupId['group-c'].rate_multiplier, 3);
  assert.equal(byGroupId['group-c'].base_rate_multiplier, 3);
  assert.equal(byGroupId['group-c'].user_rate_multiplier, '');
  assert.equal(byGroupId['group-c'].rate_data_complete, true);
  assert.equal(byGroupId['group-c'].rate_source, 'base');

  const planRows = Object.fromEntries(
    result.rows.filter((row) => row.record_type === 'plan').map((row) => [row.plan_id, row]),
  );
  assert.equal(planRows['plan-a'].rate_multiplier, 1.25);
  assert.equal(planRows['plan-b'].rate_multiplier, 2.5);
  assert.equal(planRows.ambiguous.rate_multiplier, 9);
  assert.equal(planRows.ambiguous.user_rate_multiplier, '');
  assert.equal(planRows.ambiguous.rate_source, 'base');
});

test('rates failure is non-fatal and marks base fallback as unverified', async () => {
  const { result } = await runCollector({
    '/api/v1/groups/available': apiResponse([
      { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
    ]),
    '/api/v1/groups/rates': new Error('rates endpoint unavailable'),
    '/api/v1/payment/checkout-info': apiResponse({ plans: [] }),
    '/api/v1/payment/plans': apiResponse([]),
  });

  assert.equal(result.rateData.complete, false);
  assert.match(result.rateData.error, /NETWORK_ERROR.*groups\/rates/i);
  assert.equal(result.rateData.errorCode, 'network_error');
  assert.equal(result.rateData.authRequired, false);
  assert.equal(result.rateData.optionalUnavailable, false);
  assert.equal(result.rateData.partial, true);
  const row = result.rows.find((candidate) => candidate.record_type === 'group');
  assert.ok(row);
  assert.equal(row.status, 'ok');
  assert.equal(row.rate_multiplier, 1.5);
  assert.equal(row.base_rate_multiplier, 1.5);
  assert.equal(row.user_rate_multiplier, '');
  assert.equal(row.rate_data_complete, false);
  assert.equal(row.rate_source, 'base_fallback_unverified');
  assert.equal('error' in row, false);
});

test('HTTP 503 remains an HTTP error instead of requiring reauthorization', async () => {
  const unavailable = apiResponse('service unavailable', 503);
  const { result } = await runCollector({
    '/api/v1/groups/available': unavailable,
    '/api/v1/groups/rates': unavailable,
    '/api/v1/payment/checkout-info': unavailable,
    '/api/v1/payment/plans': unavailable,
  });

  assert.equal(result.rows.length, 1);
  assert.equal(result.rows[0].record_type, 'error');
  assert.equal(result.rows[0].error_code, 'http_error');
  assert.equal(result.rows[0].status_label, 'HTTP 接口错误');
  assert.equal(result.rateData.errorCode, 'http_error');
  assert.equal(result.rateData.authRequired, false);
  assert.equal(result.rateData.optionalUnavailable, false);
  assert.equal(result.rateData.partial, true);
});

test('rates HTTP 401 and 403 require reauthorization', async () => {
  for (const status of [401, 403]) {
    const { result } = await runCollector({
      '/api/v1/groups/available': apiResponse([
        { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
      ]),
      '/api/v1/groups/rates': apiResponse('authorization expired', status),
      '/api/v1/payment/checkout-info': apiResponse({
        plans: [{ id: 'plan-a', name: 'Plan A', group_id: 'group-a', price: 10 }],
      }),
    });

    assert.equal(result.rateData.complete, false, `HTTP ${status}`);
    assert.equal(result.rateData.errorCode, 'reauth_required', `HTTP ${status}`);
    assert.equal(result.rateData.authRequired, true, `HTTP ${status}`);
    assert.equal(result.rateData.optionalUnavailable, false, `HTTP ${status}`);
    assert.equal(result.rateData.partial, true, `HTTP ${status}`);
  }
});

test('rates HTTP 404 and 405 are optional endpoint unavailability', async () => {
  for (const status of [404, 405]) {
    const { result } = await runCollector({
      '/api/v1/groups/available': apiResponse([
        { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
      ]),
      '/api/v1/groups/rates': apiResponse('not supported', status),
      '/api/v1/payment/checkout-info': apiResponse({
        plans: [{ id: 'plan-a', name: 'Plan A', group_id: 'group-a', price: 10 }],
      }),
    });

    assert.equal(result.rateData.complete, false, `HTTP ${status}`);
    assert.equal(result.rateData.errorCode, 'http_error', `HTTP ${status}`);
    assert.equal(result.rateData.authRequired, false, `HTTP ${status}`);
    assert.equal(result.rateData.optionalUnavailable, true, `HTTP ${status}`);
    assert.equal(result.rateData.partial, false, `HTTP ${status}`);
    const group = result.rows.find((row) => row.record_type === 'group');
    assert.equal(group.rate_source, 'base_fallback_unverified', `HTTP ${status}`);
  }
});

test('HTTP 200 malformed rates payload is not accepted as complete data', async () => {
  const { result } = await runCollector({
    '/api/v1/groups/available': apiResponse([
      { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
    ]),
    '/api/v1/groups/rates': {
      status: 200,
      body: { code: 500, message: 'unexpected success payload' },
    },
    '/api/v1/payment/checkout-info': apiResponse({
      plans: [{ id: 'plan-a', name: 'Plan A', group_id: 'group-a', price: 10 }],
    }),
  });

  assert.equal(result.rateData.complete, false);
  assert.equal(result.rateData.errorCode, 'unsupported_response');
  assert.equal(result.rateData.authRequired, false);
  assert.equal(result.rateData.optionalUnavailable, false);
  assert.equal(result.rateData.partial, true);
  const group = result.rows.find((row) => row.record_type === 'group');
  assert.equal(group.rate_multiplier, 1.5);
  assert.equal(group.user_rate_multiplier, '');
  assert.equal(group.rate_source, 'base_fallback_unverified');
});

test('HTTP 200 business auth codes silently refresh and retry once', async () => {
  for (const rejectedBody of [
    { code: 401, message: 'access token expired' },
    { success: false, status: 403, message: 'session rejected' },
  ]) {
    const replacement = `replacement-${rejectedBody.code || rejectedBody.status}`;
    let rateCalls = 0;
    let refreshCalls = 0;
    const { result } = await runCollector({
      '/api/v1/groups/available': apiResponse([
        { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
      ]),
      '/api/v1/groups/rates': (request) => {
        rateCalls += 1;
        return request.headers.Authorization === `Bearer ${replacement}`
          ? apiResponse({ 'group-a': 1.25 })
          : { status: 200, body: rejectedBody };
      },
      '/api/v1/payment/checkout-info': apiResponse({
        plans: [{ id: 'plan-a', name: 'Plan A', group_id: 'group-a', price: 10 }],
      }),
      '/api/v1/auth/refresh': () => {
        refreshCalls += 1;
        return { status: 200, body: { code: 0, data: { access_token: replacement } } };
      },
    }, {}, {
      localStorage: {
        access_token: 'expired-access',
        refresh_token: 'valid-refresh',
      },
    });

    assert.equal(rateCalls, 2);
    assert.equal(refreshCalls, 1);
    assert.equal(result.rateData.complete, true);
    assert.equal(result.rateData.authRequired, false);
  }
});

test('HTTP 200 business auth rejection after one retry requires reauthorization', async () => {
  let rateCalls = 0;
  let refreshCalls = 0;
  const { result } = await runCollector({
    '/api/v1/groups/available': apiResponse([
      { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
    ]),
    '/api/v1/groups/rates': () => {
      rateCalls += 1;
      return { status: 200, body: { success: false, code: 401, message: 'token still expired' } };
    },
    '/api/v1/payment/checkout-info': apiResponse({
      plans: [{ id: 'plan-a', name: 'Plan A', group_id: 'group-a', price: 10 }],
    }),
    '/api/v1/auth/refresh': () => {
      refreshCalls += 1;
      return { status: 200, body: { code: 0, data: { access_token: 'replacement-access' } } };
    },
  }, {}, {
    localStorage: {
      access_token: 'expired-access',
      refresh_token: 'valid-refresh',
    },
  });

  assert.equal(rateCalls, 2);
  assert.equal(refreshCalls, 1);
  assert.equal(result.rateData.complete, false);
  assert.equal(result.rateData.errorCode, 'reauth_required');
  assert.equal(result.rateData.authRequired, true);
});

test('HTTP 200 non-auth business errors never require reauthorization', async () => {
  let refreshCalls = 0;
  const { result } = await runCollector({
    '/api/v1/groups/available': apiResponse([
      { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
    ]),
    '/api/v1/groups/rates': {
      status: 200,
      body: { success: false, code: 500, message: 'forbidden plan tier' },
    },
    '/api/v1/payment/checkout-info': apiResponse({
      plans: [{ id: 'plan-a', name: 'Plan A', group_id: 'group-a', price: 10 }],
    }),
    '/api/v1/auth/refresh': () => {
      refreshCalls += 1;
      return apiResponse({ access_token: 'unexpected' });
    },
  }, {}, {
    localStorage: {
      access_token: 'valid-access',
      refresh_token: 'valid-refresh',
    },
  });

  assert.equal(refreshCalls, 0);
  assert.equal(result.rateData.complete, false);
  assert.equal(result.rateData.errorCode, 'unsupported_response');
  assert.equal(result.rateData.authRequired, false);
  assert.equal(result.rateData.partial, true);
});

test('same-name groups do not bind an ambiguous plan by alias', async () => {
  const { result } = await runCollector({
    '/api/v1/groups/available': apiResponse([
      { id: 'group-a', name: 'Shared', platform: 'openai', rate_multiplier: 1, status: 'active' },
      { id: 'group-b', name: 'Shared', platform: 'anthropic', rate_multiplier: 2, status: 'paused' },
    ]),
    '/api/v1/groups/rates': apiResponse({ 'group-a': 1.25, 'group-b': 2.5 }),
    '/api/v1/payment/checkout-info': apiResponse({
      plans: [{ id: 'ambiguous', name: 'Ambiguous', group_name: 'Shared', rate_multiplier: 9, price: 30 }],
    }),
  });

  const plan = result.rows.find((row) => row.plan_id === 'ambiguous');
  assert.ok(plan);
  assert.equal(plan.group_id, '');
  assert.equal(plan.group_status, '');
  assert.equal(plan.rate_multiplier, 9);
  assert.equal(plan.user_rate_multiplier, '');
  assert.equal(plan.rate_source, 'base');
});

test('options.site preserves the complete site URL including its path', async () => {
  const site = 'https://monitor.example/tenant/acme/';
  const { result } = await runCollector({
    '/api/v1/groups/available': apiResponse([
      { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
    ]),
    '/api/v1/groups/rates': apiResponse({ 'group-a': 1.25 }),
    '/api/v1/payment/checkout-info': apiResponse({
      plans: [{ id: 'plan-a', name: 'Plan A', group_id: 'group-a', price: 10 }],
    }),
  }, { site });

  assert.ok(result.rows.length > 0);
  assert.ok(result.rows.every((row) => row.site === site));
  assert.ok(result.rows.every((row) => row.site_host === 'monitor.example'));
});

test('concurrent 401 responses share one same-origin refresh and retry once', async () => {
  const oldAccessToken = 'old-access-token';
  const newAccessToken = 'new-access-token';
  const refreshSecret = 'refresh-secret';
  let refreshCalls = 0;
  const guarded = (data) => (request) => (
    request.headers.Authorization === `Bearer ${newAccessToken}`
      ? apiResponse(data)
      : apiResponse('expired', 401)
  );
  const { result, requests, localStorage } = await runCollector({
    '/api/v1/groups/available': guarded([
      { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
    ]),
    '/api/v1/groups/rates': guarded({ 'group-a': 1.25 }),
    '/api/v1/payment/checkout-info': guarded({
      plans: [{ id: 'plan-a', name: 'Plan A', group_id: 'group-a', price: 10 }],
    }),
    '/api/v1/auth/refresh': (request) => {
      refreshCalls += 1;
      assert.equal(request.url, 'https://monitor.example/api/v1/auth/refresh');
      assert.equal(request.method, 'POST');
      assert.equal(request.headers.Authorization, undefined);
      assert.deepEqual(JSON.parse(request.body), { refresh_token: refreshSecret });
      return {
        status: 200,
        body: { code: 0, data: { access_token: newAccessToken } },
      };
    },
  }, {}, {
    localStorage: {
      access_token: oldAccessToken,
      refresh_token: refreshSecret,
    },
  });

  assert.equal(refreshCalls, 1);
  for (const pathname of [
    '/api/v1/groups/available',
    '/api/v1/groups/rates',
    '/api/v1/payment/checkout-info',
  ]) {
    assert.equal(requests.filter((request) => request.pathname === pathname).length, 2, pathname);
  }
  assert.equal(localStorage.getItem('access_token'), newAccessToken);
  assert.equal(localStorage.getItem('refresh_token'), refreshSecret);
  assert.equal(result.rateData.authRequired, false);
  assert.equal(result.rateData.complete, true);
  const serialized = JSON.stringify(result);
  assert.equal(serialized.includes(oldAccessToken), false);
  assert.equal(serialized.includes(newAccessToken), false);
  assert.equal(serialized.includes(refreshSecret), false);
});

test('nested session tokens are refreshed in their original JSON location', async () => {
  const oldAccessToken = 'nested-old-access';
  const newAccessToken = 'nested-new-access';
  const refreshSecret = 'nested-refresh-secret';
  const sessionState = {
    account: {
      session: {
        accessToken: oldAccessToken,
        refreshToken: refreshSecret,
      },
    },
  };
  const { result, sessionStorage } = await runCollector({
    '/api/v1/groups/available': apiResponse([
      { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
    ]),
    '/api/v1/groups/rates': (request) => (
      request.headers.Authorization === `Bearer ${newAccessToken}`
        ? apiResponse({ 'group-a': 1.25 })
        : apiResponse('expired', 403)
    ),
    '/api/v1/payment/checkout-info': apiResponse({
      plans: [{ id: 'plan-a', name: 'Plan A', group_id: 'group-a', price: 10 }],
    }),
    '/api/v1/auth/refresh': {
      status: 200,
      body: {
        success: true,
        result: { tokens: { accessToken: newAccessToken } },
      },
    },
  }, {}, {
    sessionStorage: { auth_state: JSON.stringify(sessionState) },
  });

  const persisted = JSON.parse(sessionStorage.getItem('auth_state'));
  assert.equal(persisted.account.session.accessToken, newAccessToken);
  assert.equal(persisted.account.session.refreshToken, refreshSecret);
  assert.equal(sessionStorage.getItem('access_token'), null);
  assert.equal(result.rateData.complete, true);
  assert.equal(result.rateData.authRequired, false);
});

test('a refreshed access token stays in memory when no access-token location exists', async () => {
  const newAccessToken = 'memory-only-access';
  let rateCalls = 0;
  const { result, localStorage, sessionStorage } = await runCollector({
    '/api/v1/groups/available': apiResponse([
      { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
    ]),
    '/api/v1/groups/rates': (request) => {
      rateCalls += 1;
      return request.headers.Authorization === `Bearer ${newAccessToken}`
        ? apiResponse({ 'group-a': 1.25 })
        : apiResponse('cookie session expired', 401);
    },
    '/api/v1/payment/checkout-info': apiResponse({
      plans: [{ id: 'plan-a', name: 'Plan A', group_id: 'group-a', price: 10 }],
    }),
    '/api/v1/auth/refresh': {
      status: 200,
      body: { code: 0, data: { access_token: newAccessToken } },
    },
  }, {}, {
    sessionStorage: { refresh_token: 'memory-refresh-secret' },
  });

  assert.equal(rateCalls, 2);
  assert.equal(localStorage.getItem('access_token'), null);
  assert.equal(sessionStorage.getItem('access_token'), null);
  assert.equal(result.rateData.complete, true);
  assert.equal(result.rateData.authRequired, false);
  assert.equal(JSON.stringify(result).includes(newAccessToken), false);
});

test('failed silent refresh preserves the reauthorization requirement', async () => {
  let rateCalls = 0;
  let refreshCalls = 0;
  const { result, localStorage } = await runCollector({
    '/api/v1/groups/available': apiResponse([
      { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
    ]),
    '/api/v1/groups/rates': () => {
      rateCalls += 1;
      return apiResponse('expired', 401);
    },
    '/api/v1/payment/checkout-info': apiResponse({
      plans: [{ id: 'plan-a', name: 'Plan A', group_id: 'group-a', price: 10 }],
    }),
    '/api/v1/auth/refresh': () => {
      refreshCalls += 1;
      return apiResponse('refresh rejected', 401);
    },
  }, {}, {
    localStorage: {
      access_token: 'expired-access',
      refresh_token: 'expired-refresh',
    },
  });

  assert.equal(rateCalls, 1);
  assert.equal(refreshCalls, 1);
  assert.equal(localStorage.getItem('access_token'), 'expired-access');
  assert.equal(result.rateData.errorCode, 'reauth_required');
  assert.equal(result.rateData.authRequired, true);
  const serialized = JSON.stringify(result);
  assert.equal(serialized.includes('expired-access'), false);
  assert.equal(serialized.includes('expired-refresh'), false);
});

test('a request is retried only once when the refreshed token is also rejected', async () => {
  let rateCalls = 0;
  let refreshCalls = 0;
  const { result } = await runCollector({
    '/api/v1/groups/available': apiResponse([
      { id: 'group-a', name: 'Primary', rate_multiplier: 1.5 },
    ]),
    '/api/v1/groups/rates': () => {
      rateCalls += 1;
      return apiResponse('still unauthorized', 401);
    },
    '/api/v1/payment/checkout-info': apiResponse({
      plans: [{ id: 'plan-a', name: 'Plan A', group_id: 'group-a', price: 10 }],
    }),
    '/api/v1/auth/refresh': () => {
      refreshCalls += 1;
      return {
        status: 200,
        body: { data: { access_token: 'replacement-access' } },
      };
    },
  }, {}, {
    localStorage: {
      access_token: 'expired-access',
      refresh_token: 'valid-refresh',
    },
  });

  assert.equal(rateCalls, 2);
  assert.equal(refreshCalls, 1);
  assert.equal(result.rateData.errorCode, 'reauth_required');
  assert.equal(result.rateData.authRequired, true);
});

test('cross-origin API bases never receive stored access or refresh tokens', async () => {
  const { requests, result } = await runCollector({}, {
    base: 'https://outside.example/api/v1',
  }, {
    localStorage: {
      access_token: 'private-access',
      refresh_token: 'private-refresh',
    },
  });

  assert.equal(requests.length, 0);
  assert.equal(JSON.stringify(result).includes('private-access'), false);
  assert.equal(JSON.stringify(result).includes('private-refresh'), false);
});
