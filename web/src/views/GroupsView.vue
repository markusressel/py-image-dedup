<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { usePolling } from '../polling'
import { status } from '../status'
import { fileName, folder, formatRelative } from '../format'
import type { GroupStatus } from '../types'
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

    <div class="group-list">
      <RouterLink
        v-for="group in groups.data.value?.items ?? []"
        :key="group.id"
        :to="`/groups/${group.id}`"
        class="group-row"
        :class="`group-${group.status}`"
      >
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
