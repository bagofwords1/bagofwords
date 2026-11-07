# Feedback Loop — KMS Lighthouse connector (new)

Verification record for the read-only `kms_lighthouse` connector: what was
tested, with which command and result, which test backs each claim in the
connector guide (`docs/kms-lighthouse.md`), where the code differs from the
plan, and what a real tenant must still answer.

**Verification boundary.** Every test runs against
`tools/kms_lighthouse/mock_kms_server.py`, a mock built from the KMS Lighthouse
Public API Documentation 5.0 PDF plus the login call found in a public test
repository. No real tenant was contacted. The live-probe checklist at the end
lists what a tenant must confirm.

## What was added

- `backend/app/data_sources/clients/kms_lighthouse_client.py`: the client
  (auth and session pool, transport, search, browse, article and scenario
  reads, the content scrubber, the planner browse note).
- `KmsLighthouseConfig`, `KmsLighthouseUserPassCredentials`,
  `KmsLighthouseTokenCredentials` in `backend/app/schemas/data_sources/configs.py`;
  the registry entry in `backend/app/schemas/data_source_registry.py`.
- Type-gated wiring: the file-source sets, the planner file-scope note, the
  per-user credential test and save, the legacy credential routes, the
  agent-tool Connect and inactive-connection texts, two error codes
  (`backend/app/errors/codes.py`) with catalog entries in every maintained
  locale.
- Frontend: file-type lists, the member credential flow and a placeholder icon.
- On-site diagnostics (P12): `backend/app/data_sources/clients/kms_lighthouse_diagnostics.py`,
  `KmsLighthouseClient.diagnostics`, `POST /api/connections/{id}/kms-diagnostics`,
  `frontend/components/datasources/KmsDiagnosticsPanel.vue`, the error code
  `connection.kms_only` and the `kmsDiagnostics` strings in all 10 catalogs.
- Tests: `backend/tests/unit/test_kms_lighthouse_{client,content,wiring,credentials}.py`,
  `backend/tests/e2e/test_kms_lighthouse{,_agent_tools,_credentials}.py`,
  `test_contract_kms_lighthouse` in `backend/tests/e2e/test_connection_file_browse.py`,
  and `test_kms_lighthouse_base_url_never_contacts_metadata` in
  `backend/tests/unit/test_egress_package_b.py`.
- A comment in `backend/tests/integrations/ds_clients.py` saying why KMS is not
  in `DATA_SOURCES` (no catalog, so the harness's table-count assertion can
  never pass).

## How to run

```bash
# The mock on its own (for a manual look or a UI pass):
python tools/kms_lighthouse/mock_kms_server.py --port 8096

# The KMS unit gate (laptop; targeted, never the full suite):
cd backend
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true \
  uv run pytest tests/unit/test_kms_lighthouse_*.py -q -rf -p no:cacheprovider
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true \
  uv run pytest tests/unit/test_egress_package_b.py -q -rf -k kms -p no:cacheprovider

# The e2e files (run on the desktop mirror, see the P8 row):
TESTING=true .venv/bin/python -m pytest tests/e2e/test_kms_lighthouse.py \
  tests/e2e/test_kms_lighthouse_agent_tools.py tests/e2e/test_kms_lighthouse_credentials.py \
  -m e2e --db=sqlite -q -p no:cacheprovider
TESTING=true .venv/bin/python -m pytest tests/e2e/test_connection_file_browse.py -k kms \
  -m e2e --db=sqlite -q -p no:cacheprovider
```

## Test commands and results

Each phase ended with an independent verifier run on the phase's final commit.
The numbers are the verifier's scenario count and, where it was recorded, the
size of the KMS unit gate at that point. From P2 on every phase re-ran the
cumulative KMS unit gate (every `tests/unit/test_kms_lighthouse_*.py` that
existed), and from P7 on also `test_egress_package_b.py -k kms`.

| Phase | What it proved | Commands (from `backend/`) | Verified result |
|---|---|---|---|
| P1 | Schemas, registry, mock boundary, auth and transport core | `uv run pytest tests/unit/test_kms_lighthouse_client.py -q`; `uv run pytest tests/unit/test_discovery_progress_contract.py tests/unit/test_drive_clients.py -q` | PASS 13/13 at `79d2da9d9`; 72 KMS tests; shared files 86 passed with one pre-existing environment failure deselected (missing `libodbc.so.2`, unrelated to KMS); 6 mutations caught |
| P2 | Expiry, races, redaction, closed errors | KMS gate; `grep -n "__name__" app/data_sources/clients/kms_lighthouse_client.py` (no type-name comparison) | PASS 13/13 at `09ab7a2d2`; gate 100; 20 of 22 mutants killed; race test 20/20 stable |
| P2b | Session pool and login hold | KMS gate | PASS 12/12 at `381c27486`; gate 124; 24 of 25 mutants killed |
| P3 | Search and the content scrubber | KMS gate | PASS 19/19 at `eeadcca90`; gate 1156; scrub corpus honest under 8 mutants; 200 KB adversarial input worst 0.243 s; no secrets in logs or messages |
| P4 | Browse and folder paths | KMS gate | PASS 22/22 at `4d0c5b3d6` |
| P5 | Article read (AI text and item HTML) | KMS gate; `! grep -n "test_execute_query_is_unavailable_until_read_exists" tests/unit/test_kms_lighthouse_client.py` | PASS 18/18 at `1fb907289` |
| P6 | Scenario read | KMS gate | PASS at `f854415b6` |
| P7 | Wiring into the file tools and per-user credentials | `uv run pytest tests/unit/test_kms_lighthouse_wiring.py tests/unit/test_graph_onenote_client.py tests/unit/test_default_user_auth_modes.py tests/unit/test_report_context_file_source_omitted.py -q`; `uv run pytest tests/unit/test_egress_package_b.py -q -k "kms or neo4j or connection_hosts"`; KMS gate | PASS at `68fd2dc83` |
| P8 | API flow end to end (desktop) | rsync to the desktop mirror, then `TESTING=true .venv/bin/python -m pytest tests/e2e/test_kms_lighthouse.py -m e2e --db=sqlite -q -p no:cacheprovider`; KMS gate on the laptop | PASS at `b1f03c45b`; the non-admin owner test was a strict xfail until the owner fallback was removed (below) |
| P8b | Agent tools and the file browser end to end (desktop) | `TESTING=true .venv/bin/python -m pytest tests/e2e/test_kms_lighthouse_agent_tools.py tests/e2e/test_connection_file_browse.py -k kms -m e2e --db=sqlite -q -p no:cacheprovider`; KMS gate on the laptop | PASS at `5b8649624` |
| P9 | Frontend lists, icon, member credential flow | grep of the five frontend lists for `'kms_lighthouse'`; `git diff --quiet -- frontend/components/datasources/AgentFilesPanel.vue`; `file frontend/public/data_sources_icons/kms_lighthouse.png` (PNG, RGBA); `tests/unit/test_kms_lighthouse_credentials.py` and `tests/e2e/test_kms_lighthouse_credentials.py`; KMS gate | PASS 19/19 at `b536c8e8b`; the frontend `.mjs` test was not run (no `node_modules`) |
| P12 | On-site diagnostics: report, safety rules, route, panel wiring, docs | `uv run pytest tests/unit/test_kms_lighthouse_diagnostics.py -q` (with `BOW_DATABASE_URL` and `TESTING=true` when no `.env` exists); on the desktop mirror `TESTING=true .venv/bin/python -m pytest tests/e2e/test_kms_lighthouse_diagnostics.py -m e2e --db=sqlite -q -p no:cacheprovider`; KMS gate | PASS at `245c7d6fc` (commits `f4fdbf529`, `4ce4ca5a1`, `3bfc86101`, `ae9bbc698`, `245c7d6fc`): diagnostics 209 unit tests, 10 e2e tests; gate 2207 (1998 before this phase); egress 10; 22 KMS e2e on the desktop; the 8 safety mutants and the route permission mutant of `f4fdbf529` caught, then the deadline, raw-exception (summary and `error_type`), API prefix and type-label mutants caught at `245c7d6fc`; the panel compiles with `@vue/compiler-sfc`. The panel has not been run in a browser (no stack: P11) |

**P12 gate (laptop, with `BOW_DATABASE_URL=sqlite:///./db/test_kms.db TESTING=true`):
`tests/unit/test_kms_lighthouse_*.py` 2207 passed in 185 s, exit 0;
`tests/unit/test_egress_package_b.py -k kms` 10 passed, exit 0.**

**Gate before P12 (P10, re-run once on the laptop at `b536c8e8b` plus this docs
change, which touches no code):**

| Command | Result |
|---|---|
| `uv run pytest tests/unit/test_kms_lighthouse_*.py -q -rf -p no:cacheprovider` | 1998 passed in 92 s (client 127, content 1764, wiring 72, credentials 35); exit 0 |
| `uv run pytest tests/unit/test_egress_package_b.py -q -rf -k kms -p no:cacheprovider` | 10 passed, 65 deselected; exit 0 |

The e2e files were not re-run for P10 (desktop only; no code changed since P9).

## Claim to test mapping

Every behaviour the guide claims, and the test that backs it. Claims with no
automated test are marked and point to the verification boundary or to P11.

| Guide claim | Backed by |
|---|---|
| Registry: title, files category, enterprise, beta, `catalog_ownership="none"` (no indexing step) | `test_registry_contract` |
| Config validation: HTTPS unless Allow HTTP, no user info, query or fragment, path empty or the prefix, plain prefix | `test_invalid_configuration_is_rejected`, `test_base_url_with_prefix_is_normalised`, `test_text_flags_from_an_unvalidated_config_are_read_as_text` |
| The base URL passes the egress check | `test_kms_lighthouse_base_url_never_contacts_metadata` |
| Userpass login keeps cookies and a body token | `test_login_establishes_session`, `test_duplicate_session_cookies_still_log_in` |
| A login with no session is not a login | `test_login_without_a_session_is_not_treated_as_logged_in` |
| The token variant never logs in; a 401 is the token message | `test_token_variant_never_logs_in`, `test_token_expiry_is_auth_error_without_login` |
| Token format: visible ASCII, trimmed, checked before any request | `test_pasted_token_is_trimmed_and_must_be_plain_text`, `test_pasted_token_is_checked_before_any_request`, `test_token_pattern_is_anchored_at_both_ends`, `test_unusable_login_token_is_a_server_error_before_any_request` |
| Variant selection by `auth_type`, then by presence | `test_auth_variant_follows_auth_type_when_its_fields_are_present`, `test_switching_auth_variant_uses_the_new_one` |
| Username trimmed, password as typed | `test_username_is_trimmed_but_password_is_not` |
| Fixed failure messages without URL, body or secret | `test_failed_login_message_has_no_secret`, `test_error_kinds_have_fixed_messages_without_auth_markers`, `test_unexpected_response_shapes_never_echo_provider_text`, `test_odd_shapes_never_echo_provider_text`, `test_public_methods_close_their_errors`, `test_closed_errors_pass_fixed_errors_and_close_the_rest` |
| KMS messages pass through the agent's tool-error wording | `test_kms_errors_pass_through_friendly_tool_error`, `test_every_kms_error_kind_passes_through_friendly_tool_error` |
| Test connection: identity, empty top level hint, fresh login | `test_test_connection_reports_identity`, `test_top_level_rejection_hints_root_folder`, `test_test_connection_logs_in_fresh_but_honours_hold`, `test_connection_lifecycle_and_secrets` |
| Test connection success shows "Connected successfully. Found N documents."; N counts articles and scenarios (not folders) of one recursive listing that stops at 200 entries (folders included; `max_results` when lower) or 40 folder calls, so a cut listing makes N a floor even well below 200 | Not asserted by a KMS test: platform code (`_connected_message`, `_acount_files_for_validation` in `connection_service.py`); checked in the P11 UI pass |
| Saved password not returned by the API; a member cannot edit | `test_connection_lifecycle_and_secrets` |
| Validation errors never echo the rejected value | `test_rejected_credentials_are_not_echoed_by_validation_errors` |
| Members read as themselves; no row means the Connect prompt and no KMS call | `test_user_required_members_read_as_themselves`, `test_member_without_credentials_is_told_to_connect_by_every_file_tool`, `test_search_and_read_tell_a_member_without_credentials_to_connect` |
| Admin without a row runs as the connection identity | `test_admin_who_is_not_the_owner_runs_as_the_connection_identity`, `test_owner_without_a_row_runs_as_the_connection_identity` |
| A non-admin owner connects with their own login; ownership never unlocks the shared account | `test_owner_who_is_not_an_admin_connects_with_their_own_login` |
| Member credential test drops destination keys | `test_member_credential_test_ignores_destination_keys` |
| `connection.kms_token_format` on member save and member test | `test_a_bad_kms_token_format_has_its_own_error_code`, `test_the_credential_test_reports_a_bad_kms_token_format_with_its_own_code`, `test_member_save_shows_the_kms_token_format_message` |
| Other KMS credential errors name the field only | `test_other_kms_credential_errors_name_the_field_without_the_pydantic_dump`, `test_other_credential_test_errors_keep_the_field_level_422` |
| Legacy POST refuses KMS (`connection.kms_use_connection_credentials`) | `test_the_legacy_store_refuses_kms_before_anything_is_saved`, `test_legacy_save_refuses_kms_and_stores_nothing` |
| Legacy DELETE also clears the connection-level row | `test_legacy_disconnect_clears_the_kms_connection_row` |
| Error codes have messages in every maintained catalog | `test_the_kms_error_codes_have_messages_in_every_maintained_catalog` |
| Inactive connection or agent is named as such | `test_list_files_says_an_inactive_kms_connection_is_inactive`, `test_search_and_read_say_an_inactive_kms_connection_is_inactive`, `test_p3_a_kms_connection_shared_with_an_inactive_agent_gets_the_connect_prompt` |
| Root folder bounds browsing only; reads outside it work | `test_folder_outside_root_is_refused`, `test_contract_kms_lighthouse`, `test_kms_file_scope_is_built_with_the_note_and_no_enforced_scope` |
| Planner note: cap, 40-call bound, no catalog, browse-only root, path example | `test_planner_scope_note_states_handle_cap_and_scope`, `test_kms_file_scope_descriptor_tells_the_planner_how_to_browse`, `test_planner_context_carries_the_kms_browse_note`, `test_planner_note_reads_root_and_layout_as_the_client_does`, `test_planner_note_lets_only_whole_numbers_into_the_prompt` |
| `list_files` folder help names KMS paths | `test_list_files_folder_id_help_mentions_kms_paths` |
| Description: one identity phrase, Online-only or offline wording | `test_description_names_one_identity_for_both_variants`, `test_text_flags_from_an_unvalidated_config_are_read_as_text` |
| Browse walks the tree; paths of folder names; `.txt` names | `test_list_walks_tree_from_root`, `test_entry_name_is_last_path_segment`, `test_search_names_are_safe_path_segments` |
| Open a folder by path or by `folder:<id>`; paths resolve back | `test_list_folder_by_path`, `test_list_one_folder`, `test_entry_paths_resolve_back_to_their_folder`, `test_folder_id_without_root_has_paths_from_the_top_that_resolve_back`, `test_agent_browses_a_folder_by_path_then_reads` |
| Same-name siblings get `[<id>]` segments | `test_sibling_folders_with_same_title_get_distinct_paths` |
| Unknown folder, beyond the browse limit, found with no call left | `test_unknown_folder_path_is_not_found`, `test_unknown_folder_id_without_root_is_not_found`, `test_folder_found_only_past_the_call_cap_is_named`, `test_a_folder_found_with_no_call_left_to_list_it_says_so` |
| Ids checked before any request; digits compared as numbers | `test_ids_are_validated_before_any_request`, `test_read_bad_id_is_refused_before_any_request`, `test_folder_id_digits_are_compared_as_numbers`, `test_parse_id_accepts_any_ascii_case`, `test_parse_id_refuses_unicode_case_folded_prefixes` |
| Bare digits fall back to a scenario once; `item:` never does | `test_bare_id_falls_back_to_scenario_once` |
| Online only by default; offline with `include_offline`; status encodings | `test_search_hides_offline_by_default`, `test_list_hides_offline`, `test_read_offline_item_is_not_found`, `test_offline_folder_by_id_is_not_found`, `test_online_status_encodings` |
| Search: paging, layout, identity, dedupe | `test_search_paging_is_bounded`, `test_search_sends_layout_id_only_when_set`, `test_search_runs_as_the_identity`, `test_search_dedupes_by_id`, `test_search_maps_results_and_strips_html` |
| Search at the cap is marked incomplete | `test_search_at_the_cap_says_more_results_may_exist`, `test_search_page_bound_with_visible_results_says_max_results` |
| Search deadline and cancel keep what was found | `test_search_honors_deadline`, `test_search_timeout_at_the_deadline_keeps_the_earlier_pages`, `test_search_honors_cancel_check`, `test_search_read_timeout_follows_the_deadline` |
| Query cut at 200 characters; a 400 names both causes | `test_long_query_is_capped_not_rejected`, `test_search_400_names_both_causes`, `test_search_login_4xx_is_not_blamed_on_the_query`, `test_empty_query_makes_no_request` |
| An answer without results is an error, an explicit zero is no hits | `test_search_without_results_is_a_server_error`, `test_search_with_an_explicit_empty_answer_has_no_hits` |
| Listing: 40 tree calls, `max_results`, tool limit; stops without an error | `test_list_walk_is_bounded`, `test_bad_list_limit_is_named_before_any_request`, `test_whole_number_list_limits_are_accepted` |
| Session reused across clients, per identity, for 600 s | `test_session_is_reused_across_client_instances`, `test_sessions_are_never_shared_across_identities`, `test_session_reuse_expires_after_ttl` |
| One re-login on 401 or redirect; a refused fresh session has its own message | `test_expired_session_relogs_in_once`, `test_second_rejection_is_auth_error`, `test_concurrent_expiry_relogs_in_once`, `test_concurrent_expiry_across_clients_relogs_in_once` |
| A rejected login holds new logins for 300 s, for every instance | `test_rejected_login_is_not_repeated_within_hold`, `test_rejected_relogin_starts_a_hold_for_other_instances`, `test_login_answer_without_success_starts_a_hold`, `test_login_hold_is_logged_with_fixed_text` |
| A rejected re-login forgets the dead session | `test_rejected_relogin_forgets_the_dead_session` |
| Pool bounded at 256, idle first, in-flight login kept | `test_pool_is_bounded_and_keeps_a_recently_used_hold`, `test_pool_evicts_an_idle_entry_before_a_held_one`, `test_pool_does_not_evict_an_entry_with_a_login_in_flight` |
| A young session does not wait for a login in flight | `test_young_session_does_not_wait_for_a_login_in_flight` |
| Articles prefer AI text, only for elements `/public/item` returned | `test_read_article_prefers_ai_text`, `test_ai_text_only_renders_elements_visible_in_item`, `test_a_repeated_ai_element_name_is_shown_once` |
| AI text shown as written; markup-like or empty elements render from item HTML | `test_ai_text_is_plain_text_shown_as_written`, `test_a_plain_ai_element_is_shown_from_its_ai_text`, `test_an_ambiguous_ai_element_renders_from_its_item_html`, `test_an_ai_element_with_no_text_renders_from_its_item_html`, `test_ai_text_with_a_tag_shaped_placeholder_is_shown_as_written` |
| Item-specific AI failures fall back for one read; global ones are reported | `test_read_article_falls_back_to_item_html`, `test_ai_text_404_affects_only_that_read`, `test_ai_text_item_failure_falls_back_to_item_html`, `test_ai_text_global_failure_is_not_hidden`, `test_login_failure_on_read_is_not_an_item_kind` |
| Item HTML grouped by tab, image fields skipped | `test_read_article_falls_back_to_item_html`, `test_item_elements_that_are_not_a_record_map_fail_closed` |
| Scenario tree, fetched steps, v2 accept header, `plainText=true` | `test_read_scenario_renders_decision_tree`, `test_scenario_child_steps_are_fetched_when_not_inline`, `test_scenario_request_sends_v2_accept_header` |
| Scenario caps (400 entries, 40 fetches, depth 25) and markers | `test_scenario_caps_and_cycles`, `test_scenario_node_cap_truncates`, `test_every_rendered_line_counts_toward_the_tree_bound`, `test_cut_markers_count_toward_the_tree_bound`, `test_scenario_fetch_budget_reaches_every_top_level_answer_first` |
| Unavailable steps and the all-failed note | `test_a_scenario_step_that_fails_for_itself_is_not_available`, `test_a_tree_whose_every_step_fetch_failed_says_so`, `test_a_scenario_step_failure_about_the_service_fails_the_read` |
| Step text shown as written, still scrubbed | `test_scenario_step_text_labels_and_names_are_shown_as_written`, `test_scenario_step_text_still_scrubs_machine_credentials` |
| Image token parameters removed; article token URLs scrubbed | `test_scenario_image_tokens_are_stripped`, `test_article_text_scrubs_token_urls` |
| `read_raw_bytes` and `execute_query` | `test_read_raw_bytes_respects_max_bytes`, `test_read_raw_bytes_without_a_byte_limit_returns_the_whole_text`, `test_execute_query_delegates_to_read_file` |
| Reads are never served from the shared cache | `test_file_version_is_not_implemented` |
| Dates normalised to ISO | `test_dates_are_normalised` |
| Oversized answers and transport failures carry no URL or secret | `test_oversized_response_is_refused`, `test_transport_errors_carry_no_url_or_secret`, `test_broken_responses_are_unreachable_without_chains` |
| No request URL in logs; the urllib3 gate is per thread | `test_debug_logs_never_contain_credentials_or_query`, `test_urllib3_gate_is_per_thread`, `test_urllib3_gate_reopens_after_a_transport_failure`, `test_refusals_are_logged_by_a_fixed_reason_only`, `test_unexpected_failure_is_logged_by_class_name_only` |
| Scrubber: machine credentials removed in every form | `test_search_scrubs_secret_parameters_in_every_form`, `test_search_redacts_jwts_split_by_invisible_characters`, `test_search_redacts_a_letters_only_basic_credential`, `test_search_regression_corpus` |
| Scrubber: prose, command examples and placeholders shown as written | `test_search_shows_command_line_credentials_as_written`, `test_search_scrub_keeps_ordinary_prose`, `test_search_scrub_keeps_harmless_parameters`, `test_search_keeps_a_command_line_host_path_or_url`, `test_search_regression_corpus` |
| Scrubber is linear and never raises | `test_scrub_is_linear_on_adversarial_input`, `test_scrub_never_raises_on_mixed_fragments` |
| KMS is in every file-source set; member auth modes left unset | `test_kms_lighthouse_is_in_every_file_source_set`, `test_default_user_auth_modes_leaves_kms_unset` |
| Known scrub limitations | Each class is pinned as a "shown" or "altered" case in `test_search_regression_corpus` or the limitation comment next to `_scrub`; not a promise of behaviour |
| Platform behaviour of a real tenant (prefixes, session shape, limits) | Verification boundary: mock only, see the live-probe checklist |

## Plan versus code

The plan (`onboarding/plans/2026-11-04-kms-connector-plan.md`) was written
before the build. Where it and the code at `b536c8e8b` differ, the code wins and
the guide describes the code:

1. The folder-path example is `'Folder/Subfolder'` (root-relative) in the
   planner note, the description and the `list_files` `folder_id` help; the
   plan says `'Procedures/Billing'`.
2. The planner note also states the 40-call folder bound and "There is no
   catalog: the file count shown is always 0, and every listing and read is
   live."
3. The description says "Online and offline content is shown" when
   `include_offline` is on (the plan: "Only Online content is shown, offline
   included when configured").
4. Scenario step text, labels and names are requested with `plainText=true`
   and are shown as written and scrubbed only; the plan ran `strip_html` on
   them.
5. A scenario `Image:` line keeps its URL and loses only token parameters (the
   plan: the URL without its query).
6. Article AI text: an element whose AI text looks like markup or is empty
   renders from the item HTML; only failures about that item fall back
   (a 4xx including 403, a plain 500, empty or error answers), while throttling,
   redirects, an unreachable KMS, gateway 502/503/504 and login failures are
   reported. The plan: "any non-auth failure falls back".
7. Search signals "incomplete" (`SearchStopped("max_results")`) when it reaches
   its cap with more results possible, and after 20 full pages; it always asks
   for rows of 50; a search block with no results list is an error unless it
   states a zero total. The plan had no signal and `rows=min(50, remaining)`.
8. A rejected re-login forgets the dead pooled session, so later calls stop at
   the hold instead of sending it again.
9. When the KMS client cannot be built, `list_files`, `search_files` and
   `read_file` give the Connect prompt, or the inactive-connection or
   inactive-agent text (type-gated); the plan left `list_files.py` and
   `search_files.py` unchanged.
10. The legacy `POST /api/data_sources/{id}/my-credentials` refuses KMS with a
    typed 400, and the legacy `DELETE` also clears the connection-level row; the
    plan left `upsert_my_credentials` unchanged.
11. Two dedicated error codes, `connection.kms_token_format` and
    `connection.kms_use_connection_credentials`; other KMS member-save errors
    list `field: message` pairs.
12. The scrubber is much wider than the plan's "token, access_token, jwt and auth
    parameters plus JWTs": machine credentials only (decision A), with
    `_html_to_text` in place of `strip_html` from `mail_common`.
13. Scenario caps count entries (steps, option lines, markers), not nodes; a cut
    leaves an in-place `(not shown: tree truncated)` marker; entries and fetches
    are admitted level first; a tree whose every fetch failed starts with
    `(some steps could not be loaded)`.
14. `test_connection` adds " The root folder came back empty." to its own success
    message when a root folder is set and empty; on success the platform replaces
    the client message with "Connected successfully. Found N documents."
15. A login answered `SUCCESS` with no cookie and no token has its own fixed
    message (`_NO_SESSION`); a login token that cannot go into a header is a
    `server` error (status 200); the plan's fixed-message list had neither.
16. `KmsHTTPError(status, kind, message=None)` takes an optional fixed message;
    the constructor refuses a malformed token with `KMS_TOKEN_FORMAT_MESSAGE`.
17. P4 signatures: `_resolve_folder(target, budget) -> tuple[str | None, tuple[str, ...]]`
    and `_entry_path(parent_segments, record)`; the plan wrote
    `_resolve_folder(folder_id)`.
18. A folder lookup has three not-found texts (not found; not found or beyond the
    browse limit; found but beyond the browse limit), and a `folder:<id>` lookup
    also opens offline folders so an online folder under an offline one is
    "not found", never "outside the root".
19. Pool eviction also treats an entry with a login in flight as busy (not idle).
20. Login failures are one internal class, so a tolerated branch or item failure
    is never a login outage; a login error whose kind would speak of content
    (for example a 404) becomes `server`.
21. Besides the request line and the four auth-path lines, the client logs other
    fixed-text DEBUG lines (refusal and fallback reasons); the plan's list is
    "for example", not complete.
22. List and search limits that are not whole numbers (and a list limit of 0)
    are refused with fixed messages.
23. `_SESSION_REFUSED` and `_UNSENDABLE_LOGIN` wording match the plan's r7 items
    19 and 20 (no further difference).

## Live-probe checklist

There is no tenant. When one exists, probe each item and record the answer
here; each answer either confirms a default or changes one constant or setting.

1. Which prefix serves each endpoint (`/kms/lh/api/...` or `/api/...`),
   especially `ai_answers_item`.
2. Whether `/login` sets a cookie, returns `data.token`, or both, and the
   session lifetime.
3. The expiry answer (401 or a redirect).
4. Whether a tree call without `parentId` returns the top level (or `SUCCESS`
   with `[]`).
5. The `type` and `subType` of scenarios in search and in the tree.
6. Whether item and scenario ids share one space.
7. The day/month order of `dd/mm/yyyy` dates.
8. Whether `/search/full` needs `layoutId`.
9. The longest query `/search/full` accepts.
10. The concurrent-session limit per account and the failed-login lockout
    threshold.
11. The actual types of `tabId` and `tabs[].id`.
12. Whether `ai_answers_item` filters elements by the caller's permissions the
    way `/public/item` does, and whether its `element_name` values match the
    `/public/item` `elements` keys.
13. Whether KMS has a logout endpoint that ends a `/login` session, and, with
    item 10, whether the status-sweep probe churn (a new session about every
    300 s per system-only connection) hits the concurrent-session limit. If it
    does, `test_connection` gains a logout after the probe.
14. Whether `ai_answers_item` `element_text` is plain text on a real tenant, and
    what a tenant without the AI module answers (a 403, a 404, or an empty
    answer).
15. Whether scenario requests with `plainText=true` really return plain text
    (no tags or entities in step text, labels and names).
16. Whether a 406 comes only from step fetches (the mock answers 406 without
    the v2 Accept header); a step that answers 406 renders
    `(step not available)` and the not-loaded note.
17. The real zero-hit `/search/full` answer and the name of its total field (the
    client accepts `total`, `totalResults` or `numFound` equal to 0).
18. Whether `/search/full` caps rows per page on the server side (a short page
    ends paging).

## Checklist items and diagnostics checks

The on-site diagnostics (P12) answer the checklist above as far as one account
and one safe read-only call can. Each `id` is a row of the report's `checks`.
Run it at the install, then record the answers here.

| Item | Check | What it reports |
|---|---|---|
| 1 | `prefix` | One authenticated GET per other known prefix; status and whether it served (`ai_answers_item` when an item was read, else the tree) |
| 2 | `login` | Login status, cookie count and plain names, whether `data.token` exists, top-level key names. One run cannot measure the session lifetime |
| 3 | `expiry` | The status of a request with no session, and whether it redirected |
| 4 | `top_level` | Records at the top level, and at the root folder when one is set |
| 5 | `types` | Counts of `type` and `subType` in the tree and in search |
| 6 | `id_overlap` | Item ids, scenario ids and shared ids in the sample |
| 7 | `dates` | Whether any first or second date field goes over 12 |
| 8 | `search_layout` | Status and rows with and without `layoutId` when one is set |
| 9 | `query_length` | The longest accepted of three sizes, up or down from the connector's limit |
| 10 | `concurrent_sessions` | Always skipped: ask the KMS admin. Failed logins are never sent on purpose |
| 11 | `field_types` | Python type names of `tabs[].id` and `elements.*.tabId` |
| 12 | `ai_elements` | Element names only in the AI text or only in the item, and the common count. Always `warn`: repeat as a restricted user |
| 13 | `logout` | Always skipped: ask the KMS admin |
| 14 | `ai_text` | Plain, ambiguous and blank counts; the status codes when the module is absent |
| 15 | `scenario_text` | Scenario strings with a tag or an entity |
| 16 | `scenario_steps` | Status codes seen on step fetches |
| 17 | `zero_hit` | Status, top-level and search-block key names, whether the connector can read it |
| 18 | `page_rows` | Rows returned against rows requested, and the next page after a short one |
| - | `network` | Whether this server reached KMS; else `dns`, `refused`, `timeout`, `blocked_route` or `connection_failed` (`egress_blocked` when the check runs before any call, `run_timed_out` when the route gave up on the run) |
| - | `tls` | Whether the certificate is trusted; else `untrusted_certificate` (with the integer `verify_code`), `tls_failed` or `ca_bundle_unreadable` |
| - | `session` | One authenticated GET after a login: `session_refused` ends the run. Info for an API token |

## Scrub decisions

The scrub scope (machine credentials only; authored prose and command examples
shown as written; never rewrite legitimate content) and its known limitations
are in `docs/kms-lighthouse.md` ("Content scrubbing" and "Known scrub
limitations").
