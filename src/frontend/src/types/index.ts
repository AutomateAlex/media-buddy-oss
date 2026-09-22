export type ProjectMode = 'script' | 'creative'

// the string the Studio form / pipeline sends back to the backend's
// tts_service (e.g. 'elevenlabs_rachel'); voice_id is ElevenLabs' own
// identifier (used only inside the cloud route).
export interface TtsVoice {
  voice_id: string
  provider_key: string
  display_name: string
  gender: 'female' | 'male'
  language_hint: string
  language_label?: string
  description: string
  sample_url: string
}

export interface TtsVoicesResponse {
  model: string
  voices: TtsVoice[]
}

export interface ScriptRecommendation {
  id: string
  title: string
  content_type: 'short_video' | 'youtube_long'
  score: number
  template_summary: string
  distilled_template: Record<string, unknown>
  tags: Record<string, string[]>
}

export interface ScriptRecommendationResponse {
  items: ScriptRecommendation[]
}

export interface ScriptManuscript {
  id: string
  title: string
  content_type: 'short_video' | 'youtube_long'
  language: string
  scope: string
  source: string
  raw_text: string
  template_summary?: string | null
  distilled_template: Record<string, unknown>
  industry_tags: string[]
  platform_tags: string[]
  goal_tags: string[]
  style_tags: string[]
  structure_tags: string[]
  effect_tags: string[]
  quality_score: number
  usage_count: number
  status: string
  created_at: string
  updated_at?: string | null
}

export interface ScriptManuscriptCreate {
  title: string
  raw_text: string
  content_type: 'short_video' | 'youtube_long'
  language?: string
  scope?: string
  source?: string
  industry_tags?: string[]
  platform_tags?: string[]
  goal_tags?: string[]
  style_tags?: string[]
  structure_tags?: string[]
  effect_tags?: string[]
  quality_score?: number
}

export interface ScriptListResponse {
  items: ScriptManuscript[]
  total: number
  page: number
  page_size: number
}

export interface Project {
  id: string
  name: string
  mode: ProjectMode
  status: string
  prompt?: string
  script_text?: string
  output_format: string
  duration_seconds?: number | null
  tts_provider: string
  tts_speed?: number
  include_subtitles: boolean
  output_language?: string
  current_stage?: string | null
  output_path?: string | null
  celery_task_id?: string | null
  series_id?: string | null
  // P1-7「再来一条」保真:verbatim=true=粘贴文案入口 / false=对话式;batch_run_id 非空=从批量出的。
  verbatim?: boolean
  batch_run_id?: string | null
  studio_session_id?: string | null
  created_at: string
  updated_at?: string | null
  downloaded_at?: string | null
  // 存储保留:到期日 / 已清除时间 / 收藏意向(auto_expire|keep)。
  expires_at?: string | null
  storage_purged_at?: string | null
  retention_intent?: 'auto_expire' | 'keep' | null
}

export interface ProjectCreate {
  name: string
  mode: ProjectMode
  script_text?: string
  prompt?: string
  reference_url?: string
  output_format?: string
  framing_style?: string
  duration_seconds?: number
  tts_provider?: string
  tts_speed?: number
  include_subtitles?: boolean
  include_music?: boolean
  output_language?: 'zh' | 'en'
  subtitle_language?: 'zh-Hans' | 'zh-Hant' | 'en' | 'zh-Hans+en' | 'zh-Hant+en'
  subtitle_font_scale?: number
  // 「执行文案」模式:true → 后端一字不改(跳过 CTA 合规删句)。默认/缺省 = false。
  verbatim?: boolean
  series_id?: string | null
}

export interface StageStatus {
  stage: string
  status: string
  artifacts: Record<string, unknown>
  error?: string | null
}

export interface PipelineStatus {
  project_id: string
  overall_status: string
  current_stage?: string | null
  stages: StageStatus[]
  output_path?: string | null
  celery_task_id?: string | null
  queue_position?: number | null
  queue_eta_seconds?: number | null
  pct?: number | null
}

export interface PublishProject {
  id: string
  name: string
  output_format: string
  output_path: string
  created_at: string
  has_seo_pack: boolean
  series_id?: string | null
}

export interface SeoChapter {
  time: string
  title: string
}

export interface SeoPack {
  project_id: string
  project_name: string
  platform: string
  language: string
  generated_at: string
  title_options: string[]
  description: string
  hashtags: string[]
  tags: string[]
  chapters: SeoChapter[]
  pinned_comment: string
  publish_notes: string
  source_summary: string
  estimated_cost_eur: number
}

export interface SeoPackGenerateResponse {
  items: SeoPack[]
  skipped_existing: string[]
}

export interface ToolStatusItem {
  name: string
  status: string
  provider: string
  capability: string
  runtime: string
  install_instructions: string
}

export interface SystemStatus {
  api_online: boolean
  ffmpeg_available: boolean
  worker_running: boolean
  tools: ToolStatusItem[]
  configured_tts_providers: string[]
  configured_stock_providers: string[]
}

export interface DiagnosticsReport {
  generated_at: string
  log_path: string
  log_lines: string[]
  cloud: {
    base_url: string
    desktop_authenticated: boolean
  }
  recent_projects: Array<{
    id: string
    name: string
    status: string
    current_stage?: string | null
    output_path?: string | null
    created_at?: string | null
    updated_at?: string | null
  }>
  failed_projects: Array<{
    id: string
    name: string
    status: string
    current_stage?: string | null
    updated_at?: string | null
    errors: Record<string, string>
  }>
}

// Phase 2.7a — Series
export interface Series {
  id: string
  name: string
  description?: string | null
  output_format: string
  duration_target_seconds: number
  tts_provider: string
  tts_voice?: string | null
  pipeline_mode: string
  bgm_enabled: boolean
  bgm_mood_lock?: string | null
  director_prompt?: string | null
  style_preset_id?: string | null
  industry_tag?: string | null
  nsfw_threshold: string
  forbidden_topics: string[]
  channel_rule_json?: ChannelRuleJson | null
  learned_patterns: Record<string, unknown>
  daily_video_cap: number
  daily_cost_cap_usd: number
  created_at: string
  updated_at?: string | null
}

export interface ChannelRuleJson {
  label?: string
  positioning?: string
  anchors?: string[]
  off_domain?: string[]
  subjects?: string[]
  angle_templates?: string[]
  recommendations?: string[]
  concept_channel?: boolean
  closure_rule?: string
}

// POST /api/series/design 的返回:ok=false 表示命中红线(reason 给用户看)。
export interface ChannelDesignResult {
  ok: boolean
  policy?: string
  reason?: string
  name?: string
  director_prompt?: string
  rule?: ChannelRuleJson
}

export interface SeriesCreate {
  name: string
  description?: string
  output_format?: string
  duration_target_seconds?: number
  tts_provider?: string
  tts_voice?: string
  pipeline_mode?: string
  bgm_enabled?: boolean
  bgm_mood_lock?: string
  director_prompt?: string
  style_preset_id?: string
  industry_tag?: string
  nsfw_threshold?: string
  forbidden_topics?: string[]
  channel_rule_json?: ChannelRuleJson | null
  daily_video_cap?: number
  daily_cost_cap_usd?: number
}

export interface ChannelMemory {
  series_id: string
  name: string
  positioning: string
  industry_tag?: string | null
  rule_label: string
  project_count: number
  completed_count: number
  recent_titles: string[]
  top_keywords: string[]
  good_sources: Record<string, number>
  bad_sources: Record<string, number>
  channel_rules: {
    must_stay_in_positioning: boolean
    allowed_anchors: string[]
    off_domain_examples: string[]
  }
}

export interface ChannelFitCheck {
  series_id: string
  status: 'pass' | 'warn' | 'block' | 'unknown'
  allowed: boolean
  score: number
  reason: string
  matched_anchors?: string[]
  off_domain_terms?: string[]
}

export interface ChannelDuplicateCheck {
  series_id: string
  idea: string
  risk: 'low' | 'medium' | 'high'
  is_duplicate: boolean
  guidance: string
  matches: Array<{
    project_id: string
    title: string
    status: string
    score: number
  }>
}

// Phase 2.11k — Style preset (visual identity lock for Series)
export interface StylePreset {
  id: string
  label_zh: string
  description: string
  style_block: string
  constraints_block: string
  thumbnail_emoji: string
}

// Phase 2.7c — BatchRun
export interface BatchRun {
  id: string
  series_id: string
  status: string
  requested_count: number
  completed_count: number
  failed_count: number
  moderation_blocked: number
  concurrency: number
  daily_cost_cap_usd: number
  total_cost_usd: number
  abort_reason?: string | null
  celery_task_id?: string | null
  started_at?: string | null
  finished_at?: string | null
  created_at: string
}

export interface BatchRunCreate {
  scripts?: string[]
  prompts?: string[]
  output_format?: string
  framing_style?: string
  include_music?: boolean
  tts_speed?: number
  concurrency?: number
  daily_cost_cap_usd?: number
  tts_provider?: string
  duration_seconds?: number
  output_language?: 'zh' | 'en'
  // 字幕总开关。⚠️ 后端约定:**只有显式 false 才关**,不传(undefined)= 沿用默认开。
  include_subtitles?: boolean
  subtitle_language?: 'zh-Hans' | 'zh-Hant' | 'en' | 'zh-Hans+en' | 'zh-Hant+en'
  subtitle_font_scale?: number
  allow_duplicate?: boolean
}

// Phase 2.9a — Local Asset Library
export interface LibraryClip {
  id: string
  local_path: string
  thumbnail_path?: string | null
  source?: string | null
  source_id?: string | null
  source_url?: string | null
  duration_seconds: number
  width: number
  height: number
  aspect_ratio?: string | null
  category?: string | null
  tags: string[]
  description?: string | null
  mood_tags: string[]
  motion_tags: string[]
  tag_status: string                  // pending | tagging | indexed | failed
  tag_provider?: string | null
  tag_cost_usd: number
  tag_error?: string | null
  discovered_at: string
  indexed_at?: string | null
  used_count: number
  last_used_at?: string | null
}

export interface LibraryStats {
  total: number
  by_status: Record<string, number>
  by_category: Record<string, number>
  disk_bytes: number
  tagging_cost_usd: number
}

export interface LibraryListResponse {
  items: LibraryClip[]
  total: number
  page: number
  page_size: number
}

export interface LibraryBackfillResponse {
  scanned_projects: number
  ingested_clips: number
  skipped: number
}

export interface LibraryRetryTagsResponse {
  reset_count: number
}

// Phase 2.10a — Studio Agent
export interface StudioChatMessage {
  role: 'user' | 'assistant' | 'tool' | 'system'
  content: string
  ts?: string | null
  tool_call?: {
    name?: string
    args?: Record<string, unknown>
    result?: unknown
    error?: string
  } | null
  blocked_by_safety?: boolean
  safety_reason?: string
}

export interface StudioSpec {
  video_type?: string | null
  direction?: string | null
  platform?: string | null
  aspect_ratio?: string | null
  duration_seconds?: number | null
  count?: number | null
  reference_video_url?: string | null
  reference_video_report?: Record<string, unknown> | null
  own_assets_folder?: string | null
  series_id?: string | null
  voice?: string | null
  include_subtitles?: boolean
  topics?: string[]
  scripts?: string[]
}

export interface StudioSession {
  id: string
  series_id?: string | null
  status: string
  messages: StudioChatMessage[]
  spec: StudioSpec
  created_project_ids: string[]
  created_batch_run_id?: string | null
  turn_count: number
  created_at: string
  finished_at?: string | null
}

export interface StudioSendMessageResponse {
  session: StudioSession
  last_message: StudioChatMessage
  blocked_by_safety: boolean
  safety_reason?: string | null
}

// Phase 2.9b — Audio library
export interface LibraryAudioTrack {
  id: string
  local_path: string
  source?: string | null
  source_id?: string | null
  source_url?: string | null
  title?: string | null
  license?: string | null
  duration_seconds: number
  mood?: string | null
  energy?: string | null
  narrative_role?: string | null
  tags: string[]
  description?: string | null
  tag_status: string
  discovered_at: string
  indexed_at?: string | null
  used_count: number
  last_used_at?: string | null
}

export interface LibraryAudioStats {
  total: number
  by_mood: Record<string, number>
  by_energy: Record<string, number>
  disk_bytes: number
}

export interface LibraryAudioListResponse {
  items: LibraryAudioTrack[]
  total: number
  page: number
  page_size: number
}

// Phase 2.7d — Chunks for Timeline editor
export interface ShotInfo {
  id: string
  shot_index: number
  query?: string | null
  text_critic_score?: number | null
  selected_source?: string | null
  selected_source_url?: string | null
  selected_local_path?: string | null
  selected_clip_duration?: number | null
  mm_critic_score?: number | null
  status: string
}

export interface ChunkInfo {
  id: string
  chunk_index: number
  sentence: string
  shot_type: string
  is_hero_shot: boolean
  status: string
  tts_duration_seconds?: number | null
  text_critic_avg?: number | null
  mm_critic_avg?: number | null
  retry_count: number
  shots: ShotInfo[]
}


// ── source-available build: Settings → API Keys ──
export interface SettingsKeyRow {
  name: string
  label: string
  required: boolean
  hint: string
  what: string
  configured: boolean
}
export interface SettingsKeyGroup {
  id: string
  label: string
  keys: SettingsKeyRow[]
}
