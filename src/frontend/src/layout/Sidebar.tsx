import { useEffect } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { useAuthState } from '../hooks/useAuthState'
import { Avatar } from '../components/Avatar'
import { useTourStore } from '../features/tour/useTourStore'
import brandLogo from '../assets/logo-wordmark.png'

type NavItem = {
  to: string
  labelKey: string
  sub?: string
  ic: string
  tour?: string
  /** 付费功能:未开通时在右边挂一个「未开通」角标(照打磨过的原型)。 */
  lockKey?: string
  needsChannelRadar?: boolean
}

//
//    所以要先知道「现在是哪个频道」：URL 里有就用 URL 的，
//    没有就用上次待过的那个。两个都没有就指向频道列表让他先挑一个 ——
//    **绝不拼一个空 id 出来**（那会跳到一个 404 的地址，
//    客户会以为功能坏了）。
const LAST_CHANNEL_KEY = 'media-buddy.nav.last-channel'

/** 从路径里认出频道 id：`/channels/<id>/...`。认不出给 null。 */
function channelIdFromPath(pathname: string): string | null {
  const m = /^\/channels\/([^/]+)/.exec(pathname || '')
  const id = m?.[1] ?? ''
  // `/channels/new` 是「建频道」那一页，不是某个频道
  return id && id !== 'new' ? id : null
}

function readLastChannel(): string | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage.getItem(LAST_CHANNEL_KEY) || null
  } catch {
    return null
  }
}

function rememberChannel(id: string): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(LAST_CHANNEL_KEY, id)
  } catch {
    /* 存不进去只是少一点便利，功能不受影响 */
  }
}


const RESOURCES: NavItem[] = []



function Item({ item, locked }: { item: NavItem; locked?: boolean }) {
  const { t } = useTranslation()
  return (
    <NavLink
      to={item.to}
      data-tour={item.tour}
      className={({ isActive }) => (isActive ? 'nav-item active' : 'nav-item')}
    >
      <span className="ic">{item.ic}</span>
      <span className="label">
        <span className="main">{t(item.labelKey)}</span>
        {item.sub && <span className="sub">{t(item.sub)}</span>}
      </span>
      {locked && item.lockKey && (
        <span className="nav-lock">{t(item.lockKey)}</span>
      )}
    </NavLink>
  )
}

function emailInitial(email: string | null): string {
  if (!email) return 'A'
  return email.charAt(0).toUpperCase()
}

export function Sidebar() {
  const { status: authState } = useAuthState()
  const navigate = useNavigate()
  const location = useLocation()

  // 现在在哪个频道：URL 优先，其次上次待过的那个
  const urlChannel = channelIdFromPath(location.pathname)
  useEffect(() => {
    if (urlChannel) rememberChannel(urlChannel)
  }, [urlChannel])
  const channelId = urlChannel || readLastChannel()

  // ⚠️ 一个频道都没进过时指向频道列表 —— **不拼空 id**，那是 404
  const chLink = (suffix: string) =>
    channelId ? `/channels/${channelId}/${suffix}` : '/channels'


  const workspaces: NavItem[] = [
    { to: '/channels', labelKey: 'nav.channelsHome', ic: 'H', tour: 'nav-channels' },
    // 付费功能：没开通时右边挂「未开通」角标（照打磨过的原型）
    //    挂个「未开通」角标,链接照样点得进去、照样烧额度。
    { to: '/projects', labelKey: 'nav.allProjects', ic: 'P', tour: 'nav-projects' },
    { to: '/settings', labelKey: 'nav.settings', ic: 'K' },
  ]
  const { t } = useTranslation()
  const replayTour = useTourStore((s) => s.replay)


  const auth = authState ?? {
    authenticated: false,
    plan: null,
    sub_status: null,
    email: null,
    display_name: null,
    avatar_path: null,
  }

  const initial = emailInitial(auth.email)
  const displayName = auth.display_name ?? auth.email?.split('@')[0] ?? t('nav.localMode')
  const badge = auth.authenticated ? 'READY' : 'NO KEY'
  return (
    <aside className="sidebar">
      <div className="brand-mark">
        <img src={brandLogo} alt="Media Buddy" className="brand-logo-img" />
      </div>

      <nav>
        <div className="nav-section">{t('nav.studio')}</div>
        {workspaces
          //    没有 channelId（还没进过任何频道）时也不显示 —— 那时候
          .map((it) => (
            <Item key={it.to} item={it} />
          ))}

        {RESOURCES.length > 0 && (
          <>
            <div className="nav-section">{t('nav.resources')}</div>
            {RESOURCES.map((it) => <Item key={it.to} item={it} />)}
          </>
        )}

        <div className="nav-bottom">
          <button
            type="button"
            className="nav-item nav-item-btn"
            onClick={() => {
              replayTour()
              navigate('/channels')
            }}
          >
            <span className="ic">✦</span>
            <span className="label">
              <span className="main">{t('nav.tourReplay')}</span>
            </span>
          </button>
        </div>
      </nav>

      <div className="user-card" aria-label={t('nav.accountSettings')}>
        <button
          type="button"
          className="user-card-main user-card-account"
          onClick={() => navigate('/account')}
        >
          <Avatar src={auth.avatar_path} fallback={initial} size="md" />
          <div className="meta">
            <div className="name-row">
              <span className="name">{displayName}</span>
              <span className="badge-pro">{badge}</span>
            </div>
          </div>
          <span className="chev">›</span>
        </button>
      </div>
    </aside>
  )
}
