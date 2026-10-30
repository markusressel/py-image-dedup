<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api'
import { usePolling } from '../polling'
import { status } from '../status'
import { formatTime } from '../format'

const config = usePolling(api.config, 30000)
const failures = usePolling(() => api.failures({ limit: 200 }), 30000)
const filter = ref('')

const entries = computed(() => {
  const all = config.data.value?.entries ?? []
  const f = filter.value.toLowerCase()
  return f ? all.filter((e) => e.key.toLowerCase().includes(f) || e.env.toLowerCase().includes(f)) : all
})

const decidedBy = computed(() => {
  const stats = status.value?.statistics.decided_by ?? {}
  const weak = new Set(config.data.value?.weak_rules ?? [])
  return Object.entries(stats)
    .map(([rule, count]) => ({ rule, count, weak: weak.has(rule) }))
    .sort((a, b) => b.count - a.count)
})

function showItem(value: unknown): string {
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    const entries = Object.entries(value)
    // prioritization rules: {"name": "..."}
    if (entries.length === 1 && entries[0][0] === 'name') return String(entries[0][1])
    return JSON.stringify(value)
  }
  return String(value)
}

function show(value: unknown): string {
  if (value === null || value === undefined) return '–'
  if (Array.isArray(value)) return value.length ? value.map(showItem).join('\n') : '[]'
  return showItem(value)
}

function isDefault(value: unknown, defaultValue: unknown): boolean {
  return JSON.stringify(value) === JSON.stringify(defaultValue)
}
</script>

<template>
  <div class="stack">
    <div v-if="config.error.value" class="error-box">{{ config.error.value }}</div>

    <section class="card">
      <div class="card-head"><h2>Hints</h2></div>
      <ul v-if="config.data.value?.hints.length" class="hints">
        <li v-for="(hint, i) in config.data.value.hints" :key="i" :class="`hint-${hint.level}`">
          <code>{{ hint.key }}</code>
          <span>{{ hint.message }}</span>
          <RouterLink v-if="hint.link" :to="hint.link">show</RouterLink>
        </li>
      </ul>
      <p v-else class="muted">No hints, the configuration looks fine.</p>
    </section>

    <section v-if="decidedBy.length" class="card">
      <div class="card-head">
        <h2>Deciding rules</h2>
        <span class="muted small">which prioritization rule chose the kept copy (ignored groups excluded)</span>
      </div>
      <div class="table-wrap">
        <table class="table">
          <thead><tr><th>Rule</th><th>Groups</th><th></th></tr></thead>
          <tbody>
            <tr v-for="row in decidedBy" :key="row.rule">
              <td><code>{{ row.rule }}</code> <span v-if="row.weak" class="tag tag-warn">weak tie-breaker</span></td>
              <td>{{ row.count }}</td>
              <td><RouterLink :to="{ path: '/groups', query: { decided_by: row.rule } }">show groups</RouterLink></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section id="failures" class="card">
      <div class="card-head">
        <h2>Files that failed analysis</h2>
        <span class="muted small">{{ failures.data.value?.total ?? 0 }} files</span>
      </div>
      <div class="table-wrap">
        <table class="table">
          <thead><tr><th>File</th><th>Error</th><th>Count</th><th>Last seen</th></tr></thead>
          <tbody>
            <tr v-for="f in failures.data.value?.items ?? []" :key="f.id">
              <td class="path-cell"><span class="path">{{ f.path }}</span></td>
              <td class="small">{{ f.error }}</td>
              <td>{{ f.count }}</td>
              <td class="nowrap muted">{{ formatTime(f.last_seen_at) }}</td>
            </tr>
            <tr v-if="failures.data.value && failures.data.value.items.length === 0">
              <td colspan="4" class="muted center">No failures</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>Effective configuration</h2>
        <input v-model="filter" class="search" type="search" placeholder="Filter…" />
      </div>
      <p class="muted small">
        Read-only. Change it via the YAML file or the environment variables shown here and restart the daemon.
      </p>
      <div class="table-wrap">
        <table class="table config">
          <thead><tr><th>Key / environment variable</th><th>Value</th><th>Description</th></tr></thead>
          <tbody>
            <tr v-for="entry in entries" :key="entry.key">
              <td>
                <code>{{ entry.key.replace(/^py_image_dedup\./, '') }}</code>
                <div class="muted small"><code>{{ entry.env }}</code></div>
              </td>
              <td>
                <pre class="value">{{ show(entry.value) }}</pre>
                <div v-if="!isDefault(entry.value, entry.default)" class="muted small">
                  default: {{ show(entry.default) }}
                </div>
              </td>
              <td class="small">{{ entry.description }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
