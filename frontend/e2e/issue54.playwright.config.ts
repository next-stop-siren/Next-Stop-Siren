import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: '.',
  testMatch: 'issue54*.cases.ts',
  use: {
    baseURL: process.env.E2E_ORIGIN ?? 'http://127.0.0.1:5174',
    browserName: 'chromium',
    screenshot: 'only-on-failure',
  },
  reporter: 'list',
  retries: 0,
  workers: 1,
})
