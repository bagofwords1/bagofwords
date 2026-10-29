# Feedback Loop — "Stored provider API key exfiltration via provider_id fallback in /api/llm/test_connection"

Pentest finding (Medium): `POST /api/llm/test_connection` with a saved
`provider_id` and blank credentials reuses the **stored** API key, but takes
the **destination** (`base_url` / `endpoint_url` / `provider_type`) from the
request. A user with `manage_llm` can point the test at a server they control
and receive the org's decrypted key in the `Authorization` header, even though
the UI never shows saved secrets.

## Root cause (validated)

- `backend/app/services/llm_service.py` `test_connection`: seeds
  `provider_obj.api_key/api_secret/additional_config` from the stored provider,
  but builds the object with the payload's `provider_type`.
- `_set_provider_credentials` then merges the payload's `base_url`
  (openai/custom) or `endpoint_url` (azure) over the stored config. The test
  call goes to that host with the stored secret.

## Loop A — deterministic reproduction (no external services)

`backend/tests/e2e/test_llm_test_connection_stored_secret.py` starts a local
HTTP "attacker" server that records the `Authorization`/`api-key` headers it
receives. It saves a provider through the API, then calls test_connection with
`provider_id` + blank key + `base_url` set to the recorder.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/test_llm_test_connection_stored_secret.py --db=sqlite -v
```

Before the fix (`git stash` of `llm_service.py`), the attacker server received the stored key:

```
E       AssertionError: ['Bearer sk-stored-openai-SECRET']
E       AssertionError: ['Bearer sk-stored-custom-SECRET']
>       assert resp.status_code == 400      (provider_type switch accepted)
E       assert 200 == 400
3 failed, 2 passed
```

## The fix

`llm_service.py` `test_connection`:
1. When `provider_id` is set, the payload's `provider_type` must match the
   stored one (otherwise 400).
2. If the merged config changes any destination key
   (`_DESTINATION_CONFIG_KEYS`: `base_url`, `endpoint_url`, `verify_ssl`),
   the stored secrets are dropped and only credentials typed into the request
   are used. Retesting an unchanged saved provider still reuses the stored key.

After:

```
test_test_connection_never_sends_stored_secret_to_new_endpoint[openai] PASSED
test_test_connection_never_sends_stored_secret_to_new_endpoint[custom] PASSED
test_test_connection_uses_freshly_entered_key_for_new_endpoint PASSED
test_test_connection_reuses_stored_secret_for_saved_endpoint PASSED
test_test_connection_rejects_provider_type_switch PASSED
5 passed
```

Neighbouring suites (`tests/e2e/test_llm_providers.py`,
`tests/unit/test_llm_test_connection_schema.py`): 16 passed together with the
new file. `test_llm_providers.py` needs `OPENAI_API_KEY_TEST` set (any dummy
value) or it fails on setup, with or without this change.

## What this proves / regression notes

- The stored secret never reaches a host other than the saved one through the test endpoint.
- A key typed in with a new endpoint is still sent there (the "edit endpoint + re-enter key + Test" flow works).
- The "Test" button on an unchanged saved provider still works with blank credentials.
