<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ value: string }>()

const LABELS: Record<string, string> = {
  review: 'needs review',
  open: 'open',
  resolved: 'resolved',
  ignored: 'ignored',
  done: 'done',
  simulated: 'would happen',
  skipped: 'skipped',
  failed: 'failed',
  running: 'running',
  finished: 'finished',
  interrupted: 'interrupted',
}

const label = computed(() => LABELS[props.value] ?? props.value)
</script>

<template>
  <span class="badge" :class="`badge-${value}`">{{ label }}</span>
</template>
