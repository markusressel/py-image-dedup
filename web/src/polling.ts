import { onMounted, onUnmounted, ref, type Ref } from 'vue'

/**
 * Loads data immediately and then polls it while the tab is visible.
 * @param load fetches the data
 * @param interval polling interval in ms, a function to adapt it to the current data
 */
export function usePolling<T>(
  load: () => Promise<T>,
  interval: number | ((data: T | null) => number),
): { data: Ref<T | null>; error: Ref<string | null>; loading: Ref<boolean>; refresh: () => Promise<void> } {
  const data = ref<T | null>(null) as Ref<T | null>
  const error = ref<string | null>(null)
  const loading = ref(false)
  let timer: number | undefined
  let stopped = false
  let generation = 0

  async function refresh() {
    const current = ++generation
    loading.value = true
    try {
      const result = await load()
      if (current === generation) {
        data.value = result
        error.value = null
      }
    } catch (e) {
      if (current === generation) error.value = e instanceof Error ? e.message : String(e)
    } finally {
      if (current === generation) loading.value = false
    }
  }

  function schedule() {
    window.clearTimeout(timer)
    if (stopped) return
    const ms = typeof interval === 'function' ? interval(data.value) : interval
    timer = window.setTimeout(async () => {
      if (!document.hidden) await refresh()
      schedule()
    }, ms)
  }

  function onVisibilityChange() {
    if (!document.hidden) {
      refresh()
      schedule()
    }
  }

  onMounted(async () => {
    document.addEventListener('visibilitychange', onVisibilityChange)
    await refresh()
    schedule()
  })

  onUnmounted(() => {
    stopped = true
    window.clearTimeout(timer)
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })

  return { data, error, loading, refresh }
}
