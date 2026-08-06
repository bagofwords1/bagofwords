"""Seed a table widget into the sandbox report so the chat renders ToolWidgetPreview
(with VisualizationFilter) over real rows. The LLM path is unavailable (no API credits),
so we insert the same rows the agent would have produced.
"""
import json, sqlite3, uuid, datetime, random, sys

DB = 'db/app.db'
REPORT_ID = sys.argv[1]
now = datetime.datetime.utcnow().isoformat(sep=' ')

random.seed(7)
projects = [f'{l}{n:03d}' for l in 'PXYTZ' for n in range(100, 125)][:122]
rows = [
    {
        'logistic_company': 161,
        'project': projects[i],
        'revenue': round(random.uniform(1000, 99999), 2),
        'month': f'2026-0{(i % 3) + 1}',
    }
    for i in range(122)
]
columns = [
    {'field': 'logistic_company', 'headerName': 'logistic_company'},
    {'field': 'project', 'headerName': 'project'},
    {'field': 'revenue', 'headerName': 'revenue'},
    {'field': 'month', 'headerName': 'month'},
]

c = sqlite3.connect(DB)
cur = c.cursor()

# find the assistant completion of this report
comp = cur.execute(
    "select id from completions where report_id=? and role='system' order by created_at desc limit 1",
    (REPORT_ID,),
).fetchone()
completion_id = comp[0]
cur.execute("update completions set status='success' where id=?", (completion_id,))

widget_id = str(uuid.uuid4())
step_id = str(uuid.uuid4())
query_id = str(uuid.uuid4())
viz_id = str(uuid.uuid4())
agent_exec_id = str(uuid.uuid4())
tool_exec_id = str(uuid.uuid4())
block_id = str(uuid.uuid4())

org_id, user_id = cur.execute(
    "select organization_id, user_id from reports where id=?", (REPORT_ID,)
).fetchone()

cur.execute(
    "insert into widgets (title, slug, status, id, created_at, updated_at, report_id, x, y, width, height)"
    " values (?,?,?,?,?,?,?,?,?,?,?)",
    ('Company 161 - Backlog by project', 'company-161-backlog', 'published', widget_id, now, now,
     REPORT_ID, 0, 0, 6, 4),
)

cur.execute(
    "insert into steps (title, slug, status, prompt, code, id, created_at, updated_at, widget_id, data,"
    " description, type, data_model, status_reason, view)"
    " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ('Company 161 - Backlog by project', 'company-161-backlog', 'success',
     'SELECT * FROM backlog', 'SELECT * FROM backlog', step_id, now, now, widget_id,
     # NOTE: query_id is filled in below — StepSchema.query_id is a required str and a
     # NULL makes GET /api/reports/{id}/completions raise a pydantic ValidationError.
     json.dumps({'columns': columns, 'rows': rows, 'info': {'total_rows': len(rows)}}),
     'Backlog rows', 'table',
     json.dumps({'type': 'table', 'columns': [{'generated_column_name': col['field'],
                                               'description': col['field']} for col in columns]}),
     None,
     json.dumps({'version': 'v2', 'view': {'type': 'table',
                                           'columns': [c['field'] for c in columns]}})),
)

cur.execute(
    "insert into queries (title, description, report_id, widget_id, default_step_id, organization_id,"
    " user_id, id, created_at, updated_at) values (?,?,?,?,?,?,?,?,?,?)",
    ('Company 161 - Backlog by project', 'Backlog rows', REPORT_ID, widget_id, step_id,
     org_id, user_id, query_id, now, now),
)
cur.execute("update steps set query_id=? where id=?", (query_id, step_id))

# The chat's filter row only renders when ToolWidgetPreview can resolve a visualization
# id (components/tools/ToolWidgetPreview.vue:117), which comes from
# tool_executions.artifact_refs_json.visualizations. The view JSON must validate against
# ViewSchema — TableView.columns is List[str], not a list of objects.
cur.execute(
    "insert into visualizations (title, status, report_id, query_id, view, id, created_at, updated_at)"
    " values (?,?,?,?,?,?,?,?)",
    ('Company 161 - Backlog by project', 'success', REPORT_ID, query_id,
     json.dumps({'version': 'v2', 'view': {'type': 'table',
                                           'columns': [c['field'] for c in columns],
                                           'title': 'Company 161 - Backlog by project'}}),
     viz_id, now, now),
)

cur.execute(
    "insert into agent_executions (completion_id, organization_id, user_id, report_id, status, started_at,"
    " completed_at, latest_seq, id, created_at, updated_at, is_eval_run)"
    " values (?,?,?,?,?,?,?,?,?,?,?,?)",
    (completion_id, org_id, user_id,
     REPORT_ID, 'success', now, now, 1, agent_exec_id, now, now, 0),
)

cur.execute(
    "insert into tool_executions (agent_execution_id, tool_name, tool_action, arguments_json, status, success,"
    " started_at, completed_at, duration_ms, attempt_number, max_retries, result_summary, result_json,"
    " created_widget_id, created_step_id, artifact_refs_json, id, created_at, updated_at)"
    " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (agent_exec_id, 'execute_sql', None, json.dumps({'sql': 'SELECT * FROM backlog'}), 'success', 1,
     now, now, 95100.0, 1, 3, 'Created Data', json.dumps({'rows': len(rows)}),
     widget_id, step_id, json.dumps({'visualizations': [viz_id]}), tool_exec_id, now, now),
)

cur.execute(
    "insert into completion_blocks (completion_id, agent_execution_id, source_type, tool_execution_id,"
    " block_index, loop_index, title, status, icon, content, started_at, completed_at, id, created_at,"
    " updated_at, duration_ms) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (completion_id, agent_exec_id, 'tool', tool_exec_id, 0, 0, 'Created Data', 'success', None,
     'Created Data', now, now, block_id, now, now, 95100.0),
)

c.commit()
print('widget_id', widget_id)
print('step_id', step_id)
print('rows', len(rows))
