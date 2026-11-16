'use strict';

// Run only a synthetic localhost preview and a fresh headless browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const root = path.resolve(__dirname, '..');
  const output = path.join(root, 'tmp', 'concept-audit-browser');
  fs.mkdirSync(output, { recursive: true });
  const port = await new Promise(resolve => {
    const socket = net.createServer();
    socket.listen(0, '127.0.0.1', () => {
      const value = socket.address().port;
      socket.close(() => resolve(value));
    });
  });
  const python = process.env.TENTACLE_TEST_PYTHON || path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const server = spawn(python, ['-c', 'import sys,uvicorn; from dev.preview import create_preview; uvicorn.run(create_preview(),host="127.0.0.1",port=int(sys.argv[1]),log_level="error")', String(port)],
    { cwd: root, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
  let stderr = '', browser;
  server.stderr.on('data', chunk => { stderr += chunk; });
  try {
    const base = `http://127.0.0.1:${port}`;
    let ready = false;
    for (let attempt = 0; attempt < 50; attempt++) {
      try { if ((await fetch(base)).ok) { ready = true; break; } } catch (_) {}
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    assert(ready, `Synthetic preview did not start: ${stderr}`);
    browser = await chromium.launch({ headless: true, ...(process.env.TENTACLE_TEST_CHROMIUM ? { executablePath: process.env.TENTACLE_TEST_CHROMIUM } : {}) });
    const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base);
    await page.locator('[data-view="dedup"]').click();
    await page.waitForSelector('#dedupAuditEntries [data-audit]');
    assert.equal(await page.locator('#dedupAuditEntries [data-audit]').count(), 30);
    await page.locator('#dedupAuditNext').click();
    await page.waitForFunction(() => document.querySelector('#dedupAuditPage').textContent.includes('第 2 页'));
    assert.equal(await page.locator('#dedupAuditEntries [data-audit]').count(), 3);
    await page.locator('#dedupAuditActor').selectOption('manual');
    await page.waitForFunction(() => document.querySelector('#dedupAuditPage').textContent.includes('第 1 页') &&
      [...document.querySelectorAll('#dedupAuditEntries h4')].every(el => el.textContent.startsWith('人工参与')));
    assert.equal(await page.locator('#dedupAuditEntries [data-audit]').count(), 11);
    await page.locator('#dedupAuditActor').selectOption('automatic');
    await page.waitForFunction(() => [...document.querySelectorAll('#dedupAuditEntries h4')].every(el => el.textContent.startsWith('自动')));
    assert((await page.locator('#dedupAuditEntries').innerText()).includes('新记忆'));
    await page.locator('#dedupAuditEntries details').first().locator('summary').click();
    await page.locator('#dedupRefresh').click();
    await page.waitForFunction(() => document.querySelector('#dedupAuditEntries details').open);
    await page.locator('#dedupAuditTitle').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'desktop.png') });
    page.once('dialog', dialog => dialog.accept());
    await page.locator('[data-pair="synthetic-pair"] [data-action="reevaluate"]').click();
    await page.waitForFunction(() => document.querySelector('#dedupPairs').textContent.includes('适合分组 98%'));
    assert((await page.locator('#dedupPairs').innerText()).includes('待审核'));
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().right <= 1);
    await page.locator('#dedupAuditEntries details').first().locator('summary').click();
    await page.locator('#dedupAuditTitle').scrollIntoViewIfNeeded();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth &&
      document.querySelector('#view-dedup').scrollWidth <= document.querySelector('#view-dedup').clientWidth + 1));
    assert(await page.locator('#dedupAuditTitle').evaluate(el => el.getBoundingClientRect().left >= 0));
    await page.screenshot({ path: path.join(output, 'mobile.png') });
    page.once('dialog', dialog => dialog.accept());
    const grouped = page.waitForResponse(response => response.url().includes('/api/memory/dedup/synthetic-pair/decide'));
    await page.locator('[data-pair="synthetic-pair"] [data-action="merge"]').click();
    assert.equal((await grouped).status(), 200);
    await page.waitForFunction(() => document.querySelector('#dedupCounts').textContent.includes('已分组 1'));
    await page.locator('#dedupFilter').selectOption('merged');
    await page.waitForSelector('[data-pair="synthetic-pair"]');
    assert((await page.locator('#dedupPairs').innerText()).includes('分组参考概念'));
    assert.equal(await page.locator('[data-policy]').count(), 0);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ passed: true, checks: ['actor filters', 'pagination', 'evidence expansion', 'refresh retains expansion', 'explicit reevaluation', 'mobile overflow', 'equivalence group action', 'no property overwrite control', 'no page errors'] }, null, 2));
    process.stdout.write(`Concept audit browser checks passed; screenshots: ${output}\n`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
}

main().catch(error => { process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1; });
