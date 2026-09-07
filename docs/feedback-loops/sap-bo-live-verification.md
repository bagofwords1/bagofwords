# SAP BO query verification

## Reproduce

The previous connector posted a JSON query specification and expected inline
rows. Live sample-universe queries failed with HTTP 400. SAP's documented XML
query lifecycle succeeded for the same selections and identity.

A second defect appeared when pagination reached the end: SAP returned HTTP
400 instead of an empty page. Numeric dimensions also used the wrong metadata
attribute, and schema detail failures could be cached as empty columns.

## Fix

- Resolve published object IDs and paths, create an XML query, discover its
  OData service and metadata, retrieve rows, and delete the temporary query.
- Bound pagination using the reported row count and optional max_rows.
- Preserve object labels, distinct IDs and numeric/date metadata. Propagate
  schema discovery failures so a later call can retry.
- Reject multiple result flows and explain their compatible object groups.
  Agent guidance favors separate outputs and requires justified, validated
  relationships before combining datasets. This guidance is not runtime
  enforcement of generated Python.
- Render authentication as a validated four-option dropdown. Preserve the
  canonical .unx universe suffix.

## Verification

```bash
TESTING=true backend/.venv/bin/python -m pytest --noconftest backend/tests/unit/test_businessobjects_client.py -q
```

43 tests pass, covering the HTTP lifecycle, paging boundaries, cleanup,
authorization errors, legacy OData, object identity and schema retries.
Pagination regression reproduction: 3 failed before the fix, then all passed.
An earlier combined connector and generic connection API run passed 47 tests.
Vue template compilation and git diff whitespace checks passed.

Live Enterprise authentication discovered both sample universes. Direct
queries returned 1,628 catalog rows and 211 product-sales rows. Browser testing
confirmed the authentication dropdown and successful catalog retrieval.

After the prompt update, a fresh browser sales-and-promotions request produced
two successful query outputs: a 100-row sales sample and eight promotion
records. Saved code confirms separate queries without merging or arbitrary
deduplication. Sales are sorted within a capped sample, not a globally latest
result. Earlier failed attempts remain in local report history.

## Scope

Live checks used an administrative Enterprise identity. LDAP, other configured
authentication plugins, trusted authentication and restricted-user security
profiles have not been validated live. The separate BO 4.2 SP9 installation
has not been accessed. Required prompt/context answering and automatic
multi-flow merging are outside this change. Business totals were not audited.

For a repeat live check, supply SAP_BO_HOST, SAP_BO_USER and SAP_BO_PASSWORD
through the environment using a test instance. Do not put credentials,
customer endpoints, report IDs or account details in evidence artifacts.
The diagnostic appliance used a self-signed certificate; production should
use trusted TLS and the default certificate verification.

Protocol reference: [SAP BusinessObjects RESTful Web Service SDK guide](https://help.sap.com/doc/1eeee826c0154c62a5cbd29359ecbc18/4.3/en-US/sbo43_webi_restws_dg_en.pdf).
