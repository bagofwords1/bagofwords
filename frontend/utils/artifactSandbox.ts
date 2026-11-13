// The one place that spells the artifact iframe's `sandbox` attribute, so the
// editor (ArtifactFrame), the reader (SharedDashboardView) and any fullscreen
// view stay identical.
//
// Base: scripts run, forms post, downloads are allowed — but no popups, no
// top-navigation and (critically) NO allow-same-origin, since the artifact is
// an LLM-generated srcdoc document on BOW's own origin.
//
// When a dashboard opts in (allow_popups, set in its Embed tab) we add exactly
// three tokens:
//   allow-popups                    — links/buttons may open a new tab/window
//   allow-popups-to-escape-sandbox  — that opened tab loads as a normal page
//                                     (without it an external site like
//                                     ServiceNow inherits the sandbox and breaks)
//   allow-modals                    — the app may use alert()/confirm()/prompt()
// We deliberately never add allow-same-origin or allow-top-navigation here.
export const ARTIFACT_SANDBOX_BASE = 'allow-scripts allow-downloads allow-forms'
export const ARTIFACT_SANDBOX_POPUPS = 'allow-popups allow-popups-to-escape-sandbox allow-modals'

export function artifactSandbox(allowPopups?: boolean | null): string {
  return allowPopups ? `${ARTIFACT_SANDBOX_BASE} ${ARTIFACT_SANDBOX_POPUPS}` : ARTIFACT_SANDBOX_BASE
}
