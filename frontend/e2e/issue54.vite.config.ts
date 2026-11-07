import { mergeConfig, defineConfig } from 'vite'
import base from '../vite.config.ts'

export default mergeConfig(
  base,
  defineConfig({
    server: {
      port: Number(process.env.S54_WEB_PORT ?? 5174),
      proxy: { '/api': process.env.S54_API_ORIGIN ?? 'http://127.0.0.1:8001' },
    },
  }),
)
