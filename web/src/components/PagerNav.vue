<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ offset: number; limit: number; total: number }>()
const emit = defineEmits<{ change: [offset: number] }>()

const from = computed(() => (props.total === 0 ? 0 : props.offset + 1))
const to = computed(() => Math.min(props.offset + props.limit, props.total))
</script>

<template>
  <div class="pager">
    <button :disabled="offset === 0" @click="emit('change', Math.max(0, offset - limit))">Previous</button>
    <span>{{ from }}–{{ to }} of {{ total }}</span>
    <button :disabled="offset + limit >= total" @click="emit('change', offset + limit)">Next</button>
  </div>
</template>
