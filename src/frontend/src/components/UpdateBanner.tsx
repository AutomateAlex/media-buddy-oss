// ✅【WEB 活代码·勿删】这是 WEB 版本更新提示(轮询 /version.json 提示刷新新构建),
// 名字带 "Update" 但**不是** Electron 自动更新器,别当桌面死代码清掉。见 docs/DESKTOP-RETIRED.md
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

// 构建时烙进来的版本号(git commit,见 vite.config)。和 /version.json 里的 commit 比对。
const CURRENT = (import.meta.env.VITE_GIT_COMMIT as string) || ''
const POLL_MS = 5 * 60 * 1000 // 每 5 分钟查一次

/**
 * 轮询 /version.json,发现构建号变了(=发了新版)就返回 true。仅在 web(http/https)运行;
 * Electron(file://)跳过——桌面端走它自己的自动更新器。配合 index.html 的白屏自愈看门狗,
 * 让"发版后用户卡在旧缓存"这件事既能自愈、又能被主动提示刷新。
 */
function useNewVersionAvailable(): boolean {
  const [avail, setAvail] = useState(false)
  useEffect(() => {
    if (typeof location === 'undefined' || !location.protocol.startsWith('http')) return
    let stopped = false
    const check = async () => {
      try {
        const r = await fetch('/version.json?t=' + Date.now(), { cache: 'no-store' })
        if (!r.ok) return
        const d = (await r.json()) as { commit?: string }
        if (!stopped && d?.commit && CURRENT && d.commit !== CURRENT) setAvail(true)
      } catch {
        /* 网络/解析失败忽略,不打扰用户 */
      }
    }
    void check()
    const iv = setInterval(check, POLL_MS)
    const onVis = () => {
      if (document.visibilityState === 'visible') void check()
    }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      stopped = true
      clearInterval(iv)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [])
  return avail
}

export function UpdateBanner() {
  const avail = useNewVersionAvailable()
  const { i18n } = useTranslation()
  if (!avail) return null
  const zh = (i18n.language || '').toLowerCase().startsWith('zh')
  return (
    <button
      type="button"
      onClick={() => window.location.reload()}
      style={{
        position: 'fixed',
        left: '50%',
        bottom: 20,
        transform: 'translateX(-50%)',
        zIndex: 99999,
        background: '#2563eb',
        color: '#fff',
        border: 'none',
        borderRadius: 999,
        padding: '10px 20px',
        fontSize: 14,
        fontWeight: 600,
        boxShadow: '0 6px 20px rgba(0,0,0,.28)',
        cursor: 'pointer',
      }}
    >
      {zh ? '🔄 有新版本，点此刷新' : '🔄 New version available — tap to refresh'}
    </button>
  )
}
