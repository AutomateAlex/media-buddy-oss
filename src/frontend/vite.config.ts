import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import fs from 'fs'
import { execSync } from 'child_process'
import pkg from './package.json' with { type: 'json' }

// Phase 3.2 — inject build-time identity into renderer so the About
// dialog can show version + commit + build date without a network call.
function gitCommit(): string {
  try {
    return execSync('git rev-parse HEAD', { encoding: 'utf-8' }).trim()
  } catch {
    return 'unknown'
  }
}

const BUILD_ID = gitCommit()
const BUILD_DATE = new Date().toISOString()

// 构建时把版本号写进 dist/renderer/version.json,前端轮询它比对(见 UpdateBanner):
// 发现 commit 变了(发了新版)→ 提示用户刷新,避免有人长期挂在旧缓存版本。
function versionJsonPlugin() {
  return {
    name: 'write-version-json',
    closeBundle() {
      const dir = path.resolve(__dirname, 'dist/renderer')
      try {
        fs.mkdirSync(dir, { recursive: true })
        fs.writeFileSync(
          path.join(dir, 'version.json'),
          JSON.stringify({ commit: BUILD_ID, buildDate: BUILD_DATE, version: pkg.version }),
        )
      } catch {
        /* 写不了不阻断构建 */
      }
    },
  }
}

export default defineConfig({
  plugins: [react(), versionJsonPlugin()],
  // Phase 2.11i — relative asset paths so the Electron production build
  // (which loads the bundled index.html via file://) can find /assets/*.
  // Default 'base: /' produces <script src="/assets/..."> which the browser
  // resolves against the file:// protocol root (the disk root) → 404 → black
  // screen. Vite's recommended Electron setting is './' (relative).
  base: './',
  define: {
    'import.meta.env.VITE_APP_VERSION': JSON.stringify(pkg.version),
    'import.meta.env.VITE_BUILD_DATE': JSON.stringify(BUILD_DATE),
    'import.meta.env.VITE_GIT_COMMIT': JSON.stringify(BUILD_ID),
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: 'dist/renderer',
    emptyOutDir: true,
  },
})
