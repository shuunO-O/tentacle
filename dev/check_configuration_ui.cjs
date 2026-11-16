/* Synthetic configuration + workbench browser acceptance check; no real app or secrets. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { navigate, checkResponsiveViews, checkChatInteractions } = require('./web_ui_checks.cjs');

async function freePort() {
  return new Promise(resolve => {
    const s = net.createServer();
    s.listen(0, '127.0.0.1', () => {
      const port = s.address().port;
      s.close(() => resolve(port));
    });
  });
}

(async () => {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'tmp', 'configuration-ui');
  fs.mkdirSync(output, { recursive: true });
  const port = await freePort();
  const origin = `http://127.0.0.1:${port}`;
  const python = process.env.TENTACLE_TEST_PYTHON
    || path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const server = spawn(python, ['dev/preview.py', '--port', String(port)], { cwd: root, windowsHide: true, stdio: 'pipe' });
  let browser, page, log = '';
  server.stderr.on('data', chunk => { log += chunk; });

  try {
    for (let i = 0; ; i++) {
      try { if ((await fetch(origin + '/__preview__/state')).ok) break; } catch (_) {}
      if (i >= 80 || server.exitCode !== null) throw Error(log || 'Preview not ready');
      await new Promise(resolve => setTimeout(resolve, 100));
    }

    browser = await chromium.launch({
      headless: true,
      ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}),
    });
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    const unexpected = [];
    page.on('pageerror', err => errors.push(err.message));
    page.on('request', req => {
      if (!req.url().startsWith(origin) && !req.url().startsWith('data:') && !req.url().startsWith('blob:')) {
        unexpected.push(req.url());
      }
    });

    await page.goto(origin);
    await page.locator('[data-testid="ws-status"][data-ws-status="connected"]').waitFor();
    await navigate(page, 'config');

    const roleSearch = page.getByPlaceholder('筛选供应商 / 模型 / 角色…');
    await roleSearch.waitFor();
    await roleSearch.fill('主对话');
    await page.locator('[data-config-role="chat"]').click();
    await roleSearch.fill('');

    // Edit & discard draft.
    const modelSelect = page.getByLabel('使用模型');
    const prevModel = await modelSelect.inputValue();
    await modelSelect.selectOption({ index: 1 });
    await page.locator('[data-testid="config-dirty-badge"]').waitFor();
    await page.getByRole('button', { name: '放弃修改' }).click();
    assert.equal(await modelSelect.inputValue(), prevModel);

    // Responsive views + theme switch + mobile drawer + chat check.
    await checkResponsiveViews(page, output);
    await checkChatInteractions(page, origin, output);

    assert.deepEqual(errors, []);
    assert.deepEqual(unexpected, []);
    console.log(JSON.stringify({ passed: true, screenshots: output }));
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
    throw error;
  } finally {
    if (browser) await browser.close();
    server.kill('SIGINT');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
