import sys
out = sys.argv[1]
s=open('tools/agent/stub_llm.py').read()
a=s[s.index('def _codegen_response'):s.index('def _chunk')]
new='''def _codegen_response(text: str) -> str:
    # The key is re-read on every call, so the loop can change which name the
    # "model" spells without restarting the stub.
    src = _load()[0]
    return (
        "def generate_df(ds_clients, excel_files):\\n"
        f"    df = ds_clients[{src['key']!r}].execute_query('SELECT region, COUNT(*) AS orders_count, "
        f"SUM(amount) AS total_amount FROM {src['table']} GROUP BY region ORDER BY region')\\n"
        "    return df\\n"
    )


'''
s=s.replace(a,new)
s=s.replace('SOURCES = json.loads(open(os.environ["STUB_SOURCES_FILE"]).read()) if os.environ.get("STUB_SOURCES_FILE") else []','''def _load():
    return json.loads(open(os.environ["STUB_SOURCES_FILE"]).read())


SOURCES = _load()''')
s=s.replace('''    n = len(SOURCES)
    obs = _observation_count''','''    SOURCES = _load()
    n = len(SOURCES)
    obs = _observation_count''')
open(out,'w').write(s)
