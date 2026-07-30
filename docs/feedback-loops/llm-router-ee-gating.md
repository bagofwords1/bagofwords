# Feedback Loop — "the new llm router should be enterprise-only"

The Auto model router (docs/design/auto-model-routing.md) shipped ungated: any
org could enable `model_routing`, write per-model routing hints, and have the
completion resolver start requests on the small model. This loop validates that
routing is now an **Enterprise** feature — visible but locked without a license,
and a hard runtime no-op — while community behavior (the resolved default runs)
is unchanged.

## Root cause (validated)

Routing had no license check at any of its three surfaces:

- Enabling the org toggle — `organization_settings_service.update_settings`
  (`backend/app/services/organization_settings_service.py`) accepted
  `model_routing` like any lab setting.
- Writing routing guidance — `POST /llm/models/{id}/routing_hint`
  (`backend/app/routes/llm.py:275`) was `manage_llm`-only.
- The runtime decision — `_resolve_completion_models`
  (`backend/app/services/completion_service.py:286`) routed whenever the org
  setting was on, regardless of license.

## The fix

- `app/ee/license.py` — added `"model_routing"` to the enterprise tier
  (`TIER_FEATURES`).
- `_resolve_completion_models` — routing branch now also requires
  `has_feature("model_routing")`; this is the runtime boundary and fails closed
  (a config left over from an active license can't keep routing).
- `organization_settings_service.update_settings` — enabling `model_routing`
  returns 402 without the feature; **turning it off is always allowed** so a
  lapsed license can't strand an org with routing stuck on.
- `POST /llm/models/{id}/routing_hint` — decorated `@require_enterprise(feature="model_routing")`.
- `LLMsComponent.vue` — the Auto-router control stays in the UI but shows a
  locked `ENTERPRISE` badge and a disabled toggle when
  `!hasFeature('model_routing')`.

## Loop A — backend gate (deterministic, no external services)

```bash
cd backend && source .venv/bin/activate
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD="dummy"
python -m pytest tests/e2e/rbac/test_auto_model_routing.py tests/unit/test_model_router.py -q
```

Observed after the fix: **13 e2e + 9 unit passed**. The three new e2e cases are
the gate itself:

- `test_routing_hint_endpoint_requires_enterprise` — hint POST → 402 in community mode.
- `test_enabling_router_setting_requires_enterprise` — PUT settings `{value:true}` → 402.
- `test_resolver_no_ops_without_enterprise` — setting forced on + hint present,
  but no license ⇒ the default model runs, `routed` is falsy.

The existing 10 cases run under an autouse fixture that pins the `model_routing`
feature on, proving the licensed path still works end-to-end.

## Loop B — live stack (curl + Playwright)

Boot: `tools/agent/boot_stack.sh --dev`, then seed an org + an Anthropic provider
with three custom models (Haiku=small default, Opus=default, Sonnet).

Licensed (`BOW_LICENSE_KEY` set, enterprise tier): `/settings/models` shows the
**Auto router** toggle enabled with its help popover; toggle `disabled=false`,
no badge. → `router_licensed.png`.

Community (backend restarted with `BOW_LICENSE_KEY` unset,
`/api/license` → `licensed:false`):

```
PUT /api/organization/settings {"config":{"model_routing":{"value":true}}}  → 402
POST /api/llm/models/{id}/routing_hint {"hint":"..."}                        → 402
PUT /api/organization/settings {"config":{"model_routing":{"value":false}}} → 200
```

`/settings/models` shows the label with a locked **ENTERPRISE** badge and a
greyed-out toggle (`disabled=true`). → `router_community.png`.

## What this proves / regression notes

Routing is unusable without an enterprise license at every surface — write,
config, and runtime — while the community default path is untouched and the
toggle can always be turned off. Pre-existing `datetime.utcnow()` /
`PydanticDeprecatedSince20` warnings in the e2e run are unrelated and reproduce
without this change.
