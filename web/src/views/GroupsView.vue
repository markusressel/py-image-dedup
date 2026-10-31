<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { usePolling } from '../polling'
import { status } from '../status'
import { fileName, folder, formatRelative } from '../format'
import type { BulkAction, BulkResult, GroupFilter, GroupStatus } from '../types'
import StatusBadge from '../components/StatusBadge.vue'
import ThumbImage from '../components/ThumbImage.vue'
import PagerNav from '../components/PagerNav.vue'

const LIMIT = 30
const MAX_THUMBS = 5

const route = useRoute()
const router = useRouter()

const filterStatus = computed(() => (route.query.status as GroupStatus | undefined) ?? '')
const needsReview = computed(() => route.query.needs_review === 'true')
const decidedBy = computed(() => (route.query.decided_by as string | undefined) ?? '')
const keepsSmallerCopy = computed(() => route.query.keeps_smaller_copy === 'true')
const hasSuggestion = computed(() => route.query.has_suggestion === 'true')
const search = computed(() => (route.query.q as string | undefined) ?? '')
const offset = computed(() => Number(route.query.offset ?? 0))

const searchInput = ref(search.value)
watch(search, (value) => (searchInput.value = value))

function setQuery(changes: Record<string, string | number | undefined>) {
  const query: Record<string, string> = {}
  const merged = { ...route.query, offset: undefined, ...changes }
  for (const [key, value] of Object.entries(merged)) {
    if (value !== undefined && value !== '' && value !== null && value !== 'false' && value !== 0) {
      query[key] = String(value)
    }
  }
  router.replace({ query })
}

let searchTimer: number | undefined
function onSearchInput() {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => setQuery({ q: searchInput.value }), 250)
}

const groups = usePolling(
  () =>
    api.groups({
      status: filterStatus.value,
      needs_review: needsReview.value ? true : undefined,
      decided_by: decidedBy.value,
      keeps_smaller_copy: keepsSmallerCopy.value ? true : undefined,
      has_suggestion: hasSuggestion.value ? true : undefined,
      q: search.value,
      limit: LIMIT,
      offset: offset.value,
    }),
  5000,
)

watch(() => route.query, () => groups.refresh(), { deep: true })

const STATUS_FILTERS: { value: GroupStatus | ''; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'review', label: 'Needs review' },
  { value: 'open', label: 'Open' },
  { value: 'resolved', label: 'Resolved' },
  { value: 'ignored', label: 'Ignored' },
]

// ---------------------------------------------------------------- bulk actions

const selected = ref(new Set<number>())
// all groups matching the current filter (also on other pages) instead of the selected ones
const selectAllMatching = ref(false)
const bulkBusy = ref(false)
const bulkError = ref<string | null>(null)
const bulkResult = ref<BulkResult | null>(null)

const pageIds = computed(() => (groups.data.value?.items ?? []).map((g) => g.id))
const total = computed(() => groups.data.value?.total ?? 0)
const allOnPageSelected = computed(
  () => pageIds.value.length > 0 && pageIds.value.every((id) => selected.value.has(id)),
)
const someOnPageSelected = computed(() => pageIds.value.some((id) => selected.value.has(id)))
const selectionCount = computed(() => (selectAllMatching.value ? total.value : selected.value.size))
const canAccept = computed(() => !!status.value?.duplicates_target_directory)
const userDryRun = computed(() => status.value?.user_decisions_dry_run ?? true)

function clearSelection() {
  selected.value = new Set()
  selectAllMatching.value = false
}

// a selection only makes sense for the filter it was made with
watch(() => route.query, clearSelection, { deep: true })

function toggle(id: number) {
  selectAllMatching.value = false
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selected.value = next
}

function togglePage() {
  selectAllMatching.value = false
  const next = new Set(selected.value)
  if (allOnPageSelected.value) pageIds.value.forEach((id) => next.delete(id))
  else pageIds.value.forEach((id) => next.add(id))
  selected.value = next
}

function currentFilter(): GroupFilter {
  const filter: GroupFilter = {}
  if (filterStatus.value) filter.status = filterStatus.value
  if (needsReview.value) filter.needs_review = true
  if (decidedBy.value) filter.decided_by = decidedBy.value
  if (search.value) filter.q = search.value
  if (keepsSmallerCopy.value) filter.keeps_smaller_copy = true
  if (hasSuggestion.value) filter.has_suggestion = true
  return filter
}

const BULK_LABELS: Record<BulkAction, string> = {
  accept: 'Accept the rule decision of',
  ignore: 'Ignore',
  reset: 'Reset',
}

const SKIP_REASONS: Record<string, string> = {
  resolved: 'already resolved',
  ignored: 'ignored (reset them first)',
  decided_by_user: 'already decided by you',
  no_rule_decision: 'without a rule decision',
  already_ignored: 'already ignored',
  nothing_to_reset: 'without a decision of yours',
  unknown: 'unknown',
}

const bulkSummary = computed(() => {
  const r = bulkResult.value
  if (!r) return ''
  const skipped = Object.entries(r.skipped)
    .map(([reason, n]) => `${n} ${SKIP_REASONS[reason] ?? reason}`)
    .join(', ')
  let text = `${r.changed.length} group(s) changed.`
  if (skipped) text += ` Not changed: ${skipped}.`
  if (r.action === 'accept' && r.changed.length > 0) {
    text += r.dry_run
      ? ' Dry run: the daemon only simulates moving the duplicates.'
      : ' The daemon moves the duplicates within a few seconds.'
  }
  return text
})

async function applyBulk(action: BulkAction) {
  const count = selectionCount.value
  if (count === 0) return
  let question = `${BULK_LABELS[action]} ${count} group(s)?`
  if (action === 'accept') {
    question += userDryRun.value
      ? '\n\nDry run is enabled for your decisions: moving the duplicates is only simulated.'
      : `\n\nThe duplicates are MOVED to ${status.value?.duplicates_target_directory}.`
  }
  if (!window.confirm(question)) return
  bulkBusy.value = true
  bulkError.value = null
  bulkResult.value = null
  try {
    const target = selectAllMatching.value ? { filter: currentFilter() } : { group_ids: [...selected.value] }
    bulkResult.value = await api.bulk(action, target)
    clearSelection()
    await groups.refresh()
  } catch (e) {
    bulkError.value = e instanceof Error ? e.message : String(e)
  } finally {
    bulkBusy.value = false
  }
}

function statusCount(value: GroupStatus | ''): number | undefined {
  const s = status.value?.statistics
  if (!s) return undefined
  return value === '' ? s.groups_total : s.groups[value]
}
</script>

<template>
  <div class="stack">
    <div class="toolbar">
      <div class="segmented">
        <button
          v-for="f in STATUS_FILTERS"
          :key="f.value"
          :class="{ active: filterStatus === f.value }"
          @click="setQuery({ status: f.value })"
        >
          {{ f.label }}
          <span v-if="statusCount(f.value) !== undefined" class="muted">{{ statusCount(f.value) }}</span>
        </button>
      </div>
      <label class="check">
        <input type="checkbox" :checked="needsReview" @change="setQuery({ needs_review: needsReview ? undefined : 'true' })" />
        weak decision
      </label>
      <label class="check" title="Another tool (f.ex. Immich) keeps a different copy than the rules">
        <input
          type="checkbox"
          :checked="hasSuggestion"
          @change="setQuery({ has_suggestion: hasSuggestion ? undefined : 'true' })"
        />
        suggestion
      </label>
      <input
        v-model="searchInput"
        class="search"
        type="search"
        placeholder="Filter by path…"
        @input="onSearchInput"
      />
    </div>
    <div v-if="decidedBy" class="filter-chip">
      decided by <code>{{ decidedBy }}</code>
      <button class="link" @click="setQuery({ decided_by: undefined })">clear</button>
    </div>
    <div v-if="keepsSmallerCopy" class="filter-chip">
      keeps a copy with a lower resolution or a smaller file than a duplicate
      <button class="link" @click="setQuery({ keeps_smaller_copy: undefined })">clear</button>
    </div>

    <div v-if="groups.error.value" class="error-box">{{ groups.error.value }}</div>

    <section v-if="pageIds.length > 0" class="card bulk-bar">
      <label class="check">
        <input
          type="checkbox"
          :checked="allOnPageSelected"
          :indeterminate="someOnPageSelected && !allOnPageSelected"
          @change="togglePage"
        />
        select page
      </label>
      <span v-if="selectAllMatching" class="small">
        All <strong>{{ total }}</strong> groups matching the filter are selected.
        <button class="link" @click="clearSelection">clear</button>
      </span>
      <span v-else-if="selected.size > 0" class="small">
        <strong>{{ selected.size }}</strong> selected.
        <button v-if="allOnPageSelected && total > pageIds.length" class="link" @click="selectAllMatching = true">
          Select all {{ total }} matching groups
        </button>
        <button class="link" @click="clearSelection">clear</button>
      </span>
      <div class="buttons">
        <button
          class="primary"
          :disabled="bulkBusy || selectionCount === 0 || !canAccept"
          title="Let the daemon execute the decision of the prioritization rules"
          @click="applyBulk('accept')"
        >
          Accept rule decision
        </button>
        <button :disabled="bulkBusy || selectionCount === 0" @click="applyBulk('ignore')">Ignore</button>
        <button :disabled="bulkBusy || selectionCount === 0" @click="applyBulk('reset')">Reset</button>
      </div>
      <span v-if="!canAccept" class="muted small">
        Accepting requires <code>duplicates_target_directory</code>: the web UI only moves files.
      </span>
      <span v-else-if="userDryRun" class="muted small">
        Dry run: accepted decisions are only simulated, no file is moved.
      </span>
      <span v-else class="small">
        <strong>Live:</strong> accepted decisions move the duplicates to
        <code>{{ status?.duplicates_target_directory }}</code>.
      </span>
    </section>
    <div v-if="bulkError" class="error-box">{{ bulkError }}</div>
    <div v-if="bulkSummary" class="card small">{{ bulkSummary }}</div>

    <div class="group-list">
      <div v-for="group in groups.data.value?.items ?? []" :key="group.id" class="group-select-row">
        <input
          type="checkbox"
          :checked="selectAllMatching || selected.has(group.id)"
          :disabled="selectAllMatching"
          :aria-label="`Select group #${group.id}`"
          @change="toggle(group.id)"
        />
        <RouterLink :to="`/groups/${group.id}`" class="group-row" :class="`group-${group.status}`">
          <div class="group-thumbs">
            <div
              v-for="member in (group.members ?? []).slice(0, MAX_THUMBS)"
              :key="member.id"
              class="group-thumb"
              :class="{ keep: member.role === 'keep' }"
            >
              <ThumbImage :candidate-id="member.id" :size="160" :alt="fileName(member.path)" />
            </div>
            <div v-if="(group.members?.length ?? 0) > MAX_THUMBS" class="more">
              +{{ (group.members?.length ?? 0) - MAX_THUMBS }}
            </div>
          </div>
          <div class="group-info">
            <div class="group-title">
              <StatusBadge :value="group.status" />
              <span v-if="group.execute_requested" class="tag tag-info">queued</span>
              <span v-if="group.decision_source === 'user'" class="tag">your choice</span>
              <span v-else-if="group.needs_review && group.status !== 'review'" class="tag tag-warn">weak decision</span>
              <strong class="path">{{ group.keep_path ? fileName(group.keep_path) : '–' }}</strong>
            </div>
            <div class="muted small path">{{ group.keep_path ? folder(group.keep_path) : '' }}</div>
            <div class="small">
              {{ group.candidate_count }} files ·
              <template v-if="group.decision_source === 'user'">chosen by you</template>
              <template v-else>decided by <code>{{ group.decided_by ?? 'nothing' }}</code></template>
              · {{ formatRelative(group.updated_at, status?.server_time) }}
            </div>
          </div>
          <div class="group-id muted">#{{ group.id }}</div>
        </RouterLink>
      </div>
      <div v-if="groups.data.value && groups.data.value.items.length === 0" class="card muted center">
        No duplicate groups match these filters.
      </div>
    </div>

    <PagerNav
      v-if="groups.data.value && groups.data.value.total > LIMIT"
      :offset="offset"
      :limit="LIMIT"
      :total="groups.data.value.total"
      @change="(o) => setQuery({ offset: o })"
    />
  </div>
</template>
