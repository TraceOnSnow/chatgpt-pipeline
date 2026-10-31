// Run with PLAYWRIGHT_PACKAGE=/absolute/path/to/@playwright/test node tests/inbox.browser.mjs
// The fixture owns a temporary source/workbench/Vault. No production data or model calls.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readFile, writeFile, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { mkdtemp } from 'node:fs/promises';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const { chromium, expect } = require(process.env.PLAYWRIGHT_PACKAGE || '@playwright/test');
const output = await mkdtemp(resolve(tmpdir(), 'markdown-inbox-browser-'));
const children = new Set();

async function fixture(noVault = false, extended = false, review = false) {
  const child = spawn(process.env.PYTHON || 'python3', [resolve(root, 'tests/serve_inbox_fixture.py'),
    ...(noVault ? ['--no-vault'] : []), ...(extended ? ['--extended'] : []), ...(review ? ['--review'] : [])], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: [resolve(root, 'src'), resolve(root, '../obsidian-workflow/src'),
      process.env.PYTHONPATH].filter(Boolean).join(':') },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  children.add(child);
  let stdout = '', stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk; });
  child.once('exit', () => children.delete(child));
  const data = await new Promise((resolveReady, reject) => {
    const timer = setTimeout(() => reject(Error(`Fixture startup timeout: ${stderr}`)), 15_000);
    child.once('error', error => { clearTimeout(timer); reject(error); });
    child.once('exit', code => { clearTimeout(timer); reject(Error(`Fixture exited ${code}: ${stderr}`)); });
    child.stdout.on('data', chunk => {
      stdout += chunk;
      if (stdout.includes('\n')) {
        clearTimeout(timer);
        try { resolveReady(JSON.parse(stdout.split('\n')[0])); } catch (error) { reject(error); }
      }
    });
  });
  return { ...data, child };
}

async function stop(child) {
  if (!children.has(child)) return;
  await new Promise(resolveExit => {
    const timer = setTimeout(() => child.kill('SIGKILL'), 5_000);
    child.once('exit', () => { clearTimeout(timer); resolveExit(); });
    child.kill('SIGTERM');
  });
}

const errors = [], requests = [];
let browser;
try {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1365, height: 1000 } });
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => requests.push(request.url()));
  const configured = await fixture();
  await page.goto(configured.url + '/?view=done&page=1');
  await expect(page.locator('.card')).toHaveCount(1);
  await expect(page.locator('#stats')).toContainText('0 篇已导入');
  await page.getByRole('button', { name: '查看初稿', exact: true }).click();
  await expect(page.locator('#preview')).toBeVisible();
  await expect(page.locator('#note-body')).toContainText('我做了两次实验');
  await page.locator('#import-preview').click();
  await expect(page.locator('#receipt-location')).toContainText('入库时位置');
  await expect(page.locator('#import-preview')).toBeHidden();
  await page.locator('#close-preview').click();
  await expect(page.locator('#stats')).toContainText('1 篇已导入');
  await expect(page.locator('.badge')).toContainText('已导入');
  const files = await readdir(resolve(configured.vault, 'notes'));
  assert.equal(files.filter(file => file.endsWith('.md')).length, 1);
  const notePath = resolve(configured.vault, 'notes', files[0]);
  const revised = await readFile(notePath, 'utf8') + '\n用户后续修改，必须保留。\n';
  await writeFile(notePath, revised);
  const repeated = await page.evaluate(async item => {
    const session = await (await fetch('/api/session')).json();
    const response = await fetch(`/api/items/${item}/import`, { method: 'POST',
      headers: { 'X-Workbench-Token': session.token }, body: '{}' });
    return response.json();
  }, configured.item_id);
  assert.equal(repeated.outcome, 'existing');
  assert.equal(await readFile(notePath, 'utf8'), revised);

  await page.locator('#search').fill('My experiment');
  await expect(page).toHaveURL(/q=My\+experiment/);
  await page.reload();
  await expect(page.locator('#search')).toHaveValue('My experiment');
  await expect(page.locator('.card')).toHaveCount(1);
  await page.locator('.card summary').click();
  await expect(page.locator('.excerpts')).toContainText('I tried two experiments');
  await page.waitForTimeout(6500); // Cross the actual polling interval while reading.
  await expect(page.locator('.card details')).toHaveAttribute('open', '');
  await expect(page.locator('.excerpts')).toContainText('I tried two experiments');
  await page.screenshot({ path: resolve(output, 'desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: resolve(output, 'mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 320, height: 740 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.getByRole('button', { name: '查看初稿', exact: true }).click();
  await expect(page.locator('#preview')).toBeVisible();
  assert.equal(await page.locator('#preview').evaluate(el => el.scrollWidth <= el.clientWidth), true);
  await page.keyboard.press('Escape');
  await expect(page.locator('#preview')).toBeHidden();
  await stop(configured.child);

  const extended = await fixture(false, true);
  await page.setViewportSize({ width: 1365, height: 1000 });
  await page.goto(extended.url + '/?view=todo&page=1');
  await expect(page.locator('.card')).toHaveCount(30);
  await expect(page.locator('#page-top')).toHaveText('第 1 / 2 页');
  assert.equal(await page.locator('.card img').count(), 0);
  // Read failure has a direct retry; successful excerpts keep the same DOM node.
  const firstId = await page.locator('.card').first().getAttribute('data-id');
  await page.route(`**/api/items/${firstId}`, route => route.fulfill({ status: 503,
    json: { error: '临时读取失败' } }), { times: 1 });
  await page.locator('.card summary').first().click();
  await expect(page.getByRole('button', { name: '重新读取原文' })).toBeVisible();
  await page.getByRole('button', { name: '重新读取原文' }).click();
  await expect(page.locator('.excerpts').first()).toContainText('I tried two experiments');
  await page.locator('.card summary').first().click();
  await page.locator('#next').click();
  await expect(page.locator('.card')).toHaveCount(5);
  await expect(page.locator('#page-top')).toHaveText('第 2 / 2 页');
  await expect(page.locator('#results-title')).toBeFocused();
  assert.ok(await page.locator('#results-title').evaluate(el => el.getBoundingClientRect().top >= 0));
  await page.goBack();
  await expect(page.locator('.card')).toHaveCount(30);
  await expect(page.locator('#page-top')).toHaveText('第 1 / 2 页');
  const historyLength = await page.evaluate(() => history.length);
  await page.locator('#search').fill('阅读材料 35');
  await expect(page.locator('.card')).toHaveCount(1);
  await page.locator('#search').fill('不存在的材料');
  await expect(page.locator('.empty')).toContainText('没有找到');
  assert.equal(await page.evaluate(() => history.length), historyLength);
  await page.locator('#clear-search').click();
  await expect(page.locator('.card')).toHaveCount(30);
  await page.locator('.card').first().getByRole('button', { name: '略过', exact: true }).click();
  await expect(page.locator('#tab-skipped')).toContainText('1');
  await page.locator('#notice-action').click();
  await expect(page.locator('#tab-skipped')).toContainText('0');
  await expect(page.locator('#tab-todo')).toContainText('35');
  await page.route('**/api/items?*', route => route.fulfill({ status: 503,
    json: { error: '临时清单失败' } }), { times: 1 });
  await page.locator('#next-top').click();
  await expect(page.locator('#notice')).toContainText('临时清单失败');
  await page.locator('#notice-action').click();
  await expect(page.locator('.card')).toHaveCount(5);

  await page.locator('#tab-done').click();
  await expect(page.locator('.card')).toHaveCount(2);
  // The dialog opens before data arrives; closing it must abort a late response.
  await page.route(`**/api/items/${extended.item_id}/note`, async route => {
    await new Promise(resolveDelay => setTimeout(resolveDelay, 700));
    await route.continue().catch(() => {});
  }, { times: 1 });
  await page.locator('.card').first().getByRole('button', { name: '查看初稿' }).click();
  await expect(page.locator('#preview')).toBeVisible();
  await expect(page.locator('#note-body')).toContainText('正在打开初稿');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(850);
  await expect(page.locator('#preview')).toBeHidden();
  await page.route(`**/api/items/${extended.item_id}/note`, route => route.fulfill({ status: 503,
    json: { error: '临时笔记失败' } }), { times: 1 });
  await page.locator('.card').first().getByRole('button', { name: '查看初稿' }).click();
  await page.getByRole('button', { name: '重新打开初稿' }).click();
  await expect(page.locator('#note-body')).toContainText('我做了两次实验');
  await expect(page.locator('#preview-title')).toHaveText('我的实验');
  await page.keyboard.press('Alt+ArrowRight');
  await expect(page.locator('#preview-title')).toHaveText('第二篇：长笔记');
  await expect(page.locator('#preview-position')).toHaveText('本页 2 / 2 篇');
  await expect(page.locator('#note-body strong')).toHaveText('重点');
  await expect(page.locator('#note-body ol li')).toHaveCount(2);
  await expect(page.locator('#note-body blockquote')).toHaveText('引用片段');
  await expect(page.locator('#note-body pre code')).toContainText('```html');
  await expect(page.locator('#note-body pre code')).toContainText('```');
  await expect(page.locator('#note-body a')).toHaveCount(1);
  await expect(page.locator('#note-body a')).toHaveAttribute('href', 'https://example.org/read');
  assert.equal(await page.locator('#note-body img, #note-body script').count(), 0);
  assert.equal(await page.evaluate(() => globalThis.pwned), undefined);
  for (const width of [1365, 390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    const toolbar = await page.locator('#import-preview').boundingBox();
    assert.ok(toolbar.y >= 0 && toolbar.y + toolbar.height <= 844);
    assert.equal(await page.locator('#preview').evaluate(el => el.scrollWidth <= el.clientWidth), true);
    await page.locator('#note-scroll').evaluate(el => { el.scrollTop = el.scrollHeight; });
    assert.equal(await page.locator('#import-preview').isVisible(), true);
  }
  await page.screenshot({ path: resolve(output, 'reading-mobile.png') });
  await page.route(`**/api/items/${extended.second_id}/import`, route => route.fulfill({ status: 503,
    json: { error: '临时入库失败' } }), { times: 1 });
  await page.locator('#import-preview').click();
  await expect(page.locator('#preview-status')).toContainText('临时入库失败');
  await expect(page.locator('#import-preview')).toBeEnabled();
  await expect(page.locator('#note-body')).toContainText('第 59 段');
  await page.locator('#import-preview').click();
  await expect(page.locator('#import-preview')).toBeHidden();
  await expect(page.locator('#preview-status')).toContainText('已导入');
  await expect(page.locator('#close-preview')).toBeFocused();
  await page.keyboard.press('Alt+ArrowLeft');
  await expect(page.locator('#preview-title')).toHaveText('我的实验');
  assert.equal(await page.locator('#note-scroll').evaluate(el => el.scrollTop), 0);
  await page.keyboard.press('Escape');
  const counts = await page.evaluate(async () => (await (await fetch('/api/items?view=done')).json()));
  assert.equal(counts.usage.calls, 2); // The fixture's two local mock stages only.
  assert.equal(counts.imported_count, 1);
  assert.equal(requests.some(url => new URL(url).pathname.endsWith('/process')), false);
  await stop(extended.child);

  const review = await fixture(false, false, true);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(review.url + '/?view=review&page=1');
  await expect(page.locator('#tab-review')).toHaveText('待复核 1');
  await expect(page.locator('#results-title')).toContainText('待复核');
  await expect(page.locator('#stats')).toContainText('1 篇待复核');
  await expect(page.locator('.card')).toHaveCount(1);
  await expect(page.locator('.card')).toContainText('原文中的建议与用户观点仍需核对');
  await expect(page.locator('.card')).toContainText('有一段代码尚未确认来自原文');
  const reviewDownload = page.waitForEvent('download');
  await page.getByRole('link', { name: '下载 Markdown', exact: true }).click();
  const downloadedReview = await reviewDownload;
  assert.match(downloadedReview.suggestedFilename(), /\.md$/);
  assert.match(await readFile(await downloadedReview.path(), 'utf8'), /我做了两次实验/);
  await page.getByRole('button', { name: '查看初稿', exact: true }).click();
  await expect(page.locator('#note-body')).toContainText('我做了两次实验');
  await expect(page.locator('#preview-status')).toContainText('仍待复核');
  await expect(page.locator('#preview-status')).toContainText('有一段代码尚未确认来自原文');
  const reviewImportURL = review.url + `/api/items/${review.item_id}/import`;
  const importsBeforeCancel = requests.filter(url => url === reviewImportURL).length;
  const cancelledDialog = page.waitForEvent('dialog');
  const cancelImport = page.locator('#import-preview').click();
  const cancelDialog = await cancelledDialog;
  assert.equal(cancelDialog.type(), 'confirm');
  assert.match(cancelDialog.message(), /建议与用户观点/);
  assert.match(cancelDialog.message(), /代码尚未确认/);
  await cancelDialog.dismiss();
  await cancelImport;
  await expect(page.locator('#import-preview')).toBeEnabled();
  assert.equal(requests.filter(url => url === reviewImportURL).length, importsBeforeCancel);
  const afterCancel = await page.evaluate(async () => (await (await fetch('/api/items?view=review')).json()));
  assert.equal(afterCancel.imported_count, 0);
  assert.equal(afterCancel.items[0].imported, null);
  assert.equal(afterCancel.usage.calls, 2);
  const acceptedDialog = page.waitForEvent('dialog');
  const acceptImport = page.locator('#import-preview').click();
  await (await acceptedDialog).accept();
  await acceptImport;
  await expect(page.locator('#import-preview')).toBeHidden();
  await expect(page.locator('#preview-status')).toContainText('已导入');
  await expect(page.locator('#preview-status')).toContainText('仍待复核');
  await expect(page.locator('#preview-status')).toContainText('有一段代码尚未确认来自原文');
  await page.screenshot({ path: resolve(output, 'review-mobile.png') });
  await page.locator('#close-preview').click();
  await expect(page.locator('.badge')).toContainText('已导入 · 初稿待复核');
  await expect(page.locator('.card')).toContainText('有一段代码尚未确认来自原文');
  const afterReviewImport = await page.evaluate(async () => (await (await fetch('/api/items?view=review')).json()));
  assert.equal(afterReviewImport.imported_count, 1);
  assert.equal(afterReviewImport.items[0].review_status.status, 'needs_review');
  assert.equal(afterReviewImport.usage.calls, 2);
  const reviewFiles = await readdir(resolve(review.vault, 'notes'));
  assert.equal(reviewFiles.filter(file => file.endsWith('.md')).length, 1);
  await stop(review.child);

  const unconfigured = await fixture(true);
  // Existing servers may still have the old Python handler loaded while serving
  // updated static assets. Downloads must keep working until an explicit restart.
  await page.route('**/api/session', async route => {
    const response = await route.fetch();
    const data = await response.json();
    delete data.vault_enabled;
    delete data.vault_path;
    await route.fulfill({ response, json: data });
  });
  await page.route('**/api/items?*', async route => {
    const response = await route.fetch();
    const data = await response.json();
    delete data.imported_count;
    await route.fulfill({ response, json: data });
  });
  await page.goto(unconfigured.url + '/?view=done&page=1');
  await expect(page.locator('#library-status')).toContainText('尚未配置');
  await expect(page.locator('#stats')).toContainText('0 篇已导入当前库');
  await expect(page.getByRole('button', { name: '导入 Markdown 库', exact: true })).toBeHidden();
  const download = page.waitForEvent('download');
  await page.getByRole('link', { name: '下载 Markdown', exact: true }).click();
  assert.match((await download).suggestedFilename(), /\.md$/);
  await expect(page.locator('.card')).toHaveCount(1);
  await stop(unconfigured.child);
  assert.deepEqual(errors, []);
  assert.equal(requests.every(url => new URL(url).hostname === '127.0.0.1'), true);
  console.log(JSON.stringify({ status: 'passed', output, browserErrors: errors.length,
    checks: ['generated-to-library', 'repeat-preserves-edits', 'URL-state-reload',
      'polling-preserves-reading', 'desktop-390-320-layout', 'modal-keyboard-close',
      'unconfigured-download', 'no-external-requests', 'two-pagers-history-focus',
      'search-clear-empty-history', 'skip-undo', 'inline-error-retry', 'loading-close-race',
      'sequential-reading-fixed-toolbar', 'safe-markdown', 'no-model-resubmission',
      'review-readable-download', 'review-confirm-cancel-preserves-state',
      'review-explicit-import-keeps-warnings'] }));
} finally {
  if (browser) await browser.close();
  await Promise.all([...children].map(stop));
}
