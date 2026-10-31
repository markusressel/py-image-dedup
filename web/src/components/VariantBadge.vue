<script setup lang="ts">
import { computed } from 'vue'
import type { VariantKind } from '../types'

const props = defineProps<{ kind: VariantKind }>()

// variants a human has to decide about (see REVIEW_VARIANT_KINDS in py_image_dedup/matching/models.py)
const REVIEW_KINDS: VariantKind[] = ['grayscale', 'colour-edited', 'crop', 'burst']

const TITLES: Record<VariantKind, string> = {
  exact: 'Identical file content',
  'pixel-identical': 'Identical pixels, only the metadata differs',
  copy: 'Recompressed and/or resized copy',
  rotated: 'Rotated/mirrored copy (without orientation tag)',
  grayscale: 'Grayscale version of a colour photo: a variant, always reviewed',
  'colour-edited': 'Colours/brightness/contrast differ (edited version): a variant, always reviewed',
  crop: 'Cropped version: a variant, always reviewed',
  burst: 'Another shot of a burst/series, not a copy: always reviewed',
}

const review = computed(() => REVIEW_KINDS.includes(props.kind))
</script>

<template>
  <span class="tag" :class="{ 'tag-warn': review }" :title="TITLES[kind] ?? kind">{{ kind }}</span>
</template>
