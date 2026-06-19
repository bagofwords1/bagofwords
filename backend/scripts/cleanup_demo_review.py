"""Remove all demo-review artifacts seeded for screenshots; restore app.db."""
import sqlite3, datetime
c = sqlite3.connect("db/app.db")
REVIEWER = "28f6a22c-b427-45b2-8e5d-2a59d2be8a24"
ORG = "5f7ff784-b9f6-47b9-967f-6eb70955cc4e"
MAIN = "31cd7f37"  # real main build prefix
# instructions created by the reviewer (the demo ones)
instr_ids = [r[0] for r in c.execute("SELECT id FROM instructions WHERE user_id=?", (REVIEWER,)).fetchall()]
print("demo instructions:", len(instr_ids))
for iid in instr_ids:
    # builds that contain this instruction and are NOT the real main → delete (cherry-pick + suggestion builds)
    bids = [r[0] for r in c.execute(
        "SELECT DISTINCT build_id FROM build_contents WHERE instruction_id=?", (iid,)).fetchall()]
    for bid in bids:
        ismain = c.execute("SELECT is_main FROM instruction_builds WHERE id=?", (bid,)).fetchone()
        if ismain and ismain[0] == 1:
            # real main: just drop this instruction's content row
            c.execute("DELETE FROM build_contents WHERE build_id=? AND instruction_id=?", (bid, iid))
        else:
            c.execute("DELETE FROM build_contents WHERE build_id=?", (bid,))
            c.execute("DELETE FROM instruction_builds WHERE id=?", (bid,))
    c.execute("DELETE FROM build_contents WHERE instruction_id=?", (iid,))
    c.execute("DELETE FROM instruction_versions WHERE instruction_id=?", (iid,))
    c.execute("DELETE FROM instruction_data_source_association WHERE instruction_id=?", (iid,))
    c.execute("DELETE FROM instructions WHERE id=?", (iid,))
# demo report
c.execute("DELETE FROM report_data_source_association WHERE report_id IN (SELECT id FROM reports WHERE user_id=?)", (REVIEWER,))
c.execute("DELETE FROM reports WHERE user_id=?", (REVIEWER,))
# membership + role assignment + user
c.execute("DELETE FROM role_assignments WHERE principal_id=?", (REVIEWER,))
c.execute("DELETE FROM memberships WHERE user_id=?", (REVIEWER,))
c.execute("DELETE FROM users WHERE id=?", (REVIEWER,))
# sanity: exactly one main build remains
mains = c.execute("SELECT COUNT(*) FROM instruction_builds WHERE organization_id=? AND is_main=1 AND deleted_at IS NULL", (ORG,)).fetchone()[0]
c.commit()
print("cleanup done. is_main builds in org:", mains)
c.close()
