import sqlite3,sys,os,json
sys.path.insert(0,os.getcwd())
import pandas as pd
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.code_execution.query_params import param_values_equal
from app.schemas.param_schema import ParamSpec
class SQLiteBoundary:
 description='SQLite calendar boundary test'
 def execute_query(self,sql):
  with sqlite3.connect(':memory:') as c:
   c.execute('create table events (id int, happened_at text)')
   c.executemany('insert into events values (?,?)',[(1,'2024-02-28 23:59:59.999999'),(2,'2024-02-29 00:00:00'),(3,'2024-02-29 23:59:59.999999999'),(4,'2024-03-01 00:00:00')])
   return pd.read_sql_query(sql,c)
code='''def generate_df(ds_clients, excel_files, params, calendar_date_bounds):
    start, stop = calendar_date_bounds(params['period'])
    return ds_clients['source'].execute_query("SELECT id FROM events WHERE (:start IS NULL OR happened_at >= :start) AND (:stop IS NULL OR happened_at < :stop) ORDER BY id", params={'start': start, 'stop': stop})
'''
out=[]
for bounds,ids in [({'from':'2024-02-29','to':'2024-02-29'},[2,3]),({'from':'2024-02-29'},[2,3,4]),({'to':'2024-02-29'},[1,2,3]),(None,[1,2,3,4])]:
 df,log,queries=StreamingCodeExecutor().execute_code(code=code,ds_clients={'source':SQLiteBoundary()},excel_files=[],params={'period':bounds})
 assert df.id.tolist()==ids,(bounds,df.to_dict())
 out.append({'params':bounds,'ids':ids})
spec=ParamSpec(name='instant',type='date')
assert not param_values_equal(spec,'2024-01-01T00:00:00.123456781Z','2024-01-01T00:00:00.123456782Z')
assert param_values_equal(spec,'2024-01-01T00:00:00.123456789Z','2024-01-01T02:00:00.123456789+02:00')
print(json.dumps({'calendar_boundary_checks':out,'nanosecond_and_offset_comparisons':'passed'}))
