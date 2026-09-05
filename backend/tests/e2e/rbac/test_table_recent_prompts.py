"""Exact table lineage, pagination, deduplication, and agent-admin isolation."""
import asyncio
from datetime import datetime, timedelta
from uuid import uuid4
import pytest
from app.dependencies import async_session_maker
from app.models.report import Report
from app.models.widget import Widget
from app.models.step import Step
from app.models.completion import Completion
from app.models.agent_execution import AgentExecution
from app.models.tool_execution import ToolExecution
from app.models.datasource_table import DataSourceTable
from app.models.table_usage_event import TableUsageEvent

async def seed_history(org_id, agent_id, other_agent_id, user_id):
    # No public API creates execution/lineage records without invoking an LLM.
    # Only audit records use direct DB seeding; users/agents use API fixtures.
    async with async_session_maker() as db:
        table = DataSourceTable(datasource_id=agent_id, name='shared_table')
        other = DataSourceTable(datasource_id=other_agent_id, name='shared_table')
        neighbor = DataSourceTable(datasource_id=agent_id, name='shared_table')
        db.add_all([table, other, neighbor]); await db.flush()
        report = Report(title='Prompt examples', slug=str(uuid4()), user_id=user_id, organization_id=org_id)
        db.add(report); await db.flush()
        widget = Widget(title='Examples', slug=str(uuid4()), report_id=report.id)
        db.add(widget); await db.flush()
        for i, target in enumerate([table] * 7 + [other, neighbor, None]):
            user = Completion(report_id=report.id, role='user', prompt={'content':f'Example {i}'})
            db.add(user); await db.flush()
            answer = Completion(report_id=report.id, parent_id=user.id)
            db.add(answer); await db.flush()
            execution = AgentExecution(report_id=report.id, organization_id=org_id, completion_id=answer.id)
            db.add(execution); await db.flush()
            for attempt in range(2):
                step = Step(widget_id=widget.id, slug=str(uuid4()), status='success')
                db.add(step); await db.flush()
                db.add(ToolExecution(agent_execution_id=execution.id, created_step_id=step.id,
                    tool_name='create_data', success=i != 2, status='completed'))
                db.add(TableUsageEvent(org_id=org_id, report_id=report.id,
                    data_source_id=target.datasource_id if target else agent_id,
                    datasource_table_id=target.id if target else None,
                    step_id=step.id, table_fqn='shared_table', source_type='sql', success=i != 2,
                    used_at=datetime(2026, 1, 1) + timedelta(days=i)))
        await db.commit()
        return table.id, other.id

@pytest.mark.e2e
def test_prompts_are_paginated_deduplicated_and_agent_admin_only(
    bootstrap_admin, create_data_source, invite_user_to_org, grant_resource, test_client, tmp_path,
):
    admin = bootstrap_admin()
    org_id, token = admin['org_id'], admin['token']
    agents = [create_data_source(name=f'Prompts {i}', type='network_dir',
        config={'root_path':str(tmp_path)}, credentials={'auth_type':'none'},
        user_token=token, org_id=org_id) for i in range(2)]
    table_id, other_id = asyncio.run(seed_history(org_id, agents[0]['id'], agents[1]['id'], admin['user_id']))
    headers = {'Authorization':f'Bearer {token}', 'X-Organization-Id':org_id}
    url = f"/api/data_sources/{agents[0]['id']}/tables/{table_id}/recent-prompts"
    response = test_client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    first = response.json()
    assert len(first['items']) == 5
    second = test_client.get(url, params={'offset':first['next_offset']}, headers=headers).json()
    rows = first['items'] + second['items']
    assert second['next_offset'] is None
    assert [r['prompt'] for r in rows] == [f'Example {i}' for i in range(6, -1, -1)]
    assert len({r['execution_id'] for r in rows}) == 7
    assert sum(not r['success'] for r in rows) == 1
    assert test_client.get(url.replace(table_id, other_id), headers=headers).status_code == 404
    assert test_client.get(url, params={'limit':21}, headers=headers).status_code == 422
    member = invite_user_to_org(org_id=org_id, admin_token=token)
    assert test_client.get(url, headers={**headers, 'Authorization':f"Bearer {member['token']}"}).status_code == 403
    # Being an admin of another agent never grants access to this one's prompts.
    granted = grant_resource(resource_type='data_source', resource_id=agents[1]['id'],
        principal_type='user', principal_id=member['user_id'], permissions=['manage'],
        user_token=token, org_id=org_id)
    assert granted.status_code in (200, 201), granted.text
    member_headers = {**headers, 'Authorization':f"Bearer {member['token']}"}
    assert test_client.get(url, headers=member_headers).status_code == 403
    granted = grant_resource(resource_type='data_source', resource_id=agents[0]['id'],
        principal_type='user', principal_id=member['user_id'], permissions=['manage'],
        user_token=token, org_id=org_id)
    assert granted.status_code in (200, 201), granted.text
    assert test_client.get(url, headers=member_headers).status_code == 200
    outsider = bootstrap_admin('outsider')
    assert test_client.get(url, headers={'Authorization':f"Bearer {outsider['token']}",
        'X-Organization-Id':outsider['org_id']}).status_code in (403, 404)
