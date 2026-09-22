/**
 * 生产进度区的纯逻辑(无 React、无副作用,便于单测)。
 *
 * (永不冻结,解决"素材阶段一慢就卡死"),接近 0 时渐近减速、不提前撞 0;阶段真完成时
 * 把剩余往下"拽"得更快(反映真实进展);任务真完成(overall=completed)归零。
 */

const SHORT_FORMATS = new Set([
  'youtube_shorts', 'tiktok', 'instagram_reels', 'shorts', 'reels', 'short', 'short_video',
])

/** 预估总时长(秒):短视频 10 分钟 / 长视频 20 分钟。倒计时的起点。 */
export function estTotalSeconds(outputFormat?: string | null): number {
  return SHORT_FORMATS.has((outputFormat || '').toLowerCase()) ? 600 : 1200
}

/** 已完成阶段比例(已完成+skipped / 总数)。 */
export function stageFloorFrac(completed: number, total: number): number {
  if (total <= 0) return 0
  return Math.max(0, Math.min(completed, total)) / total
}

/**
 * 时改成渐近减速(每秒乘一个 < 1 的系数)→ 永远在变小、但不会撞 0 冻住。
 */
export function tickRemaining(prev: number, estTotal: number, dt = 1): number {
  const floor = estTotal * 0.12
  const next = prev > floor ? prev - dt : prev * Math.pow(1 - 0.02, Math.max(0, dt))
  return Math.max(0.5, next)
}

/**
 * 阶段完成时把剩余往下拽(只下不上):剩余不超过"按已完成阶段数推算的剩余"。
 * 阶段进行中该值恒定、通常 ≥ 当前剩余 → 不动;阶段一完成它下跳 → 剩余跟着下跳。
 */
export function clampRemainingByStage(
  remaining: number, completed: number, total: number, estTotal: number,
): number {
  const implied = (1 - stageFloorFrac(completed, total)) * estTotal
  return Math.min(remaining, implied)
}

/**
 * 后端真实进度是保底而非天花板：阶段跳进时立刻把 ETA 往前推，阶段内没有
 * 新回报时仍由时间驱动继续递减，避免长素材阶段让 UI 看起来死机。
 */
export function clampRemainingByRealPct(
  remaining: number, realPct: number, estTotal: number,
): number {
  const safePct = Math.max(0, Math.min(100, realPct))
  return Math.min(remaining, estTotal * (1 - safePct / 100))
}

export function fracFromRemaining(remaining: number, estTotal: number): number {
  if (estTotal <= 0) return 0
  return Math.max(0, Math.min(0.999, 1 - remaining / estTotal))
}

/** 格式化成 M:SS。 */
export function formatRemaining(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export type EtaPhase = 'queued' | 'running' | 'almostDone' | 'done' | 'failed'

/** ETA 该显示哪种态。 */
export function etaPhase(
  overall: string, hasActive: boolean, remaining: number,
): EtaPhase {
  if (overall === 'completed') return 'done'
  if (overall === 'failed') return 'failed'
  if (overall !== 'running' && !hasActive) return 'queued'   // queued / pending
  if (remaining <= 5) return 'almostDone'
  return 'running'
}
