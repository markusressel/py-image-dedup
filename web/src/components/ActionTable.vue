<script setup lang="ts">
import type { Action } from '../types'
import { fileName, formatTime } from '../format'
import StatusBadge from './StatusBadge.vue'

defineProps<{ actions: Action[]; showGroup?: boolean }>()

const ACTION_LABELS: Record<string, string> = {
  move: 'move',
  delete: 'delete',
  remove_folder: 'remove folder',
}
</script>

<template>
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
            <div v-if="action.target" class="muted small path" :title="action.target">→ {{ action.target }}</div>
            <div v-if="action.message" class="small" :class="{ 'text-danger': action.status === 'failed' }">
              {{ action.message }}
            </div>
          </td>
          <td class="nowrap">{{ action.source === 'user' ? 'you (web UI)' : 'rules' }}</td>
          <td v-if="showGroup" class="nowrap">
            <RouterLink v-if="action.group_id" :to="`/groups/${action.group_id}`">#{{ action.group_id }}</RouterLink>
            <span v-else class="muted">–</span>
          </td>
        </tr>
        <tr v-if="actions.length === 0">
          <td :colspan="showGroup ? 6 : 5" class="muted center">No actions</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
