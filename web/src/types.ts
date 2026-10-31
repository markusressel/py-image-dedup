// Types of the JSON API served by py_image_dedup/web/app.py

export type GroupStatus = 'open' | 'review' | 'resolved' | 'ignored'
export type ActionStatus = 'done' | 'simulated' | 'skipped' | 'failed'
export type RunStatus = 'running' | 'finished' | 'failed' | 'interrupted'

export interface LiveStatus {
  busy: boolean
  run_id: number | null
  run_kind: string | null
  phase: string | null
  phase_index: number | null
  phase_count: number | null
  task: string | null
  done: number
  total: number
  current_item: string | null
  queue_size: number
  started_at: number | null
  updated_at: number
}

export interface RunSummary {
  files_with_duplicates: number
  moved: number
  deleted: number
  removed_folders: number
}

export interface Phase {
  id: number
  run_id: number
  idx: number | null
  name: string
  started_at: number
  finished_at: number | null
  done: number | null
  total: number | null
}

export interface Run {
  id: number
  kind: string
  trigger: string | null
  status: RunStatus
  dry_run: boolean
  started_at: number
  finished_at: number | null
  error: string | null
  summary: RunSummary | null
  phases?: Phase[]
}

export interface Statistics {
  groups: Record<GroupStatus, number>
  groups_total: number
  needs_review: number
  execute_requested: number
  duplicate_files_open: number
  actions: Record<string, Record<string, number>>
  failures: number
  decided_by: Record<string, number>
  runs: number
  keeps_smaller_copy: number
}

export interface Status {
  version: string
  server_time: number
  dry_run: boolean
  user_decisions_dry_run: boolean
  duplicates_target_directory: string | null
  hold_ambiguous_groups: boolean
  live: LiveStatus
  statistics: Statistics
  last_run: Run | null
  last_full_run: Run | null
}

export interface GroupMember {
  id: number
  group_id: number
  path: string
  role: 'keep' | 'duplicate'
  rank: number
}

export interface Group {
  id: number
  key: string
  matcher: string
  status: GroupStatus
  needs_review: boolean
  review_reason: string | null
  decision_source: 'rules' | 'user'
  keep_path: string | null
  rule_keep_path: string | null
  decided_by: string | null
  decision_reason: string | null
  execute_requested: boolean
  candidate_count: number
  first_seen_run_id: number | null
  last_seen_run_id: number | null
  created_at: number
  updated_at: number
  resolved_at: number | null
  members?: GroupMember[]
}

export interface Criterion {
  rule: string
  value: string
}

export interface Candidate {
  id: number
  group_id: number
  path: string
  rank: number
  role: 'keep' | 'duplicate'
  keep_reason: string | null
  distance: number | null
  score: number | null
  file_size: number | null
  pixel_count: number | null
  modified_at: number | null
  exif_tag_count: number | null
  metadata: {
    criteria?: Criterion[]
    exif?: Record<string, unknown> | null
  }
  exists: boolean
  moved_to: string | null
}

export interface PlannedAction {
  path: string
  action: 'move' | 'delete'
  target: string | null
}

export interface Action {
  id: number
  created_at: number
  run_id: number | null
  group_id: number | null
  path: string
  action: 'move' | 'delete' | 'remove_folder'
  target: string | null
  source: 'rules' | 'user'
  dry_run: boolean
  status: ActionStatus
  message: string | null
}

export interface GroupDetail extends Group {
  candidates: Candidate[]
  planned_actions: PlannedAction[]
  dry_run: boolean
  actions: Action[]
}

export interface Page<T> {
  total: number
  items: T[]
}

export interface Failure {
  id: number
  path: string
  phase: string | null
  error: string | null
  run_id: number | null
  count: number
  first_seen_at: number
  last_seen_at: number
}

export interface ConfigEntry {
  key: string
  env: string
  value: unknown
  default: unknown
  description: string | null
}

export interface Hint {
  level: 'info' | 'warning'
  key: string
  message: string
  link?: string
}

export interface ConfigResponse {
  entries: ConfigEntry[]
  hints: Hint[]
  weak_rules: string[]
}

export interface ImmichStatus {
  configured: boolean
  url: string | null
  public_url: string | null
  rescan_after_changes: boolean
}

export interface ImmichCandidateAsset {
  candidate_id: number
  asset_id: string | null
  url: string | null
}

export interface GroupImmichAssets {
  configured: boolean
  url: string | null
  error: string | null
  candidates: ImmichCandidateAsset[]
}

export interface ImportOutcomes {
  decided: number
  no_discarded_member: number
  all_discarded: number
  several_remaining: number
  already_decided_by_user: number
}

export interface ImmichImportResult {
  applied: boolean
  outcomes: ImportOutcomes
  decided: { group_id: number; keep_path: string }[]
  trashed: number
  unmapped: number
  unmapped_examples: string[]
}
