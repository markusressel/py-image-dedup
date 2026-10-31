<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { usePolling } from '../polling'
import { status } from '../status'
import { fileName, folder } from '../format'
import StatusBadge from '../components/StatusBadge.vue'
import ThumbImage from '../components/ThumbImage.vue'
import PagerNav from '../components/PagerNav.vue'
import VariantBadge from '../components/VariantBadge.vue'

const LIMIT = 20
const MAX_THUMBS = 6
const LEFT = 'image-match'
const RIGHT = 'hashes'

const route = useRoute()
const router = useRouter()

const category = computed(() => (route.query.category as string | undefined) ?? '')
const allStatuses = computed(() => route.query.all === 'true')
const offset = computed(() => Number(route.query.offset ?? 0))

function setQuery(changes: Record<string, string | number | undefined>) {
  const query: Record<string, string> = {}
  const merged = { ...route.query, offset: undefined, ...changes }
  for (const [key, value] of Object.entries(merged)) {
    if (value !== undefined && value !== '' && value !== null && value !== 0) query[key] = String(value)
  }
  router.replace({ query })
}

const comparison = usePolling(
  () =>
    api.compare({
      left: LEFT,
      right: RIGHT,
      category: category.value,
      // default: open and review groups (the current findings)
      all_statuses: allStatuses.value ? true : undefined,
      limit: LIMIT,
      offset: offset.value,
    }),
  15000,
)
watch(() => route.query, () => comparison.refresh(), { deep: true })

const CATEGORY_LABELS: Record<string, string> = {
  different: 'Grouped differently',
  [`only_${RIGHT}`]: `Only ${RIGHT}`,
  [`only_${LEFT}`]: `Only ${LEFT}`,
  same: 'Same groups',
}
const CATEGORY_HINTS: Record<string, string> = {
  different: 'Both matchers found some of these files, but grouped them differently (other members, split or merged)',
  [`only_${RIGHT}`]: `Found by ${RIGHT} only: new matches (or false positives) of the hash pipeline`,
  [`only_${LEFT}`]: `Found by ${LEFT} only: missed by the hash pipeline (or false positives of image-match)`,
  same: 'Both matchers found exactly the same group',
}
const CATEGORIES = ['different', `only_${RIGHT}`, `only_${LEFT}`, 'same']

const counts = computed(() => comparison.data.value?.counts ?? {})
const files = computed(() => comparison.data.value?.files ?? {})
const totalClusters = computed(() => Object.values(counts.value).reduce((a, b) => a + b, 0))
const backend = computed(() => status.value?.matching.backend)
</script>

<template>
  <div class="stack">
    <section class="card">
      <div class="card-head">
        <h2>Compare matchers: {{ LEFT }} vs. {{ RIGHT }}</h2>
        <span class="muted small">groups that share a file are compared</span>
      </div>
      <p v-if="backend === 'both'" class="small">
        <code>matching.backend: both</code>: {{ LEFT }} decides, the groups of {{ RIGHT }} are only recorded (never
        acted on). The hash pipeline runs on full runs only.
      </p>
      <p v-else-if="backend" class="small muted">
        <code>matching.backend: {{ backend }}</code>: groups of the other matcher are from earlier runs.
        Set it to <code>both</code> to record both again.
      </p>
      <div class="compare-stats">
        <button
          v-for="c in CATEGORIES"
          :key="c"
          class="compare-stat"
          :class="{ active: category === c }"
          :title="CATEGORY_HINTS[c]"
          @click="setQuery({ category: category === c ? undefined : c })"
        >
          <span class="compare-stat-value">{{ counts[c] ?? '–' }}</span>
          <span class="small">{{ CATEGORY_LABELS[c] }}</span>
          <span class="muted small">{{ files[c] ?? 0 }} files</span>
        </button>
      </div>
      <label class="check">
        <input type="checkbox" :checked="allStatuses" @change="setQuery({ all: allStatuses ? undefined : 'true' })" />
        include resolved and ignored groups (default: open and needs review)
      </label>
    </section>

    <div v-if="comparison.error.value" class="error-box">{{ comparison.error.value }}</div>
    <div v-if="comparison.data.value && totalClusters === 0" class="card muted center">
      No groups to compare yet. Run with <code>matching.backend: both</code> and wait for a full run.
    </div>

    <section v-for="(cluster, i) in comparison.data.value?.items ?? []" :key="i" class="card compare-cluster">
      <div class="card-head">
        <span class="tag" :class="{ 'tag-warn': cluster.category === 'different' }">
          {{ CATEGORY_LABELS[cluster.category] ?? cluster.category }}
        </span>
      </div>
      <div class="compare-grid">
        <div v-for="side in (['left', 'right'] as const)" :key="side" class="compare-side">
          <div class="muted small">{{ side === 'left' ? LEFT : RIGHT }}</div>
          <div v-if="cluster[side].length === 0" class="muted small">not grouped</div>
          <RouterLink
            v-for="group in cluster[side]"
            :key="group.id"
            :to="`/groups/${group.id}`"
            class="group-row"
            :class="`group-${group.status}`"
          >
            <div class="group-info">
              <div class="group-title">
                <strong>#{{ group.id }}</strong>
                <StatusBadge :value="group.status" />
                <span v-if="!group.actionable" class="tag">record-only</span>
                <span class="small">{{ group.candidate_count }} files</span>
                <span v-if="group.match_confidence !== null" class="small">
                  confidence {{ group.match_confidence.toFixed(2) }}
                </span>
              </div>
              <div class="group-thumbs compare-thumbs">
                <div
                  v-for="member in (group.members ?? []).slice(0, MAX_THUMBS)"
                  :key="member.id"
                  class="group-thumb"
                  :class="{ keep: member.role === 'keep' }"
                  :title="member.path"
                >
                  <ThumbImage :candidate-id="member.id" :size="160" :alt="fileName(member.path)" />
                </div>
                <div v-if="(group.members?.length ?? 0) > MAX_THUMBS" class="more">
                  +{{ (group.members?.length ?? 0) - MAX_THUMBS }}
                </div>
              </div>
              <div class="small compare-members">
                <span v-for="member in group.members ?? []" :key="member.id" class="path" :title="member.path">
                  {{ fileName(member.path) }}
                  <VariantBadge v-if="member.variant_kind" :kind="member.variant_kind" />
                </span>
              </div>
              <div v-if="group.match_review_reasons.length" class="muted small">
                review: {{ group.match_review_reasons.join('; ') }}
              </div>
            </div>
          </RouterLink>
        </div>
      </div>
      <div v-if="cluster.only_left_paths.length || cluster.only_right_paths.length" class="small compare-diff">
        <div v-if="cluster.only_left_paths.length">
          <strong>Only grouped by {{ LEFT }}:</strong>
          <span v-for="path in cluster.only_left_paths" :key="path" class="path" :title="path">
            <code>{{ fileName(path) }}</code> <span class="muted">{{ folder(path) }}</span>
          </span>
        </div>
        <div v-if="cluster.only_right_paths.length">
          <strong>Only grouped by {{ RIGHT }}:</strong>
          <span v-for="path in cluster.only_right_paths" :key="path" class="path" :title="path">
            <code>{{ fileName(path) }}</code> <span class="muted">{{ folder(path) }}</span>
          </span>
        </div>
      </div>
    </section>

    <PagerNav
      v-if="comparison.data.value && comparison.data.value.total > LIMIT"
      :offset="offset"
      :limit="LIMIT"
      :total="comparison.data.value.total"
      @change="(o) => setQuery({ offset: o })"
    />
  </div>
</template>
