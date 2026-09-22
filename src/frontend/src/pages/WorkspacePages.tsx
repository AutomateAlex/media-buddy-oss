import { Link, useNavigate } from 'react-router-dom'
import { useEffect, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, apiErrorText } from '../api/client'
import { ttsPreviewText, outputLanguageFromUi, effectiveSubtitleLanguage, subtitleLanguageForOutput } from '../i18n'
import { voiceDisplayName, voiceDescription } from '../lib/voiceLabels'
import { SeoPackDetailModal, platformLabel } from '../components/SeoPackModal'
import { ChannelSubTabs } from '../components/ChannelSubTabs'
import { SubtitleSizeSlider, SUBTITLE_SCALE_DEFAULT } from '../components/SubtitleSizeSlider'
import { alertDialog, confirmDialog } from '../components/Dialog'
import type { PublishProject, ScriptRecommendation, SeoPack, StudioChatMessage, StudioSession, TtsVoice } from '../types'

type Step = { label: string; active?: boolean; done?: boolean; note?: string; targetId?: string }
type Tone = 'purple' | 'red' | 'blue'
type ChatMessageUi = StudioChatMessage & { ui_pending?: boolean; ui_streaming?: boolean }
const SHORTS_AGENT_SESSION_KEY = 'media-buddy.studio.active-session-id'
const SHORTS_AGENT_DRAFT_KEY = 'media-buddy.studio.draft-input'
const LONG_AGENT_SESSION_KEY = 'media-buddy.youtube.active-session-id'
const LONG_AGENT_DRAFT_KEY = 'media-buddy.youtube.draft-input'

// 会话已失效(后端 404 "session not found",多因换设备/后端重启/会话被清)——
// 别把英文原文甩给用户;返回友好中文提示,引导点「清空对话」重开。其余错误透传 detail。
function isSessionNotFound(e: any): boolean {
  const detail = String(e?.response?.data?.detail || e?.message || '')
  return e?.response?.status === 404 && /session\s*not\s*found/i.test(detail)
}
// P2-13 可恢复错误 → 中文行动指引映射:保留技术信息也行,但一定紧跟"下一步怎么办"。
function studioErrorText(e: any, t: (key: string) => string): string {
  if (isSessionNotFound(e)) return t('workspace.sessionExpired')
  const status = e?.response?.status
  const detail = String(e?.response?.data?.detail || '')
  const msg = String(e?.message || '')
  if (e?.code === 'ECONNABORTED' || /timeout/i.test(detail + msg)) return t('workspace.errTimeout')
  if (status === 429) return t('workspace.errRateLimited')
  if (status === 401 || status === 403) return t('workspace.errAuth')
  if (!status || status >= 500) return t('workspace.errServer')
  // 其它:显示后端 detail(可能是英文/技术),但紧跟一句中文行动指引。
  return (detail || msg) ? `${detail || msg} —— ${t('workspace.errGenericHint')}` : t('workspace.errGeneric')
}

function withOptimisticStudioTurn(session: StudioSession, userText: string, thinkingText: string): StudioSession {
  const ts = new Date().toISOString()
  return {
    ...session,
    messages: [
      ...session.messages,
      { role: 'user', content: userText, ts },
      { role: 'assistant', content: thinkingText, ts, ui_pending: true } as ChatMessageUi,
    ],
  }
}

function typeAssistantResponse(
  finalSession: StudioSession,
  setSession: Dispatch<SetStateAction<StudioSession | null>>,
) {
  const assistantIndex = [...finalSession.messages]
    .map((message, index) => ({ message, index }))
    .reverse()
    .find(({ message }) => message.role === 'assistant' && typeof message.content === 'string')?.index

  if (assistantIndex === undefined) {
    setSession(finalSession)
    return
  }

  const finalMessage = finalSession.messages[assistantIndex] as StudioChatMessage
  const fullText = finalMessage.content || ''
  if (!fullText) {
    setSession(finalSession)
    return
  }

  setSession({
    ...finalSession,
    messages: finalSession.messages.map((message, index) =>
      index === assistantIndex
        ? ({ ...message, content: '', ui_streaming: true } as ChatMessageUi)
        : message,
    ),
  })

  let cursor = 0
  const chunkSize = Math.max(1, Math.ceil(fullText.length / 180))
  const timer = window.setInterval(() => {
    cursor = Math.min(fullText.length, cursor + chunkSize)
    const visible = fullText.slice(0, cursor)
    setSession((current) => {
      if (!current || current.id !== finalSession.id) return current
      return {
        ...finalSession,
        messages: finalSession.messages.map((message, index) =>
          index === assistantIndex
            ? ({ ...message, content: visible, ui_streaming: cursor < fullText.length } as ChatMessageUi)
            : message,
        ),
      }
    })
    if (cursor >= fullText.length) {
      window.clearInterval(timer)
      setSession(finalSession)
    }
  }, 18)
}

function showPendingAssistantError(
  sessionId: string,
  message: string,
  setSession: Dispatch<SetStateAction<StudioSession | null>>,
  failedPrefix: string,
) {
  setSession((current) => {
    if (!current || current.id !== sessionId) return current
    const lastPendingIndex = [...current.messages]
      .map((item, index) => ({ item, index }))
      .reverse()
      .find(({ item }) => item.role === 'assistant' && (item as ChatMessageUi).ui_pending)?.index
    if (lastPendingIndex === undefined) return current
    return {
      ...current,
      messages: current.messages.map((item, index) =>
        index === lastPendingIndex
          ? ({ ...item, content: `${failedPrefix}${message}`, ui_pending: false } as ChatMessageUi)
          : item,
      ),
    }
  })
}

// 字幕语言(可独立于配音/输出语言)。简/繁/英 + 中英双语。默认跟随界面语言。
type SubtitleLanguage = 'zh-Hans' | 'zh-Hant' | 'en' | 'zh-Hans+en' | 'zh-Hant+en'
const SUBTITLE_LANGUAGES: { value: SubtitleLanguage; key: string }[] = [
  { value: 'zh-Hans', key: 'workspace.subHans' },
  { value: 'zh-Hant', key: 'workspace.subHant' },
  { value: 'en', key: 'workspace.subEn' },
  { value: 'zh-Hans+en', key: 'workspace.subHansEn' },
  { value: 'zh-Hant+en', key: 'workspace.subHantEn' },
]

type ShortConfig = {
  goal: string
  platforms: string[]
  videoFormat: string
  videoFormatTouched?: boolean
  duration: string
  quantity: number
  styles: string[]
  voiceProvider: string
  voiceLabel: string
  voiceSpeed: number
  outputLanguage: 'zh' | 'en'
  // 高级英语配音档位。放在 config 里而不是面板的独立 state ——
  voiceTier?: 'normal' | 'advanced'
  subtitleLanguage?: SubtitleLanguage
  subtitleFontScale?: number
  includeSubtitles?: boolean
  includeMusic?: boolean
  hydrated: boolean
}

type LongConfig = {
  goal: string
  platforms: string[]
  videoFormat: string
  videoFormatTouched?: boolean
  duration: string
  quantity: number
  styles: string[]
  voiceProvider: string
  voiceLabel: string
  voiceSpeed: number
  outputLanguage: 'zh' | 'en'
  // 高级英语配音档位。放在 config 里而不是面板的独立 state ——
  voiceTier?: 'normal' | 'advanced'
  subtitleLanguage?: SubtitleLanguage
  subtitleFontScale?: number
  includeSubtitles?: boolean
  includeMusic?: boolean
  hydrated: boolean
}

// P1-5 两入口(对话式 chat / 粘贴文案 script)各自独立的「保存为默认」:
// 按(频道 × 长短 × 模式)各存一份制作参数(时长/配音/语速/字幕大小/字幕语言/输出语言/背景音乐)。
// 与频道批量默认(Channels.tsx 的 batch-settings.defaults)互不干扰;两入口互不覆盖。
type EntryDefaults = {
  duration?: string
  voiceProvider?: string
  voiceLabel?: string
  voiceSpeed?: number
  outputLanguage?: 'zh' | 'en'
  subtitleLanguage?: SubtitleLanguage
  subtitleFontScale?: number
  includeSubtitles?: boolean
  includeMusic?: boolean
  //    根因:`voiceTier` 是**独立的 useState('normal')**,压根不在这份默认里,
  //    每次进来必然回到「普通」。补进来之后它才跟着一起存/取。
  voiceTier?: 'normal' | 'advanced'
}
function entryDefaultsKey(channelId: string, videoType: 'short' | 'long', mode: 'chat' | 'script'): string {
  return `media-buddy.entry-defaults.${channelId}.${videoType}.${mode}`
}
function readEntryDefaults(
  channelId: string | undefined,
  videoType: 'short' | 'long',
  mode: 'chat' | 'script',
): EntryDefaults {
  if (typeof window === 'undefined' || !channelId) return {}
  try {
    const raw = window.localStorage.getItem(entryDefaultsKey(channelId, videoType, mode))
    const d = raw ? (JSON.parse(raw) as EntryDefaults) : {}
    return d && typeof d === 'object' ? d : {}
  } catch {
    return {}
  }
}
function saveEntryDefaults(
  channelId: string | undefined,
  videoType: 'short' | 'long',
  mode: 'chat' | 'script',
  d: EntryDefaults,
): boolean {
  if (typeof window === 'undefined' || !channelId) return false
  try {
    // 🚨 **合并写入,不是整体覆盖。**
    //    原来是 `setItem(key, JSON.stringify(d))` —— 只要有第二个调用方
    //    只带部分字段来存,就会把别的字段**整片抹掉**,而且不报错。
    //    先把这个雷拆掉再加。
    const prev = readEntryDefaults(channelId, videoType, mode)
    window.localStorage.setItem(
      entryDefaultsKey(channelId, videoType, mode), JSON.stringify({ ...prev, ...d }))
    return true
  } catch {
    // 存储失败(隐私模式/配额满)→ 明确返回 false,让调用方提示用户,不静默吞掉。
    return false
  }
}
// 「高级英语配音」说明弹窗:**看过一次就不再弹**。
//
// 再次进入就不要再显示弹窗了。」
//
//    和具体哪个频道无关。按频道记会让客户在每个频道各看一遍,更烦。
const PREMIUM_VOICE_ACK_KEY = 'media-buddy.premium-voice.acked'
function premiumVoiceAcked(): boolean {
  if (typeof window === 'undefined') return false
  try { return window.localStorage.getItem(PREMIUM_VOICE_ACK_KEY) === '1' } catch { return false }
}
function ackPremiumVoice(): void {
  if (typeof window === 'undefined') return
  try { window.localStorage.setItem(PREMIUM_VOICE_ACK_KEY, '1') } catch { /* 存不下就下次再弹,不影响功能 */ }
}

// 按(频道, 长/短)记住上次用的是对话式(chat)还是粘贴文案(script),回到入口自动回到那个模式。
function entryModeKey(channelId: string, videoType: 'short' | 'long'): string {
  return `media-buddy.entry-mode.${channelId}.${videoType}`
}
function readEntryMode(channelId: string | undefined, videoType: 'short' | 'long'): 'chat' | 'script' {
  if (!channelId) return 'chat'
  try {
    const v = window.localStorage.getItem(entryModeKey(channelId, videoType))
    return v === 'script' ? 'script' : 'chat'
  } catch {
    return 'chat'
  }
}
function saveEntryMode(channelId: string | undefined, videoType: 'short' | 'long', mode: 'chat' | 'script'): void {
  if (!channelId) return
  try { window.localStorage.setItem(entryModeKey(channelId, videoType), mode) } catch { /* ignore */ }
}

const VIDEO_FORMATS = [
  { key: 'vertical_9_16', label: '竖版 9:16', aspectRatio: '9:16', outputFormat: 'youtube_shorts', framingStyle: 'fill' },
  { key: 'vertical_9_16_blur', label: '竖版 9:16 · 背景虚化', aspectRatio: '9:16', outputFormat: 'youtube_shorts', framingStyle: 'blur' },
  { key: 'horizontal_16_9', label: '横版 16:9 · 1920×1080', aspectRatio: '16:9', outputFormat: 'youtube_landscape', framingStyle: 'fill' },
]

// Display-only i18n keys. The canonical Chinese strings above remain the
// identity/value AND flow into all LLM context — these maps ONLY change what
// renders in dropdowns/options/buttons. (decouple display from LLM-value)
const VIDEO_FORMAT_LABEL_KEYS: Record<string, string> = {
  vertical_9_16: 'workspace.formatVertical916',
  vertical_9_16_blur: 'workspace.formatVertical916Blur',
  horizontal_16_9: 'workspace.formatHorizontal169',
}
const VOICE_SPEED_LABEL_KEYS: Record<string, string> = {
  正常: 'workspace.speedNormal',
  稍快: 'workspace.speedFaster',
  快: 'workspace.speedFast',
  极快: 'workspace.speedVeryFast',
}
const DURATION_LABEL_KEYS: Record<string, string> = {
  '15 秒以内': 'workspace.durUnder15s',
  '15-30 秒': 'workspace.dur15to30s',
  '30-45 秒': 'workspace.dur30to45s',
  '45-60 秒': 'workspace.dur45to60s',
  '1 分钟': 'workspace.dur1min',
  '2 分钟': 'workspace.dur2min',
  '3 分钟': 'workspace.dur3min',
  '3-5 分钟': 'workspace.dur3to5min',
  '约 10 分钟': 'workspace.durAbout10min',
  '5-8 分钟': 'workspace.dur5to8min',
  '12-20 分钟': 'workspace.dur12to20min',
  '20-30 分钟': 'workspace.dur20to30min',
}
const SHORT_VIDEO_GOAL_LABEL_KEYS: Record<string, string> = {
  知识科普: 'workspace.goalKnowledge',
  教程教学: 'workspace.goalTutorial',
  观点解读: 'workspace.goalOpinion',
  故事叙事: 'workspace.goalStory',
  产品引流: 'workspace.goalProductTraffic',
  涨粉曝光: 'workspace.goalGrowth',
  活动促销: 'workspace.goalPromo',
  本地门店获客: 'workspace.goalLocalStore',
  品牌种草: 'workspace.goalBrandSeeding',
  '课程/服务咨询': 'workspace.goalCourseInquiry',
  私域加粉: 'workspace.goalPrivateAdd',
  私域转化: 'workspace.goalPrivateConvert',
  口碑信任: 'workspace.goalReputation',
  探店测评: 'workspace.goalStoreReview',
  新闻热点: 'workspace.goalNews',
  活动预热: 'workspace.goalEventWarmup',
}
const SHORT_PLATFORM_LABEL_KEYS: Record<string, string> = {
  'Douyin / 抖音': 'workspace.platformDouyin',
  'Kuaishou / 快手': 'workspace.platformKuaishou',
  'Xiaohongshu / 小红书': 'workspace.platformXiaohongshu',
  'Bilibili 竖屏': 'workspace.platformBilibiliVertical',
  'LinkedIn 短视频': 'workspace.platformLinkedinShort',
}
const SHORT_STYLE_LABEL_KEYS: Record<string, string> = {
  知识科普: 'workspace.styleKnowledge',
  解释型旁白: 'workspace.styleExplainNarration',
  视觉隐喻: 'workspace.styleVisualMetaphor',
  纪录片感: 'workspace.styleDocumentary',
  教程干货: 'workspace.styleTutorial',
  真实口播: 'workspace.styleRealTalking',
  诙谐幽默: 'workspace.styleFunny',
  快节奏信息流: 'workspace.styleFastInfo',
  产品展示: 'workspace.styleProductShowcase',
  高级品牌感: 'workspace.stylePremiumBrand',
  强钩子痛点: 'workspace.styleStrongHook',
  故事叙事: 'workspace.styleStory',
  故事场景: 'workspace.styleStoryScene',
  促销转化: 'workspace.stylePromoConvert',
  测评对比: 'workspace.styleReviewCompare',
  观点解读: 'workspace.styleOpinion',
  悬念反转: 'workspace.styleSuspenseTwist',
  数据冲击: 'workspace.styleDataImpact',
  沉浸氛围: 'workspace.styleImmersive',
  '探店/门店': 'workspace.styleStoreVisit',
  情绪共鸣: 'workspace.styleEmotionResonance',
}
const LONG_PLATFORM_LABEL_KEYS: Record<string, string> = {
  长视频: 'workspace.platformLongVideo',
}
const LONG_STYLE_LABEL_KEYS: Record<string, string> = {
  专业解说: 'workspace.lstyleProfessional',
  纪录片感: 'workspace.styleDocumentary',
  章节化叙事: 'workspace.lstyleChaptered',
  深度分析: 'workspace.lstyleDeepAnalysis',
  故事讲述: 'workspace.lstyleStorytelling',
  教程干货: 'workspace.styleTutorial',
  视觉隐喻: 'workspace.styleVisualMetaphor',
  案例拆解: 'workspace.lstyleCaseBreakdown',
  平稳旁白: 'workspace.lstyleSteadyNarration',
  '强开场 Hook': 'workspace.lstyleStrongOpenHook',
  系列频道感: 'workspace.lstyleSeriesChannel',
  高信息密度: 'workspace.lstyleHighDensity',
}

// Renders the English display for a canonical Chinese option string; falls back
// to the original string if no key is mapped (so nothing ever renders blank).
function displayLabel(t: (k: string) => any, map: Record<string, string>, value: string) {
  const key = map[value]
  return key ? t(key) : value
}

// Voice picker display-only i18n, keyed by stable provider_key. The stored
// voiceLabel / machineContext voice_label keep the original Chinese display_name
// (set via cleanVoiceDisplayName on the canonical value), so the LLM input is
// unchanged. Remote-merged voices not in this map fall back to their raw name.
// 音色名/描述映射 + voiceDisplayName/voiceDescription 已抽到 ../lib/voiceLabels(共享给
// 所有音色选择器·含千问 24 音色)。这里保留语言提示映射(仅本页用)。
// Language-hint display. The raw hint (zh, en, zh-TW…) and any language_label
// stay as the stored data; this only prettifies what shows in the picker.
const VOICE_LANG_LABEL_KEYS: Record<string, string> = {
  zh: 'workspace.voiceLangZh',
  'zh-TW': 'workspace.voiceLangZhTw',
  'zh-HK': 'workspace.voiceLangZhHk',
  'zh-SG': 'workspace.voiceLangZhSg',
  en: 'workspace.voiceLangEn',
  multi: 'workspace.voiceLangMulti',
  '中国大陆普通话': 'workspace.voiceLangMainland',
  '中文 / 华语': 'workspace.voiceLangChinese',
  '中国台湾中文': 'workspace.voiceLangTaiwan',
  '新加坡中文': 'workspace.voiceLangSingapore',
}

// voiceDisplayName / voiceDescription 见 ../lib/voiceLabels(已 import)。
function voiceLangText(t: (k: string) => any, v: TtsVoice) {
  const raw = v.language_label || v.language_hint
  const key = VOICE_LANG_LABEL_KEYS[raw]
  return key ? t(key) : raw
}

const DEFAULT_SHORT_VIDEO_FORMAT = 'vertical_9_16'
const DEFAULT_LONG_VIDEO_FORMAT = 'horizontal_16_9'
const DEFAULT_LONG_VIDEO_WIDTH = 1920
const DEFAULT_LONG_VIDEO_HEIGHT = 1080
const DEFAULT_LONG_DURATION = '约 10 分钟'
const DEFAULT_LONG_DURATION_SECONDS = 600
const MIN_LONG_DURATION_SECONDS = 300 // 长视频最低 5 分钟起
const LONG_SCRIPT_SKELETON = [
  '0-30s: 强钩子，提出冲突、反常识或观众会关心的问题',
  '30-90s: 建立背景和观看收益，让观众知道为什么要继续看',
  '主体: 4-6 个章节，每章只讲一个核心点，并配具体例子、画面、数据或故事细节',
  '中后段: 加一段反转、实用建议或观众能带走的知识，防止只写成百科简介',
  '结尾: 回收开头问题，总结观点，给一个明确评论/收藏/关注动作',
].join('；')
const DEFAULT_VOICE_SPEED = 1.2
// 长视频默认语速 = 快(1.4):批量弹窗本就默认 1.4,单条长视频也对齐到"快"。
const DEFAULT_LONG_VOICE_SPEED = 1.4
const VOICE_SPEEDS = [
  { label: '正常', value: 1.0 },
  { label: '稍快', value: 1.2 },
  { label: '快', value: 1.4 },
  { label: '极快', value: 1.6 },
]
// 语速挡位已接通真实变速(ElevenLabs 走 atempo、Azure 走 rate),并支持按挡试听。
const SHOW_VOICE_SPEED = true
// 短视频区间:下限 45 秒(去掉 15秒以内/15-30秒/30-45秒,产品决定不再要更短)、上限 2 分钟。
const SHORT_DURATIONS = ['45-60 秒', '1 分钟', '2 分钟']
// 长视频区间:下限 5 分钟(去掉 3-5 分钟——起点 3 分钟低于 5 分钟下限、会被强制拉回不生效)、
// 上限 ~10 分钟(去掉 12-20/20-30:脚本填不满)。
const LONG_DURATIONS = [DEFAULT_LONG_DURATION, '5-8 分钟']

function estimateDurationFromScript(text: string, voiceSpeed = DEFAULT_VOICE_SPEED) {
  const compact = text
    .replace(/【MACHINE_CONTEXT_JSON】[\s\S]*?【\/MACHINE_CONTEXT_JSON】/g, '')
    .replace(/[，。！？、；：“”‘’（）《》【】\s,.!?;:'"()[\]{}<>-]/g, '')
  const cjkCount = (compact.match(/[\u4e00-\u9fff]/g) ?? []).length
  const latinWords = (compact.match(/[A-Za-z0-9]+/g) ?? []).length
  const speechUnits = cjkCount + latinWords * 2
  if (speechUnits < 80) return ''
  const seconds = speechUnits / (4.2 * Math.max(0.7, Math.min(1.2, voiceSpeed || DEFAULT_VOICE_SPEED)))
  // 下限 45 秒:更短的估算一律归到 45-60 秒(更短档已去掉)。
  if (seconds <= 60) return '45-60 秒'
  if (seconds <= 85) return '1 分钟'
  return '2 分钟' // 短视频上限 2 分钟(更长的档已去掉)
}

function estimateScriptSeconds(text: string, voiceSpeed = DEFAULT_VOICE_SPEED) {
  const compact = text
    .replace(/【MACHINE_CONTEXT_JSON】[\s\S]*?【\/MACHINE_CONTEXT_JSON】/g, '')
    .replace(/[，。！？、；：“”‘’（）《》【】\s,.!?;:'"()[\]{}<>-]/g, '')
  const cjkCount = (compact.match(/[\u4e00-\u9fff]/g) ?? []).length
  const latinWords = (compact.match(/[A-Za-z0-9]+/g) ?? []).length
  const speechUnits = cjkCount + latinWords * 2
  return speechUnits / (3.6 * Math.max(0.7, Math.min(1.2, voiceSpeed || DEFAULT_VOICE_SPEED)))
}

function buildLongScriptTooShortMessage(
  text: string,
  targetSeconds: number,
  t: (key: string, opts?: Record<string, unknown>) => string,
  voiceSpeed = DEFAULT_VOICE_SPEED,
) {
  const script = extractLatestDraftScript(text)
  const estimatedSeconds = estimateScriptSeconds(script || text, voiceSpeed)
  const estimatedMinutes = Math.max(1, Math.round(estimatedSeconds / 60))
  const targetMinutes = Math.max(1, Math.round(targetSeconds / 60))
  const minMinutes = Math.round(MIN_LONG_DURATION_SECONDS / 60)
  return t('workspace.longScriptTooShortMessage', { estimatedMinutes, minMinutes, targetMinutes })
}

function selectedVideoFormat(config: ShortConfig) {
  return VIDEO_FORMATS.find((item) => item.key === (config.videoFormat || DEFAULT_SHORT_VIDEO_FORMAT)) ?? VIDEO_FORMATS[0]
}

function explicitVideoFormatFromText(text: string) {
  if (/横版|横屏|16\s*:\s*9|landscape/i.test(text)) return 'horizontal_16_9'
  if (/竖版|竖屏|9\s*:\s*16|vertical/i.test(text)) return DEFAULT_SHORT_VIDEO_FORMAT
  return ''
}

type FocusStatus = 'empty' | 'draft' | 'confirmed'
type FocusItem = { label: string; value: string }

function WorkspaceHeader({
  crumb,
  backTo,
  backLabel,
}: {
  crumb: string
  backTo?: string
  backLabel?: string
}) {
  const { t } = useTranslation()
  const resolvedBackLabel = backLabel ?? t('workspace.backToParent')
  return (
    <header className="page-header">
      <div className="crumb">
        <span className="seg">Studio</span>
        <span className="sep">/</span>
        <span className="here">{crumb}</span>
      </div>
      {backTo && (
        <Link to={backTo} className="page-back-link">
          {resolvedBackLabel}
        </Link>
      )}
    </header>
  )
}

function Stepper({ steps, tone }: { steps: Step[]; tone: Tone }) {
  const { t } = useTranslation()
  const goToStep = (targetId?: string) => {
    if (!targetId) return
    document.getElementById(targetId)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <nav className={`workspace-stepper stepper-${tone}`} aria-label={t('workspace.workflow')}>
      {steps.map((s, index) => (
        <button
          type="button"
          key={s.label}
          className={`step-item${s.done ? ' done' : ''}${s.active ? ' active' : ''}`}
          onClick={() => goToStep(s.targetId)}
          aria-current={s.active ? 'step' : undefined}
        >
          <span>{s.done ? '?' : index + 1}</span>
          <strong>{s.label}</strong>
          {s.note && <em>{s.note}</em>}
        </button>
      ))}
    </nav>
  )
}

function AgentShell({
  tone,
  left,
  center,
  right,
  leftId,
  centerId,
  rightId,
  leftTitle,
  leftSummary,
}: {
  tone: Tone
  left: ReactNode
  center: ReactNode
  right: ReactNode
  leftId?: string
  centerId?: string
  rightId?: string
  /** 手机端配置栏折叠区的标题 + 收起时的一行摘要(桌面端不显、常展开)。 */
  leftTitle?: string
  leftSummary?: string
}) {
  return (
    <section className={`agent-workbench workbench-${tone}${right ? '' : ' no-right'}`}>
      {/* 配置栏:桌面端常显(CSS 强制展开+隐藏 summary)。手机端为可折叠 <details>,
          但默认展开(open)——否则用户往下拉只看到对话区、看不到设置,以为"没了"。
          想收起再点标题。对话区在手机端用 CSS order 提到最上(主操作优先)。 */}
      <details id={leftId} className="agent-panel agent-cfg" open>
        <summary className="agent-cfg-summary">
          <span className="agent-cfg-title">{leftTitle || '设置'}</span>
          {leftSummary ? <span className="agent-cfg-peek">{leftSummary}</span> : null}
          <span className="agent-cfg-chev" aria-hidden="true">▾</span>
        </summary>
        <div className="agent-cfg-body">{left}</div>
      </details>
      <main id={centerId} className="agent-panel agent-chat-panel">{center}</main>
      {right && <aside id={rightId} className="agent-panel">{right}</aside>}
    </section>
  )
}

function FieldList({ items }: { items: Array<[string, string]> }) {
  return (
    <div className="brief-fields">
      {items.map(([label, value]) => (
        <div key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </div>
  )
}

function ChatCard({
  title,
  intro,
  bullets,
  chips,
  button,
}: {
  title: string
  intro: string
  bullets: string[]
  chips: string[]
  button: string
}) {
  const { t } = useTranslation()
  return (
    <>
      <div className="agent-card-title">
        <span>?</span>
        <div>
          <h2>{title}</h2>
          <p>{intro}</p>
        </div>
      </div>
      <div className="agent-message">
        <strong>{t('workspace.aiSuggestsStart')}</strong>
        <ul>
          {bullets.map((b) => <li key={b}>{b}</li>)}
        </ul>
      </div>
      <div className="chip-row">
        {chips.map((c) => <button key={c}>{c}</button>)}
      </div>
      <div className="agent-input">
        <input placeholder={t('workspace.tellMeAdjustPlaceholder')} />
        <button>{button}</button>
      </div>
    </>
  )
}

function ScriptBlocks({ blocks, primary }: { blocks: Array<[string, string]>; primary: string }) {
  const { t } = useTranslation()
  return (
    <>
      <div className="agent-side-head">
        <h3>{t('workspace.scriptFocus')}</h3>
        <span>{t('workspace.pendingConfirm')}</span>
      </div>
      <div className="script-block-list">
        {blocks.map(([title, body]) => (
          <div key={title} className="script-block">
            <span>{title}</span>
            <p>{body}</p>
          </div>
        ))}
      </div>
      <div className="version-actions">
        <button className="btn-primary">{primary}</button>
      </div>
    </>
  )
}

function visibleUserContent(content: string) {
  content = content.replace(/【MACHINE_CONTEXT_JSON】[\s\S]*?【\/MACHINE_CONTEXT_JSON】\s*/g, '')
  return content.includes('【用户最新输入】')
    ? content.split('【用户最新输入】').pop()?.trim() ?? ''
    : content
}

function inferConfigFromConversation(messages: any[], fallback: ShortConfig): ShortConfig {
  const userText = messages
    .filter((m) => m?.role !== 'assistant' && m?.role !== 'system' && m?.role !== 'tool')
    .map((m) => typeof m.content === 'string' ? visibleUserContent(m.content) : '')
    .join('\n')

  const platforms = [
    /youtube\s*shorts|youtube\s*shot|youtubeshorts|shorts/i.test(userText) ? 'YouTube Shorts' : '',
    /小红书|xiaohongshu|rednote/i.test(userText) ? 'Xiaohongshu / 小红书' : '',
    /tiktok/i.test(userText) ? 'TikTok' : '',
    /instagram\s*reels|\breels\b/i.test(userText) ? 'Instagram Reels' : '',
    /抖音|douyin/i.test(userText) ? 'Douyin / 抖音' : '',
    /快手|kuaishou/i.test(userText) ? 'Kuaishou / 快手' : '',
  ].filter(Boolean)

  const isCommercial = /产品|服务|门店|店铺|品牌|引流|获客|咨询|预约|私信|下单|成交|转化|促销|优惠|团购|领取|报价/.test(userText)
  const isKnowledge = /知识|科普|原理|为什么|是什么|怎么回事|科学|物理|化学|生物|医学|健康|血管|心脏|身体|结构|分叉|层层|覆盖|细胞|宇宙|历史|冷知识|解释|证明|研究|数据|装修|室内|家装|技巧/.test(userText)
  const isTutorial = /教程|教学|技巧|方法|步骤|怎么|如何|避坑|清单|指南|攻略/.test(userText)
  const isCommentary = /观点|锐评|解读|分析|评论|热点|趋势|争议|为什么说/.test(userText)
  const isStory = /故事|经历|案例|真实发生|那天|后来|转折|反转/.test(userText)
  const isGrowth = /涨粉|粉丝|账号|流量|播放量|曝光量|起号|破圈/.test(userText)

  let goal = fallback.goal
  if (isCommercial && /促销|下单|转化|优惠|团购/.test(userText)) goal = '活动促销'
  else if (isCommercial && /私信|咨询|预约|领取|成交|报价/.test(userText)) goal = '私域转化'
  else if (isCommercial && /品牌|种草|口碑|信任/.test(userText)) goal = '品牌种草'
  else if (isCommercial) goal = '产品引流'
  else if (isKnowledge) goal = '知识科普'
  else if (isTutorial) goal = '教程教学'
  else if (isCommentary) goal = '观点解读'
  else if (isStory) goal = '故事叙事'
  else if (isGrowth) goal = '涨粉曝光'

  let duration = fallback.duration
  if (/1\s*分钟|一分钟|60\s*秒/.test(userText)) duration = '1 分钟'
  // 短视频上限 2 分钟:3 分钟 / 3-5 分钟的诉求一律封顶到 2 分钟。
  else if (/3\s*[-~到至]\s*5\s*分钟|三\s*[-~到至]\s*五\s*分钟|3\s*分钟|三分钟|2\s*分钟|两分钟|二分钟/.test(userText)) duration = '2 分钟'
  else if (/30\s*秒|半分钟|45\s*秒/.test(userText)) duration = '45-60 秒'
  const estimatedDuration = looksLikeUserScript(userText)
    ? estimateDurationFromScript(userText, fallback.voiceSpeed || DEFAULT_VOICE_SPEED)
    : ''
  if (estimatedDuration && SHORT_DURATIONS.indexOf(estimatedDuration) > SHORT_DURATIONS.indexOf(duration || '')) {
    duration = estimatedDuration
  }

  // 风格由用户在下拉里手动选(真实口播 / 诙谐幽默…),不再按文本自动猜一堆已下线的细分风格。
  // 只保留仍可选的风格,空则默认真实口播。
  const validShortStyles = new Set(SHORT_STYLES)
  const styles = new Set([...fallback.styles].filter((s) => validShortStyles.has(s)))

  const explicitFormat = explicitVideoFormatFromText(userText)
  let videoFormat = fallback.videoFormatTouched
    ? (fallback.videoFormat || DEFAULT_SHORT_VIDEO_FORMAT)
    : DEFAULT_SHORT_VIDEO_FORMAT
  if (explicitFormat) videoFormat = explicitFormat

  return {
    ...fallback,
    goal: goal || '知识科普',
    platforms: platforms.length > 0 ? platforms : fallback.platforms,
    duration: duration || fallback.duration,
    videoFormat: videoFormat || fallback.videoFormat || DEFAULT_SHORT_VIDEO_FORMAT,
    videoFormatTouched: fallback.videoFormatTouched || Boolean(explicitFormat),
    quantity: fallback.quantity || 1,
    styles: Array.from(styles).length > 0 ? Array.from(styles) : ['教程干货'],
    voiceProvider: fallback.voiceProvider || 'azure_yunyang',
    voiceLabel: fallback.voiceLabel || '云扬 · 中文男声（深沉嗓音）',
    voiceSpeed: fallback.voiceSpeed || DEFAULT_VOICE_SPEED,
    outputLanguage: fallback.outputLanguage || outputLanguageFromUi(),
    hydrated: true,
  }
}

function looksLikeUserScript(text: string) {
  // 与后端 studio_flow.looks_like_script 对齐(同一判据,避免前后端漂移导致
  // 「AI 已收稿、前端却说没脚本、不能生成」)。判据:非 JSON、去空白 ≥50 字、
  const stripped = (text ?? '').trim()
  if (!stripped || stripped.startsWith('{') || stripped.startsWith('[')) return false
  if (stripped.includes('MACHINE_CONTEXT_JSON')) return false
  const compact = stripped.replace(/\s+/g, '')
  if (compact.length < 50) return false
  const parts = stripped.split(/[。！？.!?]+/).map((p) => p.trim()).filter(Boolean)
  if (parts.length === 0) return false
  const questions = parts.filter((p) => /[?？]$/.test(p)).length
  return (parts.length - questions) / Math.max(1, parts.length) > 0.6
}

function extractFocusFromUserScript(text: string): FocusItem[] | null {
  if (!looksLikeUserScript(text)) return null
  const lines = text
    .replace(/\r/g, '')
    .split(/\n|(?<=[。！？!?])/)
    .map((line) => line.trim())
    .filter(Boolean)
  const hookLine = lines.find((line) => /你知道|为什么|有没有|是什么|吗/.test(line)) ?? lines[0]
  const ctaLine = [...lines].reverse().find((line) => /评论|告诉我|关注|私信|预约|下单|领取/.test(line))
  const focusLines = lines
    .filter((line) => !ctaLine || line !== ctaLine)
    .filter((line) => line !== hookLine)
    .slice(0, 4)
  return [
    {
      label: 'Hook 0-3s',
      value: hookLine || '从客户剧本开头提炼 Hook。',
    },
    {
      label: '视频重点',
      value: focusLines.join(' ') || text.slice(0, 180),
    },
    {
      label: '表达风格',
      value: '客户已提供完整剧本，优先保留原文结构，再根据平台和时长做压缩或分镜。',
    },
    {
      label: 'CTA',
      value: ctaLine ?? '客户剧本中未明显出现 CTA，可补充评论、私信、关注或预约动作。',
    },
  ]
}

function extractFocusFromAiDraft(content: string): FocusItem[] | null {
  const draftRegex = /[\[【]?\s*第\s*(\d+)\s*稿[\s\S]*?(?=(?:[\[【]?\s*第\s*\d+\s*稿)|$)/g
  const drafts = Array.from(content.matchAll(draftRegex))
  if (drafts.length === 0) return null

  const latest = drafts[drafts.length - 1]
  const latestDraft = latest[0]
  const latestDraftNo = latest[1]
  const cleaned = latestDraft
    .replace(/\r/g, '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)

  const draftIndex = cleaned.findIndex((line) => /第\s*\d+\s*稿/.test(line))
  const afterTitle = cleaned.slice(Math.max(0, draftIndex + 1))
  const draftText = cleaned.join(' ')
  const hookLine = afterTitle.find((line) => /吗|什么|为什么|你知道|有没有|误区|关键/.test(line))
  const ctaLine = [...afterTitle].reverse().find((line) => /评论|私信|预约|下单|告诉我|领取|关注/.test(line))
  const focusLines = afterTitle
    .filter((line) => line !== hookLine && line !== ctaLine)
    .filter((line) => line.length > 8)
    .slice(0, 4)
  const quoted = draftText.match(/[“"]([^”"]{8,120})[”"]/)
  const hook = hookLine ?? quoted?.[1] ?? afterTitle[0] ?? `从 AI 第 ${latestDraftNo} 稿里提炼核心开场。`

  return [
    { label: `Hook 0-3s · 第 ${latestDraftNo} 稿`, value: hook },
    {
      label: '视频重点',
      value: focusLines.length > 0
        ? focusLines.join(' ')
        : draftText.slice(0, 220) || '根据最新 AI 草稿综合判断视频要表达的核心重点。',
    },
    {
      label: '表达风格',
      value: '根据最新 AI 草稿的语气、节奏、平台和时长综合提炼，可在这里手动修正。',
    },
    {
      label: 'CTA',
      value: ctaLine ?? '最新草稿中未明显出现 CTA，可补充评论、私信、关注、预约或下单动作。',
    },
  ]
}

function extractLatestDraftScript(content: string): string {
  const draftRegex = /[\[【]?\s*第\s*(\d+)\s*稿[^\n\r]*[\]】]?([\s\S]*?)(?=(?:[\[【]?\s*第\s*\d+\s*稿)|$)/g
  const drafts = Array.from(content.matchAll(draftRegex))
  // No [第 N 稿] marker → this is a chat / clarify reply, NOT a script. Return
  // "" so the long-video length gate (send(): the too-short block) skips it.
  // reply — e.g. the reply to "你好" — get measured as a too-short script and
  // wrongly blocked with "低于 9 分钟最低线".
  if (drafts.length === 0) return ''
  const body = drafts[drafts.length - 1][2]
    .replace(/^\s*[：:]\s*/, '')
    .replace(/\n?\s*(?:字数|约\s*\d+\s*字|≈|=).*/i, '')

  // The director appends a conversational revision prompt after the script.
  // It is not narration and must never be sent to TTS. Keep the real CTA
  // (for example, “评论区告诉我……”); only remove the follow-up prompt.
  const footerMarkers = [
    '要改吗', '想改哪里', '继续改还是', '需要怎么改', '还想怎么改',
    '还需要再改', '需要再改', '需要我改', '要不要改',
    '觉得行直接', '觉得可以就', '满意就说', '满意就提交',
    '（这版改了', '(这版改了',
  ]
  const footerStart = footerMarkers
    .map((marker) => body.indexOf(marker))
    .filter((index) => index >= 0)
    .sort((left, right) => left - right)[0]

  return (footerStart === undefined ? body : body.slice(0, footerStart)).trim()
}

function buildFocusDraft(config: ShortConfig): FocusItem[] {
  const primaryPlatform = config.platforms[0] ?? '短视频平台'
  const style = config.styles.join(' + ') || '清晰直接'
  return [
    {
      label: 'Hook 0-3s',
      value: `围绕“${config.goal}”做强钩子，前三秒先抛出痛点或结果。`,
    },
    {
      label: '视频重点',
      value: `目标是${config.goal}，面向 ${primaryPlatform} 等平台，重点突出一个核心卖点，不把信息讲散。`,
    },
    {
      label: '表达风格',
      value: `整体按“${style}”来处理，时长控制在 ${config.duration}，生成 ${config.quantity} 条可测试变体。`,
    },
    {
      label: 'CTA',
      value: '结尾给一个明确动作：评论、私信、预约、领取资料或下单，后续可以按平台分别调整。',
    },
  ]
}

function longUserTextFromMessages(messages: any[]) {
  return messages
    .filter((m) => m?.role !== 'assistant' && m?.role !== 'system' && m?.role !== 'tool')
    .map((m) => typeof m.content === 'string' ? visibleUserContent(m.content) : '')
    .join('\n')
}

function inferLongDurationFromText(text: string, fallback = DEFAULT_LONG_DURATION) {
  // 长视频上限 10 分钟:所有 >10 分钟的自然语言诉求一律封顶到"约 10 分钟"
  // (脚本填不满更长的·必被"成片过短"闸判失败)。
  if (/45\s*[-~到至]\s*60\s*分钟|45\s*分钟|四十五分钟|1\s*小时|一小时/.test(text)) return DEFAULT_LONG_DURATION
  if (/30\s*[-~到至]\s*45\s*分钟|30\s*分钟|半小时|三十分钟/.test(text)) return DEFAULT_LONG_DURATION
  if (/20\s*[-~到至]\s*30\s*分钟|20\s*分钟|二十分钟/.test(text)) return DEFAULT_LONG_DURATION
  if (/12\s*[-~到至]\s*20\s*分钟|15\s*分钟|十五分钟|12\s*分钟|十二分钟/.test(text)) return DEFAULT_LONG_DURATION
  if (/8\s*[-~到至]\s*12\s*分钟|10\s*分钟|十分钟|8\s*分钟|八分钟/.test(text)) return DEFAULT_LONG_DURATION
  if (/5\s*[-~到至]\s*8\s*分钟|5\s*分钟|五分钟/.test(text)) return '5-8 分钟'
  if (/3\s*[-~到至]\s*5\s*分钟|3\s*分钟|三分钟/.test(text)) return '3-5 分钟'
  return fallback || DEFAULT_LONG_DURATION
}

function longDurationSecondsFromLabel(label: string) {
  if (/45\s*[-~到至]\s*60|45|60|一小时|1\s*小时/.test(label)) return 3600
  if (/30\s*[-~到至]\s*45|30|半小时/.test(label)) return 2700
  if (/20\s*[-~到至]\s*30|20/.test(label)) return 1800
  if (/12\s*[-~到至]\s*20|12|15/.test(label)) return 1200
  if (/5\s*[-~到至]\s*8|5/.test(label)) return 480
  if (/3\s*[-~到至]\s*5|3/.test(label)) return 300
  return DEFAULT_LONG_DURATION_SECONDS
}

function inferLongConfigFromConversation(messages: any[], fallback: LongConfig): LongConfig {
  const text = longUserTextFromMessages(messages)
  const shortLike = inferConfigFromConversation(messages, {
    ...fallback,
    platforms: LONG_PLATFORMS,
    videoFormat: DEFAULT_LONG_VIDEO_FORMAT,
    videoFormatTouched: true,
  })
  const styles = new Set<string>(fallback.styles)
  if (/教程|教学|步骤|方法|怎么|如何/.test(text)) styles.add('教程干货')
  if (/历史|故事|人物|命运|转折|反转|纪录片/.test(text)) {
    styles.add('纪录片感')
    styles.add('故事讲述')
  }
  if (/分析|观点|趋势|行业|商业|案例|拆解/.test(text)) styles.add('深度分析')
  if (/科学|原理|宇宙|物理|数学|知识|科普|解释/.test(text)) styles.add('专业解说')
  return {
    ...shortLike,
    goal: shortLike.goal || fallback.goal || LONG_VIDEO_GOALS[0],
    platforms: LONG_PLATFORMS,
    duration: inferLongDurationFromText(text, fallback.duration || DEFAULT_LONG_DURATION),
    videoFormat: DEFAULT_LONG_VIDEO_FORMAT,
    videoFormatTouched: true,
    quantity: fallback.quantity || 1,
    styles: styles.size > 0 ? Array.from(styles) : (shortLike.styles.length > 0 ? shortLike.styles : ['专业解说']),
    hydrated: true,
  }
}

function buildLongFocusDraft(config: LongConfig): FocusItem[] {
  const style = config.styles.join(' + ') || '专业解说'
  return [
    {
      label: '开场 Hook',
      value: '设计长视频开头，前 30 秒先交代冲突、悬念或清晰收益。',
    },
    {
      label: '章节主线',
      value: '面向长视频，把内容拆成清晰章节，每一章只解决一个问题，避免散。',
    },
    {
      label: '表达风格',
      value: `整体按“${style}”来处理，目标时长 ${config.duration}，节奏比短视频更稳，但每段都要有推进。`,
    },
    {
      label: '发布准备',
      value: '保留标题、简介、章节时间轴和置顶评论的生成空间，最终在发布中心生成 SEO Pack。',
    },
  ]
}

function ShortScriptFocusPanel({
  config,
  items,
  status,
  onGenerate,
  onChange,
  onConfirm,
  onStartProduction,
  generating,
}: {
  config: ShortConfig
  items: FocusItem[]
  status: FocusStatus
  onGenerate: () => void
  onChange: (next: FocusItem[]) => void
  onConfirm: () => void
  onStartProduction: () => void
  generating: boolean
}) {
  const { t } = useTranslation()
  const updateItem = (index: number, value: string) => {
    onChange(items.map((item, i) => (i === index ? { ...item, value } : item)))
  }

  if (status === 'empty') {
    return (
      <div className="script-empty-state">
        <div className="agent-side-head">
          <h3>{t('workspace.creativeFocus')}</h3>
          <span>{t('workspace.waitingAiDistill')}</span>
        </div>
        <div className="empty-focus-box">
          <strong>{t('workspace.keepEmptyForNow')}</strong>
          <p>{t('workspace.shortFocusEmptyHint')}</p>
        </div>
        <button className="btn-primary" onClick={onGenerate}>{t('workspace.distillFromCurrentInfo')}</button>
      </div>
    )
  }

  return (
    <>
      <div className="agent-side-head">
        <h3>{status === 'confirmed' ? t('workspace.confirmedFocus') : t('workspace.pendingFocus')}</h3>
        <span>{status === 'confirmed' ? t('workspace.willFlowBackToAi') : t('workspace.editable')}</span>
      </div>
      <div className="focus-params">
        <strong>{config.goal}</strong>
        <span className="sep">·</span>
        <strong>{config.duration}</strong>
        <span className="sep">·</span>
        <strong>{t('workspace.countUnit', { count: config.quantity })}</strong>
      </div>
      <p className="script-helper">{t('workspace.editParamsContextHint')}</p>
      <div className="script-block-list">
        {items.map((item, index) => (
          <label key={item.label} className="script-block script-edit-block">
            <span>{item.label}</span>
            <textarea
              value={item.value}
              onChange={(e) => updateItem(index, e.target.value)}
              rows={3}
            />
          </label>
        ))}
      </div>
      <button className="btn-primary focus-confirm-btn" onClick={status === 'confirmed' ? onStartProduction : onConfirm} disabled={generating}>
        {status === 'confirmed' ? (generating ? t('workspace.creatingTask') : t('workspace.startGenerate')) : t('workspace.confirmFocus')}
      </button>
    </>
  )
}

function ShortAgentChat({
  config,
  focusItems,
  focusStatus,
  onAiDraft,
  onUserScript,
  onClearContext,
  onStartProduction,
}: {
  config: ShortConfig
  focusItems: FocusItem[]
  focusStatus: FocusStatus
  onAiDraft: (content: string, messages: any[]) => void
  onUserScript: (content: string) => void
  onClearContext: () => void
  onStartProduction: () => void
}) {
  const { t } = useTranslation()
  const nav = useNavigate()
  const { id: channelId } = useParams()
  const sessionKey = channelId ? `${SHORTS_AGENT_SESSION_KEY}.${channelId}` : SHORTS_AGENT_SESSION_KEY
  const draftKey = channelId ? `${SHORTS_AGENT_DRAFT_KEY}.${channelId}` : SHORTS_AGENT_DRAFT_KEY
  const [session, setSession] = useState<StudioSession | null>(null)
  const [input, setInput] = useState(() => localStorage.getItem(draftKey) || '')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [scriptPatterns, setScriptPatterns] = useState<ScriptRecommendation[]>([])
  const messagesEndRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)

  const focusInput = () => {
    window.setTimeout(() => inputRef.current?.focus(), 0)
  }

  useEffect(() => {
    let cancelled = false
    async function load() {
      const savedId = localStorage.getItem(sessionKey)
      try {
        if (savedId) {
          try {
            const existing = await api.studio.getSession(savedId)
            if (!cancelled && existing.status !== 'submitted' && existing.status !== 'aborted') {
              setSession(existing)
              focusInput()
              return
            }
          } catch (inner: any) {
            // 存的会话已失效(session not found / 404)→ 清掉,下面自动新建一个,不打扰用户。
            if (inner?.response?.status !== 404) throw inner
          }
          localStorage.removeItem(sessionKey)
        }
        const created = await api.studio.createSession({ series_id: channelId })
        if (!cancelled) {
          localStorage.setItem(sessionKey, created.id)
          setSession(created)
          focusInput()
        }
      } catch (e: any) {
        if (!cancelled) setError(studioErrorText(e, t))
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [channelId, sessionKey])

  useEffect(() => {
    if (session?.id && session.status !== 'submitted' && session.status !== 'aborted') {
      localStorage.setItem(sessionKey, session.id)
    }
    if (session?.status === 'submitted' || session?.status === 'aborted') {
      localStorage.removeItem(sessionKey)
      localStorage.removeItem(draftKey)
    }
  }, [draftKey, session?.id, session?.status, sessionKey])

  useEffect(() => {
    if (input) localStorage.setItem(draftKey, input)
    else localStorage.removeItem(draftKey)
  }, [draftKey, input])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    if (session?.status !== 'submitted') focusInput()
  }, [session?.messages?.length])

  useEffect(() => {
    if (!sending && session?.status !== 'submitted') focusInput()
  }, [sending, session?.status])

  useEffect(() => {
    let cancelled = false
    async function loadPatterns() {
      if (!config.hydrated) {
        setScriptPatterns([])
        return
      }
      try {
        const res = await api.scriptIntelligence.recommendations({
          content_type: 'short_video',
          platform: config.platforms[0] || '',
          goal: config.goal || '',
          limit: 3,
        })
        if (!cancelled) setScriptPatterns(res.items)
      } catch {
        if (!cancelled) setScriptPatterns([])
      }
    }
    void loadPatterns()
    return () => {
      cancelled = true
    }
  }, [config.hydrated, config.goal, config.platforms.join('|')])

  useEffect(() => {
    if (!session || session.status !== 'submitted') return
    const pid = session.created_project_ids?.[0]
    const bid = session.created_batch_run_id
    if (pid) {
      const timer = window.setTimeout(() => nav(`/project/${pid}`), 2500)
      return () => window.clearTimeout(timer)
    }
  }, [session?.status, session?.created_project_ids, session?.created_batch_run_id, session?.series_id, nav])

  const send = async (e?: React.FormEvent) => {
    e?.preventDefault()
    if (!session || !input.trim() || sending) return
    const text = input.trim()
    // "开始执行 / 开始生成 / 出片" in chat → directly start production, so the
    // director's guidance「跟我说开始执行」actually works. Customers shouldn't
    // need to be taught — saying it just runs. (generateSelectedShort alerts if
    // there's no script yet, so this is safe.)
    if (text.length <= 12 && /(开始执行|开始生成|开始制作|开始出片|直接生成|马上生成|出片)/.test(text)) {
      setInput('')
      onStartProduction()
      return
    }
    const shouldAttachContext = config.hydrated || focusItems.length > 0
    const explicitFormat = explicitVideoFormatFromText(text)
    const effectiveConfig = {
      ...config,
      videoFormat: explicitFormat || (config.videoFormatTouched ? config.videoFormat : DEFAULT_SHORT_VIDEO_FORMAT),
    }
    const format = selectedVideoFormat(effectiveConfig)
    const machineContext = {
      module: 'short_video',
      voice: config.voiceProvider || 'azure_yunyang',
      voice_label: config.voiceLabel || '',
      voice_speed: config.voiceSpeed || DEFAULT_VOICE_SPEED,
      subtitle_language: effectiveSubtitleLanguage(config.subtitleLanguage, config.outputLanguage),
      subtitle_font_scale: config.subtitleFontScale ?? SUBTITLE_SCALE_DEFAULT,
      include_subtitles: config.includeSubtitles !== false,
      output_language: config.outputLanguage || 'zh',
      aspect_ratio: format.aspectRatio,
      output_format: format.outputFormat,
      video_format: format.label,
      platforms: config.platforms,
      duration_label: config.duration,
      count: config.quantity || 1,
    }
    const context = shouldAttachContext
      ? [
          `视频目标：${config.goal || '待确认'}`,
          `平台：${config.platforms.join(' / ') || '待确认'}`,
          `视频格式：${format.label}（${format.aspectRatio}，生成输出 ${format.outputFormat}；这是用户当前选择，除非用户明确改格式，否则不要覆盖）`,
          `时长：${config.duration || '待确认'}`,
          `数量：${config.quantity || '待确认'}`,
          `风格：${config.styles.join(' / ') || '待确认'}`,
          `配音：${config.voiceLabel || '待确认'} (${config.voiceProvider || '待确认'})`,
          `语速：${VOICE_SPEEDS.find((item) => item.value === config.voiceSpeed)?.label || '正常'} (${config.voiceSpeed || DEFAULT_VOICE_SPEED}x)`,
          `输出语言：${config.outputLanguage === 'en' ? '英文(系统在生成时把中文定稿翻成英文，请你仍用中文写稿)' : '中文'}`,
          scriptPatterns.length > 0
            ? `Script intelligence references (use structure only, do not copy wording): ${scriptPatterns.map((item) => `${item.title}: ${item.template_summary}`).join(' | ')}`
            : '',
          focusItems.length > 0
            ? `当前创作重点（${focusStatus === 'confirmed' ? '已确认' : '待确认'}）：${focusItems.map((item) => `${item.label}: ${item.value}`).join('；')}`
            : '当前创作重点：尚未提炼',
        ].join('\n')
      : ''
    let messageForAgent = shouldAttachContext
      ? `【短视频工作室上下文】\n${context}\n\n【用户最新输入】\n${text}`
      : text
    if (shouldAttachContext) {
      messageForAgent = `【MACHINE_CONTEXT_JSON】${JSON.stringify(machineContext)}【/MACHINE_CONTEXT_JSON】\n\n${messageForAgent}`
    }
    setInput('')
    setSending(true)
    setError(null)
    setSession((current) => current?.id === session.id ? withOptimisticStudioTurn(current, text, t('workspace.aiThinking')) : current)
    onUserScript(text)
    try {
      const res = await api.studio.sendMessage(session.id, messageForAgent)
      typeAssistantResponse(res.session, setSession)
      const latestAssistant = [...res.session.messages]
        .reverse()
        .find((m: any) => m.role === 'assistant' && typeof m.content === 'string')
      if (latestAssistant?.content) onAiDraft(latestAssistant.content, res.session.messages)
    } catch (err: any) {
      const message = err?.code === 'ECONNABORTED' || /timeout/i.test(err?.message || '')
        ? t('workspace.aiThinkingTimeout')
        : studioErrorText(err, t)
      setError(message)
      showPendingAssistantError(session.id, message, setSession, t('workspace.aiReplyFailedPrefix'))
    } finally {
      setSending(false)
      focusInput()
    }
  }

  const clearConversation = async () => {
    if (!(await confirmDialog(t('workspace.confirmClearShortChat')))) return
    const currentId = session?.id
    setInput('')
    setError(null)
    setSending(false)
    localStorage.removeItem(sessionKey)
    localStorage.removeItem(draftKey)
    onClearContext()
    try {
      if (currentId) await api.studio.abort(currentId)
    } catch {
      // The local clear is what matters; abort is best-effort for stale sessions.
    }
    try {
      const created = await api.studio.createSession({ series_id: channelId })
      localStorage.setItem(sessionKey, created.id)
      setSession(created)
      focusInput()
    } catch (e: any) {
      setSession(null)
      setError(studioErrorText(e, t))
    }
  }

  return (
    <div className="short-studio-chat">
      <header className="page-head short-chat-head">
        <div>
          <span className="primary-work-badge">{t('workspace.currentMainArea')}</span>
          <h1>{t('workspace.aiDirectorAssistant')}</h1>
          <p className="lede">{t('workspace.shortDirectorLede')}</p>
        </div>
        <button
          type="button"
          className="btn-secondary"
          onClick={clearConversation}
          disabled={sending}
        >
          {t('workspace.clearChat')}
        </button>
      </header>

      <div className="chat-messages">
        {!session && (
          <div className="chat-empty">
            <p>{t('workspace.initializingAiDirector')}</p>
            <p className="hint">{error || t('workspace.preparingChatWorkspace')}</p>
          </div>
        )}
        {session?.messages.length === 0 && (
          <div className="chat-empty">
            <p>{t('workspace.shortChatGreeting')}</p>
            <p className="hint">{t('workspace.shortChatGreetingHint')}</p>
          </div>
        )}
        {session?.messages.map((m, i) => (
          <ShortChatBubble key={i} message={m} />
        ))}
        <div ref={messagesEndRef} />
      </div>

      <form className="chat-input-row" onSubmit={send}>
        <input
          ref={inputRef}
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={
            session?.status === 'submitted'
              ? t('workspace.projectSubmitted')
              : sending
                ? t('workspace.continueTypingNext')
                : t('workspace.shareYourIdea')
          }
          disabled={!session || session.status === 'submitted'}
          autoFocus
        />
        <button
          type="submit"
          className="btn-primary"
          disabled={sending || !input.trim() || !session || session.status === 'submitted'}
        >
          {sending ? '...' : t('workspace.send')}
        </button>
      </form>

      {error && <div className="error-banner">{error}</div>}
    </div>
  )
}

function ShortChatBubble({ message }: { message: any }) {
  const { t } = useTranslation()
  const role = message.role
  if (role === 'tool' || role === 'system') return null
  const isAssistant = role === 'assistant'
  const isBlocked = message.blocked_by_safety
  const isPending = Boolean((message as ChatMessageUi).ui_pending)
  const isStreaming = Boolean((message as ChatMessageUi).ui_streaming)
  const content = typeof message.content === 'string'
    ? visibleUserContent(message.content)
    : message.content
  return (
    <div className={`chat-bubble ${isAssistant ? 'assistant' : 'user'}${isBlocked ? ' blocked' : ''}${isPending ? ' thinking' : ''}${isStreaming ? ' streaming' : ''}`}>
      <div className="chat-bubble-role">
        {isAssistant ? 'AI' : t('workspace.you')}
        {isBlocked && <span className="safety-tag"> · {t('workspace.safetyBlocked')}</span>}
      </div>
      <div className="chat-bubble-content">
        {content}
        {(isPending || isStreaming) && <span className="typing-cursor" />}
      </div>
      {message.tool_call && (
        <div className="chat-tool-call">
          {t('workspace.toolCall')} <code>{message.tool_call.name}</code>
          {message.tool_call.error && <span className="tool-error"> · {t('workspace.failed')}: {message.tool_call.error}</span>}
        </div>
      )}
    </div>
  )
}

const SHORT_VIDEO_GOALS = [
  '知识科普',
  '教程教学',
  '观点解读',
  '故事叙事',
  '产品引流',
  '涨粉曝光',
  '活动促销',
  '本地门店获客',
  '品牌种草',
  '课程/服务咨询',
  '私域加粉',
  '私域转化',
  '口碑信任',
  '探店测评',
  '新闻热点',
  '活动预热',
]

const SHORT_PLATFORMS = [
  'TikTok',
  'Instagram Reels',
  'YouTube Shorts',
  'Facebook Reels',
  'Douyin / 抖音',
  'Kuaishou / 快手',
  'Xiaohongshu / 小红书',
  'Bilibili 竖屏',
  'Snapchat Spotlight',
  'Pinterest Idea Pins',
  'LinkedIn 短视频',
]

// 风格逐个打磨,做好一个放一个。当前保留常用 5 个;诙谐幽默已专门调教,其余走通用、后续逐个调。
const SHORT_STYLES = [
  '真实口播',
  '诙谐幽默',
  '故事叙事',
  '强钩子痛点',
  '解释型旁白',
]

const LONG_VIDEO_GOALS = [
  '深度知识解说',
  '长篇教程教学',
  '纪录片叙事',
  '观点深度分析',
  '商业案例拆解',
  '产品长视频介绍',
  '课程内容预热',
  '频道系列内容',
  '访谈/播客剪辑',
  '历史故事解读',
  '科学原理解释',
  '行业趋势分析',
]

const LONG_PLATFORMS = ['长视频']

const LONG_STYLES = [
  '专业解说',
  '纪录片感',
  '章节化叙事',
  '深度分析',
  '故事讲述',
  '教程干货',
  '视觉隐喻',
  '案例拆解',
  '平稳旁白',
  '强开场 Hook',
  '系列频道感',
  '高信息密度',
]

const BUILTIN_TTS_VOICES: TtsVoice[] = [
  {
    voice_id: 'zh-CN-XiaoxiaoNeural',
    provider_key: 'azure_xiaoxiao',
    display_name: '晓晓 · 中文女声（省钱）',
    gender: 'female',
    language_hint: 'zh',
    description: '温暖、自然，适合大多数短视频口播。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-XiaoyiNeural',
    provider_key: 'azure_xiaoyi',
    display_name: '晓伊 · 中文女声（省钱）',
    gender: 'female',
    language_hint: 'zh',
    description: '年轻、活泼，适合生活方式和种草内容。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-YunyangNeural',
    provider_key: 'azure_yunyang',
    display_name: '云扬 · 中文男声（深沉嗓音）',
    gender: 'male',
    language_hint: 'zh',
    description: '稳重、专业，适合知识讲解和产品介绍。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-YunjianNeural',
    provider_key: 'azure_yunjian',
    display_name: '云健 · 中文男声（省钱）',
    gender: 'male',
    language_hint: 'zh',
    description: '更有力量感，适合活动、促销和情绪推进。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-YunxiNeural',
    provider_key: 'azure_yunxi',
    display_name: '云希 · 中文男声（省钱）',
    gender: 'male',
    language_hint: 'zh',
    description: '阳光、清晰，适合泛知识和品牌口播。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-XiaochenNeural',
    provider_key: 'azure_xiaochen',
    display_name: '晓辰 · 商业女声',
    gender: 'female',
    language_hint: 'zh',
    description: '明亮、干净，适合广告口播和产品介绍。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-XiaohanNeural',
    provider_key: 'azure_xiaohan',
    display_name: '晓涵 · 情绪女声',
    gender: 'female',
    language_hint: 'zh',
    description: '表达力更强，适合故事、情绪共鸣和反转内容。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-XiaomengNeural',
    provider_key: 'azure_xiaomeng',
    display_name: '晓梦 · 聊天女声',
    gender: 'female',
    language_hint: 'zh',
    description: '轻松、聊天感，适合生活方式和轻知识。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-XiaomoNeural',
    provider_key: 'azure_xiaomo',
    display_name: '晓墨 · 故事女声',
    gender: 'female',
    language_hint: 'zh',
    description: '叙事感更强，适合故事型脚本。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-XiaoqiuNeural',
    provider_key: 'azure_xiaoqiu',
    display_name: '晓秋 · 清晰女声',
    gender: 'female',
    language_hint: 'zh',
    description: '清楚、稳定，适合知识讲解。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-XiaorouNeural',
    provider_key: 'azure_xiaorou',
    display_name: '晓柔 · 温柔女声',
    gender: 'female',
    language_hint: 'zh',
    description: '柔和、轻缓，适合疗愈、家居和生活内容。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-XiaoruiNeural',
    provider_key: 'azure_xiaorui',
    display_name: '晓睿 · 沉稳女声',
    gender: 'female',
    language_hint: 'zh',
    description: '更严肃、更平稳，适合深度讲解。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-XiaoshuangNeural',
    provider_key: 'azure_xiaoshuang',
    display_name: '晓双 · 轻快女声',
    gender: 'female',
    language_hint: 'zh',
    description: '轻快、亲和，适合口播和短内容。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-XiaoyanNeural',
    provider_key: 'azure_xiaoyan',
    display_name: '晓颜 · 标准女声',
    gender: 'female',
    language_hint: 'zh',
    description: '标准、清楚，适合通用中文旁白。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-XiaoyouNeural',
    provider_key: 'azure_xiaoyou',
    display_name: '晓悠 · 可爱女声',
    gender: 'female',
    language_hint: 'zh',
    description: '更可爱、更年轻，适合轻松娱乐内容。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-YunfengNeural',
    provider_key: 'azure_yunfeng',
    display_name: '云枫 · 严肃男声',
    gender: 'male',
    language_hint: 'zh',
    description: '严肃、沉稳，适合知识和观点类内容。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-YunhaoNeural',
    provider_key: 'azure_yunhao',
    display_name: '云皓 · 广告男声',
    gender: 'male',
    language_hint: 'zh',
    description: '更有活力，适合广告、促销和品牌口播。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-YunjieNeural',
    provider_key: 'azure_yunjie',
    display_name: '云杰 · 自然男声',
    gender: 'male',
    language_hint: 'zh',
    description: '自然、平实，适合日常讲解。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-YunxiaNeural',
    provider_key: 'azure_yunxia',
    display_name: '云夏 · 年轻男声',
    gender: 'male',
    language_hint: 'zh',
    description: '年轻、清亮，适合轻知识和品牌内容。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-YunyeNeural',
    provider_key: 'azure_yunye',
    display_name: '云野 · 平静男声',
    gender: 'male',
    language_hint: 'zh',
    description: '平静、稳，适合长一点的解释视频。',
    sample_url: '',
  },
  {
    voice_id: 'zh-CN-YunzeNeural',
    provider_key: 'azure_yunze',
    display_name: '云泽 · 纪录片男声',
    gender: 'male',
    language_hint: 'zh',
    description: '纪录片感更强，适合历史、科学和深度内容。',
    sample_url: '',
  },
  {
    voice_id: 'zh-TW-HsiaoChenNeural',
    provider_key: 'azure_tw_hsiaochen',
    display_name: '小陈 · 台湾女声',
    gender: 'female',
    language_hint: 'zh-TW',
    description: '台湾普通话女声。',
    sample_url: '',
  },
  {
    voice_id: 'zh-TW-HsiaoYuNeural',
    provider_key: 'azure_tw_hsiaoyu',
    display_name: '小雨 · 台湾女声',
    gender: 'female',
    language_hint: 'zh-TW',
    description: '更柔和的台湾普通话女声。',
    sample_url: '',
  },
  {
    voice_id: 'zh-TW-YunJheNeural',
    provider_key: 'azure_tw_yunjhe',
    display_name: '云哲 · 台湾男声',
    gender: 'male',
    language_hint: 'zh-TW',
    description: '台湾普通话男声。',
    sample_url: '',
  },
  {
    voice_id: 'zh-HK-HiuGaaiNeural',
    provider_key: 'azure_hk_hiugaai',
    display_name: '晓佳 · 粤语女声',
    gender: 'female',
    language_hint: 'zh-HK',
    description: '香港粤语女声。',
    sample_url: '',
  },
  {
    voice_id: 'zh-HK-HiuMaanNeural',
    provider_key: 'azure_hk_hiumaan',
    display_name: '晓曼 · 粤语女声',
    gender: 'female',
    language_hint: 'zh-HK',
    description: '香港粤语女声，语气更柔和。',
    sample_url: '',
  },
  {
    voice_id: 'zh-HK-WanLungNeural',
    provider_key: 'azure_hk_wanlung',
    display_name: '云龙 · 粤语男声',
    gender: 'male',
    language_hint: 'zh-HK',
    description: '香港粤语男声。',
    sample_url: '',
  },
  {
    voice_id: 'en-US-AvaNeural',
    provider_key: 'azure_ava',
    display_name: 'Ava · English Female',
    gender: 'female',
    language_hint: 'en',
    description: '自然英文女声，适合海外短视频。',
    sample_url: '',
  },
  {
    voice_id: 'en-US-AndrewNeural',
    provider_key: 'azure_andrew',
    display_name: 'Andrew · English Male',
    gender: 'male',
    language_hint: 'en',
    description: '自然英文男声，适合英文解说和产品介绍。',
    sample_url: '',
  },
  {
    voice_id: 'en-US-AriaNeural',
    provider_key: 'azure_aria',
    display_name: 'Aria · 英文女声（表现力）',
    gender: 'female',
    language_hint: 'en',
    description: '表现力更强的英文女声。',
    sample_url: '',
  },
  {
    voice_id: 'en-US-BrianNeural',
    provider_key: 'azure_brian',
    display_name: 'Brian · 英文男声（沉稳）',
    gender: 'male',
    language_hint: 'en',
    description: '沉稳清楚的英文男声。',
    sample_url: '',
  },
  {
    voice_id: 'en-US-GuyNeural',
    provider_key: 'azure_guy',
    display_name: 'Guy · 英文男声（新闻感）',
    gender: 'male',
    language_hint: 'en',
    description: '更像新闻播报的英文男声。',
    sample_url: '',
  },
  {
    voice_id: 'en-US-JennyNeural',
    provider_key: 'azure_jenny',
    display_name: 'Jenny · 英文女声（助手感）',
    gender: 'female',
    language_hint: 'en',
    description: '自然、亲和的英文女声。',
    sample_url: '',
  },
  {
    voice_id: 'en-US-DavisNeural',
    provider_key: 'azure_davis',
    display_name: 'Davis · 英文男声（聊天感）',
    gender: 'male',
    language_hint: 'en',
    description: '聊天感更强的英文男声。',
    sample_url: '',
  },
  {
    voice_id: 'en-US-SteffanNeural',
    provider_key: 'azure_steffan',
    display_name: 'Steffan · 英文男声（清晰）',
    gender: 'male',
    language_hint: 'en',
    description: '清晰稳定的英文男声。',
    sample_url: '',
  },
]

function cleanVoiceDisplayName(name: string) {
  return name
    .replace(/^Azure\s+/i, '')
    .replace('（省钱默认）', '（深沉嗓音）')
    .replace('(省钱默认)', '(深沉嗓音)')
}

function cleanVoice(voice: TtsVoice): TtsVoice {
  return {
    ...voice,
    display_name: cleanVoiceDisplayName(voice.display_name),
  }
}

function mergeVoices(remote: TtsVoice[]) {
  // 服务端返回了非 azure 目录(短视频的 ElevenLabs 普通话)→ 原样信任,替换内置 azure 列表。
  const nonAzure = remote.filter((voice) => !voice.provider_key.startsWith('azure_'))
  if (nonAzure.length > 0) {
    return remote.map((voice) => cleanVoice(voice))
  }
  const byKey = new Map<string, TtsVoice>()
  const keep = (voice: TtsVoice) => voice.provider_key.startsWith('azure_')
  for (const voice of BUILTIN_TTS_VOICES.filter(keep)) byKey.set(voice.provider_key, cleanVoice(voice))
  for (const voice of remote.filter(keep)) byKey.set(voice.provider_key, cleanVoice({ ...byKey.get(voice.provider_key), ...voice }))
  return Array.from(byKey.values())
}

function ShortConfigPanel({
  config,
  onChange,
}: {
  config: ShortConfig
  onChange: (next: ShortConfig) => void
}) {
  const { t } = useTranslation()
  // 短视频工作室 → 拉短视频音色目录(flag 开时是 ElevenLabs 普通话)。
  const VOICE_CATALOG_FORMAT = 'youtube_shorts'
  const [voices, setVoices] = useState<TtsVoice[]>(mergeVoices([]))
  // 高级英语配音档:'normal'=普通(千问)/'advanced'=高级英语(ElevenLabs 英/美音)。仅英文输出可用。
  // 🚨 **档位存在 `config` 里,不是独立 state。**
  //    根因正是它当初是个独立的 `useState('normal')`,**在 config 之外**,
  //    所以整套「保存/恢复制作参数」的机制根本碰不到它。
  //    放进 config 之后,它自动跟着音色、语速那些一起存取,不用额外接线。
  //    ⚠️ 这两个面板都拿不到 `channelId`,想在面板里自己存也存不了 ——
  //       所以「放进 config」不只是更干净,是唯一可行的路。
  const voiceTier: 'normal' | 'advanced' = config.voiceTier || 'normal'
  const setVoiceTier = (v: 'normal' | 'advanced') => onChange({ ...config, voiceTier: v })
  const [playing, setPlaying] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const patchConfig = (patch: Partial<ShortConfig>) => {
    onChange({ ...config, hydrated: true, ...patch })
  }

  const startManualConfig = () => {
    onChange({
      ...config,
      goal: config.goal || SHORT_VIDEO_GOALS[0],
      platforms: config.platforms.length > 0 ? config.platforms : ['TikTok', 'Instagram Reels', 'YouTube Shorts'],
      duration: config.duration || '45-60 秒',
      videoFormat: config.videoFormat || DEFAULT_SHORT_VIDEO_FORMAT,
      videoFormatTouched: config.videoFormatTouched || false,
      quantity: config.quantity || 1,
      styles: config.styles.length > 0 ? config.styles : ['真实口播'],
      voiceProvider: config.voiceProvider || 'azure_yunyang',
      voiceLabel: config.voiceLabel || '云扬 · 中文男声（深沉嗓音）',
      voiceSpeed: config.voiceSpeed || DEFAULT_VOICE_SPEED,
      outputLanguage: config.outputLanguage || 'zh',
      hydrated: true,
    })
  }

  const onLanguageChange = (lang: 'zh' | 'en') => {
    if (lang !== 'en') setVoiceTier('normal')  // 高级配音仅英文;切走英文 → 回普通档
    // 中文，整条片子就废了 —— 他忘记手动改的代价是退钱。
    // 🚨 三个分支都要带上：配音本来就是英文的那条走 else，字幕照样可能是错的。
    const subPatch = (() => {
      const next = subtitleLanguageForOutput(lang, config.subtitleLanguage)
      return next ? { subtitleLanguage: next as SubtitleLanguage } : {}
    })()
    const currentIsEnglish = voices.find((v) => v.provider_key === config.voiceProvider)?.language_hint === 'en'
    if (lang === 'en' && !currentIsEnglish) {
      const aria = voices.find((v) => v.provider_key === 'azure_aria')
        || voices.find((v) => v.language_hint === 'en')
      patchConfig({
        outputLanguage: 'en',
        voiceProvider: aria?.provider_key || 'azure_aria',
        voiceLabel: cleanVoiceDisplayName(aria?.display_name || 'Aria'),
        ...subPatch,
      })
    } else if (lang === 'zh' && currentIsEnglish) {
      patchConfig({
        outputLanguage: 'zh',
        voiceProvider: 'azure_yunyang',
        voiceLabel: '云扬 · 中文男声（深沉嗓音）',
        ...subPatch,
      })
    } else {
      patchConfig({ outputLanguage: lang, ...subPatch })
    }
  }

  useEffect(() => {
    let cancelled = false
    // 英文输出 + 高级档 → 拉 ElevenLabs 英文目录(英/美音);否则普通目录(千问)。
    const wantAdvanced = config.outputLanguage === 'en' && voiceTier === 'advanced'
    const req = wantAdvanced
      ? api.tts.listVoices(VOICE_CATALOG_FORMAT, { tier: 'advanced', outputLanguage: 'en' })
      : api.tts.listVoices(VOICE_CATALOG_FORMAT)
    req
      .then((res) => {
        if (cancelled) return
        const merged = mergeVoices(res.voices)
        setVoices(merged)
        // 选中音色不在当前列表(切档/切语言)→ 自动落到列表第一个有效音色。
        if (config.hydrated && merged.length > 0 && !merged.some((v) => v.provider_key === config.voiceProvider)) {
          const snap = (config.outputLanguage === 'en'
            ? merged[0]
            : merged.find((v) => v.language_hint !== 'en')) || merged[0]
          if (snap) {
            patchConfig({
              voiceProvider: snap.provider_key,
              voiceLabel: cleanVoiceDisplayName(snap.display_name),
            })
          }
        }
      })
      .catch(() => {
        if (cancelled) return
        if (wantAdvanced) {
          // 高级英语目录加载失败 → 提示 + 回退普通,绝不静默按高级处理。
          void alertDialog(t('workspace.voiceTierLoadFailed'))
          setVoiceTier('normal')
        } else {
          setVoices(mergeVoices([]))
        }
      })
    return () => {
      cancelled = true
      audioRef.current?.pause()
    }
  }, [config.outputLanguage, voiceTier])

  useEffect(() => {
    localStorage.setItem('media-buddy.tts.selected-voice', config.voiceProvider)
  }, [config.voiceProvider])

  const toggleInList = (value: string, selected: string[]) => {
    return selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value]
  }

  const toggleAllPlatforms = () => {
    patchConfig({
      platforms: config.platforms.length === SHORT_PLATFORMS.length ? [] : SHORT_PLATFORMS,
    })
  }

  const previewVoice = (v: TtsVoice) => {
    if (playing === v.provider_key) {
      audioRef.current?.pause()
      audioRef.current = null
      setPlaying(null)
      return
    }
    audioRef.current?.pause()
    const audio = new Audio(v.sample_url && Math.abs((config.voiceSpeed || DEFAULT_VOICE_SPEED) - DEFAULT_VOICE_SPEED) < 0.001
      ? v.sample_url
      : api.tts.previewUrl(v.provider_key, config.voiceSpeed || DEFAULT_VOICE_SPEED, ttsPreviewText(config.outputLanguage)))
    audio.onended = () => setPlaying(null)
    audio.onerror = () => setPlaying(null)
    audioRef.current = audio
    setPlaying(v.provider_key)
    audio.play().catch(() => setPlaying(null))
  }

  // 按语速挡位试听:用当前选中的音色,现场合成该速度的预览,让客户直接感受快慢。
  const previewAtSpeed = (speed: number) => {
    const provider = config.voiceProvider || voices[0]?.provider_key
    if (!provider) return
    const key = `speed:${provider}:${speed}`
    if (playing === key) {
      audioRef.current?.pause(); audioRef.current = null; setPlaying(null); return
    }
    audioRef.current?.pause()
    // 语速试听也按输出语言取文案(英文页用英文文案;缓存按文案分开→不覆盖中文试听)。
    const audio = new Audio(api.tts.previewUrl(provider, speed, ttsPreviewText(config.outputLanguage)))
    audio.onended = () => setPlaying(null)
    audio.onerror = () => setPlaying(null)
    audioRef.current = audio
    setPlaying(key)
    audio.play().catch(() => setPlaying(null))
  }

  const selectedVoice = voices.find((v) => v.provider_key === config.voiceProvider)
  const selectedVoiceLabel = selectedVoice ? voiceDisplayName(t, selectedVoice) : cleanVoiceDisplayName(config.voiceLabel || '')

  if (!config.hydrated) {
    return (
      <>
        <h3>{t('workspace.projectInput')}</h3>
        <div className="config-empty-state">
          <strong>{t('workspace.fillAfterAiDraft')}</strong>
          <p>{t('workspace.shortConfigEmptyHint')}</p>
          <button className="btn-secondary" onClick={startManualConfig}>{t('workspace.manualSetParams')}</button>
        </div>
      </>
    )
  }

  return (
    <>
      <h3>{t('workspace.projectInput')}</h3>
      <div className="short-config-form">
        <details className="config-fold">
          <summary>
            <span>{t('workspace.platform')}</span>
            <strong>{config.platforms.length === SHORT_PLATFORMS.length ? t('workspace.allPlatforms') : config.platforms.map((item) => displayLabel(t, SHORT_PLATFORM_LABEL_KEYS, item)).join(' / ')}</strong>
          </summary>
          <div className="config-check-grid">
            <label className="config-check all">
              <input
                type="checkbox"
                checked={config.platforms.length === SHORT_PLATFORMS.length}
                onChange={toggleAllPlatforms}
              />
              {t('workspace.allPlatforms')}
            </label>
            {SHORT_PLATFORMS.map((item) => (
              <label className="config-check" key={item}>
                <input
                  type="checkbox"
                  checked={config.platforms.includes(item)}
                  onChange={() => patchConfig({ platforms: toggleInList(item, config.platforms) })}
                />
                {displayLabel(t, SHORT_PLATFORM_LABEL_KEYS, item)}
              </label>
            ))}
          </div>
        </details>

        <label className="config-row">
          <span>{t('workspace.videoFormat')}</span>
          <select value={config.videoFormat} onChange={(e) => patchConfig({ videoFormat: e.target.value, videoFormatTouched: true })}>
            {VIDEO_FORMATS.map((item) => (
              <option key={item.key} value={item.key}>{displayLabel(t, VIDEO_FORMAT_LABEL_KEYS, item.key)}</option>
            ))}
          </select>
        </label>

        <label className="config-row">
          <span>{t('workspace.duration')}</span>
          <select value={config.duration} onChange={(e) => patchConfig({ duration: e.target.value })}>
            {SHORT_DURATIONS.map((item) => (
              <option key={item} value={item}>{displayLabel(t, DURATION_LABEL_KEYS, item)}</option>
            ))}
          </select>
        </label>

        <details className="config-fold">
          <summary>
            <span>{t('workspace.style')}</span>
            <strong>{config.styles.map((item) => displayLabel(t, SHORT_STYLE_LABEL_KEYS, item)).join(' / ')}</strong>
          </summary>
          <div className="config-check-grid">
            {SHORT_STYLES.map((item) => (
              <label className="config-check" key={item}>
                <input
                  type="checkbox"
                  checked={config.styles.includes(item)}
                  onChange={() => patchConfig({ styles: toggleInList(item, config.styles) })}
                />
                {displayLabel(t, SHORT_STYLE_LABEL_KEYS, item)}
              </label>
            ))}
          </div>
        </details>

        <label className="config-row">
          <span>{t('workspace.outputLanguage')}</span>
          <select
            value={config.outputLanguage}
            onChange={(e) => onLanguageChange(e.target.value as 'zh' | 'en')}
          >
            <option value="zh">{t('workspace.langChinese')}</option>
            <option value="en">{t('workspace.langEnglish')}</option>
          </select>
        </label>

        <label className="config-row">
          <span>{t('workspace.subtitleSwitch')}</span>
          <button
            type="button"
            role="switch"
            aria-checked={config.includeSubtitles !== false}
            className={`mb-switch${config.includeSubtitles !== false ? ' is-on' : ''}`}
            onClick={() => patchConfig({ includeSubtitles: config.includeSubtitles === false })}
            title={config.includeSubtitles !== false ? t('workspace.subtitlesOn') : t('workspace.subtitlesOff')}
          >
            <span className="mb-switch-knob" />
          </button>
        </label>

        {config.includeSubtitles !== false && (
          <>
        <label className="config-row">
          <span>{t('workspace.subtitleLanguage')}</span>
          <select
            value={effectiveSubtitleLanguage(config.subtitleLanguage, config.outputLanguage)}
            onChange={(e) => patchConfig({ subtitleLanguage: e.target.value as SubtitleLanguage })}
          >
            {SUBTITLE_LANGUAGES.map((o) => (
              <option key={o.value} value={o.value}>{t(o.key)}</option>
            ))}
          </select>
        </label>

        <div className="config-row config-row-subsize">
          <SubtitleSizeSlider
            value={config.subtitleFontScale ?? SUBTITLE_SCALE_DEFAULT}
            onChange={(v) => patchConfig({ subtitleFontScale: v })}
            subtitleLanguage={effectiveSubtitleLanguage(config.subtitleLanguage, config.outputLanguage)}
            portrait={(config.videoFormat || '').includes('9_16')}
            labels={{
              title: t('workspace.subtitleFontSize'),
              hint: t('workspace.subSizeHint'),
              small: t('workspace.subSizeSmall'),
              standard: t('workspace.subSizeStandard'),
              large: t('workspace.subSizeLarge'),
              xlarge: t('workspace.subSizeXlarge'),
            }}
          />
        </div>
          </>
        )}

        {/* 背景音乐开关。默认关 —— 躲 YouTube 版权投诉,客户想要才打开。 */}
        <label className="config-row">
          <span>{t('workspace.backgroundMusic')}</span>
          <button
            type="button"
            role="switch"
            aria-checked={config.includeMusic === true}
            className={`mb-switch${config.includeMusic === true ? ' is-on' : ''}`}
            onClick={() => patchConfig({ includeMusic: !(config.includeMusic === true) })}
            title={config.includeMusic === true ? t('workspace.musicOn') : t('workspace.musicOff')}
          >
            <span className="mb-switch-knob" />
          </button>
        </label>

        <details className="config-fold voice-config" open>
          <summary>
            <span>{t('workspace.voiceover')}</span>
            <strong>{selectedVoiceLabel || t('workspace.selectVoice')}{SHOW_VOICE_SPEED ? ` · ${displayLabel(t, VOICE_SPEED_LABEL_KEYS, VOICE_SPEEDS.find((item) => item.value === config.voiceSpeed)?.label || '正常')}` : ''}</strong>
          </summary>
          {SHOW_VOICE_SPEED && (
          <div className="voice-speed-row">
            <span>{t('workspace.speechSpeed')}</span>
            <div className="voice-speed-options" role="group" aria-label={t('workspace.speechSpeed')}>
              {VOICE_SPEEDS.map((item) => {
                const active = Math.abs((config.voiceSpeed || DEFAULT_VOICE_SPEED) - item.value) < 0.001
                const previewing = playing === `speed:${config.voiceProvider || voices[0]?.provider_key}:${item.value}`
                return (
                  <div key={item.label} className={`voice-speed-opt${active ? ' active' : ''}`}>
                    <button type="button" className="voice-speed-pick" onClick={() => patchConfig({ voiceSpeed: item.value })}>
                      {displayLabel(t, VOICE_SPEED_LABEL_KEYS, item.label)}
                    </button>
                    <button
                      type="button"
                      className="voice-speed-play"
                      title={t('workspace.previewThisSpeed')}
                      aria-label={t('workspace.previewThisSpeed')}
                      onClick={() => previewAtSpeed(item.value)}
                    >
                      {previewing ? '⏸' : '▶'}
                    </button>
                  </div>
                )
              })}
            </div>
          </div>
          )}
          {config.outputLanguage === 'en' && (
            <>
              <div className="voice-tier-toggle" role="group" aria-label={t('workspace.voiceTier')}>
                <button
                  type="button"
                  className={`voice-tier-btn${voiceTier === 'normal' ? ' active' : ''}`}
                  onClick={() => setVoiceTier('normal')}
                >
                  {t('workspace.voiceTierNormal')}
                </button>
                <button
                  type="button"
                  className={`voice-tier-btn${voiceTier === 'advanced' ? ' active' : ''}`}
                  onClick={() => {
                    if (voiceTier === 'advanced') return
                    setVoiceTier('advanced')
                  }}
                >
                  {t('workspace.voiceTierAdvanced')}
                </button>
              </div>
            </>
          )}
          <div className="config-voice-list">
            {voices
              .filter((v) => config.outputLanguage === 'en' ? true : v.language_hint !== 'en')
              .map((v) => {
              const selected = config.voiceProvider === v.provider_key
              const isPlaying = playing === v.provider_key
              return (
                <div className={`config-voice-row${selected ? ' selected' : ''}`} key={v.provider_key}>
                  <label>
                    <input
                      type="radio"
                      name="short-voice"
                      checked={selected}
                      onChange={() => patchConfig({ voiceProvider: v.provider_key, voiceLabel: cleanVoiceDisplayName(v.display_name) })}
                    />
                    <span>
                      <strong>{voiceDisplayName(t, v)}</strong>
                      <em>{voiceLangText(t, v)} · {voiceDescription(t, v)}</em>
                    </span>
                  </label>
                  <button
                    type="button"
                    className="voice-preview-btn"
                    onClick={() => previewVoice(v)}
                    title={v.sample_url ? t('workspace.previewVoice') : t('workspace.generatePreview')}
                  >
                    {isPlaying ? t('workspace.stop') : t('workspace.preview')}
                  </button>
                </div>
              )
            })}
          </div>
        </details>
      </div>
    </>
  )
}

// P2-12 AI 生成知情提示:首次进视频工作台弹一次(不勾选、不阻断),之后同一用户不再打扰。
const AI_NOTICE_SEEN_KEY = 'media-buddy.ai-notice-seen'
function AiGenerationNotice() {
  const { t } = useTranslation()
  const [show, setShow] = useState(false)
  useEffect(() => {
    try {
      if (!localStorage.getItem(AI_NOTICE_SEEN_KEY)) setShow(true)
    } catch {
      /* localStorage 不可用则不弹,不阻断 */
    }
  }, [])
  if (!show) return null
  const dismiss = () => {
    try { localStorage.setItem(AI_NOTICE_SEEN_KEY, '1') } catch { /* ignore */ }
    setShow(false)
  }
  return (
    <div className="ai-notice-overlay" onClick={dismiss}>
      <div className="ai-notice-card" onClick={(e) => e.stopPropagation()}>
        <h3>{t('workspace.aiNoticeTitle')}</h3>
        <p>{t('workspace.aiNoticeBody')}</p>
        <button type="button" className="btn-primary" onClick={dismiss}>
          {t('workspace.aiNoticeGotIt')}
        </button>
      </div>
    </div>
  )
}

// AI 助手顶部模式切换:〔智能对话〕聊天出稿 · 〔执行文案〕甩定稿一字不改直出。
function AssistantModeTabs({
  mode,
  onChange,
}: {
  mode: 'chat' | 'script'
  onChange: (m: 'chat' | 'script') => void
}) {
  const { t } = useTranslation()
  return (
    <div className="assistant-mode-tabs" role="tablist">
      <button
        type="button"
        role="tab"
        aria-selected={mode === 'chat'}
        className={mode === 'chat' ? 'active' : ''}
        onClick={() => onChange('chat')}
      >
        {t('workspace.assistantModeChat')}
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={mode === 'script'}
        className={mode === 'script' ? 'active' : ''}
        onClick={() => onChange('script')}
      >
        {t('workspace.assistantModeScript')}
      </button>
    </div>
  )
}

// 「执行文案」面板:客户把定稿粘进来 → 直接出片,不过 AI、不问、一个字不改。
function ScriptExecutePanel({
  value,
  onChange,
  onSubmit,
  generating,
}: {
  value: string
  onChange: (v: string) => void
  onSubmit: () => void
  generating: boolean
}) {
  const { t } = useTranslation()
  const chars = value.replace(/\s/g, '').length
  return (
    <div className="script-execute-panel">
      <header className="page-head">
        <div>
          <h1>{t('workspace.scriptExecuteTitle')}</h1>
          <p className="lede">{t('workspace.scriptExecuteHint')}</p>
        </div>
      </header>
      <textarea
        className="script-execute-textarea"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={t('workspace.scriptExecutePlaceholder')}
        disabled={generating}
      />
      <div className="script-execute-actions">
        <span className="script-execute-count">
          {chars} {t('workspace.scriptExecuteCharsUnit')}
        </span>
        <button
          type="button"
          className="btn-primary"
          onClick={onSubmit}
          disabled={generating || !value.trim()}
        >
          {generating ? t('workspace.scriptExecuteGenerating') : t('workspace.scriptExecuteButton')}
        </button>
      </div>
    </div>
  )
}

export function ShortsWorkspace() {
  const { t } = useTranslation()
  const nav = useNavigate()
  const { id: channelId } = useParams()
  // P1-5:初始值合并「对话式」入口存过的默认(首帧即生效,不被音色 hydration 冲掉)。
  const [config, setConfig] = useState<ShortConfig>(() => ({
    goal: SHORT_VIDEO_GOALS[0],
    platforms: ['TikTok', 'Instagram Reels', 'YouTube Shorts'],
    duration: '45-60 秒',
    videoFormat: DEFAULT_SHORT_VIDEO_FORMAT,
    videoFormatTouched: false,
    quantity: 1,
    styles: ['真实口播'],
    voiceProvider: 'azure_yunyang',
    voiceLabel: '云扬 · 中文男声（深沉嗓音）',
    voiceSpeed: DEFAULT_VOICE_SPEED,
    outputLanguage: outputLanguageFromUi(),
    hydrated: true,
    // 🚨 **必须用「上次真正用的那个模式」读,不能写死 'chat'。**
    //    根因:保存走 `saveEntryDefaults(..., assistantMode, …)`(按当前模式存),
    //    这里却写死读 `'chat'` —— 客户在【粘贴文案】模式下存的设定,
    //    下次进来根本读不到。而下面那个「切模式重载」的 useEffect 带着
    //    `modeDefaultInit` 守卫会**跳过首帧**,所以也补不回来。
    //    ⚠️ 这里的模式必须和下面 `assistantMode` 的初始值**同源**
    //       (都用 `readEntryMode`),否则又会分叉。
    ...readEntryDefaults(channelId, 'short', readEntryMode(channelId, 'short')),
  }))
  const [focusItems, setFocusItems] = useState<FocusItem[]>([])
  const [focusStatus, setFocusStatus] = useState<FocusStatus>('empty')
  const [latestScript, setLatestScript] = useState('')
  const [generating, setGenerating] = useState(false)

  const regenerateFocus = () => {
    setFocusItems(buildFocusDraft(config.hydrated ? config : {
      ...config,
      goal: SHORT_VIDEO_GOALS[0],
      platforms: ['TikTok', 'Instagram Reels', 'YouTube Shorts'],
      duration: '45-60 秒',
      videoFormat: DEFAULT_SHORT_VIDEO_FORMAT,
      videoFormatTouched: false,
      quantity: 1,
      styles: ['真实口播'],
      voiceProvider: 'azure_yunyang',
      voiceLabel: '云扬 · 中文男声（深沉嗓音）',
      voiceSpeed: DEFAULT_VOICE_SPEED,
      outputLanguage: outputLanguageFromUi(),
      hydrated: true,
    }))
    setFocusStatus('draft')
  }

  const handleAiDraft = (content: string, messages: any[]) => {
    const extracted = extractFocusFromAiDraft(content)
    if (!extracted) return
    const script = extractLatestDraftScript(content)
    if (script) setLatestScript(script)
    setConfig((current) => inferConfigFromConversation(messages, current))
    setFocusItems(extracted)
    setFocusStatus('draft')
  }

  const handleUserScript = (content: string) => {
    const extracted = extractFocusFromUserScript(content)
    if (!extracted) return
    setLatestScript(content.trim())
    const pseudoMessages = [{ role: 'user', content }]
    setConfig((current) => inferConfigFromConversation(pseudoMessages, current))
    setFocusItems(extracted)
    setFocusStatus('draft')
  }

  const updateConfig = (next: ShortConfig) => {
    setConfig({ ...next, hydrated: true })
    // P1-5:自动把当前入口(对话式/粘贴文案)的制作参数记为该入口默认,下次进该入口自动恢复(无需按钮)。
    saveEntryDefaults(channelId, 'short', assistantMode, {
      duration: next.duration, voiceProvider: next.voiceProvider, voiceLabel: next.voiceLabel,
      voiceSpeed: next.voiceSpeed, outputLanguage: next.outputLanguage,
      subtitleLanguage: next.subtitleLanguage, subtitleFontScale: next.subtitleFontScale,
      includeSubtitles: next.includeSubtitles, includeMusic: next.includeMusic,
      voiceTier: next.voiceTier,
    })
    if (focusStatus !== 'empty') {
      setFocusItems(buildFocusDraft({ ...next, hydrated: true }))
      setFocusStatus('draft')
    }
  }

  const updateFocusItems = (next: FocusItem[]) => {
    setFocusItems(next)
    setFocusStatus('draft')
  }

  const confirmFocusItems = () => {
    if (focusItems.length === 0) return
    setFocusStatus('confirmed')
  }

  const clearShortsContext = () => {
    setConfig({
      goal: SHORT_VIDEO_GOALS[0],
      platforms: ['TikTok', 'Instagram Reels', 'YouTube Shorts'],
      duration: '45-60 秒',
      videoFormat: DEFAULT_SHORT_VIDEO_FORMAT,
      videoFormatTouched: false,
      quantity: 1,
      styles: ['真实口播'],
      voiceProvider: 'azure_yunyang',
      voiceLabel: '云扬 · 中文男声（深沉嗓音）',
      voiceSpeed: DEFAULT_VOICE_SPEED,
      outputLanguage: outputLanguageFromUi(),
      hydrated: true,
    })
    setFocusItems([])
    setFocusStatus('empty')
    setLatestScript('')
  }

  const generateSelectedShort = async () => {
    if (generating) return
    const script = latestScript.trim()
    if (!script) {
      void alertDialog(t('workspace.noScriptShort'))
      document.getElementById('shorts-agent-chat')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      return
    }
    const format = selectedVideoFormat({
      ...config,
      videoFormat: config.videoFormatTouched ? config.videoFormat : DEFAULT_SHORT_VIDEO_FORMAT,
    })
    const provider = config.voiceProvider || 'azure_yunyang'
    const goal = config.goal || '短视频'
    const platform = config.platforms[0] || format.label
    setGenerating(true)
    try {
      const project = await api.projects.create({
        name: `${goal} · ${platform} · ${new Date().toLocaleString()}`,
        mode: 'script',
        script_text: script,
        output_format: format.outputFormat,
        framing_style: format.framingStyle || 'fill',
        tts_provider: provider,
        tts_speed: config.voiceSpeed || DEFAULT_VOICE_SPEED,
        output_language: config.outputLanguage || 'zh',
        subtitle_language: effectiveSubtitleLanguage(config.subtitleLanguage, config.outputLanguage),
        subtitle_font_scale: config.subtitleFontScale ?? SUBTITLE_SCALE_DEFAULT,
        include_subtitles: config.includeSubtitles !== false,
        include_music: config.includeMusic === true,
        series_id: channelId,
      })
      await api.pipeline.run(project.id)
      nav(`/project/${project.id}`)
    } catch (e: any) {
      void alertDialog(apiErrorText(e))
    } finally {
      setGenerating(false)
    }
  }

  // 「执行文案」模式:客户甩定稿 → 直出,verbatim=true(后端一字不改)。不过 AI、不改稿。
  // 记住上次用的是对话式(chat)还是粘贴文案(script),回到本入口自动回到那个模式。
  const [assistantMode, setAssistantMode] = useState<'chat' | 'script'>(() => readEntryMode(channelId, 'short'))
  // P1-5:切换入口(对话式↔粘贴文案)时恢复该入口默认;两入口各存各的、互不覆盖;不动频道批量默认。
  //       首帧由 useState 初始值处理,这里跳过 mount、只在真正切模式时重载(避免音色 hydration 竞态)。
  //       保存改为自动(见 updateConfig),不再有按钮。
  const modeDefaultInit = useRef(true)
  useEffect(() => {
    if (modeDefaultInit.current) { modeDefaultInit.current = false; return }
    const d = readEntryDefaults(channelId, 'short', assistantMode)
    if (Object.keys(d).length === 0) return
    setConfig((c) => ({ ...c, ...d, hydrated: true }))
  }, [assistantMode, channelId])
  useEffect(() => {
    saveEntryMode(channelId, 'short', assistantMode)
  }, [channelId, assistantMode])
  const [verbatimScript, setVerbatimScript] = useState('')
  const generateVerbatimShort = async () => {
    if (generating) return
    const script = verbatimScript.trim()
    if (!script) {
      void alertDialog(t('workspace.scriptExecuteEmpty'))
      return
    }
    const format = selectedVideoFormat({
      ...config,
      videoFormat: config.videoFormatTouched ? config.videoFormat : DEFAULT_SHORT_VIDEO_FORMAT,
    })
    const provider = config.voiceProvider || 'azure_yunyang'
    const goal = config.goal || '短视频'
    const platform = config.platforms[0] || format.label
    setGenerating(true)
    try {
      const project = await api.projects.create({
        name: `${goal} · ${platform} · ${new Date().toLocaleString()}`,
        mode: 'script',
        script_text: script,
        verbatim: true,
        output_format: format.outputFormat,
        framing_style: format.framingStyle || 'fill',
        tts_provider: provider,
        tts_speed: config.voiceSpeed || DEFAULT_VOICE_SPEED,
        output_language: config.outputLanguage || 'zh',
        subtitle_language: effectiveSubtitleLanguage(config.subtitleLanguage, config.outputLanguage),
        subtitle_font_scale: config.subtitleFontScale ?? SUBTITLE_SCALE_DEFAULT,
        include_subtitles: config.includeSubtitles !== false,
        include_music: config.includeMusic === true,
        series_id: channelId,
      })
      await api.pipeline.run(project.id)
      nav(`/project/${project.id}`)
    } catch (e: any) {
      void alertDialog(apiErrorText(e))
    } finally {
      setGenerating(false)
    }
  }

  const shortSteps: Step[] = [
    {
      label: t('workspace.stepGoalInput'),
      done: config.hydrated,
      active: !config.hydrated,
      note: config.hydrated ? t('workspace.stepSet') : t('workspace.stepFillHere'),
      targetId: 'shorts-config-panel',
    },
    {
      label: t('workspace.stepAiDirector'),
      done: focusStatus === 'confirmed',
      active: config.hydrated && focusStatus !== 'confirmed',
      note: focusStatus === 'empty' ? t('workspace.stepKeepTalking') : focusStatus === 'draft' ? t('workspace.stepConfirmFocus') : t('workspace.stepConfirmed'),
      targetId: 'shorts-agent-chat',
    },
    {
      label: t('workspace.stepBatchGenerate'),
      active: focusStatus === 'confirmed',
      note: focusStatus === 'confirmed' ? t('workspace.stepCanStart') : t('workspace.stepAwaitFocus'),
      targetId: 'shorts-production-preview',
    },
    {
      label: t('workspace.stepPublishPrep'),
      note: t('workspace.stepAfterGenerate'),
      targetId: 'shorts-production-preview',
    },
  ]

  return (
    <>
      <WorkspaceHeader
        crumb={t('workspace.shortVideoStudio')}
        backTo={channelId ? `/channels/${channelId}` : '/channels'}
        backLabel={t('workspace.backToChannel')}
      />
      <ChannelSubTabs channelId={channelId} active="short" />
      <AiGenerationNotice />
      <div className="workspace-page">
        <AgentShell
          tone="blue"
          leftId="shorts-config-panel"
          centerId="shorts-agent-chat"
          rightId="shorts-focus-panel"
          leftTitle="短视频设置"
          leftSummary={[config.duration, config.voiceLabel].filter(Boolean).join(' · ')}
          left={
            <ShortConfigPanel config={config} onChange={updateConfig} />
          }
          center={
            <div className="assistant-center">
              <AssistantModeTabs mode={assistantMode} onChange={setAssistantMode} />
              {assistantMode === 'chat' ? (
                <ShortAgentChat
                  config={config}
                  focusItems={focusItems}
                  focusStatus={focusStatus}
                  onAiDraft={handleAiDraft}
                  onUserScript={handleUserScript}
                  onClearContext={clearShortsContext}
                  onStartProduction={generateSelectedShort}
                />
              ) : (
                <ScriptExecutePanel
                  value={verbatimScript}
                  onChange={setVerbatimScript}
                  onSubmit={generateVerbatimShort}
                  generating={generating}
                />
              )}
            </div>
          }
          right={null}
        />
      </div>
    </>
  )
}

function LongConfigPanel({
  config,
  onChange,
}: {
  config: LongConfig
  onChange: (next: LongConfig) => void
}) {
  const { t } = useTranslation()
  const VOICE_CATALOG_FORMAT = 'youtube_landscape'
  const [voices, setVoices] = useState<TtsVoice[]>(mergeVoices([]))
  // 高级英语配音档:'normal'=普通(千问)/'advanced'=高级英语(ElevenLabs 英/美音)。仅英文输出可用。
  // 🚨 **档位存在 `config` 里,不是独立 state。**
  //    根因正是它当初是个独立的 `useState('normal')`,**在 config 之外**,
  //    所以整套「保存/恢复制作参数」的机制根本碰不到它。
  //    放进 config 之后,它自动跟着音色、语速那些一起存取,不用额外接线。
  //    ⚠️ 这两个面板都拿不到 `channelId`,想在面板里自己存也存不了 ——
  //       所以「放进 config」不只是更干净,是唯一可行的路。
  const voiceTier: 'normal' | 'advanced' = config.voiceTier || 'normal'
  const setVoiceTier = (v: 'normal' | 'advanced') => onChange({ ...config, voiceTier: v })
  const [playing, setPlaying] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const patchConfig = (patch: Partial<LongConfig>) => {
    onChange({ ...config, hydrated: true, ...patch })
  }

  const startManualConfig = () => {
    onChange({
      ...config,
      goal: config.goal || LONG_VIDEO_GOALS[0],
      platforms: LONG_PLATFORMS,
      duration: config.duration || DEFAULT_LONG_DURATION,
      videoFormat: DEFAULT_LONG_VIDEO_FORMAT,
      videoFormatTouched: true,
      quantity: config.quantity || 1,
      styles: config.styles.length > 0 ? config.styles : ['专业解说'],
      voiceProvider: config.voiceProvider || 'azure_yunyang',
      voiceLabel: config.voiceLabel || '云扬 · 中文男声（深沉嗓音）',
      voiceSpeed: config.voiceSpeed || DEFAULT_VOICE_SPEED,
      outputLanguage: config.outputLanguage || 'zh',
      hydrated: true,
    })
  }

  const onLanguageChange = (lang: 'zh' | 'en') => {
    if (lang !== 'en') setVoiceTier('normal')  // 高级配音仅英文;切走英文 → 回普通档
    // 中文，整条片子就废了 —— 他忘记手动改的代价是退钱。
    // 🚨 三个分支都要带上：配音本来就是英文的那条走 else，字幕照样可能是错的。
    const subPatch = (() => {
      const next = subtitleLanguageForOutput(lang, config.subtitleLanguage)
      return next ? { subtitleLanguage: next as SubtitleLanguage } : {}
    })()
    const currentIsEnglish = voices.find((v) => v.provider_key === config.voiceProvider)?.language_hint === 'en'
    if (lang === 'en' && !currentIsEnglish) {
      const aria = voices.find((v) => v.provider_key === 'azure_aria')
        || voices.find((v) => v.language_hint === 'en')
      patchConfig({
        outputLanguage: 'en',
        voiceProvider: aria?.provider_key || 'azure_aria',
        voiceLabel: cleanVoiceDisplayName(aria?.display_name || 'Aria'),
        ...subPatch,
      })
    } else if (lang === 'zh' && currentIsEnglish) {
      patchConfig({
        outputLanguage: 'zh',
        voiceProvider: 'azure_yunyang',
        voiceLabel: '云扬 · 中文男声（深沉嗓音）',
        ...subPatch,
      })
    } else {
      patchConfig({ outputLanguage: lang, ...subPatch })
    }
  }

  useEffect(() => {
    let cancelled = false
    // 英文输出 + 高级档 → 拉 ElevenLabs 英文目录(英/美音);否则普通目录(千问)。
    const wantAdvanced = config.outputLanguage === 'en' && voiceTier === 'advanced'
    const req = wantAdvanced
      ? api.tts.listVoices(VOICE_CATALOG_FORMAT, { tier: 'advanced', outputLanguage: 'en' })
      : api.tts.listVoices(VOICE_CATALOG_FORMAT)
    req
      .then((res) => {
        if (cancelled) return
        const merged = mergeVoices(res.voices)
        setVoices(merged)
        // 选中音色不在当前列表(切档/切语言)→ 自动落到列表第一个有效音色。
        if (config.hydrated && merged.length > 0 && !merged.some((v) => v.provider_key === config.voiceProvider)) {
          const snap = (config.outputLanguage === 'en'
            ? merged[0]
            : merged.find((v) => v.language_hint !== 'en')) || merged[0]
          if (snap) {
            patchConfig({
              voiceProvider: snap.provider_key,
              voiceLabel: cleanVoiceDisplayName(snap.display_name),
            })
          }
        }
      })
      .catch(() => {
        if (cancelled) return
        if (wantAdvanced) {
          // 高级英语目录加载失败 → 提示 + 回退普通,绝不静默按高级处理。
          void alertDialog(t('workspace.voiceTierLoadFailed'))
          setVoiceTier('normal')
        } else {
          setVoices(mergeVoices([]))
        }
      })
    return () => {
      cancelled = true
      audioRef.current?.pause()
    }
  }, [config.outputLanguage, voiceTier])

  useEffect(() => {
    localStorage.setItem('media-buddy.youtube.tts.selected-voice', config.voiceProvider)
  }, [config.voiceProvider])

  const toggleInList = (value: string, selected: string[]) => {
    return selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value]
  }

  const previewVoice = (v: TtsVoice) => {
    if (playing === v.provider_key) {
      audioRef.current?.pause()
      audioRef.current = null
      setPlaying(null)
      return
    }
    audioRef.current?.pause()
    const audio = new Audio(v.sample_url && Math.abs((config.voiceSpeed || DEFAULT_VOICE_SPEED) - DEFAULT_VOICE_SPEED) < 0.001
      ? v.sample_url
      : api.tts.previewUrl(v.provider_key, config.voiceSpeed || DEFAULT_VOICE_SPEED, ttsPreviewText(config.outputLanguage)))
    audio.onended = () => setPlaying(null)
    audio.onerror = () => setPlaying(null)
    audioRef.current = audio
    setPlaying(v.provider_key)
    audio.play().catch(() => setPlaying(null))
  }

  // 按语速挡位试听:用当前选中的音色,现场合成该速度的预览,让客户直接感受快慢。
  const previewAtSpeed = (speed: number) => {
    const provider = config.voiceProvider || voices[0]?.provider_key
    if (!provider) return
    const key = `speed:${provider}:${speed}`
    if (playing === key) {
      audioRef.current?.pause(); audioRef.current = null; setPlaying(null); return
    }
    audioRef.current?.pause()
    // 语速试听也按输出语言取文案(英文页用英文文案;缓存按文案分开→不覆盖中文试听)。
    const audio = new Audio(api.tts.previewUrl(provider, speed, ttsPreviewText(config.outputLanguage)))
    audio.onended = () => setPlaying(null)
    audio.onerror = () => setPlaying(null)
    audioRef.current = audio
    setPlaying(key)
    audio.play().catch(() => setPlaying(null))
  }

  const selectedVoice = voices.find((v) => v.provider_key === config.voiceProvider)
  const selectedVoiceLabel = selectedVoice ? voiceDisplayName(t, selectedVoice) : cleanVoiceDisplayName(config.voiceLabel || '')

  if (!config.hydrated) {
    return (
      <>
        <h3>{t('workspace.longVideoInput')}</h3>
        <div className="config-empty-state">
          <strong>{t('workspace.fillAfterAiDraft')}</strong>
          <p>{t('workspace.longConfigEmptyHint')}</p>
          <button className="btn-secondary" onClick={startManualConfig}>{t('workspace.manualSetParams')}</button>
        </div>
      </>
    )
  }

  return (
    <>
      <h3>{t('workspace.longVideoInput')}</h3>
      <div className="short-config-form">
        <label className="config-row">
          <span>{t('workspace.platform')}</span>
          <select value={LONG_PLATFORMS[0]} disabled>
            {LONG_PLATFORMS.map((item) => <option key={item} value={item}>{displayLabel(t, LONG_PLATFORM_LABEL_KEYS, item)}</option>)}
          </select>
        </label>

        <label className="config-row">
          <span>{t('workspace.videoFormat')}</span>
          <select value={DEFAULT_LONG_VIDEO_FORMAT} disabled>
            {VIDEO_FORMATS.filter((item) => item.key === DEFAULT_LONG_VIDEO_FORMAT).map((item) => (
              <option key={item.key} value={item.key}>{displayLabel(t, VIDEO_FORMAT_LABEL_KEYS, item.key)}</option>
            ))}
          </select>
        </label>

        <label className="config-row">
          <span>{t('workspace.targetDuration')}</span>
          <select value={config.duration} onChange={(e) => patchConfig({ duration: e.target.value })}>
            {LONG_DURATIONS.map((item) => (
              <option key={item} value={item}>{displayLabel(t, DURATION_LABEL_KEYS, item)}</option>
            ))}
          </select>
        </label>

        <details className="config-fold">
          <summary>
            <span>{t('workspace.style')}</span>
            <strong>{config.styles.map((item) => displayLabel(t, LONG_STYLE_LABEL_KEYS, item)).join(' / ')}</strong>
          </summary>
          <div className="config-check-grid">
            {LONG_STYLES.map((item) => (
              <label className="config-check" key={item}>
                <input
                  type="checkbox"
                  checked={config.styles.includes(item)}
                  onChange={() => patchConfig({ styles: toggleInList(item, config.styles) })}
                />
                {displayLabel(t, LONG_STYLE_LABEL_KEYS, item)}
              </label>
            ))}
          </div>
        </details>

        <label className="config-row">
          <span>{t('workspace.outputLanguage')}</span>
          <select
            value={config.outputLanguage}
            onChange={(e) => onLanguageChange(e.target.value as 'zh' | 'en')}
          >
            <option value="zh">{t('workspace.langChinese')}</option>
            <option value="en">{t('workspace.langEnglish')}</option>
          </select>
        </label>

        <label className="config-row">
          <span>{t('workspace.subtitleSwitch')}</span>
          <button
            type="button"
            role="switch"
            aria-checked={config.includeSubtitles !== false}
            className={`mb-switch${config.includeSubtitles !== false ? ' is-on' : ''}`}
            onClick={() => patchConfig({ includeSubtitles: config.includeSubtitles === false })}
            title={config.includeSubtitles !== false ? t('workspace.subtitlesOn') : t('workspace.subtitlesOff')}
          >
            <span className="mb-switch-knob" />
          </button>
        </label>

        {config.includeSubtitles !== false && (
          <>
        <label className="config-row">
          <span>{t('workspace.subtitleLanguage')}</span>
          <select
            value={effectiveSubtitleLanguage(config.subtitleLanguage, config.outputLanguage)}
            onChange={(e) => patchConfig({ subtitleLanguage: e.target.value as SubtitleLanguage })}
          >
            {SUBTITLE_LANGUAGES.map((o) => (
              <option key={o.value} value={o.value}>{t(o.key)}</option>
            ))}
          </select>
        </label>

        <div className="config-row config-row-subsize">
          <SubtitleSizeSlider
            value={config.subtitleFontScale ?? SUBTITLE_SCALE_DEFAULT}
            onChange={(v) => patchConfig({ subtitleFontScale: v })}
            subtitleLanguage={effectiveSubtitleLanguage(config.subtitleLanguage, config.outputLanguage)}
            portrait={(config.videoFormat || '').includes('9_16')}
            labels={{
              title: t('workspace.subtitleFontSize'),
              hint: t('workspace.subSizeHint'),
              small: t('workspace.subSizeSmall'),
              standard: t('workspace.subSizeStandard'),
              large: t('workspace.subSizeLarge'),
              xlarge: t('workspace.subSizeXlarge'),
            }}
          />
        </div>
          </>
        )}

        {/* 背景音乐开关。默认关 —— 躲 YouTube 版权投诉,客户想要才打开。 */}
        <label className="config-row">
          <span>{t('workspace.backgroundMusic')}</span>
          <button
            type="button"
            role="switch"
            aria-checked={config.includeMusic === true}
            className={`mb-switch${config.includeMusic === true ? ' is-on' : ''}`}
            onClick={() => patchConfig({ includeMusic: !(config.includeMusic === true) })}
            title={config.includeMusic === true ? t('workspace.musicOn') : t('workspace.musicOff')}
          >
            <span className="mb-switch-knob" />
          </button>
        </label>

        <details className="config-fold voice-config" open>
          <summary>
            <span>{t('workspace.voiceover')}</span>
            <strong>{selectedVoiceLabel || t('workspace.selectVoice')}{SHOW_VOICE_SPEED ? ` · ${displayLabel(t, VOICE_SPEED_LABEL_KEYS, VOICE_SPEEDS.find((item) => item.value === config.voiceSpeed)?.label || '正常')}` : ''}</strong>
          </summary>
          {SHOW_VOICE_SPEED && (
          <div className="voice-speed-row">
            <span>{t('workspace.speechSpeed')}</span>
            <div className="voice-speed-options" role="group" aria-label={t('workspace.speechSpeed')}>
              {VOICE_SPEEDS.map((item) => {
                const active = Math.abs((config.voiceSpeed || DEFAULT_VOICE_SPEED) - item.value) < 0.001
                const previewing = playing === `speed:${config.voiceProvider || voices[0]?.provider_key}:${item.value}`
                return (
                  <div key={item.label} className={`voice-speed-opt${active ? ' active' : ''}`}>
                    <button type="button" className="voice-speed-pick" onClick={() => patchConfig({ voiceSpeed: item.value })}>
                      {displayLabel(t, VOICE_SPEED_LABEL_KEYS, item.label)}
                    </button>
                    <button
                      type="button"
                      className="voice-speed-play"
                      title={t('workspace.previewThisSpeed')}
                      aria-label={t('workspace.previewThisSpeed')}
                      onClick={() => previewAtSpeed(item.value)}
                    >
                      {previewing ? '⏸' : '▶'}
                    </button>
                  </div>
                )
              })}
            </div>
          </div>
          )}
          {config.outputLanguage === 'en' && (
            <>
              <div className="voice-tier-toggle" role="group" aria-label={t('workspace.voiceTier')}>
                <button
                  type="button"
                  className={`voice-tier-btn${voiceTier === 'normal' ? ' active' : ''}`}
                  onClick={() => setVoiceTier('normal')}
                >
                  {t('workspace.voiceTierNormal')}
                </button>
                <button
                  type="button"
                  className={`voice-tier-btn${voiceTier === 'advanced' ? ' active' : ''}`}
                  onClick={() => {
                    if (voiceTier === 'advanced') return
                    setVoiceTier('advanced')
                  }}
                >
                  {t('workspace.voiceTierAdvanced')}
                </button>
              </div>
            </>
          )}
          <div className="config-voice-list">
            {voices
              .filter((v) => config.outputLanguage === 'en' ? true : v.language_hint !== 'en')
              .map((v) => {
              const selected = config.voiceProvider === v.provider_key
              const isPlaying = playing === v.provider_key
              return (
                <div className={`config-voice-row${selected ? ' selected' : ''}`} key={v.provider_key}>
                  <label>
                    <input
                      type="radio"
                      name="long-voice"
                      checked={selected}
                      onChange={() => patchConfig({ voiceProvider: v.provider_key, voiceLabel: cleanVoiceDisplayName(v.display_name) })}
                    />
                    <span>
                      <strong>{voiceDisplayName(t, v)}</strong>
                      <em>{voiceLangText(t, v)} · {voiceDescription(t, v)}</em>
                    </span>
                  </label>
                  <button
                    type="button"
                    className="voice-preview-btn"
                    onClick={() => previewVoice(v)}
                    title={v.sample_url ? t('workspace.previewVoice') : t('workspace.generatePreview')}
                  >
                    {isPlaying ? t('workspace.stop') : t('workspace.preview')}
                  </button>
                </div>
              )
            })}
          </div>
        </details>
      </div>
    </>
  )
}

function LongAgentChat({
  config,
  focusItems,
  focusStatus,
  onAiDraft,
  onUserScript,
  onClearContext,
  onStartProduction,
}: {
  config: LongConfig
  focusItems: FocusItem[]
  focusStatus: FocusStatus
  onAiDraft: (content: string, messages: any[]) => void
  onUserScript: (content: string) => void
  onClearContext: () => void
  onStartProduction: () => void
}) {
  const { t } = useTranslation()
  const nav = useNavigate()
  const { id: channelId } = useParams()
  const sessionKey = channelId ? `${LONG_AGENT_SESSION_KEY}.${channelId}` : LONG_AGENT_SESSION_KEY
  const draftKey = channelId ? `${LONG_AGENT_DRAFT_KEY}.${channelId}` : LONG_AGENT_DRAFT_KEY
  const [session, setSession] = useState<StudioSession | null>(null)
  const [input, setInput] = useState(() => localStorage.getItem(draftKey) || '')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [scriptPatterns, setScriptPatterns] = useState<ScriptRecommendation[]>([])
  const messagesEndRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)

  const focusInput = () => {
    window.setTimeout(() => inputRef.current?.focus(), 0)
  }

  useEffect(() => {
    let cancelled = false
    async function load() {
      const savedId = localStorage.getItem(sessionKey)
      try {
        if (savedId) {
          try {
            const existing = await api.studio.getSession(savedId)
            if (!cancelled && existing.status !== 'submitted' && existing.status !== 'aborted') {
              setSession(existing)
              focusInput()
              return
            }
          } catch (inner: any) {
            // 存的会话已失效(session not found / 404)→ 清掉,下面自动新建一个,不打扰用户。
            if (inner?.response?.status !== 404) throw inner
          }
          localStorage.removeItem(sessionKey)
        }
        const created = await api.studio.createSession({ series_id: channelId })
        if (!cancelled) {
          localStorage.setItem(sessionKey, created.id)
          setSession(created)
          focusInput()
        }
      } catch (e: any) {
        if (!cancelled) setError(studioErrorText(e, t))
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [channelId, sessionKey])

  useEffect(() => {
    if (session?.id && session.status !== 'submitted' && session.status !== 'aborted') {
      localStorage.setItem(sessionKey, session.id)
    }
    if (session?.status === 'submitted' || session?.status === 'aborted') {
      localStorage.removeItem(sessionKey)
      localStorage.removeItem(draftKey)
    }
  }, [draftKey, session?.id, session?.status, sessionKey])

  useEffect(() => {
    if (input) localStorage.setItem(draftKey, input)
    else localStorage.removeItem(draftKey)
  }, [draftKey, input])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    if (session?.status !== 'submitted') focusInput()
  }, [session?.messages?.length])

  useEffect(() => {
    if (!sending && session?.status !== 'submitted') focusInput()
  }, [sending, session?.status])

  useEffect(() => {
    let cancelled = false
    async function loadPatterns() {
      if (!config.hydrated) {
        setScriptPatterns([])
        return
      }
      try {
        const res = await api.scriptIntelligence.recommendations({
          content_type: 'youtube_long',
          platform: '长视频',
          goal: '',
          limit: 3,
        })
        if (!cancelled) setScriptPatterns(res.items)
      } catch {
        if (!cancelled) setScriptPatterns([])
      }
    }
    void loadPatterns()
    return () => {
      cancelled = true
    }
  }, [config.hydrated])

  useEffect(() => {
    if (!session || session.status !== 'submitted') return
    const pid = session.created_project_ids?.[0]
    if (pid) {
      const timer = window.setTimeout(() => nav(`/project/${pid}`), 2500)
      return () => window.clearTimeout(timer)
    }
  }, [session?.status, session?.created_project_ids, nav])

  const send = async (e?: React.FormEvent) => {
    e?.preventDefault()
    if (!session || !input.trim() || sending) return
    const text = input.trim()
    // "开始执行 / 开始生成 / 出片" in chat → directly start production, so the
    // director's guidance「跟我说开始执行」works here too (no teaching needed).
    if (text.length <= 12 && /(开始执行|开始生成|开始制作|开始出片|直接生成|马上生成|出片)/.test(text)) {
      setInput('')
      onStartProduction()
      return
    }
    const shouldAttachContext = true
    const effectiveConfig = {
      ...config,
      platforms: LONG_PLATFORMS,
      videoFormat: DEFAULT_LONG_VIDEO_FORMAT,
      videoFormatTouched: true,
    }
    const format = selectedVideoFormat(effectiveConfig)
    const durationSeconds = longDurationSecondsFromLabel(config.duration || DEFAULT_LONG_DURATION)
    const machineContext = {
      module: 'youtube_long',
      voice: config.voiceProvider || 'azure_yunyang',
      voice_label: config.voiceLabel || '',
      voice_speed: config.voiceSpeed || DEFAULT_VOICE_SPEED,
      subtitle_language: effectiveSubtitleLanguage(config.subtitleLanguage, config.outputLanguage),
      subtitle_font_scale: config.subtitleFontScale ?? SUBTITLE_SCALE_DEFAULT,
      include_subtitles: config.includeSubtitles !== false,
      output_language: config.outputLanguage || 'zh',
      aspect_ratio: format.aspectRatio,
      output_width: DEFAULT_LONG_VIDEO_WIDTH,
      output_height: DEFAULT_LONG_VIDEO_HEIGHT,
      resolution: `${DEFAULT_LONG_VIDEO_WIDTH}x${DEFAULT_LONG_VIDEO_HEIGHT}`,
      output_format: 'youtube_landscape',
      video_format: format.label,
      platforms: LONG_PLATFORMS,
      duration_label: config.duration || DEFAULT_LONG_DURATION,
      duration_seconds: durationSeconds,
      target_duration_seconds: durationSeconds,
      minimum_duration_seconds: MIN_LONG_DURATION_SECONDS,
      script_skeleton: LONG_SCRIPT_SKELETON,
      count: 1,
    }
    const context = shouldAttachContext
      ? [
          '平台：长视频',
          `视频格式：横版 16:9，默认尺寸 ${DEFAULT_LONG_VIDEO_WIDTH}×${DEFAULT_LONG_VIDEO_HEIGHT}，生成输出 youtube_landscape；这是长视频模块固定默认格式，除非以后单独改长视频逻辑，否则不要改成竖屏或其他尺寸`,
          `目标时长：${config.duration || DEFAULT_LONG_DURATION}（约 ${Math.round(durationSeconds / 60)} 分钟；默认长视频稿件按 10 分钟左右写，用户明确要更长时再按用户要求加长）`,
          `最低合格线：不得低于 ${Math.round(MIN_LONG_DURATION_SECONDS / 60)} 分钟；如果第一稿不足，必须继续扩写到合格后再提交。中文口播 10 分钟通常需要约 2000-2400 字，20/30 分钟按比例加长`,
          `默认文案骨架：${LONG_SCRIPT_SKELETON}`,
          `风格：${config.styles.join(' / ') || '待确认'}`,
          `配音：${config.voiceLabel || '待确认'} (${config.voiceProvider || '待确认'})`,
          `语速：${VOICE_SPEEDS.find((item) => item.value === config.voiceSpeed)?.label || '正常'} (${config.voiceSpeed || DEFAULT_VOICE_SPEED}x)`,
          `输出语言：${config.outputLanguage === 'en' ? '英文(系统在生成时把中文定稿翻成英文，请你仍用中文写稿)' : '中文'}`,
          scriptPatterns.length > 0
            ? `Script intelligence references (use structure only, do not copy wording): ${scriptPatterns.map((item) => `${item.title}: ${item.template_summary}`).join(' | ')}`
            : '',
          focusItems.length > 0
            ? `当前长视频重点（${focusStatus === 'confirmed' ? '已确认' : '待确认'}）：${focusItems.map((item) => `${item.label}: ${item.value}`).join('；')}`
            : '当前长视频重点：尚未提炼',
        ].join('\n')
      : ''
    let messageForAgent = shouldAttachContext
      ? `【长视频工作室上下文】\n${context}\n\n【用户最新输入】\n${text}`
      : text
    if (shouldAttachContext) {
      messageForAgent = `【MACHINE_CONTEXT_JSON】${JSON.stringify(machineContext)}【/MACHINE_CONTEXT_JSON】\n\n${messageForAgent}`
    }
    setInput('')
    setSending(true)
    setError(null)
    setSession((current) => current?.id === session.id ? withOptimisticStudioTurn(current, text, t('workspace.aiThinking')) : current)
    onUserScript(text)
    try {
      const res = await api.studio.sendMessage(session.id, messageForAgent)
      const latestAssistant = [...res.session.messages]
        .reverse()
        .find((m: any) => m.role === 'assistant' && typeof m.content === 'string')
      const latestScript = latestAssistant?.content ? extractLatestDraftScript(latestAssistant.content) : ''
      const estimatedSeconds = latestScript
        ? estimateScriptSeconds(latestScript, config.voiceSpeed || DEFAULT_VOICE_SPEED)
        : 0
      if (latestAssistant?.content && latestScript && estimatedSeconds > 0 && estimatedSeconds < MIN_LONG_DURATION_SECONDS) {
        const blockedText = buildLongScriptTooShortMessage(
          latestAssistant.content,
          durationSeconds,
          t,
          config.voiceSpeed || DEFAULT_VOICE_SPEED,
        )
        const patchedSession = {
          ...res.session,
          messages: res.session.messages.map((m: any) =>
            m === latestAssistant ? { ...m, content: blockedText } : m,
          ),
        }
        typeAssistantResponse(patchedSession, setSession)
        setError(t('workspace.longScriptTooShortBlocked'))
      } else {
        typeAssistantResponse(res.session, setSession)
        if (latestAssistant?.content) onAiDraft(latestAssistant.content, res.session.messages)
      }
    } catch (err: any) {
      const message = err?.code === 'ECONNABORTED' || /timeout/i.test(err?.message || '')
        ? t('workspace.aiThinkingTimeout')
        : studioErrorText(err, t)
      setError(message)
      showPendingAssistantError(session.id, message, setSession, t('workspace.aiReplyFailedPrefix'))
    } finally {
      setSending(false)
      focusInput()
    }
  }

  const clearConversation = async () => {
    if (!(await confirmDialog(t('workspace.confirmClearLongChat')))) return
    const currentId = session?.id
    setInput('')
    setError(null)
    setSending(false)
    localStorage.removeItem(sessionKey)
    localStorage.removeItem(draftKey)
    onClearContext()
    try {
      if (currentId) await api.studio.abort(currentId)
    } catch {
      // The local clear is what matters; abort is best-effort for stale sessions.
    }
    try {
      const created = await api.studio.createSession({ series_id: channelId })
      localStorage.setItem(sessionKey, created.id)
      setSession(created)
      focusInput()
    } catch (e: any) {
      setSession(null)
      setError(studioErrorText(e, t))
    }
  }

  return (
    <div className="short-studio-chat">
      <header className="page-head short-chat-head">
        <div>
          <span className="primary-work-badge">{t('workspace.currentMainArea')}</span>
          <h1>{t('workspace.longDirectorAssistant')}</h1>
          <p className="lede">{t('workspace.longDirectorLede')}</p>
        </div>
        <button
          type="button"
          className="btn-secondary"
          onClick={clearConversation}
          disabled={sending}
        >
          {t('workspace.clearChat')}
        </button>
      </header>

      <div className="chat-messages">
        {!session && (
          <div className="chat-empty">
            <p>{t('workspace.initializingLongDirector')}</p>
            <p className="hint">{error || t('workspace.preparingLongChatWorkspace')}</p>
          </div>
        )}
        {session?.messages.length === 0 && (
          <div className="chat-empty">
            <p>{t('workspace.longChatGreeting')}</p>
            <p className="hint">{t('workspace.longChatGreetingHint')}</p>
          </div>
        )}
        {session?.messages.map((m, i) => (
          <ShortChatBubble key={i} message={m} />
        ))}
        <div ref={messagesEndRef} />
      </div>

      <form className="chat-input-row" onSubmit={send}>
        <input
          ref={inputRef}
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={
            session?.status === 'submitted'
              ? t('workspace.projectSubmitted')
              : sending
                ? t('workspace.continueTypingNext')
                : t('workspace.shareYourLongIdea')
          }
          disabled={!session || session.status === 'submitted'}
          autoFocus
        />
        <button
          type="submit"
          className="btn-primary"
          disabled={sending || !input.trim() || !session || session.status === 'submitted'}
        >
          {sending ? '...' : t('workspace.send')}
        </button>
      </form>

      {error && <div className="error-banner">{error}</div>}
    </div>
  )
}

function LongScriptFocusPanel({
  config,
  items,
  status,
  generating,
  onGenerate,
  onChange,
  onConfirm,
  onStartProduction,
}: {
  config: LongConfig
  items: FocusItem[]
  status: FocusStatus
  generating: boolean
  onGenerate: () => void
  onChange: (next: FocusItem[]) => void
  onConfirm: () => void
  onStartProduction: () => void
}) {
  const { t } = useTranslation()
  const updateItem = (index: number, value: string) => {
    onChange(items.map((item, i) => (i === index ? { ...item, value } : item)))
  }

  if (status === 'empty') {
    return (
      <div className="script-empty-state">
        <div className="agent-side-head">
          <h3>{t('workspace.longVideoFocus')}</h3>
          <span>{t('workspace.waitingAiDistill')}</span>
        </div>
        <div className="empty-focus-box">
          <strong>{t('workspace.keepEmptyForNow')}</strong>
          <p>{t('workspace.longFocusEmptyHint')}</p>
        </div>
        <button className="btn-primary" onClick={onGenerate}>{t('workspace.distillFromCurrentInfo')}</button>
      </div>
    )
  }

  return (
    <>
      <div className="agent-side-head">
        <h3>{status === 'confirmed' ? t('workspace.confirmedLongFocus') : t('workspace.pendingLongFocus')}</h3>
        <span>{status === 'confirmed' ? t('workspace.canGenerate') : t('workspace.editable')}</span>
      </div>
      <p className="script-helper">
        {t('workspace.longFocusParamsHint', { duration: config.duration })}
      </p>
      <div className="script-block-list">
        {items.map((item, index) => (
          <label key={item.label} className="script-block script-edit-block">
            <span>{item.label}</span>
            <textarea
              value={item.value}
              onChange={(e) => updateItem(index, e.target.value)}
              rows={3}
            />
          </label>
        ))}
      </div>
      <button className="btn-primary focus-confirm-btn" onClick={status === 'confirmed' ? onStartProduction : onConfirm} disabled={generating}>
        {status === 'confirmed' ? (generating ? t('workspace.creatingLongTask') : t('workspace.sendToLongProduction')) : t('workspace.confirmFocus')}
      </button>
    </>
  )
}

export function YoutubeWorkspace() {
  const { t } = useTranslation()
  const nav = useNavigate()
  const { id: channelId } = useParams()
  // P1-5:初始值直接合并「对话式」入口保存过的默认(放 useState 初始化里 → 首帧即生效,
  //       不被配置面板的音色 hydration 用旧闭包冲掉)。
  const [config, setConfig] = useState<LongConfig>(() => ({
    goal: LONG_VIDEO_GOALS[0],
    platforms: LONG_PLATFORMS,
    duration: DEFAULT_LONG_DURATION,
    videoFormat: DEFAULT_LONG_VIDEO_FORMAT,
    videoFormatTouched: true,
    quantity: 1,
    styles: ['专业解说'],
    voiceProvider: 'azure_yunyang',
    voiceLabel: '云扬 · 中文男声（深沉嗓音）',
    voiceSpeed: DEFAULT_LONG_VOICE_SPEED,
    outputLanguage: outputLanguageFromUi(),
    hydrated: true,
    // 🚨 **必须用「上次真正用的那个模式」读,不能写死 'chat'。**
    //    根因:保存走 `saveEntryDefaults(..., assistantMode, …)`(按当前模式存),
    //    这里却写死读 `'chat'` —— 客户在【粘贴文案】模式下存的设定,
    //    下次进来根本读不到。而下面那个「切模式重载」的 useEffect 带着
    //    `modeDefaultInit` 守卫会**跳过首帧**,所以也补不回来。
    //    ⚠️ 这里的模式必须和下面 `assistantMode` 的初始值**同源**
    //       (都用 `readEntryMode`),否则又会分叉。
    ...readEntryDefaults(channelId, 'long', readEntryMode(channelId, 'long')),
  }))
  const [focusItems, setFocusItems] = useState<FocusItem[]>([])
  const [focusStatus, setFocusStatus] = useState<FocusStatus>('empty')
  const [latestScript, setLatestScript] = useState('')
  const [generating, setGenerating] = useState(false)

  const hydratedLongConfig = (next: LongConfig): LongConfig => ({
    ...next,
    platforms: LONG_PLATFORMS,
    videoFormat: DEFAULT_LONG_VIDEO_FORMAT,
    videoFormatTouched: true,
    hydrated: true,
  })

  const regenerateFocus = () => {
    setFocusItems(buildLongFocusDraft(config.hydrated ? config : {
      ...config,
      goal: LONG_VIDEO_GOALS[0],
      platforms: LONG_PLATFORMS,
      duration: DEFAULT_LONG_DURATION,
      videoFormat: DEFAULT_LONG_VIDEO_FORMAT,
      videoFormatTouched: true,
      quantity: 1,
      styles: ['专业解说'],
      voiceProvider: 'azure_yunyang',
      voiceLabel: '云扬 · 中文男声（深沉嗓音）',
      voiceSpeed: DEFAULT_VOICE_SPEED,
      outputLanguage: outputLanguageFromUi(),
      hydrated: true,
    }))
    setFocusStatus('draft')
  }

  const handleAiDraft = (content: string, messages: any[]) => {
    const extracted = extractFocusFromAiDraft(content)
    if (!extracted) return
    const script = extractLatestDraftScript(content)
    if (script) setLatestScript(script)
    setConfig((current) => inferLongConfigFromConversation(messages, current))
    setFocusItems(extracted)
    setFocusStatus('draft')
  }

  const handleUserScript = (content: string) => {
    const extracted = extractFocusFromUserScript(content)
    if (!extracted) return
    setLatestScript(content.trim())
    const pseudoMessages = [{ role: 'user', content }]
    setConfig((current) => inferLongConfigFromConversation(pseudoMessages, current))
    setFocusItems(extracted)
    setFocusStatus('draft')
  }

  const updateConfig = (next: LongConfig) => {
    const hydrated = hydratedLongConfig(next)
    setConfig(hydrated)
    // P1-5:自动把当前入口(对话式/粘贴文案)的制作参数记为该入口默认,下次进该入口自动恢复(无需按钮)。
    saveEntryDefaults(channelId, 'long', assistantMode, {
      duration: hydrated.duration, voiceProvider: hydrated.voiceProvider, voiceLabel: hydrated.voiceLabel,
      voiceSpeed: hydrated.voiceSpeed, outputLanguage: hydrated.outputLanguage,
      subtitleLanguage: hydrated.subtitleLanguage, subtitleFontScale: hydrated.subtitleFontScale,
      includeSubtitles: hydrated.includeSubtitles, includeMusic: hydrated.includeMusic,
      voiceTier: hydrated.voiceTier,
    })
    if (focusStatus !== 'empty') {
      setFocusItems(buildLongFocusDraft(hydrated))
      setFocusStatus('draft')
    }
  }

  const updateFocusItems = (next: FocusItem[]) => {
    setFocusItems(next)
    setFocusStatus('draft')
  }

  const confirmFocusItems = () => {
    if (focusItems.length === 0) return
    setFocusStatus('confirmed')
  }

  const clearLongContext = () => {
    setConfig({
      goal: LONG_VIDEO_GOALS[0],
      platforms: LONG_PLATFORMS,
      duration: DEFAULT_LONG_DURATION,
      videoFormat: DEFAULT_LONG_VIDEO_FORMAT,
      videoFormatTouched: true,
      quantity: 1,
      styles: ['专业解说'],
      voiceProvider: 'azure_yunyang',
      voiceLabel: '云扬 · 中文男声（深沉嗓音）',
      voiceSpeed: DEFAULT_VOICE_SPEED,
      outputLanguage: outputLanguageFromUi(),
      hydrated: true,
    })
    setFocusItems([])
    setFocusStatus('empty')
    setLatestScript('')
  }

  const generateSelectedLong = async () => {
    if (generating) return
    const script = latestScript.trim()
    if (!script) {
      void alertDialog(t('workspace.noScriptLong'))
      document.getElementById('youtube-agent-chat')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      return
    }
    // 多长出多长:按脚本【实际长度】出片,不再拦短稿、也不用面板固定时长——
    // 客户自带稿多短都尊重(用户已确认)。
    const estimatedSeconds = estimateScriptSeconds(script, config.voiceSpeed || DEFAULT_VOICE_SPEED)
    const provider = config.voiceProvider || 'azure_yunyang'
    const durationSeconds = Math.max(20, Math.round(estimatedSeconds))
    setGenerating(true)
    try {
      const project = await api.projects.create({
        name: `长视频 · ${new Date().toLocaleString()}`,
        mode: 'script',
        script_text: script,
        output_format: 'youtube_landscape',
        duration_seconds: durationSeconds,
        tts_provider: provider,
        tts_speed: config.voiceSpeed || DEFAULT_VOICE_SPEED,
        output_language: config.outputLanguage || 'zh',
        subtitle_language: effectiveSubtitleLanguage(config.subtitleLanguage, config.outputLanguage),
        subtitle_font_scale: config.subtitleFontScale ?? SUBTITLE_SCALE_DEFAULT,
        include_subtitles: config.includeSubtitles !== false,
        include_music: config.includeMusic === true,
        series_id: channelId,
      })
      await api.pipeline.run(project.id)
      nav(`/project/${project.id}`)
    } catch (e: any) {
      void alertDialog(apiErrorText(e))
    } finally {
      setGenerating(false)
    }
  }

  // 「执行文案」模式:客户甩定稿 → 直出,verbatim=true(后端一字不改)。多长出多长。
  // 记住上次用的是对话式(chat)还是粘贴文案(script),回到本入口自动回到那个模式。
  const [assistantMode, setAssistantMode] = useState<'chat' | 'script'>(() => readEntryMode(channelId, 'long'))
  // P1-5:切换入口(对话式↔粘贴文案)时恢复该入口默认。首帧由 useState 初始值处理,
  //       这里跳过 mount、只在真正切模式时重载(避免与配置面板音色 hydration 竞态)。保存改为自动(见 updateConfig)。
  const modeDefaultInit = useRef(true)
  useEffect(() => {
    if (modeDefaultInit.current) { modeDefaultInit.current = false; return }
    const d = readEntryDefaults(channelId, 'long', assistantMode)
    if (Object.keys(d).length === 0) return
    setConfig((c) => ({ ...c, ...d, hydrated: true }))
  }, [assistantMode, channelId])
  useEffect(() => {
    saveEntryMode(channelId, 'long', assistantMode)
  }, [channelId, assistantMode])
  const [verbatimScript, setVerbatimScript] = useState('')
  const generateVerbatimLong = async () => {
    if (generating) return
    const script = verbatimScript.trim()
    if (!script) {
      void alertDialog(t('workspace.scriptExecuteEmpty'))
      return
    }
    const estimatedSeconds = estimateScriptSeconds(script, config.voiceSpeed || DEFAULT_VOICE_SPEED)
    const provider = config.voiceProvider || 'azure_yunyang'
    const durationSeconds = Math.max(20, Math.round(estimatedSeconds))
    setGenerating(true)
    try {
      const project = await api.projects.create({
        name: `长视频 · ${new Date().toLocaleString()}`,
        mode: 'script',
        script_text: script,
        verbatim: true,
        output_format: 'youtube_landscape',
        duration_seconds: durationSeconds,
        tts_provider: provider,
        tts_speed: config.voiceSpeed || DEFAULT_VOICE_SPEED,
        output_language: config.outputLanguage || 'zh',
        subtitle_language: effectiveSubtitleLanguage(config.subtitleLanguage, config.outputLanguage),
        subtitle_font_scale: config.subtitleFontScale ?? SUBTITLE_SCALE_DEFAULT,
        include_subtitles: config.includeSubtitles !== false,
        include_music: config.includeMusic === true,
        series_id: channelId,
      })
      await api.pipeline.run(project.id)
      nav(`/project/${project.id}`)
    } catch (e: any) {
      void alertDialog(apiErrorText(e))
    } finally {
      setGenerating(false)
    }
  }

  const youtubeSteps: Step[] = [
    {
      label: t('workspace.stepTopicInput'),
      done: config.hydrated,
      active: !config.hydrated,
      note: config.hydrated ? t('workspace.stepSet') : t('workspace.stepFillHere'),
      targetId: 'youtube-config-panel',
    },
    {
      label: t('workspace.stepAiDirector'),
      done: focusStatus === 'confirmed',
      active: config.hydrated && focusStatus !== 'confirmed',
      note: focusStatus === 'empty' ? t('workspace.stepKeepTalking') : focusStatus === 'draft' ? t('workspace.stepConfirmFocus') : t('workspace.stepConfirmed'),
      targetId: 'youtube-agent-chat',
    },
    {
      label: t('workspace.stepBatchGenerate'),
      active: focusStatus === 'confirmed',
      note: focusStatus === 'confirmed' ? t('workspace.stepCanStart') : t('workspace.stepAwaitFocus'),
      targetId: 'youtube-focus-panel',
    },
    {
      label: t('workspace.stepPublishPrep'),
      note: t('workspace.stepAfterGenerate'),
      targetId: 'youtube-focus-panel',
    },
  ]

  return (
    <>
      <WorkspaceHeader
        crumb={t('workspace.longVideoStudio')}
        backTo={channelId ? `/channels/${channelId}` : '/channels'}
        backLabel={t('workspace.backToChannel')}
      />
      <ChannelSubTabs channelId={channelId} active="long" />
      <AiGenerationNotice />
      <div className="workspace-page">
        <AgentShell
          tone="red"
          leftId="youtube-config-panel"
          centerId="youtube-agent-chat"
          rightId="youtube-focus-panel"
          leftTitle="长视频设置"
          leftSummary={[config.duration, config.voiceLabel].filter(Boolean).join(' · ')}
          left={
            <LongConfigPanel config={config} onChange={updateConfig} />
          }
          center={
            <div className="assistant-center">
              <AssistantModeTabs mode={assistantMode} onChange={setAssistantMode} />
              {assistantMode === 'chat' ? (
                <LongAgentChat
                  config={config}
                  focusItems={focusItems}
                  focusStatus={focusStatus}
                  onAiDraft={handleAiDraft}
                  onUserScript={handleUserScript}
                  onClearContext={clearLongContext}
                  onStartProduction={generateSelectedLong}
                />
              ) : (
                <ScriptExecutePanel
                  value={verbatimScript}
                  onChange={setVerbatimScript}
                  onSubmit={generateVerbatimLong}
                  generating={generating}
                />
              )}
            </div>
          }
          right={null}
        />
      </div>
    </>
  )
}

export function EditorWorkspace() {
  const { t } = useTranslation()
  return (
    <>
      <WorkspaceHeader
        crumb={t('workspace.smartEditingStudio')}
      />
      <div className="workspace-page">
        <EditorCockpit />
      </div>
    </>
  )
}

function EditorCockpit() {
  const { t } = useTranslation()
  const tracks = [
    { name: t('workspace.trackRawFootage'), tone: 'raw', blocks: [22, 16, 28, 12, 18, 25, 15] },
    { name: t('workspace.trackCutFootage'), tone: 'cut', blocks: [8, 4, 10, 6, 5, 9, 4] },
    { name: t('workspace.trackMainCut'), tone: 'main', blocks: [18, 20, 15, 24, 16] },
    { name: 'B-roll', tone: 'broll', blocks: [10, 12, 8, 13, 9, 11] },
    { name: t('workspace.trackMusicMood'), tone: 'music', blocks: [30, 22, 35, 18] },
    { name: t('workspace.trackSubtitles'), tone: 'subs', blocks: [6, 7, 5, 8, 6, 7, 5, 8, 6] },
  ]
  return (
    <section className="editor-cockpit">
      <aside className="agent-panel editor-left">
        <h3>{t('workspace.footageAndScript')}</h3>
        <div className="media-stack">
          {['主机位_62min.mp4', '产品B-roll.mov', '客户补充素材.mp4'].map((f, i) => (
            <div className="media-file" key={f}>
              <div className="media-thumb">{i === 0 ? '62:00' : i === 1 ? '08:42' : '14:18'}</div>
              <div><strong>{f}</strong><span>{i === 0 ? t('workspace.mainFootage') : t('workspace.supplementFootage')}</span></div>
            </div>
          ))}
        </div>
        <FieldList items={[
          [t('workspace.clientScript'), t('workspace.provided')],
          [t('workspace.targetFinalCut'), '12 分钟'],
          [t('workspace.editingStyle'), t('workspace.editingStyleValue')],
          [t('workspace.keepFocus'), t('workspace.keepFocusValue')],
        ]} />
        <button className="panel-upload">{t('workspace.addScriptOrFootage')}</button>
      </aside>

      <main className="editor-timeline-panel">
        <div className="production-head">
          <div>
            <h2>{t('workspace.aiAutoEditCockpit')}</h2>
            <p>{t('workspace.aiAutoEditDesc')}</p>
          </div>
          <button className="btn-primary">{t('workspace.startAutoEdit')}</button>
        </div>
        <div className="timeline-status">
          <strong>{t('workspace.aiEditingRange')}</strong>
          <span>{t('workspace.editingPipeline')}</span>
        </div>
        <div className="auto-timeline">
          {tracks.map((track) => (
            <div className="auto-track" key={track.name}>
              <span className="track-name">{track.name}</span>
              <div className={`track-blocks track-${track.tone}`}>
                {track.blocks.map((w, i) => <i key={`${track.name}-${i}`} style={{ flex: w }} />)}
              </div>
            </div>
          ))}
          <div className="playhead" />
        </div>
      </main>

      <aside className="agent-panel editor-actions">
        <h3>{t('workspace.aiEditingActions')}</h3>
        {[
          [t('workspace.actionDeletedCuts'), '42 段'],
          [t('workspace.actionDetectedHighlights'), '12 个'],
          [t('workspace.actionReorderedByScript'), '78%'],
          [t('workspace.actionMatchedBroll'), '18 个'],
          [t('workspace.actionAddedSubtitles'), '96 条'],
          [t('workspace.actionProcessing'), t('workspace.actionProcessingValue')],
        ].map(([label, value]) => (
          <div className="edit-action" key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
          </div>
        ))}
        <div className="version-actions">
          <button className="btn-secondary">{t('workspace.exportEditPlan')}</button>
          <button className="btn-primary">{t('workspace.applyPlanAndEdit')}</button>
        </div>
      </aside>
    </section>
  )
}

function SeoPackRow({
  pack,
  project,
  onOpen,
}: {
  pack: SeoPack
  project?: PublishProject
  onOpen: () => void
}) {
  const { t } = useTranslation()
  const moduleCount = [
    pack.title_options.length > 0,
    Boolean(pack.description),
    pack.hashtags.length > 0,
    pack.tags.length > 0,
    pack.chapters.length > 0,
    Boolean(pack.pinned_comment),
    Boolean(pack.publish_notes),
  ].filter(Boolean).length
  return (
    <button type="button" className="seo-pack-row" onClick={onOpen}>
      <strong>{pack.project_name}</strong>
      <span>{platformLabel(project?.output_format || pack.platform, t)}</span>
      <span>{t('workspace.publishModulesCount', { count: moduleCount })}</span>
      <span>{t('workspace.titlesCount', { count: pack.title_options.length })}</span>
      <em>{t('workspace.viewPublishAssets')}</em>
    </button>
  )
}

export function PublishCenter() {
  const { t } = useTranslation()
  const [searchParams] = useSearchParams()
  const { id: channelId } = useParams()
  const [projects, setProjects] = useState<PublishProject[]>([])
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [packs, setPacks] = useState<SeoPack[]>([])
  const [pickerOpen, setPickerOpen] = useState(false)
  const [loadingProjects, setLoadingProjects] = useState(false)
  const [loadingPacks, setLoadingPacks] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [activePackId, setActivePackId] = useState<string | null>(null)

  const selectedProjects = projects.filter((p) => selectedIds.includes(p.id))
  const hasExisting = selectedProjects.some((p) => p.has_seo_pack)
  const activePack = packs.find((p) => p.project_id === activePackId)
  const activeProject = activePack
    ? projects.find((p) => p.id === activePack.project_id)
    : undefined
  const focusedProjectId = searchParams.get('project')

  const loadExistingPacks = async (list: PublishProject[]) => {
    const withPack = list.filter((p) => p.has_seo_pack)
    if (withPack.length === 0) {
      setPacks([])
      return
    }
    setLoadingPacks(true)
    try {
      const loaded = await Promise.all(
        withPack.map(async (p) => {
          try {
            return await api.publish.getSeoPack(p.id)
          } catch {
            return null
          }
        }),
      )
      setPacks(loaded.filter(Boolean) as SeoPack[])
    } finally {
      setLoadingPacks(false)
    }
  }

  const loadProjects = async () => {
    setLoadingProjects(true)
    try {
      const raw = await api.publish.projects()
      const list = channelId ? raw.filter((p) => p.series_id === channelId) : raw
      setProjects(list)
      void loadExistingPacks(list)
      return list
    } finally {
      setLoadingProjects(false)
    }
  }

  useEffect(() => {
    void loadProjects()
  }, [channelId])

  useEffect(() => {
    if (!focusedProjectId) return
    if (projects.some((p) => p.id === focusedProjectId)) {
      setSelectedIds((prev) => (prev.includes(focusedProjectId) ? prev : [focusedProjectId]))
    }
    if (packs.some((p) => p.project_id === focusedProjectId)) {
      setActivePackId(focusedProjectId)
    }
  }, [focusedProjectId, projects, packs])

  const openPicker = async () => {
    await loadProjects()
    setPickerOpen(true)
  }

  const toggleProject = (id: string) => {
    setSelectedIds((prev) => (
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    ))
  }

  const generate = async () => {
    if (selectedIds.length === 0) return
    let overwrite = false
    if (hasExisting) {
      overwrite = await confirmDialog(t('workspace.confirmOverwriteSeoPack'))
      if (!overwrite) {
        setMessage(t('workspace.readExistingNoLlmCost'))
      }
    }
    setGenerating(true)
    setMessage(null)
    try {
      const res = await api.publish.generateSeoPack(selectedIds, overwrite)
      setPacks((prev) => {
        const next = new Map(prev.map((pack) => [pack.project_id, pack]))
        res.items.forEach((pack) => next.set(pack.project_id, pack))
        return Array.from(next.values())
      })
      await loadProjects()
      if (res.skipped_existing.length > 0 && !overwrite) {
        setMessage(t('workspace.skippedExistingSeoPack', { count: res.skipped_existing.length }))
      } else {
        setMessage(t('workspace.generatedSeoPack', { count: res.items.length }))
      }
    } catch (e: any) {
      setMessage(e?.response?.data?.detail || e.message || t('workspace.generateFailed'))
    } finally {
      setGenerating(false)
    }
  }

  return (
    <>
      <header className="page-header">
        <div className="crumb">
          <Link to="/channels" className="seg">{t('workspace.channels')}</Link>
          <span className="sep">/</span>
          <span className="here">{t('workspace.publishCenter')}</span>
        </div>
        <Link to={channelId ? `/channels/${channelId}` : '/channels'} className="page-back-link">{t('workspace.backToParent')}</Link>
      </header>
      <ChannelSubTabs channelId={channelId} active="publish" />
      <div className="workspace-page">
        <section className="workspace-hero workspace-green">
          <div>
            <p className="workspace-eyebrow">{t('workspace.publishPrep')}</p>
            <h1>{t('workspace.publishCenter')}</h1>
            <p>{channelId ? t('workspace.publishChannelOnly') : t('workspace.publishIntro')}</p>
          </div>
        </section>

        <section className="publish-workbench">
          <aside className="publish-panel">
            <div className="publish-panel-head">
              <span>1</span>
              <div>
                <strong>{t('workspace.selectProject')}</strong>
                <p>{t('workspace.selectProjectHint')}</p>
              </div>
            </div>
            {selectedProjects.length === 0 ? (
              <button type="button" className="publish-empty-select" onClick={openPicker}>
                {t('workspace.selectVideosToPublish')}
              </button>
            ) : (
              <div className="publish-selection-list">
                {selectedProjects.map((p) => (
                  <div key={p.id} className="publish-selected-item">
                    <strong>{p.name}</strong>
                    <span>{platformLabel(p.output_format, t)}{p.has_seo_pack ? ` · ${t('workspace.hasSeoPack')}` : ''}</span>
                  </div>
                ))}
              </div>
            )}
          </aside>

          <aside className="publish-panel">
            <div className="publish-panel-head">
              <span>2</span>
              <div>
                <strong>{t('workspace.confirmPublish')}</strong>
                <p>{t('workspace.confirmPublishHint')}</p>
              </div>
            </div>
            <button
              type="button"
              className="publish-generate-button"
              onClick={generate}
              disabled={selectedIds.length === 0 || generating}
            >
              {generating ? t('workspace.publishing') : t('workspace.confirmPublish')}
            </button>
            {message && <div className="publish-message">{message}</div>}
          </aside>

          <aside className="publish-panel">
            <div className="publish-panel-head">
              <span>3</span>
              <div>
                <strong>{t('workspace.copyPublishDraft')}</strong>
                <p>{t('workspace.copyPublishDraftHint')}</p>
              </div>
            </div>
            <div className="publish-cost-note">
              {t('workspace.publishCostNote')}
            </div>
          </aside>
        </section>

        {packs.length > 0 && (
          <section className="seo-pack-results">
            {loadingPacks && <div className="publish-message">{t('workspace.readingExistingSeoPack')}</div>}
            {packs.map((pack) => (
              <SeoPackRow
                key={pack.project_id}
                pack={pack}
                project={projects.find((p) => p.id === pack.project_id)}
                onOpen={() => setActivePackId(pack.project_id)}
              />
            ))}
          </section>
        )}
        {activePack && (
          <SeoPackDetailModal
            pack={activePack}
            project={activeProject}
            onClose={() => setActivePackId(null)}
          />
        )}
        {pickerOpen && (
          <div className="modal-backdrop" onClick={() => setPickerOpen(false)}>
            <div className="modal-content publish-picker" onClick={(e) => e.stopPropagation()}>
              <button className="modal-close" onClick={() => setPickerOpen(false)}>×</button>
              <div className="modal-body">
                <h3>{t('workspace.selectCompletedProject')}</h3>
                {loadingProjects ? (
                  <p className="hint">{t('workspace.loading')}</p>
                ) : projects.length === 0 ? (
                  <p className="hint">{t('workspace.noPublishableProjects')}</p>
                ) : (
                  <div className="publish-project-list">
                    {projects.map((p) => (
                      <label key={p.id} className="publish-project-row">
                        <input
                          type="checkbox"
                          checked={selectedIds.includes(p.id)}
                          onChange={() => toggleProject(p.id)}
                        />
                        <span>
                          <strong>{p.name}</strong>
                          <em>{platformLabel(p.output_format, t)}{p.has_seo_pack ? ` · ${t('workspace.hasSeoPack')}` : ''}</em>
                        </span>
                      </label>
                    ))}
                  </div>
                )}
                <div className="modal-actions">
                  <button type="button" className="btn-secondary" onClick={() => setPickerOpen(false)}>
                    {t('workspace.cancel')}
                  </button>
                  <button type="button" className="btn-primary" onClick={() => setPickerOpen(false)}>
                    {t('workspace.confirm')}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  )
}
