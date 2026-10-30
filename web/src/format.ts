export function formatTime(timestamp: number | null | undefined): string {
  if (!timestamp) return '–'
  return new Date(timestamp * 1000).toLocaleString()
}

export function formatRelative(timestamp: number | null | undefined, now = Date.now() / 1000): string {
  if (!timestamp) return '–'
  const seconds = Math.round(now - timestamp)
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

export function formatDuration(start: number | null | undefined, end: number | null | undefined): string {
  if (!start) return '–'
  const seconds = Math.max(0, Math.round((end ?? Date.now() / 1000) - start))
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '–'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit++
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

export function formatPixels(pixels: number | null | undefined): string {
  if (!pixels) return '–'
  return `${(pixels / 1_000_000).toFixed(1)} MP`
}

export function fileName(path: string): string {
  const parts = path.split(/[\\/]/)
  return parts[parts.length - 1] || path
}

export function folder(path: string): string {
  const index = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  return index > 0 ? path.slice(0, index) : ''
}

export function percent(done: number, total: number): number {
  if (!total) return 0
  return Math.min(100, Math.round((done / total) * 100))
}
