import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

type TabKey = 'intel' | 'long' | 'short' | 'projects' | 'publish'

/**
 * 频道子导航标签行 —— 频道级子导航,在频道的 5 个子页顶部持久显示、高亮当前页、
 * 可互相一点直达。
 *
 * 仅手机端:显隐与 sticky 吸顶全由 CSS `.channel-mobile-tabs` 控制(桌面 >640px
 * `display:none`,桌面 5 页都不出现,零改动)。当前页那个 chip 是不可点的高亮 span
 * (你在这),其余是 <Link> 跳对应路由。
 */
export function ChannelSubTabs({ channelId, active }: { channelId?: string; active: TabKey }) {
  const { t } = useTranslation()
  if (!channelId) return null
  const base = `/channels/${channelId}`
  const items: { key: TabKey; to: string; label: string }[] = [
    { key: 'intel', to: base, label: t('channels.channelIntelligence') },
    { key: 'long', to: `${base}/youtube`, label: t('channels.tabLongVideo') },
    { key: 'short', to: `${base}/shorts`, label: t('channels.tabShortVideo') },
    { key: 'projects', to: `${base}/projects`, label: t('channels.channelProjects') },
  ]
  return (
    <nav className="channel-mobile-tabs" aria-label={t('channels.channelWorkbench')}>
      {items.map((it) =>
        it.key === active ? (
          <span key={it.key} className="channel-mtab active" aria-current="page">
            {it.label}
          </span>
        ) : (
          <Link key={it.key} to={it.to} className="channel-mtab">
            {it.label}
          </Link>
        ),
      )}
    </nav>
  )
}
