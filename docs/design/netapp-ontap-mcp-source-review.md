# ONTAP-MCP source review for the direct REST connector

Reviewed 2026-09-13 at NetApp/ontap-mcp commit `7ba6baf2e7e3ea0d458e2519f288c5eb5a2353d8`. Static source inspection; no ONTAP appliance calls, implementation changes, or integration tests. This supplements the ONTAP 9.14.1 connector plan. MCP remains reference material, not a runtime dependency.

Source root: https://github.com/NetApp/ontap-mcp/tree/7ba6baf2e7e3ea0d458e2519f288c5eb5a2353d8

## Request flow

`tool/tool.go:622` defines cluster_name, path, path_params, filters, fields, and max_records. `server/server.go:674` resolves placeholders with URL path escaping, encodes filters using url.Values, and invokes `rest/client.go:407` GenericGet. The latter prefixes /api, sends HTTPS GET, follows _links.next.href, accumulates raw JSON records, and handles singleton responses separately. The MCP handler strips links and returns JSON as tool text.

Reusable design: one shared HTTP transport and pagination implementation behind resource mappings; explicit field projection; encoded filters; parent UUID parameters. Resource helpers in rest/volume.go, rest/lun.go, rest/snapshot.go resolve names within an SVM and reject ambiguous matches. rest/snapmirror.go resolves relationships by destination.path.

## Transport and limits

rest/client.go uses Basic authentication or a bearer token, with credential-source precedence script > file > inline configuration. TLS verification is enabled unless configuration explicitly disables it. The HTTP timeout is two minutes per request. The inspected request wrapper has no explicit retry/backoff policy.

GenericGet has a default page size of 500 and an optional total record cap. However, the MCP handler also writes a positive max_records directly into the page-size parameter, so a 10,000-result request can request a 10,000-record page. Omitted limits collect all pages. The output has no explicit truncation indicator. Its loop guard detects repeated consecutive next links, not arbitrary cycles.

Our connector should independently bound page size, total rows, pages, elapsed time, and parent fan-out; report truncation; validate next links; and apply bounded transient-error retries. Preserve HTTP status and ONTAP errors, including a useful fallback for non-JSON error bodies.

## Version and catalog findings

conf/ontap_api_catalog.json targets 9.16. cmd/generate.go can fetch /docs/api/swagger.yaml from a cluster and generate a catalog. It selects GET operations, excludes private operations and /storage/qos, and allows only selected parameterized paths. This is not a complete Swagger inventory.

DescribeOntapEndpoint optionally filters fields and filters by cluster version. The version is reduced to generation.major (e.g. 9.14); endpoint introduction is not an execution gate. OntapGet does not validate requested fields/paths against the catalog and sets ignore_unknown_fields=true for versions >=9.11, or if version detection fails.

For 9.14.1P9, retain the independently inspected 9.14.1 Swagger contract, validate fields before execution, and verify the appliance's own Swagger/version before claiming patch-specific support. Avoid silently ignoring explicitly requested unknown analytical fields.

## QoS coverage finding

rest/qospolicy.go and server/listqospolicy.go combine /storage/qos/policies with /private/cli/vserver, /private/cli/qos/policy-group, and /private/cli/qos/adaptive-policy-group. The typed response distinguishes SVM and cluster scope. Admin discovery failure fails the tool even after public REST succeeds. Some parsing failures log and skip records.

Consequently, our public-REST qos_policies table must state its supported coverage; complete equivalence to the MCP QoS tool is not established. Any CLI-backed extension needs separate scope and permission handling. Do not silently skip failed records in analytical results.

## Additional differences to retain in the plan

- Read-only mode gates tool registration by annotations; GenericGet itself has no endpoint/parameter safety allowlist. Our curated GET allowlist remains necessary.
- stripLinks unmarshals JSON into Go `any`, which converts numbers to float64. This can lose integer precision above 2^53. Preserve integer values when normalizing counters and bytes.
- Client initialization reads /cluster?fields=* and sends an MCP identification tag through a further cluster GET containing OS, hashed hostname, and MCP version. This is MCP-specific behavior, not needed for our connector.
- Async job handling polls /cluster/jobs/{uuid}; useful if actions are added later, outside the current read-only connector scope.

Decision: adopt the request-shaping and UUID-scoping patterns; implement BOW's own bounded, typed, version-specific REST interface. No MCP deployment is required.
