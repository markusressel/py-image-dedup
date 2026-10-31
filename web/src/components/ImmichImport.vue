<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { status } from '../status'
import { fileName, formatBytes, formatPixels } from '../format'
import type { ImmichImportResult, ImmichStatus, ImportComparison } from '../types'

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

function compare(c: ImportComparison | null): string {
  if (!c) return '–'
  return `${formatPixels(c.pixel_count)}, ${formatBytes(c.file_size)}, ${c.exif_tag_count ?? '–'} EXIF tags`
}

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
      Uses the files in the Immich trash (f.ex. from its duplicate review) as your decisions for open groups
      where all copies but one are trashed in Immich. If the remaining copy is the one the prioritization rules keep,
      the others are moved to the duplicates directory. If Immich kept a different copy, nothing is moved: the group
      goes to review with Immich's choice as a suggestion you can accept. Other groups are not changed.
    </p>
    <div class="actions">
      <button :disabled="busy" @click="run(false)">Preview import of Immich trash</button>
      <button
        v-if="result && !result.applied && result.outcomes.agrees_with_rules + result.outcomes.disagrees_with_rules > 0"
        class="primary"
        :disabled="busy"
        @click="run(true)"
      >
        Apply: move duplicates of {{ result.outcomes.agrees_with_rules }} agreeing groups,
        send {{ result.outcomes.disagrees_with_rules }} to review
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
        <li>
          <strong>{{ result.outcomes.agrees_with_rules }}</strong> groups agree with the rules
          ({{ result.applied ? 'will be' : 'would be' }} moved)
        </li>
        <li>
          <strong>{{ result.outcomes.disagrees_with_rules }}</strong> groups disagree with the rules
          ({{ result.applied ? 'went' : 'go' }} to review with Immich's choice as suggestion, nothing is moved)
          <details v-if="result.disagreeing.length > 0">
            <summary>Show disagreeing groups</summary>
            <div class="table-wrap">
              <table class="table">
                <thead>
                  <tr>
                    <th>Group</th>
                    <th>Immich keeps</th>
                    <th>Rules keep</th>
                    <th>Decided by</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="d in result.disagreeing" :key="d.group_id">
                    <td>
                      <RouterLink :to="`/groups/${d.group_id}`">#{{ d.group_id }}</RouterLink>
                      <span v-if="d.already_suggested" class="muted small"> (already in review)</span>
                    </td>
                    <td>
                      <code class="path" :title="d.suggested_keep_path">{{ fileName(d.suggested_keep_path) }}</code>
                      <div class="muted small">{{ compare(d.suggested) }}</div>
                    </td>
                    <td>
                      <code class="path" :title="d.rule_keep_path ?? ''">{{ fileName(d.rule_keep_path ?? '–') }}</code>
                      <div class="muted small">{{ compare(d.rule) }}</div>
                    </td>
                    <td><code>{{ d.decided_by ?? '–' }}</code></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </details>
        </li>
        <li>{{ result.outcomes.several_remaining }} groups with more than one copy left in Immich (not changed)</li>
        <li>{{ result.outcomes.all_discarded }} groups with all copies trashed in Immich (not changed)</li>
        <li>{{ result.outcomes.already_decided_by_user }} groups you already decided (not changed)</li>
        <li>{{ result.outcomes.no_discarded_member }} groups without trashed copies</li>
      </ul>
      <p v-if="result.applied" class="muted small">
        The daemon executes the decisions of the agreeing groups after the current run.
        Disagreeing groups wait in review until you decide.
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

.import-result details {
  margin: 4px 0;
}

.import-result ul {
  margin: 4px 0;
  padding-left: 20px;
}
</style>
