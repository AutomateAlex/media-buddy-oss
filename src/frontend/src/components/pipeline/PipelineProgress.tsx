import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import type { PipelineStatus, StageStatus } from '../../types'
import {
  estTotalSeconds, stageFloorFrac, tickRemaining, clampRemainingByStage,
  clampRemainingByRealPct, fracFromRemaining, formatRemaining, etaPhase,
} from './progressModel'

const DISPLAY_STAGES = [
  { id: 'script', labelKey: 'stageScriptLabel', hintKey: 'stageScriptHint' },
  { id: 'voice', labelKey: 'stageVoiceLabel', hintKey: 'stageVoiceHint' },
  { id: 'assets', labelKey: 'stageAssetsLabel', hintKey: 'stageAssetsHint' },
  { id: 'music', labelKey: 'stageMusicLabel', hintKey: 'stageMusicHint' },
  { id: 'rough_cut', labelKey: 'stageRoughCutLabel', hintKey: 'stageRoughCutHint' },
  { id: 'fine_cut', labelKey: 'stageFineCutLabel', hintKey: 'stageFineCutHint' },
  { id: 'subtitles', labelKey: 'stageSubtitlesLabel', hintKey: 'stageSubtitlesHint' },
  { id: 'seo', labelKey: 'stageSeoLabel', hintKey: 'stageSeoHint' },
  { id: 'export', labelKey: 'stageExportLabel', hintKey: 'stageExportHint' },
] as const

const LEGACY_COMPOSE_STAGES = new Set(['rough_cut', 'fine_cut', 'subtitles', 'seo', 'export'])

function getStageStatus(stage: string, stageMap: Map<string, StageStatus>): string {
  const direct = stageMap.get(stage)?.status
  if (direct) return direct
  const compose = stageMap.get('compose')?.status
  if (compose && LEGACY_COMPOSE_STAGES.has(stage)) return compose
  return 'pending'
}

function getCurrentStage(status: PipelineStatus | null, stageMap: Map<string, StageStatus>) {
  if (!status) return null
  if (status.current_stage) return status.current_stage
  const running = DISPLAY_STAGES.find((stage) => getStageStatus(stage.id, stageMap) === 'running')
  if (running) return running.id
  if (status.overall_status === 'failed') {
    return DISPLAY_STAGES.find((stage) => getStageStatus(stage.id, stageMap) === 'failed')?.id || null
  }
  if (status.overall_status === 'running') {
    return DISPLAY_STAGES.find((stage) => getStageStatus(stage.id, stageMap) === 'pending')?.id || null
  }
  return null
}

function countCompleted(stageMap: Map<string, StageStatus>) {
  return DISPLAY_STAGES.filter((stage) => {
    const st = getStageStatus(stage.id, stageMap)
    return st === 'completed' || st === 'skipped'
  }).length
}

function getError(status: PipelineStatus | null, t: TFunction) {
  if (status?.overall_status !== 'failed') return null
  // 对客户保持"神秘":失败时只给通用提示,不暴露内部环节名或原始报错。
  return t('pipelineProgress.pipelineFailedCheckLogs')
}

export function PipelineProgress({
  status,
  outputFormat,
}: {
  status: PipelineStatus | null
  outputFormat?: string | null
}) {
  const { t } = useTranslation()
  const stageMap = new Map((status?.stages || []).map((s) => [s.stage, s]))
  const overall = status?.overall_status || 'pending'
  const currentStage = getCurrentStage(status, stageMap)
  const completed = countCompleted(stageMap)
  const hasActive = DISPLAY_STAGES.some(
    (s) => getStageStatus(s.id, stageMap) === 'running' || s.id === currentStage,
  )
  const error = getError(status, t)
  const estTotal = estTotalSeconds(outputFormat)
  const total = DISPLAY_STAGES.length

  // 是否"真正在渲染":overall=running 或有活跃阶段。排队/未开始(pending/queued/claimed)为 false。
  const running = overall === 'running' || hasActive
  // 时回落到"已完成阶段比例"。这是精确进度条的锚。
  const realPct = status?.pct != null
    ? Math.max(0, Math.min(100, status.pct))
    : Math.round(stageFloorFrac(completed, total) * 100)

  // ETA 是主时钟：真实阶段进度只能让它更快，不会把它封顶冻结在某个里程碑。
  const remainRef = useRef(estTotal)
  const seededRef = useRef(false)
  const ctxRef = useRef({ overall, running, completed, total, estTotal, realPct })
  ctxRef.current = { overall, running, completed, total, estTotal, realPct }
  const [, setTick] = useState(0)

  if (!seededRef.current && status) {
    const stageSeed = clampRemainingByStage(estTotal, completed, total, estTotal)
    remainRef.current = clampRemainingByRealPct(stageSeed, realPct, estTotal)
    seededRef.current = true
  }

  useEffect(() => {
    const id = setInterval(() => {
      const c = ctxRef.current
      if (c.overall === 'completed') remainRef.current = 0
      else if (c.overall === 'failed') { /* 冻结当前显示 */ }
      else if (c.running) {
        const ticked = tickRemaining(remainRef.current, c.estTotal, 1)
        const stageClamped = clampRemainingByStage(
          ticked, c.completed, c.total, c.estTotal,
        )
        remainRef.current = clampRemainingByRealPct(stageClamped, c.realPct, c.estTotal)
      }
      setTick((n) => n + 1)
    }, 1000)
    return () => clearInterval(id)
  }, [])

  const remaining = overall === 'completed' ? 0 : remainRef.current
  const displayFrac = overall === 'completed'
    ? 1
    : running
      ? fracFromRemaining(remaining, estTotal)
      : 0
  // 百分比取"时间时钟"和"已完成阶段比例"的较大者:ETA 时钟起步慢、或后端 pct 迟报时,
  const stagePct = Math.floor(stageFloorFrac(completed, total) * 100)
  const percent = overall === 'completed'
    ? 100
    : Math.min(99, Math.max(Math.floor(displayFrac * 100), stagePct))
  const phase = etaPhase(overall, hasActive, remaining)

  // 对客户保持"神秘":不暴露内部环节名(脚本/配音/素材/混剪…),只给通用进度文案。
  const headline = error
    ? t('pipelineProgress.stuckGeneric')
    : hasActive
      ? t('pipelineProgress.workingGeneric')
      : overall === 'completed'
        ? t('pipelineProgress.done')
        : t('pipelineProgress.waitingToStart')

  const etaValue =
    phase === 'done' ? t('pipelineProgress.etaDone')
    : phase === 'queued' ? t('pipelineProgress.etaQueued')
    : phase === 'almostDone' ? t('pipelineProgress.etaAlmostDone')
    : formatRemaining(remaining)
  const showEta = phase !== 'failed'

  return (
    <div className="pipeline-progress">
      <div className="pp-head">
        <div className="pp-main">
          <div className="pp-percent-row">
            <span className="pp-percent">{percent}%</span>
            {showEta && (
              <div className={`pp-eta pp-eta-${phase}`}>
                <small>{t('pipelineProgress.etaLabel')}</small>
                <strong>{etaValue}</strong>
              </div>
            )}
          </div>
          <div className="pp-status-line">
            <span className="pp-headline">{headline}</span>
            <span className="pp-sub">
              {t('pipelineProgress.stagesProcessed', { completed, total: DISPLAY_STAGES.length })}
            </span>
          </div>
        </div>
        <div className={`pp-bar${overall === 'running' ? ' pp-bar-live' : ''}`}>
          <i style={{ width: `${percent}%` }} />
        </div>
      </div>

      {/* 内部环节明细(脚本/配音/素材/混剪…)对客户隐藏 — 保持制作神秘感 */}

      {error && <div className="error-banner">{error}</div>}
    </div>
  )
}
