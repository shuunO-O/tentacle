/* Browser checks shared by the synthetic configuration preview runner. */
const assert = require('node:assert/strict');
const path = require('node:path');

async function navigate(page, view) {
  const menuBtn = page.locator('[data-testid="mobile-menu-btn"]');
  if (await menuBtn.isVisible() && await menuBtn.getAttribute('aria-expanded') !== 'true') {
    await menuBtn.click();
  }
  await page.locator(`[data-panel-nav="${view}"]`).click();
  await page.locator(`[data-testid="workbench-root"][data-primary-panel="${view}"]`).waitFor();
  assert.equal(await page.locator('[data-testid="mobile-nav-drawer"]').count(), 0, 'choosing a view closes mobile drawer');
}

async function checkResponsiveViews(page, output) {
  for (const width of [1440, 1024, 768, 390]) {
    await page.setViewportSize({ width, height: 844 });
    for (const view of ['chat', 'admin', 'graph', 'dedup', 'terminal', 'usage', 'status', 'settings', 'config', 'subagents']) {
      await navigate(page, view);
      if (view === 'graph') {
        await page.locator('[data-testid="memory-graph-canvas"]').waitFor();
        assert.equal(await page.locator('#memoryGraphFrame').count(), 0, 'memory-graph renders natively without iframe');
      }
      if (view === 'admin') await page.locator('table').first().waitFor();
      if (view === 'usage') await page.locator('canvas').first().waitFor();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      assert.ok(overflow <= 1, `${view} at ${width}px must not overflow horizontally (got ${overflow})`);
      if (width === 390 || (width === 1440 && (view === 'config' || view === 'graph'))) {
        await page.screenshot({ path: path.join(output, `${view}-${width}.png`) });
      }
    }
  }

  // Mobile drawer + dual theme toggle checks at narrow viewport.
  await page.setViewportSize({ width: 390, height: 844 });
  await navigate(page, 'chat');
  const menuBtn = page.locator('[data-testid="mobile-menu-btn"]');
  await menuBtn.click();
  assert.equal(await menuBtn.getAttribute('aria-expanded'), 'true');
  await page.locator('[data-testid="mobile-nav-drawer"]').waitFor();
  await page.getByRole('button', { name: '关闭导航菜单' }).click();
  assert.equal(await menuBtn.getAttribute('aria-expanded'), 'false');

  const initialTheme = await page.locator('html').getAttribute('data-theme');
  await page.getByRole('button', { name: '切换深浅主题' }).click();
  const flippedTheme = await page.locator('html').getAttribute('data-theme');
  assert.notEqual(flippedTheme, initialTheme, 'theme toggle switches dark/light attribute');
  await page.getByRole('button', { name: '切换深浅主题' }).click();
}

async function checkChatInteractions(page, origin, output) {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await navigate(page, 'chat');
  const input = page.locator('[data-testid="chat-input"]');
  await input.fill('浏览器回归消息检查');
  await page.locator('[data-testid="chat-action-btn"]').click();
  await page.locator('[data-msg-role="agent"]').first().waitFor();
  const scrollOk = await page.locator('[data-testid="chat-timeline"]').evaluate(e => e.scrollWidth <= e.clientWidth + 1);
  assert.ok(scrollOk, 'chat timeline has no horizontal overflow');
  await page.screenshot({ path: path.join(output, 'chat-interactions.png') });
}

module.exports = { navigate, checkResponsiveViews, checkChatInteractions };
