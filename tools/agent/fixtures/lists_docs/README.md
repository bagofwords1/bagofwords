# Lists docs workspace (docs.bagofwords.com → Agents → Lists)

Recreates the fictional "Northwind Analytics / Contract Desk" workspace used for
the Lists docs screenshots. All companies and people are made up.

1. Run a backend on a **fresh** sqlite DB (so the org has no other data), e.g.
   `tools/agent/restart_backend.sh backend/db/docs.db`, plus the frontend.
2. `cd backend && .venv/bin/python ../tools/agent/seed_org.py --email maya@northwind.example --name "Maya Chen" --db-path db/docs.db`
   then rename the org to "Northwind Analytics" (`PUT /api/organization`).
3. `OPENAI_API_KEY=... BOW_ADMIN_EMAIL=maya@northwind.example .venv/bin/python ../tools/agent/setup_openai_llm.py`
4. `node tools/agent/fixtures/lists_docs/make_docs_contracts.mjs /tmp/bow-docs-contracts`
   and move `acme_logistics_amendment_1.pdf` out of that folder for now.
5. `.venv/bin/python ../tools/agent/fixtures/lists_docs/seed_docs.py /tmp/bow-docs-contracts`
6. In the app, as Maya, run with the Contract Desk agent:
   - "Read every contract in the folder and save each customer to the Contracts list, with the quote for each value."
   - "Go through every contract and save each concrete obligation (payments, notices, reports, deadlines) to the Obligations list, with the quote for each."
   - Edit Initech's `governing_law` to "State of New York" (locks it), put the amendment back in the folder, then:
     "A new amendment for Acme Logistics was added to the folder. Re-check all contracts and update the Contracts list."
   - "From the Contracts list, show total annual contract value by currency as a bar chart, and list the contracts that renew automatically in the next 12 months."
7. Capture at 1440×900 with `deviceScaleFactor: 2`, light theme, `en` locale.

Staged images: `docs/screenshots/pending-changes/lists/` (published at `/images/agents/lists/`).
