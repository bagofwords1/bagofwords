// Composer slash commands: text the composer runs itself instead of sending
// it to the agent. Keep the registry tiny and explicit — a command must be
// typed as the whole message ("/compact …"), so ordinary prompts that merely
// start with a slash ("/etc/hosts is…") are never swallowed.

export interface SlashCommandDef {
    name: string
    // i18n keys under prompt.slash.*
    descriptionKey: string
    usageKey: string
}

export const SLASH_COMMANDS: SlashCommandDef[] = [
    {
        name: 'compact',
        descriptionKey: 'prompt.slash.compact.description',
        usageKey: 'prompt.slash.compact.usage',
    },
]

export interface ParsedSlashCommand {
    name: string
    args: string
}

const COMMAND_RE = /^\s*\/([a-z][a-z0-9-]*)(?:\s+([\s\S]*))?$/i

export function parseSlashCommand(text: string): ParsedSlashCommand | null {
    const m = COMMAND_RE.exec(text || '')
    if (!m) return null
    const name = m[1].toLowerCase()
    if (!SLASH_COMMANDS.some(c => c.name === name)) return null
    return { name, args: (m[2] || '').trim() }
}

// Commands matching what the user is typing ("/", "/co", "/compact").
// Only while the first word is still being typed — once there is a space the
// user is writing arguments and the menu gets out of the way.
export function matchingSlashCommands(text: string): SlashCommandDef[] {
    const m = /^\/([a-z0-9-]*)$/i.exec((text || '').trimStart())
    if (!m) return []
    const prefix = m[1].toLowerCase()
    return SLASH_COMMANDS.filter(c => c.name.startsWith(prefix))
}

export const COMPACT_INSTRUCTIONS_MAX = 800
export const COMPACT_KEEP_MAX = 20

export interface CompactArgs {
    instructions?: string
    keep?: number
}

// "/compact"                      → {}
// "/compact keep 4"               → { keep: 4 }
// "/compact focus on churn"       → { instructions: "focus on churn" }
export function parseCompactArgs(args: string): CompactArgs {
    const a = (args || '').trim()
    if (!a) return {}
    const keep = /^keep\s+(\d{1,3})$/i.exec(a)
    if (keep) {
        const n = Math.max(1, Math.min(COMPACT_KEEP_MAX, parseInt(keep[1], 10)))
        return { keep: n }
    }
    return { instructions: a.slice(0, COMPACT_INSTRUCTIONS_MAX) }
}
