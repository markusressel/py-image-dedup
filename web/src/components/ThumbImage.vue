<script setup lang="ts">
import { ref } from 'vue'
import { thumbnailUrl } from '../api'

defineProps<{ candidateId: number; size?: number; alt?: string }>()
const failed = ref(false)
</script>

<template>
  <div class="thumb">
    <img
      v-if="!failed"
      :src="thumbnailUrl(candidateId, size ?? 320)"
      :alt="alt ?? ''"
      loading="lazy"
      decoding="async"
      @error="failed = true"
    />
    <span v-else class="thumb-missing">no preview</span>
  </div>
</template>
