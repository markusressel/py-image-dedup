<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../api'
import { status } from '../status'
import type { Action } from '../types'
import { fileName, formatTime } from '../format'
import StatusBadge from './StatusBadge.vue'

defineProps<{ actions: Action[]; showGroup?: boolean }>()
const emit = defineEmits<{ changed: [] }>()

const ACTION_LABELS: Record<string, string> = {
  move: 'move',
  delete: 'delete',
  remove_folder: 'remove folder',
  restore: 'restore',
}

const busy = ref<number | null>(null)
const error = ref<string | null>(null)

async function undo(action: Action) {
  const dryRun = status.value?.user_decisions_dry_run ?? true
  const question =
    `Move ${action.target} back to ${action.path}?\n\n` +
    'This only happens if nothing exists at the original location. The group of the file is set to ignored, ' +
    'so it is not moved again.' +
    (dryRun ? '\n\nDry run is enabled for your decisions: this is only simulated.' : '')
  if (!window.confirm(question)) return
  busy.value = action.id
  error.value = null
  try {
    await api.undoAction(action.id)
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <div v-if="error" class="error-box">{{ error }}</div>
  <div class="table-wrap">
    <table class="table">
      <thead>
        <tr>
          <th>Time</th>
          <th>Action</th>
          <th>Status</th>
          <th>File</th>
          <th>Decided by</th>
          <th v-if="showGroup">Group</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="action in actions" :key="action.id">
          <td class="nowrap muted">{{ formatTime(action.created_at) }}</td>
          <td class="nowrap">
            <span :class="`action-${action.action}`">{{ ACTION_LABELS[action.action] ?? action.action }}</span>
          </td>
          <td class="nowrap"><StatusBadge :value="action.status" /></td>
          <td class="path-cell">
            <div :title="action.path">{{ fileName(action.path) }}</div>
            <div class="muted small path" :title="action.path">{{ action.path }}</div>
            <div v-if="action.target" class="muted small path" :title="action.target">
              {{ action.action === 'restore' ? '← from' : '→' }} {{ action.target }}
            </div>
            <div v-if="action.message" class="small" :class="{ 'text-danger': action.status === 'failed' }">
              {{ action.message }}
            </div>
          </td>
          <td class="nowrap">{{ action.source === 'user' ? 'you (web UI)' : 'rules' }}</td>
          <td v-if="showGroup" class="nowrap">
            <RouterLink v-if="action.group_id" :to="`/groups/${action.group_id}`">#{{ action.group_id }}</RouterLink>
            <span v-else class="muted">–</span>
          </td>
          <td class="nowrap">
            <span v-if="action.restore_requested" class="tag tag-info">restore queued</span>
            <button
              v-else-if="action.undoable"
              :disabled="busy !== null"
              title="Move the file back to its original location"
              @click="undo(action)"
            >
              Undo
            </button>
          </td>
        </tr>
        <tr v-if="actions.length === 0">
          <td :colspan="showGroup ? 7 : 6" class="muted center">No actions</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
