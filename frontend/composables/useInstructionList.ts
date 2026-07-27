// Loading instruction rows for list / tree surfaces.
//
// Every caller used to fire a single capped request and render `items` as if it
// were the whole set — no pagination, and `total` was thrown away. On an org
// past the cap that silently dropped the oldest instructions with nothing in the
// UI to say so, which reads as "some of my instructions are just gone".
//
// These helpers ask for the light projection (`view=light`: no instruction body,
// no nested user records, a `preview` prefix instead) and keep paging until
// `items.length >= total`, so a surface either has every row it is entitled to
// or throws.

export const INSTRUCTION_PAGE_SIZE = 500

export interface InstructionListResult<T = any> {
  items: T[]
  total: number
}

/**
 * Fetch every instruction matching `query`, paging through the light list.
 *
 * `query` takes the same filters as `GET /instructions` minus paging — `skip`,
 * `limit` and `view` are managed here and any caller-supplied values for them
 * are ignored.
 */
export async function fetchAllInstructions<T = any>(
  query: Record<string, any>,
): Promise<InstructionListResult<T>> {
  const { skip: _s, limit: _l, view: _v, ...filters } = query || {}
  const items: T[] = []
  let total = 0

  for (let skip = 0; ; skip += INSTRUCTION_PAGE_SIZE) {
    const { data, error } = await useMyFetch<any>('/api/instructions', {
      method: 'GET',
      query: { ...filters, view: 'light', skip, limit: INSTRUCTION_PAGE_SIZE },
    })
    // Surface the failure instead of returning a short list that looks complete
    // — a partial result presented as whole is the bug this replaces.
    if (error?.value) throw error.value

    const payload: any = (data as any)?.value
    const page: T[] = payload?.items || []
    total = Number(payload?.total ?? page.length)
    items.push(...page)

    // An empty page also terminates: without it a `total` that disagrees with
    // what the filters actually return (a post-query cut such as the per-user
    // table-accessibility filter) would loop forever.
    if (!page.length || items.length >= total) break
  }

  return { items, total }
}

/** The label a tree/list row shows: title, else the body prefix, else a stub. */
export function instructionRowLabel(ins: any, max = 60): string {
  const title = (ins?.title || '').trim()
  if (title) return title
  const body = (ins?.preview ?? ins?.text ?? '') as string
  return body.split('\n')[0].slice(0, max) || 'Untitled'
}

/** Text a client-side filter can match on. The full body is not loaded here;
 *  `preview` covers the head of it, and the server `search` filter covers the
 *  rest for callers that need it. */
export function instructionSearchText(ins: any): string {
  return `${ins?.title || ''}\n${ins?.preview ?? ins?.text ?? ''}`.toLowerCase()
}
