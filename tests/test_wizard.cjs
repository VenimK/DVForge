const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright-core');

const root = path.join(__dirname, '../web');
const host = { os: 'macOS', os_raw: 'Darwin', arch: 'aarch64', cores_logical: 8, ram_gb: 8, hostname: 'Test Mac' };
const target = { id: 'macos-arm64-dmg', platform: 'macos', arch: 'aarch64', ext: 'dmg', label: 'macOS ARM64', buildable: true, ready: true, route: 'local', missing_tools: [] };
const requirements = [{ id: 'flutter', label: 'Flutter', present: true, status: 'installed', version: '3.24.5' }];
const failures = [];
async function assertWidth(page, viewport, stage) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
  assert.equal(overflow, false, `horizontal overflow at ${viewport.width}, ${stage}`);
}
let starts = 0;
const server = http.createServer((req, res) => {
  if (req.url.startsWith('/api/')) {
    const responses = {
      '/api/matrix': { host, targets: [target] },
      '/api/environment': { ok: true, host, requirements, targets: [target.id], problems: [] },
      '/api/toolchains': { tools: [] }, '/api/prereqs': requirements,
      '/api/config': req.method === 'POST' ? { ok: true } : { appname: 'Test Client', androidappid: '', serverIP: 'build.example.org' },
      '/api/config/status': { source: 'config' },
      '/api/build/preflight': { ok: true, host, problems: [], outputs: ['/workspace/output/macos/1.4.9/aarch64'] },
      '/api/build/start': { ok: true }, '/api/build/status': { running: true },
      '/api/advanced-keys': [],
      '/api/preview': { env: {}, custom_txt: '', custom_b64: '' },
    };
    if (req.url === '/api/build/start') starts += 1;
    if (req.url === '/api/build/stream') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.end('data: ' + JSON.stringify({ line: '=== Build macOS ===' }) + '\n\n');
      return;
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(responses[req.url] || {}));
    return;
  }
  const file = path.join(root, req.url === '/' ? 'index.html' : req.url.split('?')[0]);
  if (!file.startsWith(root) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Type': file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' });
  res.end(fs.readFileSync(file));
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ headless: true, ...(process.env.DVFORGE_TEST_BROWSER ? { executablePath: process.env.DVFORGE_TEST_BROWSER } : {}) });
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      page.on('pageerror', error => failures.push(error.message));
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      await page.waitForFunction(() => !document.querySelector('#environment-next').disabled);
      assert.equal(await page.locator('.panel.is-active').getAttribute('id'), 'tab-environment');
      await assertWidth(page, viewport, 'environment');
      await page.locator('#environment-next').click();
      assert.equal(await page.locator('.panel.is-active').getAttribute('id'), 'tab-config');
      await assertWidth(page, viewport, 'config');
      await page.locator('#config-next').click();
      await page.waitForSelector('#tab-targets.is-active');
      await assertWidth(page, viewport, 'targets');
      await page.locator('.cell.ready').click();
      await page.locator('#btn-build').click();
      await page.waitForFunction(() => !document.querySelector('#review-start').disabled);
      assert.match(await page.locator('#review-summary').innerText(), /output\/macos\/1\.4\.9\/aarch64/);
      await assertWidth(page, viewport, 'review');
      const before = starts;
      await page.locator('#review-start').click();
      await page.waitForFunction(() => document.querySelector('#build-status').textContent === 'running');
      assert.equal(starts, before + 1);
      assert.equal(await page.locator('.panel.is-active').getAttribute('id'), 'tab-console');
      await page.waitForFunction(() => document.querySelector('#current-stage').textContent === 'Build macOS');
      assert.equal(await page.locator('#log-view').isVisible(), false);
      assert.equal(await page.locator('#btn-cancel').evaluate(el => !!el.closest('.wizard-footer')), true);
      await page.evaluate(() => {
        conLine('Progress: 2/8 phases');
        conLine('warning: test warning');
        conLine('error[E0308]: test failure');
        conLine('[25/100] Compiling test');
      });
      assert.equal(await page.locator('#session-percent').innerText(), '25%');
      assert.equal(await page.locator('#step-percent').innerText(), '25%');
      assert.equal(await page.locator('#log-counts').innerText(), '1 errors · 1 warnings');
      await page.locator('#show-log').check();
      await page.locator('#log-filter').selectOption('error');
      await page.waitForFunction(() => document.querySelector('#console').textContent.trim() === 'error[E0308]: test failure');
      await page.locator('#log-filter').selectOption('normal');
      await page.waitForFunction(() => document.querySelector('#console').textContent.includes('Compiling test') && !document.querySelector('#console').textContent.includes('test failure'));
      assert.match(await page.evaluate(() => fullLogText()), /test failure/);
      await page.locator('#show-log').uncheck();
      assert.equal(await page.locator('#log-view').isVisible(), false);
      const footer = await page.locator('.wizard-footer').boundingBox();
      assert.ok(Math.abs(footer.y + footer.height - viewport.height) < 2);
      await assertWidth(page, viewport, 'build');
      if (process.env.DVFORGE_TEST_SCREENSHOTS) await page.screenshot({ path: path.join(process.env.DVFORGE_TEST_SCREENSHOTS, `wizard-${viewport.width}.png`), fullPage: true, animations: 'disabled' });
      await page.close();
    }
    assert.deepEqual(failures, []);
    const missing = await browser.newPage();
    await missing.route('**/api/environment', route => route.fulfill({ json: { ok: false, targets: [target.id],
      requirements: [{ id: 'flutter', label: 'Flutter', present: false, status: 'missing' }], problems: ['Missing Flutter'] } }));
    await missing.goto(`http://127.0.0.1:${server.address().port}`);
    await missing.waitForFunction(() => document.querySelector('#environment-status').textContent === 'Missing Flutter');
    assert.equal(await missing.locator('#environment-next').isDisabled(), true);
    assert.equal(await missing.locator('.tab[data-tab="config"]').isDisabled(), true);
    await missing.close();
    const delayed = await browser.newPage();
    await delayed.route('**/api/prereqs', route => route.fulfill({ json: [{ id: 'appimage_builder', label: 'AppImage packaging', present: false, hint: 'Use Auto install' }] }));
    await delayed.route('**/api/toolchains', async route => {
      await new Promise(resolve => setTimeout(resolve, 500));
      await route.fulfill({ json: { tools: [{ id: 'appimage_builder', satisfies: 'appimage_builder', installable: true, present: false }], local_total: '' } });
    });
    await delayed.goto(`http://127.0.0.1:${server.address().port}`);
    await delayed.waitForSelector('[data-install="appimage_builder"]');
    assert.equal(await delayed.locator('[data-install="appimage_builder"]').isVisible(), true);
    assert.equal(await delayed.locator('#install-missing').isVisible(), true);
    await delayed.close();
    process.stdout.write('PASS: five-step wizard, desktop/mobile, no real builds, no page errors\n');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
