/* Synthetic browser check: owns its preview process and ephemeral browser. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'tmp', 'media-ui');
fs.mkdirSync(output, { recursive: true });
const origin = 'http://127.0.0.1:18429';
const server = spawn(path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
  ['dev/preview.py', '--port', '18429'], { cwd: root, windowsHide: true, stdio: 'pipe' });
let browser, page;
(async () => {
  try {
    for (let tries = 0; tries < 50; tries++) {
      try { if ((await fetch(origin)).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [], sends = [];
    page.on('pageerror', error => errors.push(error.message));
    const attachments = [
      { local_id: 'synthetic-audio', filename: '讲解 <script>unsafe</script>.mp3', mime: 'audio/mp3' },
      { local_id: 'synthetic-video', filename: '短片.mp4', mime: 'video/mp4' },
    ];
    let upload = 0;
    await page.route(origin + '/api/files/upload', route => route.fulfill({
      json: { ok: true, file: attachments[upload++] },
    }));
    await page.route(origin + '/api/chat/send', async route => {
      sends.push(route.request().postDataJSON());
      await route.fulfill({ json: { ok: true, total_tokens: 0 } });
    });
    await page.goto(origin);
    await page.locator('#chatInput').waitFor();
    await page.locator('#chatFileInput').setInputFiles([
      { name: '讲解.mp3', mimeType: 'audio/mpeg', buffer: Buffer.from('synthetic audio') },
      { name: '短片.mp4', mimeType: 'video/mp4', buffer: Buffer.from('synthetic video') },
    ]);
    await page.waitForFunction(() => document.querySelectorAll('.att-media').length === 2);
    assert.equal(await page.locator('#chatAttachments script').count(), 0);
    await page.locator('#chatInput').fill('请分析这两个文件');
    await page.locator('#chatSend').click();
    await page.waitForFunction(() => document.querySelectorAll('.msg-media').length === 2);
    assert.deepEqual(sends[0].files, ['synthetic-audio', 'synthetic-video']);
    assert.equal(await page.locator('.msg-media audio[controls]').count(), 1);
    assert.equal(await page.locator('.msg-media video[controls]').count(), 1);
    assert.equal(await page.locator('.msg-media script').count(), 0);
    assert.equal(await page.locator('#chatAttachments').isVisible(), false);
    await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().right <= 0);
    await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true, animations: 'disabled' });
    for (const media of await page.locator('.msg-media').all()) {
      assert.ok(await media.evaluate(element => element.scrollWidth <= element.clientWidth));
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, screenshots: output }));
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
    throw error;
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
