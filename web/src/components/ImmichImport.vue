<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { status } from '../status'
import type { ImmichImportResult, ImmichStatus } from '../types'

const immich = ref<ImmichStatus | null>(null)
const result = ref<ImmichImportResult | null>(null)
const error = ref<string | null>(null)
const busy = ref(false)

const trashedText = computed(() => {
  const r = result.value
  if (!r) return ''
  const unmapped = r.unmapped
    ? ` (${r.unmapped} outside the configured path mappings, f.ex. ${r.unmapped_examples[0]})`
    : ''
  return `${r.trashed} files in the Immich trash${unmapped}.`
})

onMounted(async () => {
  try {
    immich.value = await api.immich()
  } catch {
    // older backend or not reachable: just hide the card
  }
})

async function run(apply: boolean) {
  busy.value = true
  error.value = null
  try {
    result.value = await api.importImmichTrash(apply)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section v-if="immich?.configured" class="card">
    <div class="card-head">
      <h2>Immich</h2>
      <span class="muted small">{{ immich.url }}</span>
    </div>
    <p class="muted small">
      Uses the files in the Immich trash (f.ex. from its duplicate review) as your decisions: in every open group
      where all copies but one are trashed in Immich, the remaining copy is kept and the others are moved to the
      duplicates directory. Other groups are not changed.
    </p>
    <div class="actions">
      <button :disabled="busy" @click="run(false)">Preview import of Immich trash</button>
      <button
        v-if="result && !result.applied && result.outcomes.decided > 0"
        class="primary"
        :disabled="busy"
        @click="run(true)"
      >
        Apply {{ result.outcomes.decided }} decisions
      </button>
    </div>
    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="result" class="import-result">
      <p>
        <strong v-if="result.applied">Applied.</strong>
        <strong v-else>Preview, nothing changed yet.</strong>
        {{ trashedText }}
      </p>
      <ul>
        <li><strong>{{ result.outcomes.decided }}</strong> groups {{ result.applied ? 'are' : 'would be' }} decided</li>
        <li>{{ result.outcomes.several_remaining }} groups with more than one copy left in Immich (not changed)</li>
        <li>{{ result.outcomes.all_discarded }} groups with all copies trashed in Immich (not changed)</li>
        <li>{{ result.outcomes.already_decided_by_user }} groups you already decided (not changed)</li>
        <li>{{ result.outcomes.no_discarded_member }} groups without trashed copies</li>
      </ul>
      <p v-if="result.applied" class="muted small">
        The daemon executes the decisions after the current run.
        <template v-if="status?.user_decisions_dry_run">
          Dry run is enabled for your decisions, so they are only simulated.
        </template>
      </p>
    </div>
  </section>
</template>

<style scoped>
.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 8px 0;
}

.import-result ul {
  margin: 4px 0;
  padding-left: 20px;
}
</style>
