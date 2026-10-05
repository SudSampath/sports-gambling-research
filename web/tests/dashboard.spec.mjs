import {test, expect} from '@playwright/test';

test.beforeEach(async ({page}) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('./');
  await expect(page.getByLabel('Select campaign')).toBeEnabled();
  expect(errors).toEqual([]);
});

test('Given public evidence, readers see actual counts and zero eligible opportunities', async ({page}, testInfo) => {
  await expect(page.getByRole('heading', {name: '500 markets screened. 0 eligible decisions.'})).toBeVisible();
  await expect(page.getByText('476', {exact: true})).toBeVisible();
  await expect(page.getByText('24', {exact: true})).toBeVisible();
  await expect(page.getByText('Public market data', {exact: true})).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('overview.png'), fullPage: true});
});

test('Given an unsupported record, filters preserve unknown probabilities and accessible lineage', async ({page}, testInfo) => {
  await page.getByRole('button', {name: 'Candidates', exact: true}).click();
  await page.getByLabel('Filter candidate eligibility').selectOption('eligible');
  await expect(page.getByText('No candidate records match this filter.')).toBeVisible();
  await page.getByLabel('Filter candidate eligibility').selectOption('excluded');
  await page.getByLabel('Search candidate decisions').fill('unsupported');
  await expect(page.getByRole('table').locator('tbody tr')).toHaveCount(10);
  await expect(page.getByRole('table').locator('tbody tr').first()).toContainText('Unknown');
  await page.getByRole('table').getByRole('button').first().click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByText('Settlement-rule SHA-256', {exact: true})).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await page.getByRole('button', {name: 'Next →', exact: true}).click();
  await expect(page.locator('.pagination').getByText(/page 2 of/)).toBeVisible();
  await expect(page.getByRole('button', {name: 'Next →', exact: true})).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('candidates.png'), fullPage: true});
});

test('Given synthetic fixtures, fictional capital and settlement estimates stay explicit', async ({page}, testInfo) => {
  await page.getByLabel('Select campaign').selectOption('1');
  await expect(page.getByRole('heading', {name: '10,000 candidates. 4 paper positions with fills.'})).toBeVisible();
  await page.getByRole('button', {name: 'Paper portfolio', exact: true}).click();
  await expect(page.getByText('Fictional USD · shared portfolio balances', {exact: true})).toBeVisible();
  await expect(page.getByText('50 of 50 lifecycle records shown', {exact: false})).toBeVisible();
  await expect(page.getByText('Pending inventory is not counted as realized profit.', {exact: false})).toBeVisible();
  await page.getByLabel('Select campaign').selectOption('2');
  await expect(page.getByText('Synthetic final rules use a virtual future settlement time.', {exact: false})).toBeVisible();
  await expect(page.getByRole('table').filter({hasText: 'Settlement timestamp'})).toContainText('$29.40');
  await page.getByRole('button', {name: 'Overview', exact: true}).click();
  await expect(page.getByText(/2 positions with fills; 6-second decision TTL/)).toBeVisible();
  await page.getByRole('button', {name: 'Paper portfolio', exact: true}).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('portfolio.png'), fullPage: true});
});

test('Given recovery evidence, Restate components explain durable effects without execution controls', async ({page}, testInfo) => {
  const requests = [];
  page.on('request', request => requests.push(request.method()));
  await page.getByRole('button', {name: 'How Restate works', exact: true}).click();
  await page.getByRole('button', {name: /PaperPortfolio/}).click();
  await expect(page.getByRole('heading', {name: 'Serialize capital admission'})).toBeVisible();
  await expect(page.getByText(/Each operation ran 1 \/ 1 time/)).toBeVisible();
  await expect(page.getByText('330.8 / sec', {exact: true})).toBeVisible();
  expect(requests.every(method => method === 'GET')).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: testInfo.outputPath('restate.png'), fullPage: true});
});

test('Given a missing report, the app reports failure instead of fabricated results', async ({page}) => {
  await page.route('**/data/public-screen.json', route => route.fulfill({status: 503, body: 'unavailable'}));
  await page.reload();
  await expect(page.getByRole('alert')).toContainText('could not be loaded');
  await expect(page.getByRole('heading', {name: /markets screened/})).toHaveCount(0);
});
