import type { Project, PublishProject } from '../types'

type ProjectLike = Pick<Project, 'id' | 'name' | 'output_format'> & {
  script_text?: string | null
  prompt?: string | null
  platform?: string | null
}

const INTERNAL_NAME_PATTERNS = [
  /^full pipeline(?:\s+[a-z0-9-]+)?$/i,
  /^pipeline(?:\s+[a-z0-9-]+)?$/i,
  /^smoke(?:[-\s][a-z0-9-]+)?$/i,
  /^test(?:[-\s][a-z0-9-]+)?$/i,
]

const GENERIC_NAME_PATTERNS = [
  /^长视频$/i,
  /^短视频$/i,
  /^youtube\s+long\s+video$/i,
  /^youtube\s+landscape$/i,
  /^youtube\s+shorts$/i,
]

const SENTENCE_END_RE = /[。！？!?；;]/
const OPENING_FILLER_RE = /^(你知道|你可能|你是否|有没有想过|在自然界中|今天|本期|这期|接下来|我们将|让我们|来，我们)/i
const TOPIC_SUFFIX_RE = /([一-龥A-Za-z0-9·]{1,18}(?:蛇|狼|白蚁|蚂蚁|昆虫|动物|竹子|植物|宇宙|分形|维度|黑洞|星系|地球|火星|咖啡|食谱|料理|蛋糕|面包|牛排|沙拉|汤|酱|鱼|鸡|牛肉|猪肉|米饭|面条))/i
const FOOD_TOPIC_RE = /(食谱|料理|做法|烹饪|厨房|蛋糕|面包|牛排|沙拉|汤|酱|鱼|鸡|牛肉|猪肉|米饭|面条)/
const SCIENCE_TOPIC_RE = /(宇宙|分形|维度|黑洞|星系|地球|火星|科学|数学|物理|生态|自然)/
const TRUTH_TOPIC_RE = /(真相|误解|其实|并不是|不是|低估|被忽视|鲜为人知)/

function cleanScriptLine(line: string): string {
  return line
    .replace(/^#{1,6}\s*/, '')
    .replace(/^《(.+)》$/, '$1')
    .replace(/^"(.+)"$/, '$1')
    .replace(/^“(.+)”$/, '$1')
    .replace(/^\*\*(.+)\*\*$/, '$1')
    .replace(/^\[?第\s*\d+\s*稿[^\]]*\]?\s*/i, '')
    .replace(/^标题[:：]\s*/i, '')
    .trim()
}

function scriptLines(value?: string | null): string[] {
  const raw = String(value || '').trim()
  if (!raw) return []
  return raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
}

function firstUsefulLine(value?: string | null): string {
  const lines = scriptLines(value)
  for (const line of lines.slice(0, 12)) {
    const cleaned = cleanScriptLine(line)
    if (!cleaned) continue
    if (/^(开场|第一幕|第二幕|chapter\s*\d+|part\s*\d+)/i.test(cleaned)) continue
    if (cleaned.length <= 2) continue
    return truncateName(cleaned)
  }
  return ''
}

function explicitTemporaryTitle(value?: string | null): string {
  for (const line of scriptLines(value).slice(0, 10)) {
    const cleaned = cleanScriptLine(line)
    if (!cleaned || cleaned.length <= 2) continue
    if (/^(开场|第一幕|第二幕|第三幕|chapter\s*\d+|part\s*\d+)/i.test(cleaned)) continue
    const wasMarkedTitle = /^#{1,6}\s*/.test(line) || /^《.+》$/.test(line.trim()) || /^标题[:：]/i.test(line)
    const looksLikeShortTitle = cleaned.length <= 24 && !SENTENCE_END_RE.test(cleaned) && !OPENING_FILLER_RE.test(cleaned)
    if (wasMarkedTitle || looksLikeShortTitle) return truncateName(cleaned)
  }
  return ''
}

function normalizeTopic(raw: string): string {
  return raw
    .replace(/^(一只|一种|一个|一群|这个|这种|那种|这些|那些|所谓的)/, '')
    .replace(/[，,。！？!?；;：:].*$/, '')
    .replace(/(?:这一名字|这个名字|这种生物|这种动物|这个角色).*$/, '')
    .trim()
}

function deriveTemporaryTitle(value?: string | null): string {
  const text = scriptLines(value).slice(0, 10).map(cleanScriptLine).join('。').slice(0, 700)
  if (!text) return ''

  const topicPatterns = [
    /你(?:可能)?以为([^，,。！？!?]{1,18})(?:只是|就是|不过是|只会)/,
    /(?:关于|了解|认识|走进|揭秘|解读)([^，,。！？!?]{2,18})/,
    /(?:它们|它|这种生物|这种动物|这群小东西)(?:就是|叫做|是)([^，,。！？!?]{2,18})/,
    TOPIC_SUFFIX_RE,
  ]
  let topic = ''
  for (const pattern of topicPatterns) {
    const match = text.match(pattern)
    if (match?.[1]) {
      topic = normalizeTopic(match[1])
      break
    }
  }
  if (!topic || topic.length < 1 || topic.length > 18) return ''

  if (FOOD_TOPIC_RE.test(topic) || FOOD_TOPIC_RE.test(text)) {
    return truncateName(`${topic}制作指南`)
  }
  if (TRUTH_TOPIC_RE.test(text)) {
    return truncateName(`${topic}的真相`)
  }
  if (/为什么|为何|如何|怎么/.test(text)) {
    return truncateName(`${topic}是怎么回事`)
  }
  if (SCIENCE_TOPIC_RE.test(topic) || SCIENCE_TOPIC_RE.test(text)) {
    return truncateName(`${topic}科普`)
  }
  return truncateName(`${topic}的故事`)
}

function temporaryTitleFromContent(project: ProjectLike | PublishProject): string {
  return (
    explicitTemporaryTitle((project as ProjectLike).script_text) ||
    deriveTemporaryTitle((project as ProjectLike).script_text) ||
    explicitTemporaryTitle((project as ProjectLike).prompt) ||
    deriveTemporaryTitle((project as ProjectLike).prompt)
  )
}

function truncateName(value: string, full = false): string {
  const text = value.replace(/\s+/g, ' ').trim()
  if (full || text.length <= 28) return text
  return `${text.slice(0, 28)}...`
}

function cleanStoredName(value: string): string {
  return value
    .replace(/\s*·\s*开始生产\s*$/i, '')
    .replace(/\s*-\s*开始生产\s*$/i, '')
    .replace(/\s*·\s*长视频\s*·\s*.*$/i, '')
    .replace(/\s*·\s*YouTube\s*长视频\s*·\s*.*$/i, '')
    .replace(/\s*·\s*(TikTok|Reels|Shorts|YouTube Shorts)\s*·\s*.*$/i, '')
    .trim()
}

function isInternalOrGenericName(value: string): boolean {
  const cleaned = cleanStoredName(value)
  if (!cleaned) return true
  return (
    INTERNAL_NAME_PATTERNS.some((pattern) => pattern.test(cleaned)) ||
    GENERIC_NAME_PATTERNS.some((pattern) => pattern.test(cleaned))
  )
}

function looksLikeWorkflowName(value: string): boolean {
  const raw = value.trim()
  return (
    /开始生产/i.test(raw) ||
    INTERNAL_NAME_PATTERNS.some((pattern) => pattern.test(cleanStoredName(raw)))
  )
}

function formatLabel(project: ProjectLike): string {
  const fmt = project.output_format || project.platform || ''
  if (fmt === 'youtube_landscape' || fmt === 'youtube_long') return '长视频项目'
  if (fmt === 'youtube_shorts' || fmt === 'tiktok' || fmt === 'instagram_reels') return '短视频项目'
  return '视频项目'
}

export function projectDisplayName(
  project: ProjectLike | PublishProject,
  opts?: { full?: boolean },
): string {
  const full = !!opts?.full
  const rawName = String(project.name || '')
  const name = cleanStoredName(rawName)
  const workflowName = looksLikeWorkflowName(rawName)
  if (name && !workflowName && !isInternalOrGenericName(name)) {
    return truncateName(name, full)
  }

  const temporaryTitle = temporaryTitleFromContent(project)
  if (temporaryTitle) return temporaryTitle

  const scriptTitle = firstUsefulLine((project as ProjectLike).script_text)
  if (scriptTitle) return scriptTitle

  const promptTitle = firstUsefulLine((project as ProjectLike).prompt)
  if (promptTitle) return promptTitle

  if (name && !isInternalOrGenericName(name)) {
    return truncateName(name, full)
  }

  const shortId = String(project.id || '').slice(0, 8)
  return `${formatLabel(project as ProjectLike)}${shortId ? ` ${shortId}` : ''}`
}
