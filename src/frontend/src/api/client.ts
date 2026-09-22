import axios from 'axios'
// ⚠️ 只用来把后端错误码翻成人话。i18n/index.ts 只依赖 i18next + 文案文件，
//    不会反过来 import 这里，不成环。
import i18n from '../i18n'
import type {
  Project,
  ProjectCreate,
  PipelineStatus,
  SystemStatus,
  Series,
  SeriesCreate,
  ChannelDesignResult,
  ChannelDuplicateCheck,
  ChannelFitCheck,
  ChannelMemory,
  BatchRun,
  BatchRunCreate,
  ChunkInfo,
  LibraryClip,
  LibraryStats,
  LibraryListResponse,
  LibraryBackfillResponse,
  LibraryRetryTagsResponse,
  LibraryAudioTrack,
  LibraryAudioStats,
  LibraryAudioListResponse,
  DiagnosticsReport,
  StudioSession,
  StudioSendMessageResponse,
  StylePreset,
  TtsVoicesResponse,
  ScriptRecommendationResponse,
  ScriptListResponse,
  ScriptManuscript,
  ScriptManuscriptCreate,
  PublishProject,
  SeoPack,
  SeoPackGenerateResponse,
  SettingsKeyGroup,
} from '../types'
// Type-only import — erased at runtime, so no circular dependency with the
// hook (the hook imports `api` from here). Keeps auth/status going through
// the shared http client (baseURL + app token + Bearer interceptor).
import type { AuthStatus } from '../hooks/useAuthState'

// 上云 P3 — base URL resolution, so ONE React bundle runs everywhere:
//  1. VITE_API_BASE_URL — explicit build-time override, still wins if set.
//  2. Dev → '' (same-origin via the vite proxy).
//  3. Hosted web (served over https) → its OWN origin, so the same build
//     works on any host with no per-environment rebuild
//     VITE_API_BASE_URL or everyone gets 127.0.0.1" footgun.
//  4. Desktop (electron, file://) → the local 127.0.0.1 backend (unchanged).
const ENV_BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.trim()
function resolveBaseURL(): string {
  if (ENV_BASE_URL) return ENV_BASE_URL
  if (import.meta.env.DEV) return ''
  if (typeof window !== 'undefined' && (window.location?.protocol === 'https:' || window.location?.protocol === 'http:')) {
    return window.location.origin // the backend serves this UI itself → same origin, any port
  }
  return 'http://127.0.0.1:8000' // file:// fallback
}
const baseURL = resolveBaseURL()
const http = axios.create({ baseURL, timeout: 30000 })
/**
 * 共享的 axios 实例（已带 baseURL / X-MediaBuddyToken / web 的 Bearer）。
 *
 * ⚠️ 导出它是给 `features/*` 里的自包含模块用的 —— 那些模块**复用这个实例**
 * 但把调用定义写在自己目录里，不往这个 725 行的文件里继续堆端点。
 */
export { http as sharedHttp }
const STUDIO_MESSAGE_TIMEOUT_MS = 180000

// Same-origin build: the backend serves this UI and trusts requests from its
// own origin, so no per-request token or bearer header is needed.
export async function initApiToken(): Promise<void> {}
function _withToken(url: string): string {
  return url
}
export async function authHeaders(): Promise<Record<string, string>> {
  return {}
}

// 统一解析后端报错为可读文案。FastAPI 把 HTTPException 内容包在 { detail: ... } 里
// (detail 可能是字符串,也可能是 { error, reason, ... } 字典)。直接读 response.data 或
// alert(response.data.detail) 在字典型 detail 下会显示 [object Object] 或裸 "status code"。
// 这个 helper 解包一层、优先取后端的中文 reason,任何调用方都能拿到友好文案。
// 一次性幂等键。`crypto.randomUUID` 在旧 Electron / 非 https 页面下可能没有，
// 所以给一个够用的兜底（这只是个去重标记，不用于安全）。
function _newIdemKey(): string {
  try {
    const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto
    if (c?.randomUUID) return c.randomUUID()
  } catch { /* 忽略：往下走兜底 */ }
  return `k-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

export function apiErrorText(err: unknown, fallback = 'Request failed'): string {
  const e = err as { response?: { data?: unknown }; message?: string }
  const data = e?.response?.data
  const detail = (data && typeof data === 'object' && 'detail' in data)
    ? (data as { detail?: unknown }).detail
    : data
  if (detail && typeof detail === 'object') {
    const d = detail as {
      reason?: unknown; error?: unknown; message?: unknown
      inflight?: unknown; cap?: unknown
    }
    if (typeof d.reason === 'string' && d.reason.trim()) return d.reason
    // 🚨 人话优先，机器码垫底。
    //
    // 只有 `queue_full` 四个字母 —— 老代码只认 reason 和 error，**不认 message**，
    // 于是回退到 error 把机器码原样弹出。后端其实早写好了中文，是前端把它丢了。
    // 一次：以为没发出去 → 又建了一遍 → 同一份稿子两条片、扣两次钱。
    //
    //
    // 后端 429 返回 `{error:'queue_full', message:'你已有 N 条…'}`，
    // 但这里以前**从来不读 `message`**，只读 reason → error ——
    // 客户看到的弹窗内容就是字面量 `queue_full`，看不懂。
    // 他以为没发出去，又建了一遍 → 同一份稿子两条片、扣两次钱。
    //
    // ⚠️ 后端那句 `message` 是**写死的中文**。英文界面的客户会收到中文弹窗
    //    （和字幕语言那个坑同一类）。所以认识的码优先用 i18n，
    //    后端 message 只当兜底。
    if (typeof d.error === 'string' && d.error.trim()) {
      const key = `errors.${d.error.trim()}`
      const translated = i18n.t(key, {
        inflight: typeof d.inflight === 'number' ? d.inflight : undefined,
        cap: typeof d.cap === 'number' ? d.cap : undefined,
        defaultValue: '',
      })
      if (translated) return translated
    }
    if (typeof d.message === 'string' && d.message.trim()) return d.message
    if (typeof d.error === 'string' && d.error.trim()) return d.error
  }
  if (typeof detail === 'string' && detail.trim()) return detail
  return e?.message || fallback
}

export type SuspectLevel = 'red' | 'orange' | 'yellow'
export interface SuspectSample {
  project_id: string
  user_id: string | null
  detail: string | null
  verdict: string
  created_at: string | null
}
export interface SuspectType {
  type: string
  label: string
  level: SuspectLevel
  base_level: string
  escalated: boolean
  count: number
  customers: number
  samples: SuspectSample[]
}
export interface SuspectZone {
  level: SuspectLevel
  label: string
  types: SuspectType[]
}
export interface SuspectBoard {
  window_days: number
  total_flagged_videos: number
  zones: SuspectZone[]
}

// 操作错误看板(请求层 4xx/5xx,可疑片看板看不到的失败)。
export interface ApiErrorSample {
  user_id: string | null
  detail: string
  created_at: string | null
}
export interface ApiErrorType {
  type: string
  label: string
  level: SuspectLevel
  base_level: string
  escalated: boolean
  count: number
  customers: number
  samples: ApiErrorSample[]
}
export interface ApiErrorZone {
  level: SuspectLevel
  label: string
  types: ApiErrorType[]
}
export interface ApiErrorBoard {
  window_days: number
  total_errors: number
  zones: ApiErrorZone[]
}

export interface FailedGenSample {
  project_id: string
  user_id: string | null
  detail: string | null
  created_at: string | null
}
export interface FailedGenType {
  type: string
  label: string
  level: SuspectLevel
  base_level: string
  escalated: boolean
  count: number
  customers: number
  samples: FailedGenSample[]
}
export interface FailedGenZone {
  level: SuspectLevel
  label: string
  types: FailedGenType[]
}
export interface FailedGenBoard {
  window_days: number
  total_failed: number
  zones: FailedGenZone[]
}

export interface FaqItem {
  id: string
  question: string
  answer: string
  category: string | null
  sort_order: number
  is_active: boolean
}

export const api = {
  auth: {
    status: () => http.get<AuthStatus>('/api/auth/status').then((r) => r.data),
    uploadAvatar: (file: File) => {
      const fd = new FormData()
      fd.append('file', file)
      return http
        .post<{ avatar_url: string }>('/api/auth/avatar', fd, {
          headers: { 'Content-Type': 'multipart/form-data' },
          timeout: 60000,
        })
        .then((r) => r.data)
    },
  },
  settings: {
    listKeys: () =>
      http.get<{ groups: SettingsKeyGroup[]; missing_required: string[] }>('/api/settings/keys').then((r) => r.data),
    setKey: (name: string, value: string) =>
      http.put<{ ok: boolean }>(`/api/settings/keys/${encodeURIComponent(name)}`, { value }).then((r) => r.data),
    deleteKey: (name: string) =>
      http.delete<{ ok: boolean }>(`/api/settings/keys/${encodeURIComponent(name)}`).then((r) => r.data),
  },
  projects: {
    list: (params?: { series_id?: string; unassigned?: boolean }) =>
      http.get<Project[]>('/api/projects/', { params }).then((r) => r.data),
    // 🚨 每次调用自带一个幂等键：**同一个 HTTP 请求**被发两遍（网络重试、
    //    客户端重发）只会建出一条项目，不会变成两条片、扣两次钱。
    //
    // ⚠️ 老实说清楚它挡不住什么：
    //    - 客户过几分钟**自己主动**再建一条 → 新的一次调用 = 新键 → 照常放行（本来就该放行）
    //    - 一次调用 = 一条项目，所以批量不会被并成一条
    //    真正防「同一份稿子出两条片」的是**出片接口那道按项目查重**，不是这里。
    create: (data: ProjectCreate) =>
      http.post<Project>('/api/projects/', {
        idempotency_key: _newIdemKey(),
        ...data,
      }).then((r) => r.data),
    get: (id: string) =>
      http.get<Project>(`/api/projects/${id}`).then((r) => r.data),
    delete: (id: string) =>
      http.delete<void>(`/api/projects/${id}`).then((r) => r.data),
    stop: (id: string) =>
      http.post<Project>(`/api/projects/${id}/stop`).then((r) => r.data),
    restart: (id: string) =>
      http.post<Project>(`/api/projects/${id}/restart`).then((r) => r.data),
    assignSeries: (id: string, series_id: string | null) =>
      http.patch<Project>(`/api/projects/${id}/series`, { series_id }).then((r) => r.data),
    outputUrl: (id: string) => _withToken(`${baseURL}/api/projects/${id}/output`),
    // 强制下载(带视频标题文件名);单个下载用这个。
    downloadUrl: (id: string) => _withToken(`${baseURL}/api/projects/${id}/output?download=1`),
    // 多选下载:打包成一个 zip(只触发一个下载,手机也稳)。
    downloadZipUrl: (ids: string[]) =>
      _withToken(`${baseURL}/api/projects/download-zip?ids=${encodeURIComponent(ids.join(','))}`),
    markDownloaded: (id: string) =>
      http.post<Project>(`/api/projects/${id}/mark-downloaded`).then((r) => r.data),
    chunks: (id: string) =>
      http.get<ChunkInfo[]>(`/api/projects/${id}/chunks`).then((r) => r.data),
    setRetentionIntent: (id: string, retention_intent: 'keep' | 'auto_expire') =>
      http
        .post<Project>(`/api/projects/${id}/retention-intent`, { retention_intent })
        .then((r) => r.data),
  },
  pipeline: {
    run: (project_id: string) =>
      http
        .post<{ task_id: string; project_id: string }>('/api/pipeline/run', {
          project_id,
        })
        .then((r) => r.data),
    status: (project_id: string) =>
      http
        .get<PipelineStatus>(`/api/pipeline/status/${project_id}`)
        .then((r) => r.data),
  },
  publish: {
    projects: () =>
      http.get<PublishProject[]>('/api/publish/projects').then((r) => r.data),
    generateSeoPack: (project_ids: string[], overwrite: boolean = false) =>
      http
        .post<SeoPackGenerateResponse>('/api/publish/seo-pack', {
          project_ids,
          overwrite,
        })
        .then((r) => r.data),
    getSeoPack: (project_id: string) =>
      http.get<SeoPack>(`/api/publish/seo-pack/${project_id}`).then((r) => r.data),
  },
  system: {
    status: () =>
      http.get<SystemStatus>('/api/system/status').then((r) => r.data),
  },
  logs: {
    diagnostics: (lines: number = 300) =>
      http
        .get<DiagnosticsReport>('/api/logs/diagnostics', { params: { lines } })
        .then((r) => r.data),
  },
  series: {
    list: () => http.get<Series[]>('/api/series/').then((r) => r.data),
    create: (data: SeriesCreate) =>
      http.post<Series>('/api/series/', data).then((r) => r.data),
    // 自定义频道:一段描述 → AI 生成整套频道规则(预览,不落库)。命中红线返回 {ok:false,reason}。
    design: (description: string, language: string) =>
      http
        .post<ChannelDesignResult>('/api/series/design', { description, output_language: language }, { timeout: 120000 })
        .then((r) => r.data),
    get: (id: string) =>
      http.get<Series>(`/api/series/${id}`).then((r) => r.data),
    update: (id: string, patch: Partial<Series>) =>
      http.patch<Series>(`/api/series/${id}`, patch).then((r) => r.data),
    delete: (id: string) =>
      http.delete<void>(`/api/series/${id}`).then((r) => r.data),
    listProjects: (id: string) =>
      http.get<Project[]>(`/api/series/${id}/projects`).then((r) => r.data),
    memory: (id: string) =>
      http.get<ChannelMemory>(`/api/series/${id}/memory`).then((r) => r.data),
    dedupe: (id: string, idea: string) =>
      http
        .post<ChannelDuplicateCheck>(`/api/series/${id}/dedupe`, { idea })
        .then((r) => r.data),
    fitCheck: (id: string, idea: string, script_text?: string) =>
      http
        .post<ChannelFitCheck>(`/api/series/${id}/fit-check`, { idea, script_text })
        .then((r) => r.data),
    createBatch: (id: string, body: BatchRunCreate) =>
      http
        .post<BatchRun>(`/api/series/${id}/batch`, body)
        .then((r) => r.data),
  },
  batch: {
    get: (id: string) =>
      http.get<BatchRun>(`/api/batch/${id}`).then((r) => r.data),
    stop: (id: string) =>
      http.post<BatchRun>(`/api/batch/${id}/stop`).then((r) => r.data),
    listProjects: (id: string) =>
      http.get<Project[]>(`/api/batch/${id}/projects`).then((r) => r.data),
  },
  stylePresets: {
    list: () =>
      http
        .get<{ presets: StylePreset[] }>('/api/style-presets/')
        .then((r) => r.data.presets),
  },
  studio: {
    createSession: (body: { series_id?: string; initial_message?: string }) =>
      http.post<StudioSession>('/api/studio/sessions', body).then((r) => r.data),
    getSession: (id: string) =>
      http.get<StudioSession>(`/api/studio/sessions/${id}`).then((r) => r.data),
    sendMessage: (id: string, content: string) =>
      http
        .post<StudioSendMessageResponse>(
          `/api/studio/sessions/${id}/messages`,
          { content },
          { timeout: STUDIO_MESSAGE_TIMEOUT_MS },
        )
        .then((r) => r.data),
    abort: (id: string) =>
      http.post(`/api/studio/sessions/${id}/abort`).then((r) => r.data),
  },
  tts: {
    // opts.tier='advanced' + outputLanguage='en' → 高级英语配音的 ElevenLabs 英文音色目录。
    listVoices: (outputFormat?: string, opts?: { tier?: string; outputLanguage?: string }) =>
      http
        .get<TtsVoicesResponse>('/api/tts/voices', {
          params: {
            ...(outputFormat ? { output_format: outputFormat } : {}),
            ...(opts?.tier ? { tier: opts.tier } : {}),
            ...(opts?.outputLanguage ? { output_language: opts.outputLanguage } : {}),
          },
        })
        .then((r) => r.data),
    // text 可选:传对应语言的试听文案(千问按文本自动判语种),不传则后端用中文默认。
    previewUrl: (provider: string, speed: number = 1.0, text?: string) =>
      _withToken(`${baseURL}/api/tts/preview?provider=${encodeURIComponent(provider)}&speed=${encodeURIComponent(String(speed))}${text ? `&text=${encodeURIComponent(text)}` : ''}`),
  },
  scriptIntelligence: {
    recommendations: (params: { content_type: 'short_video' | 'youtube_long'; industry?: string; platform?: string; goal?: string; limit?: number }) =>
      http.get<ScriptRecommendationResponse>('/api/script-intelligence/recommendations', { params }).then((r) => r.data),
    list: (params: { page?: number; page_size?: number; content_type?: 'short_video' | 'youtube_long' }) =>
      http.get<ScriptListResponse>('/api/script-intelligence/manuscripts', { params }).then((r) => r.data),
    create: (data: ScriptManuscriptCreate) =>
      http.post<ScriptManuscript>('/api/script-intelligence/manuscripts', data).then((r) => r.data),
  },
}
