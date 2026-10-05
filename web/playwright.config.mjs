import {defineConfig} from '@playwright/test';

export default defineConfig({
  testDir: './tests', testMatch: '**/*.spec.mjs', forbidOnly: Boolean(process.env.CI),
  workers: 2, reporter: 'list',
  use: {baseURL: process.env.DASHBOARD_URL || 'http://127.0.0.1:4175', trace: 'retain-on-failure'},
  projects: [
    {name: 'desktop', use: {browserName: 'chromium', viewport: {width: 1440, height: 1050}}},
    {name: 'mobile', use: {browserName: 'chromium', viewport: {width: 390, height: 844}}},
  ],
  webServer: process.env.DASHBOARD_URL ? undefined : {
    command: 'python3 -m http.server 4175 --bind 127.0.0.1 --directory dist',
    url: 'http://127.0.0.1:4175', reuseExistingServer: false,
  },
});
