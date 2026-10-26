// Browser QA with synthetic API responses. UI_REVIEW_PRODUCTION=1 serves the built template.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE ||
  (process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES ? process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES + '/playwright' : 'playwright'));
const { spawn } = require('node:child_process');
const output = process.env.SCREENSHOT_DIR || '/tmp/lajdnsma-ui-review';
const url = process.env.UI_REVIEW_URL || 'http://127.0.0.1:5199';
const fixture = {
  key_count: 3, model_count: 3, last_24h_calls: 18426, hourly_calls: 842, minute_calls: 23,
  last_24h_tokens: 2840000, hourly_tokens: 126400, minute_tokens: 4200,
  max_requests_per_minute: 60, concurrent_requests: 3, max_concurrent_requests: 8,
  max_retry_num: 3, max_empty_responses: 3, max_requests_per_day_per_ip: 1000,
  enable_thinking: true, max_thinking: true, fake_streaming: false,
  local_version: '2026.10', remote_version: '2026.10', current_time: '2026-10-26 12:30',
  available_models: ['gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-2.5-flash-search'],
  api_key_stats: Array.from({length: 3}, (_, i) => ({ api_key: `key#demo-${i+1}`, calls_24h: 1400-i*210, total_tokens: 240000-i*20000, usage_percent: 28-i*7, limit: 5000,
    model_stats: { 'gemini-2.5-flash': { calls: 1400-i*210, tokens: 240000 } } })),
  calls_time_series: Array.from({length: 18}, (_, i) => ({ time: `${String(10+Math.floor(i/6)).padStart(2,'0')}:${String(i%6*10).padStart(2,'0')}`, value: 22 + (i*17 % 65) })),
  tokens_time_series: Array.from({length: 18}, (_, i) => ({ time: `${String(10+Math.floor(i/6)).padStart(2,'0')}:${String(i%6*10).padStart(2,'0')}`, value: 1000 + (i*151 % 1800) })),
  logs: [{ timestamp: '12:30:04', level: 'INFO', message: '请求完成', model: 'gemini-2.5-flash', status_code: 200 }, { timestamp: '12:30:02', level: 'INFO', message: '模型列表已更新' }],
};
(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined, headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] });
  const errors = [];
  const production = process.env.UI_REVIEW_PRODUCTION;
  const server = process.env.UI_REVIEW_URL ? null : spawn(production ? (process.env.PYTHON || 'python') : process.execPath,
    production ? ['-m', 'http.server', '5199', '--bind', '127.0.0.1', '--directory', path.join(__dirname, '../app/templates')]
      : [path.join(__dirname, '../hajimiUI/node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', '5199', '--strictPort'],
    { cwd: path.join(__dirname, '../hajimiUI'), stdio: 'pipe' });
  try {
    for (let attempt = 0; attempt < 50; attempt++) {
      try { const ready = await fetch(url); if (ready.ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    for (const [name, width, height, dark, authenticated] of [
      ['desktop', 1440, 1080, false, true], ['dark', 1440, 1080, true, true],
      ['mobile', 390, 844, false, true], ['login', 1440, 900, false, false],
      ['vertex', 1440, 1080, false, true], ['empty', 390, 844, false, true],
    ]) {
      const context = await browser.newContext({ viewport: { width, height }, reducedMotion: 'reduce' });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      await page.addInitScript(({ dark, authenticated }) => {
        localStorage.setItem('darkMode', String(dark));
        if (authenticated) sessionStorage.setItem('gw_pw', 'local-preview');
      }, { dark, authenticated });
      const data = { ...fixture };
      if (name === 'vertex') Object.assign(data, { enable_vertex: true, vertex_express_api_key: true, google_credentials_json: true });
      if (name === 'empty') Object.assign(data, { key_count: 0, model_count: 0,
        api_key_stats: [], available_models: [], calls_time_series: [], tokens_time_series: [],
        last_24h_calls: 0, last_24h_tokens: 0, hourly_calls: 0, minute_calls: 0, hourly_tokens: 0, minute_tokens: 0, logs: [] });
      const savedKeys = [];
      await page.route('**/api/**', route => {
        if (route.request().method() === 'POST' && new URL(route.request().url()).pathname === '/api/update-config') {
          const body = route.request().postDataJSON();
          savedKeys.push(body.key);
          data[body.key] = body.value;
          return route.fulfill({ json: { status: 'success' } });
        }
        return route.fulfill({ json: data });
      });
      if (process.env.QA_FONT_DIR) {
        await page.route('**/__qa-fonts/**', route => route.fulfill({
          body: fs.readFileSync(path.join(process.env.QA_FONT_DIR, 'files', path.basename(new URL(route.request().url()).pathname))),
          contentType: 'font/woff2',
        }));
      }
      await page.goto(url);
      if (process.env.QA_FONT_DIR) {
        const css = fs.readFileSync(path.join(process.env.QA_FONT_DIR, '400.css'), 'utf8').replaceAll('./files/', '/__qa-fonts/');
        await page.addStyleTag({ content: css });
      }
      await page.getByRole('heading', { name: authenticated ? '服务总览' : '欢迎回到控制台', exact: true }).waitFor();
      if (authenticated && !['vertex', 'empty'].includes(name)) await page.locator('canvas').first().waitFor();
      await page.evaluate(() => document.fonts.ready);
      if (authenticated) {
        await page.getByRole('button', { name: '刷新数据', exact: true }).click();
        await page.setViewportSize({ width, height: height + 1 });
        await page.setViewportSize({ width, height });
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${name}: horizontal overflow`);
      await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: true });
      if (authenticated && name !== 'vertex') {
        const opener = page.getByRole('button', { name: '查看可用模型' });
        await opener.click();
        const dialog = page.getByRole('dialog', { name: '可用模型' });
        await dialog.waitFor();
        await page.keyboard.press('Tab');
        assert.equal(await dialog.evaluate(el => el.contains(document.activeElement)), true);
        await page.keyboard.press('Escape');
        await dialog.waitFor({ state: 'hidden' });
        assert.equal(await opener.evaluate(el => el === document.activeElement), true);
      }
      if (name === 'desktop') {
        // A failed refresh must preserve the previous data and offer recovery.
        await page.route('**/api/**', route => route.fulfill({ status: 503, json: { detail: 'test outage' } }));
        await page.getByRole('button', { name: '刷新数据', exact: true }).click();
        await page.getByRole('alert').filter({ hasText: '上次成功同步' }).waitFor();
        await page.getByText('数据待更新', { exact: true }).waitFor();
        await page.getByText('更新失败 · 上次同步', { exact: false }).waitFor();
        await page.screenshot({ path: path.join(output, 'offline.png'), fullPage: true });
        await page.route('**/api/**', route => route.fulfill({ json: fixture }));
        await page.getByRole('button', { name: '重试', exact: true }).click();
        await page.getByRole('alert').waitFor({ state: 'hidden' });
      }
      if (name === 'vertex') {
        await page.getByText('当前面板暂不汇总 Vertex 用量', { exact: false }).waitFor();
        assert.deepEqual(await page.locator('.protocol-backend').allTextContents(), ['Vertex AI', 'Vertex AI', 'Vertex AI', 'AI Studio']);
        assert.equal(await page.getByLabel('Vertex Express API 密钥', { exact: true }).inputValue(), '');
        assert.equal(await page.getByLabel('Google 凭证 JSON', { exact: true }).inputValue(), '');
        await page.getByRole('switch', { name: '伪流式', exact: true }).click();
        await page.getByLabel('管理密码', { exact: true }).fill('local-preview');
        await page.getByRole('button', { name: '保存', exact: true }).click();
        await page.getByRole('status').filter({ hasText: '本次提交已保存' }).waitFor();
        assert.deepEqual(savedKeys, ['fake_streaming'], 'blank stored credential fields must not be submitted');
        assert.equal(await page.getByLabel('管理密码', { exact: true }).inputValue(), '');
      }
      if (name === 'empty') {
        assert.equal(await page.locator('canvas').count(), 0, 'empty state should not load the chart');
        const message = await page.getByText('点击「+ 添加」导入密钥。', { exact: true }).boundingBox();
        assert.ok(message.x >= 0 && message.x + message.width <= width, 'empty-state instructions must fit the viewport');
      }
      await context.close();
    }
    assert.deepEqual(errors, []);
    console.log('Passed: desktop, dark, mobile, login, Vertex, empty, offline/recovery; no overflow or browser errors; dialog focus and Escape.');
  } finally { server?.kill(); await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
