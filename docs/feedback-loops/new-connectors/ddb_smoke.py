import os, sys, decimal
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[3] / "backend"))
import boto3
EP = os.environ.get("DDB_EP")  # None → real AWS
TBL = os.environ.get("DDB_TABLE", "bow-test-orders")
kw = dict(region_name="us-east-1")
if EP: kw.update(endpoint_url=EP, aws_access_key_id="x", aws_secret_access_key="y")
ddb = boto3.client("dynamodb", **kw)

def seed():
    try: ddb.delete_table(TableName=TBL); ddb.get_waiter("table_not_exists").wait(TableName=TBL)
    except ddb.exceptions.ResourceNotFoundException: pass
    ddb.create_table(TableName=TBL, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName":"customer_id","AttributeType":"S"},{"AttributeName":"order_date","AttributeType":"S"},{"AttributeName":"status","AttributeType":"S"}],
        KeySchema=[{"AttributeName":"customer_id","KeyType":"HASH"},{"AttributeName":"order_date","KeyType":"RANGE"}],
        GlobalSecondaryIndexes=[{"IndexName":"by_status","KeySchema":[{"AttributeName":"status","KeyType":"HASH"},{"AttributeName":"order_date","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}],
        Tags=[{"Key":"bow-test","Value":"session"}])
    ddb.get_waiter("table_exists").wait(TableName=TBL)
    t = boto3.resource("dynamodb", **kw).Table(TBL)
    with t.batch_writer() as w:
        for i in range(60):
            w.put_item(Item={"customer_id": f"c-{i%6}", "order_date": f"2025-0{1+i%9}-{10+i%18}T{i:02d}", "status": ["OPEN","SHIPPED","CANCELLED"][i%3],
                             "total": decimal.Decimal(str(round(10.5+i*3.25,2))), "items": [{"sku": f"s{i}", "qty": 1+i%4}],
                             "address": {"city": ["Paris","Austin","Tel Aviv"][i%3], "zip": f"{75000+i}"}, "tags": {"vip"} if i%5==0 else {"std"}})

if __name__ == "__main__":
    seed()
    from app.data_sources.clients.dynamodb_client import DynamodbClient
    ck = dict(region="us-east-1", tables=TBL, max_items=50)
    if EP: ck.update(endpoint_url=EP, access_key="x", secret_key="y")
    c = DynamodbClient(**ck)
    print(c.test_connection())
    t = c.get_schemas()[0]
    print(t.name, "|", t.description); print([ (col.name, col.dtype) for col in t.columns])
    df = c.execute_query(f'SELECT * FROM "{TBL}" WHERE "customer_id" = ?', parameters=["c-1"])
    print("by key:", len(df), list(df.columns)[:4], df["total"].dtype)
    df = c.execute_query(f'SELECT "customer_id", "total" FROM "{TBL}"."by_status" WHERE "status" = \'SHIPPED\'')
    print("by GSI:", len(df), round(df["total"].sum(), 2))
    df = c.execute_query(f'SELECT * FROM "{TBL}"')
    print("scan capped:", len(df), "truncated:", df.attrs["truncated"])
    df = c.execute_query(f'SELECT "address"."city" FROM "{TBL}" WHERE "customer_id" = \'c-2\'')
    print("nested:", df.iloc[0].to_dict())
    for bad in [f'DELETE FROM "{TBL}" WHERE "customer_id" = \'c-1\'', f'SELECT * FROM "{TBL}"; DELETE FROM "{TBL}"', f'UPDATE "{TBL}" SET x=1']:
        try: c.execute_query(bad); print("NOT BLOCKED", bad); sys.exit(1)
        except ValueError as e: print("blocked:", str(e)[:70])
    print(c.prompt_schema()[:600])
    print("ALL OK")
