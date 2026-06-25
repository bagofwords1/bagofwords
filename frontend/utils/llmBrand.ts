// Resolve which LLM *brand* produced a completion, so we can overlay the right
// model icon (from /public/llm_providers_icons/) on the assistant avatar.
//
// Name-first by design: a Claude / GPT / Gemini model served through AWS Bedrock
// or a custom OpenAI-compatible endpoint has a hosting provider_type of
// "bedrock" / "custom", but the meaningful brand is the model family itself.
// So we match the model id/name first and only fall back to the provider type
// when the name is unrecognizable. Unknown -> "custom" (renders a generic chip).

export type LlmBrand = 'openai' | 'anthropic' | 'google' | 'azure' | 'bedrock' | 'custom'

// Substring/loose patterns matched against the lowercased model id or name.
// Ordered by specificity; first hit wins. `o1/o3/o4` are OpenAI reasoning models.
const NAME_RULES: Array<[LlmBrand, RegExp]> = [
    ['anthropic', /claude/],
    ['google', /gemini|palm|bison|gemma/],
    ['openai', /gpt|chatgpt|davinci|babbage|(?:^|[^a-z])o[134](?:[^a-z]|$)/],
]

// Known provider types map straight through to an icon we ship.
const PROVIDER_BRANDS: Record<string, LlmBrand> = {
    openai: 'openai',
    anthropic: 'anthropic',
    google: 'google',
    azure: 'azure',
    bedrock: 'bedrock',
    custom: 'custom',
}

/**
 * @param model        The model id/name used for the completion (e.g. "claude-sonnet-4-6").
 * @param providerType Optional hosting provider type (e.g. "bedrock") as a fallback only.
 */
export function resolveModelBrand(model?: string | null, providerType?: string | null): LlmBrand {
    const name = (model || '').toLowerCase()
    for (const [brand, re] of NAME_RULES) {
        if (re.test(name)) return brand
    }
    const pt = (providerType || '').toLowerCase()
    if (PROVIDER_BRANDS[pt]) return PROVIDER_BRANDS[pt]
    return 'custom'
}
