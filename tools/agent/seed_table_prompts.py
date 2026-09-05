"""Seed seven synthetic table prompts for the canvas browser regression.

Run after seed_tables_canvas.py. Requires an isolated SQLite sandbox.
"""
import argparse, asyncio, json, os, sys, uuid
from pathlib import Path
from datetime import datetime,timedelta
parser = argparse.ArgumentParser()
parser.add_argument('--seed', required=True)
parser.add_argument('--app-db', required=True)
args = parser.parse_args()
args.seed = str(Path(args.seed).resolve())
args.app_db = str(Path(args.app_db).resolve())
backend = Path(__file__).resolve().parents[2] / 'backend'
os.chdir(backend)
sys.path.insert(0, str(backend))
os.environ['TESTING'] = 'true'
os.environ['ENVIRONMENT'] = 'production'
os.environ['TEST_DATABASE_URL'] = 'sqlite:///' + str(Path(args.app_db).resolve())
import main
from sqlalchemy import select
from app.dependencies import async_session_maker
from app.models.datasource_table import DataSourceTable
from app.models.report import Report
from app.models.widget import Widget
from app.models.step import Step
from app.models.completion import Completion
from app.models.agent_execution import AgentExecution
from app.models.tool_execution import ToolExecution
from app.models.table_usage_event import TableUsageEvent
from app.models.user import User
async def run():
 s=json.load(open(args.seed))
 async with async_session_maker() as db:
  marker='table-prompts-' + s['sqlite_sources'][0]['id']
  if await db.scalar(select(Report.id).where(Report.slug == marker)):
   print('Synthetic prompt history already exists');return
  table=await db.scalar(select(DataSourceTable).where(DataSourceTable.datasource_id==s['sqlite_sources'][0]['id'],DataSourceTable.name=='orders'))
  user=await db.scalar(select(User).where(User.email==s['admin']['email']))
  report=Report(title='Synthetic prompt examples',slug=marker,user_id=user.id,organization_id=s['organization']['id'])
  db.add(report);await db.flush()
  widget=Widget(title='Examples',slug=str(uuid.uuid4()),report_id=report.id);db.add(widget);await db.flush()
  texts=['Which products drove revenue growth this quarter?','Compare repeat orders with first-time purchases.','Show weekly order volume by region.','Which customers placed more than five orders?','What is the average order value by month?','Find orders with overdue payments.','Summarize returns and cancellations for last month.']
  for i,text in enumerate(texts):
   u=Completion(report_id=report.id,role='user',prompt={'content':text});db.add(u);await db.flush()
   a=Completion(report_id=report.id,parent_id=u.id);db.add(a);await db.flush()
   ae=AgentExecution(report_id=report.id,organization_id=s['organization']['id'],completion_id=a.id);db.add(ae);await db.flush()
   step=Step(widget_id=widget.id,slug=str(uuid.uuid4()),status='success');db.add(step);await db.flush()
   db.add(ToolExecution(agent_execution_id=ae.id,created_step_id=step.id,tool_name='create_data',status='completed',success=i!=1))
   db.add(TableUsageEvent(org_id=s['organization']['id'],report_id=report.id,data_source_id=table.datasource_id,datasource_table_id=table.id,step_id=step.id,table_fqn=table.name,source_type='sql',success=i!=1,used_at=datetime(2026,9,1)+timedelta(hours=i)))
  await db.commit()
 print('Seeded seven synthetic prompts')
asyncio.run(run())
