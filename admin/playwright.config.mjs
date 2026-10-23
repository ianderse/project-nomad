import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './tests/browser', testMatch: '*.spec.mjs', timeout: 30000,
  workers: 1, retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['html', {open:'never'}]] : 'list',
  use: { baseURL: 'http://127.0.0.1:4173', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
  webServer: { command: 'npm run test:browser:serve', url: 'http://127.0.0.1:4173', reuseExistingServer: false },
})
