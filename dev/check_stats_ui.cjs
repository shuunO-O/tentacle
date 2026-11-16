/* Synthetic usage browser check; no real app, credentials or conversations. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'tmp', 'stats-ui');
fs.mkdirSync(output, { recursive: true });
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const origin = 'http://127.0.0.1:18425';
const server = spawn(python, ['dev/preview.py', '--port', '18425'], { cwd: root, windowsHide: true, stdio: 'pipe' });
let browser, page, log = '';
server.stderr.on('data', chunk => { log += chunk; });
(async () => {
  try {
    for (let i = 0; ; i++) {
      try { if ((await fetch(origin + '/__preview__/state')).ok) break; } catch (_) {}
      if (i >= 80 || server.exitCode !== null) throw Error(log || 'Preview not ready');
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [], external = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', req => { if (!req.url().startsWith(origin) && !req.url().startsWith('data:')) external.push(req.url()); });
    await page.goto(origin);
    await page.locator('[data-view="stats"]').click();
    await page.locator('#statsProvider option[value="provider-a"]').waitFor({ state: 'attached' });
    assert.equal(await page.locator('#statsCards .stats-card').count(), 4);
    assert.ok((await page.locator('#statsCards').innerText()).includes('累计用量'));
    assert.ok((await page.locator('#statsBalances').innerText()).includes('128.50'));
    assert.ok(await page.evaluate(() => document.querySelector('#statsBalances').getBoundingClientRect().top < document.querySelector('.stats-filters').getBoundingClientRect().top));
    const select = async (id, value, expected) => {
      const response = page.waitForResponse(r => r.url().startsWith(origin + '/api/usage?') && r.url().includes(expected));
      await page.locator('#' + id).selectOption(value);
      const data = await (await response).json();
      await page.waitForFunction(() => !document.querySelector('#statsSummaryHint').textContent.includes('加载'));
      return data;
    };
    assert.ok((await page.locator('#statsModel').innerText()).includes('shared（历史记录）'));
    let data = await select('statsProvider', 'provider-a', 'provider_id=provider-a');
    assert.equal(data.summary.today.requests, 2);
    await page.waitForFunction(() => document.querySelector('#statsModel').options.length === 2);
    data = await select('statsRole', 'chat', 'role=chat');
    assert.equal(data.summary.today.prompt_tokens, 1000);
    assert.equal(data.summary.all.prompt_tokens, 16000);
    assert.ok((await page.locator("#statsBalances").innerText()).includes("128.50"));
    await page.waitForTimeout(1100);
    await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
    data = await select('statsModel', 'a-shared', 'model_id=a-shared');
    assert.equal(data.summary.today.requests, 1);
    data = await select('statsProvider', 'provider-b', 'provider_id=provider-b');
    assert.equal(data.filters.model_id, null, 'incompatible model filter resets');
    assert.equal(data.summary.today.prompt_tokens, 2000);
    assert.ok(!(await page.locator("#statsBalances").innerText()).includes("128.50"));
    assert.ok((await page.locator("#statsBalances").innerText()).includes("暂不支持"));
    data = await select('statsRole', 'compress', 'role=compress');
    assert.equal(data.summary.today.requests, 0);
    await page.waitForFunction(() => document.querySelector('#statsSummaryHint').textContent.includes('暂无'));
    await page.locator('#statsReset').click();
    await page.waitForFunction(() => document.querySelector('#statsProvider').value === '' && document.querySelector('#statsSummaryHint').textContent.includes('数据点'));
    data = await select('statsProvider', ':unknown', 'provider_id=%3Aunknown');
    assert.equal(data.summary.today.prompt_tokens, 100);
    assert.equal(data.options.models.filter(m => m.id.startsWith('legacy:')).length, 1);
    await page.locator('#statsReset').click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => document.querySelector('#statsProvider').value === '');
    await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.ok(await page.locator('#view-stats').evaluate(e => e.scrollWidth <= e.clientWidth));
    const response = page.waitForResponse(r => r.url().includes('/api/usage?granularity=hour'));
    await page.locator('[data-granularity="hour"]').click();
    assert.equal((await (await response).json()).granularity, 'hour');
    const refresh = page.waitForResponse(r => r.url().includes("/api/balance?") && r.url().includes("force=true"));
    await page.locator("#statsRefresh").click();
    assert.equal((await refresh).status(), 200);
    assert.deepEqual(errors, []);
    assert.deepEqual(external, []);
    console.log(JSON.stringify({ passed: true, screenshots: output }));
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
    throw error;
  } finally {
    if (browser) await browser.close();
    server.kill('SIGINT');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
