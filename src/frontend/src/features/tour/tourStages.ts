/**
 * Tour content, grouped into route-scoped stages. Each stage's steps only
 * reference elements that exist on that stage's page, so the tour always has
 * a valid target. `onDone` decides what happens after a stage's last step:
 *   - navigate: auto-advance to the next page (we can drive it)
 *   - wait:     pause; the next stage runs when the user reaches its route
 *               (e.g. after they actually create a channel)
 *   - finish:   end the whole tour
 */
export type Placement = 'top' | 'bottom' | 'left' | 'right'

export type StageKey = 'home' | 'create' | 'hub' | 'workspace'

export type OnDone =
  | { type: 'navigate'; to: string }
  | { type: 'wait' }
  | { type: 'finish' }

export interface TourStepDef {
  target: string
  placement?: Placement
  /** center overlay card with no anchored element (welcome / done). */
  center?: boolean
  titleKey: string
  bodyKey: string
  /** marks the very last step of the whole tour → button reads "完成". */
  last?: boolean
}

export interface TourStage {
  key: StageKey
  steps: TourStepDef[]
  onDone: OnDone
}

export const STAGES: Record<StageKey, TourStage> = {
  home: {
    key: 'home',
    steps: [
      { target: 'body', center: true, titleKey: 'tour.welcomeTitle', bodyKey: 'tour.welcomeBody' },
      { target: '.sidebar', placement: 'right', titleKey: 'tour.navTitle', bodyKey: 'tour.navBody' },
      { target: '[data-tour="create-channel"]', placement: 'bottom', titleKey: 'tour.createTitle', bodyKey: 'tour.createBody' },
    ],
    onDone: { type: 'navigate', to: '/channels/new' },
  },
  create: {
    key: 'create',
    steps: [
      { target: '.channel-tabs', placement: 'bottom', titleKey: 'tour.tabsTitle', bodyKey: 'tour.tabsBody' },
      { target: '[data-tour="preset-card"]', placement: 'right', titleKey: 'tour.presetTitle', bodyKey: 'tour.presetBody' },
      { target: '[data-tour="custom-channel"]', placement: 'left', titleKey: 'tour.customTitle', bodyKey: 'tour.customBody' },
    ],
    onDone: { type: 'wait' },
  },
  hub: {
    key: 'hub',
    steps: [
      { target: '.channel-action-card.long', placement: 'bottom', titleKey: 'tour.hubActionsTitle', bodyKey: 'tour.hubActionsBody' },
      { target: '.channel-topic-list-selectable', placement: 'top', titleKey: 'tour.hubTopicsTitle', bodyKey: 'tour.hubTopicsBody' },
      { target: '.channel-bulk-primary', placement: 'left', titleKey: 'tour.hubBatchTitle', bodyKey: 'tour.hubBatchBody' },
    ],
    onDone: { type: 'wait' },
  },
  workspace: {
    key: 'workspace',
    steps: [
      { target: '.agent-cfg', placement: 'right', titleKey: 'tour.wsConfigTitle', bodyKey: 'tour.wsConfigBody' },
      { target: '.assistant-center', placement: 'left', titleKey: 'tour.wsChatTitle', bodyKey: 'tour.wsChatBody' },
      { target: '[data-tour="nav-projects"]', placement: 'right', titleKey: 'tour.wsProjectsTitle', bodyKey: 'tour.wsProjectsBody' },
      { target: 'body', center: true, titleKey: 'tour.doneTitle', bodyKey: 'tour.doneBody', last: true },
    ],
    onDone: { type: 'finish' },
  },
}

/** Map a router pathname to its tour stage (or null if no stage runs here). */
export function detectStage(pathname: string): StageKey | null {
  if (pathname === '/channels') return 'home'
  if (pathname === '/channels/new') return 'create'
  if (/^\/channels\/[^/]+\/(shorts|youtube)$/.test(pathname)) return 'workspace'
  if (/^\/channels\/[^/]+$/.test(pathname)) return 'hub'
  return null
}
