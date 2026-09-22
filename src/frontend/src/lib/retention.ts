// 存储保留(前端派生)。后端是真价来源;这里只做展示:到期倒计时 + 收藏续存的估价。

const SHORT_FORMATS = new Set(['youtube_shorts', 'tiktok', 'instagram_reels', 'instagram_feed'])

export function isShortFormat(fmt?: string | null): boolean {
  return SHORT_FORMATS.has((fmt || '').toLowerCase())
}

export function retentionPricePoints(fmt?: string | null): number {
  return isShortFormat(fmt) ? 50 : 300
}

// 收藏视频到期前多少天开始聚合提醒(与后端 MEDIA_BUDDY_RETENTION_REMIND_DAYS 默认一致)。
export const REMIND_DAYS = 7

export type RetentionTone = 'green' | 'yellow' | 'red' | 'gray' | 'none'

export interface RetentionState {
  tone: RetentionTone
  daysLeft: number | null // 向上取整的剩余天数;已清除/无到期日为 null
  purged: boolean
}

function parseUtc(iso?: string | null): Date | null {
  if (!iso) return null
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso)
  const d = new Date(hasTz ? iso : iso + 'Z')
  return isNaN(d.getTime()) ? null : d
}

// 从 expires_at / storage_purged_at 派生倒计时状态(纯展示)。
export function retentionState(
  expiresAt?: string | null,
  purgedAt?: string | null,
  now: Date = new Date(),
): RetentionState {
  if (purgedAt) return { tone: 'gray', daysLeft: null, purged: true }
  const exp = parseUtc(expiresAt)
  if (!exp) return { tone: 'none', daysLeft: null, purged: false }
  const days = Math.ceil((exp.getTime() - now.getTime()) / 86_400_000)
  let tone: RetentionTone
  if (days <= 1) tone = 'red'
  else if (days <= 3) tone = 'yellow'
  else tone = 'green'
  return { tone, daysLeft: days, purged: false }
}
