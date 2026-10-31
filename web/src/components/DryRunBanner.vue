<script setup lang="ts">
import type { Status } from '../types'

defineProps<{ status: Status }>()
</script>

<template>
  <div v-if="status.dry_run && status.user_decisions_dry_run" class="banner dry-run">
    <strong>Dry run is active.</strong>
    Nothing on disk is moved or deleted, also not by decisions made here.
    Everything shown as an action is what <em>would</em> happen.
  </div>
  <div v-else-if="status.dry_run" class="banner dry-run">
    <strong>Dry run is active for the prioritization rules.</strong>
    Decisions made here (including undo) are executed: duplicates are moved to
    <code>{{ status.duplicates_target_directory ?? '–' }}</code>.
  </div>
  <div v-else-if="!status.duplicates_target_directory" class="banner danger">
    <strong>Live mode, duplicates are deleted.</strong>
    No <code>duplicates_target_directory</code> is configured. Decisions made here are disabled because they only
    move files.
  </div>
  <div v-else class="banner live-mode">
    <strong>Live mode.</strong>
    Duplicates are moved to <code>{{ status.duplicates_target_directory }}</code>.
  </div>
</template>
