// Composer slash commands: text the composer runs itself instead of sending
// it to the agent. Keep the registry tiny and explicit — a command must be
// typed as the whole message ("/compact …"), so ordinary prompts that merely
// start with a slash ("/etc/hosts is…") are never swallowed.

export interface SlashCommandDef {
    name: string
    // i18n keys under prompt.slash.*
    descriptionKey: string
    usageKey: string
    // false: the command is only the bare word ("/clear"). With anything
    // after it the text is an ordinary prompt and goes to the agent.
    takesArgs?: boolean
    // Menu icon (Nuxt UI icon name).
    icon?: string
}

export const SLASH_COMMANDS: SlashCommandDef[] = [
    {
        name: 'compact',
        descriptionKey: 'prompt.slash.compact.description',
        usageKey: 'prompt.slash.compact.usage',
        takesArgs: true,
        icon: 'i-heroicons-archive-box-arrow-down',
    },
    {
        name: 'clear',
        descriptionKey: 'prompt.slash.clear.description',
        usageKey: 'prompt.slash.clear.usage',
        takesArgs: false,
        icon: 'i-heroicons-scissors',
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
    const def = SLASH_COMMANDS.find(c => c.name === name)
    if (!def) return null
    const args = (m[2] || '').trim()
    if (args && def.takesArgs === false) return null
    return { name, args }
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
