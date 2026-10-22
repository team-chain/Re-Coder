// 2.0.36 작업별 AI 공급자 — 화면(등급·비용 원장·비교 표)과 Core 에 넘기는 환경변수(키는 필요한 것만).
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const Module = require('node:module');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const model = require('../out/webview-test/components/modelRoutingModel.js');
const { ModelRoutingView } = require('../out/webview-test/components/ModelRouting.js');
const { FEATURES } = require('../out/webview-test/components/Hubs.js');
const env = require('../out/ai/aiKeyEnv.js');

const resolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...args) {
  return request === 'vscode' ? path.join(__dirname, '../harness/vscode-mock.js') : resolve.call(this, request, ...args);
};
const vscode = require('vscode');
const ai = require('../out/ai/aiKeys.js');
Module._resolveFilename = resolve;

const ANTHROPIC = 'sk-ant-api03-' + 'a'.repeat(40);
const OPENAI = 'sk-proj-' + 'o'.repeat(40);
const COMPAT = 'sk-' + 'c'.repeat(32);

function context() {
  const secrets = new Map(), state = new Map();
  return {
    secrets: { get: async (k) => secrets.get(k), store: async (k, v) => { secrets.set(k, v); }, delete: async (k) => { secrets.delete(k); } },
    globalState: { get: (k, d) => (state.has(k) ? state.get(k) : d), update: async (k, v) => { v === undefined ? state.delete(k) : state.set(k, v); } },
    secrets_: secrets,
  };
}

const routing = {
  quality: { provider: 'anthropic', model: 'claude-sonnet-5' },
  economy: { spec: 'compatible', provider: 'compatible', model: 'deepseek-chat', separate: true, scrubbed: true, note: '' },
  economy_operations: ['generate_plan_topics', 'generate_test_draft'],
  choices: [
    { spec: 'quality', label: '지금 품질 공급자', ready: true, provider: 'anthropic', model: 'claude-sonnet-5' },
    { spec: 'fast', label: '품질 공급자의 빠른 모델', ready: true, provider: 'anthropic', model: 'claude-sonnet-5' },
    { spec: 'anthropic', label: 'Claude API', ready: true, provider: 'anthropic', model: 'claude-sonnet-5' },
    { spec: 'openai', label: 'OpenAI API', ready: false, provider: '', model: '' },
    { spec: 'compatible', label: 'OpenAI 호환 API', ready: true, provider: 'compatible', model: 'deepseek-chat' },
  ],
};
const costs = {
  calls: 5, total_cost_usd: 0.42, unknown_cost_calls: 2, economy_saved_usd: 0.3, days: 30, persisted: true,
  groups: [
    { tier: 'quality', provider: 'anthropic', model: 'claude-sonnet-5', calls: 3, input_tokens: 12000, output_tokens: 3400, cost_usd: 0.42, unknown_cost_calls: 0, estimated_token_calls: 0, masked: 0 },
    { tier: 'economy', provider: 'compatible', model: 'deepseek-chat', calls: 2, input_tokens: 2000, output_tokens: 500, cost_usd: 0, unknown_cost_calls: 2, estimated_token_calls: 0, masked: 3 },
  ],
};
const metrics = (o) => ({ calls: 4, repair_calls: 0, input_tokens: 5000, output_tokens: 900, estimated_token_calls: 0, cost_usd: 0.05,
  unknown_cost_calls: 0, masked: 0, used: [], files: 3, remaining_errors: 0, security_score: 90, security_findings: 1, ...o });
const job = {
  id: 'abc123def456', status: 'finished', started_at: 1, finished_at: 2,
  sides: [
    { spec: 'quality', provider: 'anthropic', model: 'claude-sonnet-5', status: 'succeeded', metrics: metrics({}), error: '', seconds: 61.2 },
    { spec: 'compatible', provider: 'compatible', model: 'deepseek-chat', status: 'succeeded', metrics: metrics({ cost_usd: null, unknown_cost_calls: 6, repair_calls: 2, remaining_errors: 1, security_score: 70 }), error: '', seconds: 80 },
  ],
  verdict: { cheaper: null, fewer_repairs: 'quality', safer: 'quality', fewer_errors: 'quality' },
};

test('cost cells never show unknown prices as zero dollars', () => {
  assert.equal(model.formatUsd(null), '단가 미설정');
  assert.equal(model.formatUsd(0.004), '$0.0040');
  assert.equal(model.formatUsd(0.00001), '<$0.0001');
  assert.equal(model.formatUsd(0.4213), '$0.421');
  assert.equal(model.costCell(null, 3), '단가 미설정');
  assert.equal(model.costCell(0.5, 2), '$0.500 + 단가 미설정 2회');
  assert.equal(model.costCell(null, 0), '$0');
  assert.equal(model.formatTokens(1534000), '1.5M');
});

test('compare rows mark the better side per item and never a total winner', () => {
  const rows = Object.fromEntries(model.compareRows(job).map((r) => [r.key, r]));
  assert.equal(rows.cost.better, null);
  assert.deepEqual(rows.cost.values, ['$0.050', '단가 미설정']);
  assert.equal(rows.repairs.better, 0);
  assert.deepEqual(rows.repairs.values, ['0회', '2회']);
  assert.equal(rows.security.values[0], '90점 · 확인 1곳');
  assert.equal(rows.time.values[1], '80초');
  const failed = { ...job, sides: [job.sides[0], { ...job.sides[1], status: 'failed', metrics: null, error: 'down' }], verdict: {} };
  assert.equal(model.compareRows(failed).find((r) => r.key === 'repairs').values[1], '실패');
});

test('compare choices skip unready and duplicate provider/model pairs', () => {
  const specs = model.compareChoices(routing).map((c) => c.spec);
  assert.deepEqual(specs, ['quality', 'economy'], 'fast·anthropic 은 품질과 같은 모델, openai 는 준비 안 됨, compatible 은 economy 와 같음');
});

test('the view shows tiers, the masked note, the ledger and the comparison', () => {
  const html = renderToStaticMarkup(React.createElement(ModelRoutingView, {
    state: { routing, costs, compare: job, keys: { anthropic: true, openai: false, compatible: true }, settings: { economyProvider: 'compatible', compatibleBaseUrl: 'https://api.deepseek.com' } },
    job, compareError: '', starting: false, instruction: '할 일 앱', pair: ['quality', 'economy'],
    post: () => {}, onReload: () => {}, onInstruction: () => {}, onPair: () => {}, onStart: () => {},
  }));
  assert.match(html, /data-tier="quality"/);
  assert.match(html, /OpenAI 호환/);
  assert.match(html, /data-testid="economy-scrubbed"/);
  assert.match(html, /설계 질문 주제 · 테스트 초안/);
  assert.match(html, /단가 미설정 2회/);
  assert.match(html, /아낀 비용 \$0\.300/);
  assert.match(html, /가림 3곳/);
  assert.match(html, /OpenAI API — 키 입력 필요/);
  assert.match(html, /data-testid="compare-result"/);
  assert.equal((html.match(/data-better="true"/g) || []).length, 3);
  assert.match(html, /종합 판단은 결과 코드를 직접 보고/);
  assert.ok(!html.includes(COMPAT) && !html.includes(ANTHROPIC));
});

test('models is a develop feature', () => {
  const f = FEATURES.find((x) => x.id === 'models');
  assert.equal(f.hub, 'develop');
});

test('compatible address rules match the Core', () => {
  assert.equal(env.validateCompatibleUrl('https://api.deepseek.com'), undefined);
  assert.equal(env.validateCompatibleUrl('http://localhost:11434/v1'), undefined);
  assert.equal(env.validateCompatibleUrl('http://[::1]:8000'), undefined);
  for (const bad of ['', 'api.deepseek.com', 'http://api.deepseek.com', 'https://u:p@api.x.com', 'https://api.x.com/v1?key=1', 'ftp://x']) {
    assert.ok(env.validateCompatibleUrl(bad), bad);
  }
});

test('prices are passed only when valid', () => {
  assert.equal(env.pricesEnv(undefined), '');
  assert.equal(env.pricesEnv([1]), '');
  assert.equal(env.pricesEnv({ 'deepseek-chat': { input: 0.5, output: 1.5 }, bad: { input: -1, output: 1 }, nan: { input: 'x', output: 1 }, empty: { input: '', output: 1 }, nul: { input: null, output: 1 } }),
    JSON.stringify({ 'deepseek-chat': { input: 0.5, output: 1.5 } }));
});

test('economy keys go to the Core only when chosen', async () => {
  const c = context();
  await ai.storeAiKey(c, 'anthropic', ANTHROPIC);
  await ai.storeAiKey(c, 'compatible', COMPAT, false);
  await ai.storeAiKey(c, 'openai', OPENAI, false);
  assert.equal(await ai.currentAiProvider(c), 'anthropic', '절약용 키 저장은 기본 연결을 바꾸지 않는다');
  // 절약 공급자를 안 골랐으면 기본 키만
  assert.deepEqual(await env.aiKeyEnv(c, { economyProvider: '' }), { RECODER_AI_PROVIDER: 'anthropic', RECODER_ANTHROPIC_API_KEY: ANTHROPIC });
  // 호환 API 를 골랐지만 주소가 없으면 키를 넘기지 않고 절약 등급도 켜지 않는다
  let out = await env.aiKeyEnv(c, { economyProvider: 'compatible' });
  assert.equal(out.RECODER_COMPATIBLE_API_KEY, undefined);
  assert.equal(out.RECODER_ECONOMY_PROVIDER, undefined);
  out = await env.aiKeyEnv(c, { economyProvider: 'compatible', compatibleBaseUrl: 'https://api.deepseek.com', economyModel: 'deepseek-chat',
    modelFor: (p) => (p === 'compatible' ? 'deepseek-v4' : ''), prices: { 'deepseek-chat': { input: 0.5, output: 1 } } });
  assert.equal(out.RECODER_ECONOMY_PROVIDER, 'compatible');
  assert.equal(out.RECODER_COMPATIBLE_API_KEY, COMPAT);
  assert.equal(out.RECODER_COMPATIBLE_BASE_URL, 'https://api.deepseek.com');
  assert.equal(out.RECODER_COMPATIBLE_MODEL, 'deepseek-v4');
  assert.equal(out.RECODER_ECONOMY_MODEL, 'deepseek-chat');
  assert.equal(out.RECODER_OPENAI_API_KEY, undefined, '고르지 않은 키는 넘기지 않는다');
  assert.ok(out.RECODER_MODEL_PRICES.includes('deepseek-chat'));
  // fast 는 키가 필요 없다
  out = await env.aiKeyEnv(c, { economyProvider: 'fast' });
  assert.equal(out.RECODER_ECONOMY_PROVIDER, 'fast');
  // 알 수 없는 값은 무시
  assert.equal((await env.aiKeyEnv(c, { economyProvider: 'evil' })).RECODER_ECONOMY_PROVIDER, undefined);
  // 기존 함수 인자 형식도 그대로
  assert.equal((await env.aiKeyEnv(c, () => 'claude-opus-5-5')).RECODER_ANTHROPIC_MODEL, 'claude-opus-5-5');
  assert.deepEqual(await env.storedAiKeys(c), { anthropic: true, openai: true, compatible: true });
  await ai.clearAiKeys(c);
  assert.equal(c.secrets_.size, 0);
});

test('compatible key validation has no vendor prefix rule', () => {
  assert.equal(ai.validateApiKey('compatible', COMPAT), undefined);
  assert.equal(ai.validateApiKey('compatible', 'x'.repeat(40)), undefined);
  assert.match(ai.validateApiKey('compatible', 'short-key-1234'), /길이/);
  assert.match(ai.validateApiKey('compatible', 'has space ' + 'x'.repeat(30)), /공백/);
});

test('the economy command asks for a key without changing the default connection', async () => {
  const c = context();
  await ai.storeAiKey(c, 'anthropic', ANTHROPIC);
  const values = new Map();
  const orig = { cfg: vscode.workspace.getConfiguration, input: vscode.window.showInputBox, info: vscode.window.showInformationMessage };
  vscode.workspace.getConfiguration = () => ({ get: (k, d) => (values.has(k) ? values.get(k) : d), update: async (k, v) => { values.set(k, v); } });
  const prompts = [];
  vscode.window.showInputBox = async (o) => { prompts.push(o); return o.password ? COMPAT : 'https://api.deepseek.com'; };
  vscode.window.showInformationMessage = async () => undefined;
  try {
    assert.equal(await ai.runEconomyCommand(c, 'compatible'), true);
    assert.equal(values.get('economyProvider'), 'compatible');
    assert.equal(values.get('compatibleBaseUrl'), 'https://api.deepseek.com');
    assert.equal(prompts[0].validateInput('http://evil.example.com'), 'https 주소만 쓸 수 있습니다(http 는 내 PC 의 localhost 만 허용).');
    assert.equal(prompts[1].password, true);
    assert.equal(await ai.currentAiProvider(c), 'anthropic');
    assert.equal(ai.recentRoutingWrite(), true, '방금 바꾼 설정은 재시작을 다시 묻지 않는다');
    assert.equal(await ai.runEconomyCommand(c, 'compatible'), false, '이미 그 공급자 + 키 있음 → 바뀐 것 없음');
    vscode.window.showInputBox = async () => undefined;
    values.delete('economyProvider');
    assert.equal(await ai.runEconomyCommand(c, 'openai'), false, '키 입력을 취소하면 바꾸지 않는다');
    assert.equal(values.get('economyProvider'), undefined);
  } finally {
    vscode.workspace.getConfiguration = orig.cfg; vscode.window.showInputBox = orig.input; vscode.window.showInformationMessage = orig.info;
  }
});
