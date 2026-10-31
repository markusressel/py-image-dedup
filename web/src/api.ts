import type {
  Action,
  ConfigResponse,
  Failure,
  Group,
  GroupDetail,
  GroupImmichAssets,
  ImmichImportResult,
  ImmichStatus,
  Page,
  Run,
  Status,
} from './types'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

type Params = Record<string, string | number | boolean | null | undefined>

function query(params?: Params): string {
  if (!params) return ''
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') search.set(key, String(value))
  }
  const s = search.toString()
  return s ? `?${s}` : ''
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!response.ok) {
    let message = response.statusText
    try {
      const data = await response.json()
      if (data && typeof data.detail === 'string') message = data.detail
    } catch {
      // not JSON
    }
    throw new ApiError(response.status, message)
  }
  return (await response.json()) as T
}

export const api = {
  status: () => request<Status>('GET', '/api/status'),
  runs: (params?: Params) => request<{ items: Run[] }>('GET', `/api/runs${query(params)}`),
  run: (id: number) => request<Run>('GET', `/api/runs/${id}`),
  groups: (params?: Params) => request<Page<Group>>('GET', `/api/groups${query(params)}`),
  group: (id: number) => request<GroupDetail>('GET', `/api/groups/${id}`),
  keep: (groupId: number, candidateId: number) =>
    request<GroupDetail>('POST', `/api/groups/${groupId}/keep`, { candidate_id: candidateId }),
  ignore: (groupId: number) => request<GroupDetail>('POST', `/api/groups/${groupId}/ignore`),
  reset: (groupId: number) => request<GroupDetail>('POST', `/api/groups/${groupId}/reset`),
  actions: (params?: Params) => request<Page<Action>>('GET', `/api/actions${query(params)}`),
  failures: (params?: Params) => request<Page<Failure>>('GET', `/api/failures${query(params)}`),
  config: () => request<ConfigResponse>('GET', '/api/config'),
  immich: () => request<ImmichStatus>('GET', '/api/integrations/immich'),
  groupImmich: (groupId: number) => request<GroupImmichAssets>('GET', `/api/groups/${groupId}/immich`),
  importImmichTrash: (apply: boolean) =>
    request<ImmichImportResult>('POST', '/api/integrations/immich/import-trash', { apply }),
}

export function thumbnailUrl(candidateId: number, size = 320): string {
  return `/api/candidates/${candidateId}/thumbnail?size=${size}`
}

export function imageUrl(candidateId: number): string {
  return `/api/candidates/${candidateId}/image`
}
