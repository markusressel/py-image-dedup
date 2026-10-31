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
  // hash pipeline (matching.backend "hashes" or "both")
  hashes_analysed?: number
  hashes_failed?: number
  hashes_groups?: number
  hashes_review_groups?: number
  hashes_record_only?: boolean
  hashes_error?: string
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
  restore_requested: number
  duplicate_files_open: number
  actions: Record<string, Record<string, number>>
  failures: number
  decided_by: Record<string, number>
  runs: number
  keeps_smaller_copy: number
  groups_by_matcher: Record<string, number>
}

export type MatchingBackend = 'image-match' | 'hashes' | 'both'

export interface MatchingStatus {
  backend: MatchingBackend
  // groups of other matchers are record-only (never acted on)
  acting_matcher: string
  matchers: string[]
  embeddings: boolean
}

export interface Status {
  version: string
  server_time: number
  dry_run: boolean
  user_decisions_dry_run: boolean
  duplicates_target_directory: string | null
  hold_ambiguous_groups: boolean
  matching: MatchingStatus
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
  variant_kind: VariantKind | null
}

// how a member relates to its group (hash pipeline), see py_image_dedup/matching/models.py VariantKind
export type VariantKind =
  | 'exact'
  | 'pixel-identical'
  | 'copy'
  | 'rotated'
  | 'grayscale'
  | 'colour-edited'
  | 'crop'
  | 'burst'
export type MatchReason = 'exact' | 'pixel-identical' | 'near-duplicate' | 'embedding'

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
  // keeper suggested by another tool (f.ex. Immich) that differs from the rules, see the decision import
  suggested_keep_path: string | null
  suggestion_source: 'immich' | 'import' | null
  // hash pipeline: confidence of the weakest match, why automatic action is not allowed
  match_confidence: number | null
  match_review_reasons: string[]
  // false: found by a matcher that only records its groups (matching.backend "both"), never acted on
  actionable: boolean
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
  // hash pipeline only
  variant_kind: VariantKind | null
  // transform that turns the matched partner into this file, null if upright
  orientation: string | null
  match_confidence: number | null
  match_reason: MatchReason | null
  metadata: {
    criteria?: Criterion[]
    exif?: Record<string, unknown> | null
    match?: { notes: string[]; partner: string | null }
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
  // restore: the file was moved back from target to its original location (path)
  action: 'move' | 'delete' | 'remove_folder' | 'restore'
  target: string | null
  source: 'rules' | 'user'
  dry_run: boolean
  status: ActionStatus
  message: string | null
  // the last successful (non dry run) move of the file, can be undone
  undoable: boolean
  restore_requested: boolean
}

export interface GroupDetail extends Group {
  candidates: Candidate[]
  planned_actions: PlannedAction[]
  dry_run: boolean
  actions: Action[]
  // moved files of this group that can be moved back, and how many of them are requested to be
  undoable_moves: number
  restore_requested: number
}

export type BulkAction = 'accept' | 'ignore' | 'reset'

export interface GroupFilter {
  status?: GroupStatus
  needs_review?: boolean
  decided_by?: string
  q?: string
  keeps_smaller_copy?: boolean
  has_suggestion?: boolean
  matcher?: string
}

export interface BulkResult {
  action: BulkAction
  changed: number[]
  skipped: Record<string, number>
  dry_run: boolean
}

export interface CompareCluster {
  category: string
  left: Group[]
  right: Group[]
  // files only grouped by one of the matchers (only for clusters with groups of both)
  only_left_paths: string[]
  only_right_paths: string[]
}

export interface CompareResult {
  left: string
  right: string
  statuses: GroupStatus[]
  // same, different, only_<left>, only_<right>
  categories: string[]
  // clusters per category
  counts: Record<string, number>
  files: Record<string, number>
  total: number
  items: CompareCluster[]
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
  agrees_with_rules: number
  disagrees_with_rules: number
  no_discarded_member: number
  all_discarded: number
  several_remaining: number
  already_decided_by_user: number
}

export interface ImportComparison {
  pixel_count: number | null
  file_size: number | null
  exif_tag_count: number | null
}

export interface ImportDisagreement {
  group_id: number
  suggested_keep_path: string
  rule_keep_path: string | null
  decided_by: string | null
  suggested: ImportComparison | null
  rule: ImportComparison | null
  // the group already had this suggestion (imported before)
  already_suggested: boolean
}

export interface ImmichImportResult {
  applied: boolean
  source: 'immich' | 'import'
  outcomes: ImportOutcomes
  agreeing: { group_id: number; keep_path: string }[]
  disagreeing: ImportDisagreement[]
  trashed: number
  unmapped: number
  unmapped_examples: string[]
}
