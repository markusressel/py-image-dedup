import { ref } from 'vue'
import type { Status } from './types'

/** Daemon status, polled by App.vue and shared by all views. */
export const status = ref<Status | null>(null)
export const statusError = ref<string | null>(null)
