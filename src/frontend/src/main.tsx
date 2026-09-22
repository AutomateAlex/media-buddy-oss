import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './App'
import { UpdateBanner } from './components/UpdateBanner'
import './i18n'
// ⚠️ 顺序是 load-bearing —— Vite 按此 import 顺序拼接成一个 CSS bundle,
// 层叠顺序靠源码顺序取胜。改动前先读 styles/README.md。两条铁律:
//   1. base.css 必须最先(所有 var(--…) 都依赖它)
//   2. components.css 必须在 studio-legacy.css 之前(全局 .btn-* 在后者被重复
//      覆盖,同优先级后者胜;顺序一乱按钮 hover/disabled 就变)
import './styles/base.css'
import './styles/layout.css'
import './styles/components.css'
import './styles/projects.css'
import './styles/studio-legacy.css'
import './styles/responsive-shell.css'
import './styles/account.css'
import './styles/studio.css'
import './styles/channels.css'
import './styles/overrides.css'
import './styles/misc.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <UpdateBanner />
    <App />
  </React.StrictMode>,
)
