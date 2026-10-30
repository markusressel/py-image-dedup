<script setup lang="ts">
import { computed } from 'vue'
import { percent } from '../format'

const props = defineProps<{ done: number; total: number }>()
const value = computed(() => percent(props.done, props.total))
</script>

<template>
  <div class="progress" role="progressbar" :aria-valuenow="value" aria-valuemin="0" aria-valuemax="100">
    <div class="progress-fill" :style="{ width: `${value}%` }"></div>
  </div>
</template>
