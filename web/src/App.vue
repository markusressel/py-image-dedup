<script setup lang="ts">
import { watchEffect } from 'vue'
import { api } from './api'
import { usePolling } from './polling'
import { status, statusError } from './status'
import DryRunBanner from './components/DryRunBanner.vue'

const polled = usePolling(api.status, (s) => (s?.live.busy ? 1000 : 3000))

watchEffect(() => {
  if (polled.data.value) status.value = polled.data.value
  statusError.value = polled.error.value
})
</script>

<template>
  <header class="topbar">
    <RouterLink to="/" class="brand">
      <img src="/favicon.svg" alt="" width="22" height="22" />
      py-image-dedup
    </RouterLink>
    <nav>
      <RouterLink to="/">Dashboard</RouterLink>
      <RouterLink to="/groups">
        Groups
        <span v-if="status && status.statistics.needs_review > 0" class="count warn" title="Groups that need review">
          {{ status.statistics.needs_review }}
        </span>
      </RouterLink>
      <RouterLink to="/actions">Action log</RouterLink>
      <RouterLink to="/config">
        Config
        <span v-if="status && status.statistics.failures > 0" class="count" title="Files that failed analysis">
          {{ status.statistics.failures }}
        </span>
      </RouterLink>
    </nav>
    <div class="live" :class="{ busy: status?.live.busy, offline: !!statusError }">
      <span class="dot"></span>
      <template v-if="statusError">offline</template>
      <template v-else-if="status?.live.busy">{{ status.live.phase ?? 'working' }}</template>
      <template v-else>idle</template>
    </div>
  </header>
  <DryRunBanner v-if="status" :status="status" />
  <main>
    <div v-if="statusError" class="error-box">Cannot reach the daemon: {{ statusError }}</div>
    <RouterView v-slot="{ Component }">
      <component :is="Component" />
    </RouterView>
  </main>
</template>
