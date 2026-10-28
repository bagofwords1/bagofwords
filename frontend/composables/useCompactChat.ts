// Shared logic for the compact chat surfaces: the artifact-page bubble
// (components/report/ArtifactChatBubble.vue) and embedded chat
// (components/embed/EmbedChat.vue). Both render the same v2 completion
// blocks and consume the same SSE events as the report page, just smaller.
//
// Transport-agnostic on purpose: callers hand in a `fetch`-like function that
// already carries their credentials (the app session, or an embed session held
// in iframe memory), and this file only knows how to parse frames and fold
// events into the optimistic assistant message.

export type CompactMessage = {
    id: string
    role: 'user' | 'system' | string
    status: string
    prompt?: { content?: string } | null
    completion_blocks?: any[]
    created_at?: string
    // The system completion a user turn produced (set from completion.started
    // so the UI can reconnect / stop a run it did not finish watching).
    system_completion_id?: string
}

// Verb labels matching the report page's tool headers for the tools the
// compact surfaces are likely to see. Unknown tools fall back to a prettified
// tool_name so a new tool never renders blank.
export const COMPACT_TOOL_LABELS: Record<string, { running: string; done: string }> = {
    create_data: { running: 'Creating data', done: 'Created data' },
    describe_tables: { running: 'Describing tables', done: 'Described tables' },
    describe_entity: { running: 'Reading entity', done: 'Read entity' },
    read_query: { running: 'Reading query', done: 'Read query' },
    run_query: { running: 'Running query', done: 'Ran query' },
    read_artifact: { running: 'Reading dashboard', done: 'Read dashboard' },
    inspect_data: { running: 'Inspecting data', done: 'Inspected data' },
    search_files: { running: 'Searching files', done: 'Searched files' },
    grep_files: { running: 'Searching files', done: 'Searched files' },
    list_files: { running: 'Listing files', done: 'Listed files' },
    read_file: { running: 'Reading file', done: 'Read file' },
    clarify: { running: 'Asking a question', done: 'Asked a question' },
    execute_mcp: { running: 'Calling tool', done: 'Called tool' },
    call_tool: { running: 'Calling tool', done: 'Called tool' },
    search_tools: { running: 'Finding tools', done: 'Found tools' },
    web_fetch: { running: 'Reading page', done: 'Read page' },
}

export function compactToolLabel(block: any): string {
    const name: string = block.tool_execution?.tool_name || ''
    const entry = COMPACT_TOOL_LABELS[name]
    if (entry) return block.status === 'in_progress' ? entry.running : entry.done
    // Native MCP tools stream as mcp__<connection>__<tool>: show the tool part.
    const bare = name.startsWith('mcp__') ? name.split('__').pop() || name : name
    return bare.replace(/_/g, ' ').replace(/^./, (c: string) => c.toUpperCase())
}

// The tool's own human title ("Finding sales.csv", "Revenue by region").
// Deliberately NOT block.title — that is planner bookkeeping and never reads
// well in a compact surface.
export function compactToolDetail(block: any): string {
    const te = block.tool_execution || {}
    const args = te.arguments_json || {}
    let detail: string = args.title || ''
    // MCP calls carry no title: name the tool that was called instead.
    const name: string = te.tool_name || ''
    if (!detail && (name === 'execute_mcp' || name === 'call_tool') && args.tool_name) detail = String(args.tool_name)
    if (!detail && name.startsWith('mcp__')) detail = name.split('__').pop() || ''
    return detail && detail.toLowerCase() !== compactToolLabel(block).toLowerCase() ? detail : ''
}

// Sub-second calls are the norm; a "0.1s" on every line is just noise.
export function compactToolDuration(block: any): string {
    const ms = block.tool_execution?.duration_ms ?? block.duration_ms
    if (!ms || ms < 1000 || block.status === 'in_progress') return ''
    return `${(ms / 1000).toFixed(1)}s`
}

export function orderedCompactBlocks(msg: any) {
    return (msg?.completion_blocks || [])
        .slice()
        // Planner bookkeeping blocks ("Planning (action)" etc.) are noise in a
        // compact surface — the spinner placeholder already covers "working".
        .filter((b: any) => b.content || b.tool_execution || (b.title && !/^planning/i.test(b.title)))
        .sort((a: any, b: any) => (a.block_index ?? 0) - (b.block_index ?? 0))
}

export function messageHasContent(msg: any) {
    return (msg?.completion_blocks || []).some((b: any) => b.content)
}

// A pending tool approval ('ask' policy) on a block, if the run is paused on it.
export function pendingApproval(block: any) {
    const te = block?.tool_execution
    const c = te?.confirmation
    if (!c?.confirmation_id) return null
    if (te.status !== 'running' && block.status !== 'in_progress') return null
    const stage = te.progress_stage || ''
    if (stage && !['awaiting_confirmation', 'awaiting_approval'].includes(stage)) return null
    return c
}

/**
 * Fold one SSE event into the optimistic assistant message. Returns true when
 * something visible changed (so callers can scroll / refresh spinners).
 */
export function applyCompactEvent(evt: any, assistantMsg: CompactMessage, userMsg?: CompactMessage): boolean {
    const kind = evt?.event
    const blocks = (assistantMsg.completion_blocks ||= [])
    if (kind === 'completion.started') {
        if (evt.data?.system_completion_id) {
            assistantMsg.system_completion_id = evt.data.system_completion_id
            assistantMsg.id = evt.data.system_completion_id
            if (userMsg && evt.completion_id) userMsg.id = evt.completion_id
        }
        return false
    }
    if (kind === 'block.upsert' && evt.data?.block) {
        const block = evt.data.block
        const idx = blocks.findIndex((b: any) => b.id === block.id)
        if (idx >= 0) blocks[idx] = block
        else blocks.push(block)
        return true
    }
    if (kind === 'block.delta.text' && evt.data?.block_id) {
        // Full overwrite snapshot for a block's text field.
        const b = blocks.find((x: any) => x.id === evt.data.block_id)
        if (b && evt.data.field === 'content' && evt.data.text) {
            b.content = evt.data.text
            return true
        }
        return false
    }
    if (kind === 'block.delta.token' && evt.data?.block_id) {
        // Per-token append for the typing effect.
        const b = blocks.find((x: any) => x.id === evt.data.block_id)
        if (b && evt.data.field === 'content' && evt.data.token) {
            b.content = (b.content || '') + String(evt.data.token)
            return true
        }
        return false
    }
    if (kind === 'completion.finished') {
        assistantMsg.status = evt.data?.status || 'success'
        return true
    }
    if (kind === 'completion.error') {
        assistantMsg.status = 'error'
        return true
    }
    return false
}

/**
 * Read an SSE response body frame by frame and hand each JSON event to `onEvent`.
 * Resolves when the stream ends (or the reader throws). Ignores comments and
 * the trailing `[DONE]` sentinel.
 */
export async function readSseStream(response: Response, onEvent: (evt: any) => void, signal?: AbortSignal) {
    const reader = response.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    const onAbort = () => { try { reader.cancel() } catch { /* already closed */ } }
    signal?.addEventListener('abort', onAbort, { once: true })
    try {
        while (true) {
            const { done, value } = await reader.read()
            if (done) break
            buffer += decoder.decode(value, { stream: true })
            // SSE frames are separated by a blank line.
            const frames = buffer.split('\n\n')
            buffer = frames.pop() || ''
            for (const frame of frames) {
                const dataLine = frame.split('\n').find(l => l.startsWith('data: '))
                if (!dataLine) continue
                const raw = dataLine.slice(6)
                if (raw === '[DONE]') continue
                let evt: any
                try { evt = JSON.parse(raw) } catch { continue }
                onEvent(evt)
            }
        }
    } finally {
        signal?.removeEventListener('abort', onAbort)
    }
}

export function newUserMessage(content: string): CompactMessage {
    return { id: `local-user-${Date.now()}`, role: 'user', status: 'success', prompt: { content }, created_at: new Date().toISOString() }
}

export function newAssistantMessage(): CompactMessage {
    return { id: `local-sys-${Date.now()}`, role: 'system', status: 'in_progress', completion_blocks: [], created_at: new Date().toISOString() }
}

export function sortByCreated(list: any[]) {
    return list.slice().sort((a: any, b: any) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime())
}
