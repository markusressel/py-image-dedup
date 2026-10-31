<script setup lang="ts">
import { watch } from 'vue'
import { api } from '../api'
import { usePolling } from '../polling'
import { formatDuration, formatTime } from '../format'
import StatusBadge from '../components/StatusBadge.vue'
import ProgressBar from '../components/ProgressBar.vue'
import ActionTable from '../components/ActionTable.vue'

const props = defineProps<{ id: number }>()

const run = usePolling(() => api.run(props.id), (r) => (r?.status === 'running' ? 2000 : 30000))
const actions = usePolling(() => api.actions({ run_id: props.id, limit: 200 }), 5000)
watch(() => props.id, () => {
  run.refresh()
  actions.refresh()
})
</script>

<template>
  <div class="stack">
    <RouterLink to="/" class="back">← Dashboard</RouterLink>
    <div v-if="run.error.value" class="error-box">{{ run.error.value }}</div>
    <template v-if="run.data.value">
      <section class="card">
        <div class="card-head">
          <h2>
            Run #{{ run.data.value.id }} ({{ run.data.value.kind }})
            <StatusBadge :value="run.data.value.status" />
            <span v-if="run.data.value.dry_run" class="tag">dry run</span>
          </h2>
          <span class="muted small">
            {{ formatTime(run.data.value.started_at) }} ·
            {{ formatDuration(run.data.value.started_at, run.data.value.finished_at) }}
          </span>
        </div>
        <div v-if="run.data.value.trigger" class="small">
          Triggered by changes in <code>{{ run.data.value.trigger }}</code>
        </div>
        <div v-if="run.data.value.error" class="error-box">{{ run.data.value.error }}</div>
        <dl v-if="run.data.value.summary" class="meta inline">
          <dt>Files with duplicates</dt><dd>{{ run.data.value.summary.files_with_duplicates }}</dd>
          <dt>{{ run.data.value.dry_run ? 'Would move' : 'Moved' }}</dt><dd>{{ run.data.value.summary.moved }}</dd>
          <dt>{{ run.data.value.dry_run ? 'Would delete' : 'Deleted' }}</dt><dd>{{ run.data.value.summary.deleted }}</dd>
          <dt>Removed folders</dt><dd>{{ run.data.value.summary.removed_folders }}</dd>
          <template v-if="run.data.value.summary.hashes_groups !== undefined">
            <dt>Hash groups</dt>
            <dd>
              {{ run.data.value.summary.hashes_groups }}
              ({{ run.data.value.summary.hashes_review_groups ?? 0 }} need review{{
                run.data.value.summary.hashes_record_only ? ', record-only' : '' }})
            </dd>
          </template>
          <template v-if="run.data.value.summary.hashes_analysed !== undefined">
            <dt>Hash analysis</dt>
            <dd>
              {{ run.data.value.summary.hashes_analysed }} analysed, {{ run.data.value.summary.hashes_failed ?? 0 }}
              failed
            </dd>
          </template>
        </dl>
        <div v-if="run.data.value.summary?.hashes_error" class="error-box">
          Hash matching failed (image-match was not affected): {{ run.data.value.summary.hashes_error }}
        </div>
      </section>

      <section class="card">
        <div class="card-head"><h2>Phases</h2></div>
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr><th>Phase</th><th>Duration</th><th>Progress</th></tr>
            </thead>
            <tbody>
              <tr v-for="phase in run.data.value.phases ?? []" :key="phase.id">
                <td>
                  <span v-if="phase.idx" class="muted">{{ phase.idx }}.</span> {{ phase.name }}
                </td>
                <td class="nowrap">{{ formatDuration(phase.started_at, phase.finished_at) }}</td>
                <td class="progress-cell">
                  <template v-if="phase.total">
                    <ProgressBar :done="phase.done ?? 0" :total="phase.total" />
                    <span class="muted small">{{ phase.done }} / {{ phase.total }}</span>
                  </template>
                  <span v-else class="muted">–</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="card">
        <div class="card-head"><h2>Actions</h2></div>
        <ActionTable :actions="actions.data.value?.items ?? []" show-group />
      </section>
    </template>
  </div>
</template>
