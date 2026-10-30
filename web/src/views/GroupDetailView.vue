<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, imageUrl } from '../api'
import { usePolling } from '../polling'
import { status } from '../status'
import { fileName, folder, formatBytes, formatPixels, formatTime } from '../format'
import type { Candidate, GroupDetail } from '../types'
import StatusBadge from '../components/StatusBadge.vue'
import ThumbImage from '../components/ThumbImage.vue'
import ActionTable from '../components/ActionTable.vue'

const props = defineProps<{ id: number }>()

const group = usePolling<GroupDetail>(
  () => api.group(props.id),
  (g) => (g?.execute_requested ? 1500 : 10000),
)
watch(() => props.id, () => group.refresh())

const busy = ref(false)
const actionError = ref<string | null>(null)

const g = computed(() => group.data.value)
const canDecide = computed(() => !!status.value?.duplicates_target_directory)
const dryRun = computed(() => g.value?.dry_run ?? status.value?.dry_run ?? true)

const criteriaRows = computed(() => {
  const candidates = g.value?.candidates ?? []
  const rules = candidates[0]?.metadata.criteria?.map((c) => c.rule) ?? []
  return rules.map((rule) => ({
    rule,
    values: candidates.map((c) => c.metadata.criteria?.find((x) => x.rule === rule)?.value ?? '–'),
  }))
})

async function run(action: () => Promise<GroupDetail>) {
  busy.value = true
  actionError.value = null
  try {
    group.data.value = await action()
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

function keep(candidate: Candidate) {
  run(() => api.keep(props.id, candidate.id))
}

function plannedFor(candidate: Candidate) {
  return g.value?.planned_actions.find((a) => a.path === candidate.path)
}
</script>

<template>
  <div class="stack">
    <RouterLink to="/groups" class="back">← Duplicate groups</RouterLink>
    <div v-if="group.error.value" class="error-box">{{ group.error.value }}</div>

    <template v-if="g">
      <section class="card">
        <div class="card-head">
          <h2>
            Group #{{ g.id }}
            <StatusBadge :value="g.status" />
            <span v-if="g.execute_requested" class="tag tag-info">your decision is queued</span>
          </h2>
          <span class="muted small">{{ g.candidate_count }} files · matched by {{ g.matcher }}</span>
        </div>

        <div class="decision">
          <div v-if="g.decision_source === 'user'">
            <strong>You chose</strong> to keep <code>{{ fileName(g.keep_path ?? '') }}</code>.
            <span class="muted">The rules would keep <code>{{ fileName(g.rule_keep_path ?? '') }}</code>.</span>
          </div>
          <div v-else>
            <strong>Decided by rule</strong> <code>{{ g.decided_by ?? '–' }}</code>:
            {{ g.decision_reason }}
          </div>
          <div v-if="g.needs_review" class="warn-box">
            {{ g.review_reason }}. Please check which copy should be kept.
            <template v-if="status?.hold_ambiguous_groups && g.decision_source === 'rules'">
              Nothing happens to this group until you decide.
            </template>
          </div>
        </div>

        <div class="plan">
          <template v-if="g.status === 'ignored'">
            <strong>Ignored:</strong> the daemon never touches the files of this group.
          </template>
          <template v-else-if="g.status === 'resolved'">
            <strong>Resolved:</strong> the duplicates have been moved/deleted.
          </template>
          <template v-else-if="g.planned_actions.length > 0">
            <strong>{{ dryRun ? 'What would happen (dry run):' : 'What happens:' }}</strong>
            <ul>
              <li v-for="a in g.planned_actions" :key="a.path">
                <span :class="`action-${a.action}`">{{ a.action }}</span>
                <code class="path">{{ a.path }}</code>
                <template v-if="a.target"> → <code class="path">{{ a.target }}</code></template>
              </li>
            </ul>
          </template>
          <template v-else>Nothing to do: no other copy exists (anymore).</template>
        </div>

        <div class="buttons">
          <button v-if="g.status !== 'ignored'" :disabled="busy" @click="run(() => api.ignore(g!.id))">
            Ignore group
          </button>
          <button
            v-if="g.status === 'ignored' || g.decision_source === 'user'"
            :disabled="busy"
            @click="run(() => api.reset(g!.id))"
          >
            Reset to rule decision
          </button>
          <span v-if="!canDecide" class="muted small">
            Choosing a different copy requires <code>duplicates_target_directory</code>: the web UI only moves files.
          </span>
          <span v-else-if="dryRun" class="muted small">
            Dry run: choosing a copy only simulates moving the others.
          </span>
        </div>
        <div v-if="actionError" class="error-box">{{ actionError }}</div>
      </section>

      <section class="candidates">
        <article
          v-for="c in g.candidates"
          :key="c.id"
          class="candidate card"
          :class="{ keep: c.role === 'keep', missing: !c.exists }"
        >
          <a :href="imageUrl(c.id)" target="_blank" rel="noopener" class="candidate-image">
            <ThumbImage :candidate-id="c.id" :size="640" :alt="fileName(c.path)" />
          </a>
          <div class="candidate-head">
            <span v-if="c.role === 'keep'" class="badge badge-keep">keep</span>
            <span v-else class="badge badge-duplicate">duplicate</span>
            <span v-if="!c.exists && c.moved_to" class="tag">moved</span>
            <span v-else-if="!c.exists" class="tag tag-warn">missing</span>
            <span class="muted small">rank {{ c.rank + 1 }}</span>
          </div>
          <div class="candidate-name path" :title="c.path">{{ fileName(c.path) }}</div>
          <div class="muted small path" :title="c.path">{{ folder(c.path) }}</div>
          <div v-if="c.moved_to" class="muted small path">→ {{ c.moved_to }}</div>
          <div v-if="c.role === 'keep' && g.decision_source === 'user'" class="small">chosen by you</div>
          <div v-else-if="c.role === 'keep' && c.keep_reason" class="small">{{ c.keep_reason }}</div>
          <div v-else-if="plannedFor(c)" class="small">
            {{ dryRun ? 'would be' : 'will be' }} {{ plannedFor(c)!.action === 'move' ? 'moved' : 'deleted' }}
          </div>
          <dl class="meta">
            <dt>Size</dt><dd>{{ formatBytes(c.file_size) }}</dd>
            <dt>Resolution</dt><dd>{{ formatPixels(c.pixel_count) }}</dd>
            <dt>Modified</dt><dd>{{ formatTime(c.modified_at) }}</dd>
            <dt>EXIF tags</dt><dd>{{ c.exif_tag_count ?? '–' }}</dd>
            <dt>Distance</dt><dd>{{ c.distance?.toFixed(4) ?? '–' }}</dd>
          </dl>
          <button
            v-if="c.role !== 'keep' && g.status !== 'ignored'"
            class="primary"
            :disabled="busy || !canDecide || !c.exists"
            @click="keep(c)"
          >
            Keep this one
          </button>
        </article>
      </section>

      <section v-if="criteriaRows.length" class="card">
        <div class="card-head">
          <h2>Prioritization rules</h2>
          <span class="muted small">in configured order, the first differing rule decides</span>
        </div>
        <div class="table-wrap">
          <table class="table criteria">
            <thead>
              <tr>
                <th>Rule</th>
                <th v-for="c in g.candidates" :key="c.id" class="path" :title="c.path">
                  {{ fileName(c.path) }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in criteriaRows" :key="row.rule" :class="{ deciding: row.rule === g.decided_by }">
                <td class="nowrap"><code>{{ row.rule }}</code></td>
                <td v-for="(value, i) in row.values" :key="i">{{ value }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <h2>Actions</h2>
        </div>
        <ActionTable :actions="g.actions" />
      </section>
    </template>
    <div v-else-if="!group.error.value" class="muted">Loading…</div>
  </div>
</template>
