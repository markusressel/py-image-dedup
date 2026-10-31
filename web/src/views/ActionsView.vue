<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { usePolling } from '../polling'
import ActionTable from '../components/ActionTable.vue'
import PagerNav from '../components/PagerNav.vue'

const LIMIT = 100

const route = useRoute()
const router = useRouter()

const filterStatus = computed(() => (route.query.status as string | undefined) ?? '')
const search = computed(() => (route.query.q as string | undefined) ?? '')
const offset = computed(() => Number(route.query.offset ?? 0))
const searchInput = ref(search.value)

function setQuery(changes: Record<string, string | number | undefined>) {
  const query: Record<string, string> = {}
  for (const [key, value] of Object.entries({ ...route.query, offset: undefined, ...changes })) {
    if (value !== undefined && value !== '' && value !== null && value !== 0) query[key] = String(value)
  }
  router.replace({ query })
}

let timer: number | undefined
function onSearchInput() {
  window.clearTimeout(timer)
  timer = window.setTimeout(() => setQuery({ q: searchInput.value }), 250)
}

const actions = usePolling(
  () => api.actions({ status: filterStatus.value, q: search.value, limit: LIMIT, offset: offset.value }),
  5000,
)
watch(() => route.query, () => actions.refresh(), { deep: true })

const FILTERS = [
  { value: '', label: 'All' },
  { value: 'done', label: 'Done' },
  { value: 'simulated', label: 'Would happen (dry run)' },
  { value: 'skipped', label: 'Skipped' },
  { value: 'failed', label: 'Failed' },
]
</script>

<template>
  <div class="stack">
    <div class="toolbar">
      <div class="segmented">
        <button
          v-for="f in FILTERS"
          :key="f.value"
          :class="{ active: filterStatus === f.value }"
          @click="setQuery({ status: f.value })"
        >
          {{ f.label }}
        </button>
      </div>
      <input v-model="searchInput" class="search" type="search" placeholder="Filter by path…" @input="onSearchInput" />
    </div>
    <div v-if="actions.error.value" class="error-box">{{ actions.error.value }}</div>
    <section class="card">
      <ActionTable :actions="actions.data.value?.items ?? []" show-group @changed="actions.refresh()" />
    </section>
    <PagerNav
      v-if="actions.data.value && actions.data.value.total > LIMIT"
      :offset="offset"
      :limit="LIMIT"
      :total="actions.data.value.total"
      @change="(o) => setQuery({ offset: o })"
    />
  </div>
</template>
