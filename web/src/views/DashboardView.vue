<script setup lang="ts">
import { computed } from 'vue'
import { api } from '../api'
import { usePolling } from '../polling'
import { status } from '../status'
import { formatDuration, formatRelative, formatTime, percent } from '../format'
import ProgressBar from '../components/ProgressBar.vue'
import StatusBadge from '../components/StatusBadge.vue'

const runs = usePolling(() => api.runs({ limit: 10 }), () => (status.value?.live.busy ? 3000 : 10000))

const live = computed(() => status.value?.live)
const stats = computed(() => status.value?.statistics)

function actionCount(action: string, actionStatus: string): number {
  return stats.value?.actions[action]?.[actionStatus] ?? 0
}

const wouldMove = computed(() => actionCount('move', 'simulated') + actionCount('delete', 'simulated'))
const didMove = computed(() => actionCount('move', 'done') + actionCount('delete', 'done'))
const failedActions = computed(() => actionCount('move', 'failed') + actionCount('delete', 'failed'))
</script>

<template>
  <div v-if="status && live && stats" class="stack">
    <section class="card">
      <div class="card-head">
        <h2>Now</h2>
        <span class="muted small">last activity {{ formatRelative(live.updated_at, status.server_time) }}</span>
      </div>
      <template v-if="live.busy">
        <div class="phase-line">
          <strong>
            <template v-if="live.phase_index && live.phase_count">
              Phase {{ live.phase_index }}/{{ live.phase_count }}:
            </template>
            {{ live.phase ?? 'Working' }}
          </strong>
          <span class="muted">
            {{ live.run_kind }} run
            <RouterLink v-if="live.run_id" :to="`/runs/${live.run_id}`">#{{ live.run_id }}</RouterLink>,
            running for {{ formatDuration(live.started_at, status.server_time) }}
          </span>
        </div>
        <div v-if="live.task" class="task">
          <div class="task-line">
            <span>{{ live.task }}</span>
            <span class="muted nowrap">{{ live.done }} / {{ live.total }} ({{ percent(live.done, live.total) }}%)</span>
          </div>
          <ProgressBar :done="live.done" :total="live.total" />
          <div v-if="live.current_item" class="muted small path">{{ live.current_item }}</div>
        </div>
      </template>
      <p v-else class="idle">
        Idle, watching the source directories for changes.
        <template v-if="status.last_run">
          Last run {{ formatRelative(status.last_run.finished_at ?? status.last_run.started_at, status.server_time) }}.
        </template>
      </p>
      <div class="muted small">
        Change queue: <strong>{{ live.queue_size }}</strong> path(s) waiting to be processed
      </div>
    </section>

    <section class="tiles">
      <RouterLink to="/groups?status=review" class="tile" :class="{ attention: stats.groups.review > 0 }">
        <span class="tile-value">{{ stats.groups.review }}</span>
        <span class="tile-label">groups need review</span>
      </RouterLink>
      <RouterLink to="/groups?status=open" class="tile">
        <span class="tile-value">{{ stats.groups.open }}</span>
        <span class="tile-label">open groups</span>
      </RouterLink>
      <RouterLink to="/groups" class="tile">
        <span class="tile-value">{{ stats.duplicate_files_open }}</span>
        <span class="tile-label">duplicate files {{ status.dry_run ? 'found' : 'pending' }}</span>
      </RouterLink>
      <RouterLink to="/groups?status=resolved" class="tile">
        <span class="tile-value">{{ stats.groups.resolved }}</span>
        <span class="tile-label">resolved groups</span>
      </RouterLink>
      <RouterLink to="/actions?status=simulated" class="tile">
        <span class="tile-value">{{ wouldMove }}</span>
        <span class="tile-label">simulated actions (dry run)</span>
      </RouterLink>
      <RouterLink to="/actions?status=done" class="tile">
        <span class="tile-value">{{ didMove }}</span>
        <span class="tile-label">files moved/deleted</span>
      </RouterLink>
      <RouterLink v-if="failedActions > 0" to="/actions?status=failed" class="tile attention">
        <span class="tile-value">{{ failedActions }}</span>
        <span class="tile-label">failed actions</span>
      </RouterLink>
      <RouterLink to="/config#failures" class="tile" :class="{ attention: stats.failures > 0 }">
        <span class="tile-value">{{ stats.failures }}</span>
        <span class="tile-label">files failed analysis</span>
      </RouterLink>
      <div v-if="stats.execute_requested > 0" class="tile attention">
        <span class="tile-value">{{ stats.execute_requested }}</span>
        <span class="tile-label">your decisions waiting to be executed</span>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>Recent runs</h2>
      </div>
      <div v-if="runs.error.value" class="error-box">{{ runs.error.value }}</div>
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>Run</th>
              <th>Status</th>
              <th>Started</th>
              <th>Duration</th>
              <th>Duplicates</th>
              <th>{{ status.dry_run ? 'Would move/delete' : 'Moved/deleted' }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="run in runs.data.value?.items ?? []" :key="run.id">
              <td class="nowrap">
                <RouterLink :to="`/runs/${run.id}`">#{{ run.id }} {{ run.kind }}</RouterLink>
                <span v-if="run.dry_run" class="tag">dry run</span>
                <div v-if="run.trigger" class="muted small path">{{ run.trigger }}</div>
              </td>
              <td><StatusBadge :value="run.status" /></td>
              <td class="nowrap" :title="formatTime(run.started_at)">
                {{ formatRelative(run.started_at, status.server_time) }}
              </td>
              <td class="nowrap">{{ formatDuration(run.started_at, run.finished_at ?? status.server_time) }}</td>
              <td>{{ run.summary?.files_with_duplicates ?? '–' }}</td>
              <td>{{ run.summary ? run.summary.moved + run.summary.deleted : '–' }}</td>
            </tr>
            <tr v-if="runs.data.value && runs.data.value.items.length === 0">
              <td colspan="6" class="muted center">No runs yet</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
  <div v-else class="muted">Loading…</div>
</template>
