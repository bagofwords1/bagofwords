"""Seed GCS (fake-gcs-server) + Azure (Azurite) and exercise the clients end to end."""
import io, os, sys
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost"); os.environ["no_proxy"] = os.environ["NO_PROXY"]
import pandas as pd

FILES = {
    "sales/2025/q1.csv": b"region,revenue\nEMEA,120\nAMER,340\nAPAC,99\n",
    "sales/2025/notes.txt": b"Quarterly notes\nHIT: churn rose in APAC\nend\n",
    "logs/app.log": ("line\n" * 5000 + "ERROR café exploded\n" + "line\n" * 10).encode(),
    "readme.md": b"# Bucket readme\nproduct catalogue\n",
    ".DS_Store": b"junk",
}

def seed_gcs():
    from google.cloud import storage
    from google.auth.credentials import AnonymousCredentials
    c = storage.Client(project="test", credentials=AnonymousCredentials(), client_options={"api_endpoint": "http://127.0.0.1:4443"})
    try: c.create_bucket("bow-test")
    except Exception: pass
    b = c.bucket("bow-test")
    for k, v in FILES.items(): b.blob(k).upload_from_string(v)

AZ_CONN = ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
           "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
           "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;")
def seed_az():
    from azure.storage.blob import BlobServiceClient
    s = BlobServiceClient.from_connection_string(AZ_CONN)
    try: s.create_container("bow-test")
    except Exception: pass
    cc = s.get_container_client("bow-test")
    for k, v in FILES.items(): cc.upload_blob(k, v, overwrite=True)

def exercise(client, label):
    print(f"== {label}")
    t = client.test_connection(); print(" test_connection:", t); assert t["success"], t
    files = client.list_files(); names = sorted(f["id"] for f in files)
    print(" list:", names); assert ".DS_Store" not in names and "sales/2025/q1.csv" in names
    top = client.list_files(recursive=False); print(" top-level:", sorted((f["id"], f["is_folder"]) for f in top))
    assert {"sales/", "logs/"} <= {f["id"] for f in top if f["is_folder"]}
    df = client.read_file("sales/2025/q1.csv"); assert isinstance(df, pd.DataFrame) and df["revenue"].sum() == 559; print(" csv sum:", df["revenue"].sum())
    w = client.read_file("logs/app.log", offset=0, length=100); print(" window:", {k: w[k] for k in ("next_cursor","total_size","eof")})
    assert w["total_size"] == len(FILES["logs/app.log"]) and not w["eof"]
    tail = client.read_file("logs/app.log", offset=w["total_size"]-80, length=1000); print(" tail keys:", sorted(tail)); assert tail["eof"] and "café" in str(tail.get("content") or tail.get("text") or tail), tail
    past = client.read_file("logs/app.log", offset=w["total_size"]+10, length=10); assert past["eof"], past
    g = client.grep_files("ERROR", file_ids=["logs/app.log"]); print(" grep matches:", [m.get("line") or m.get("text") for m in g["matches"]][:2])
    assert g["matches"], g
    g2 = client.grep_files("HIT", name_pattern="*.txt"); assert g2["matches"], g2
    g3 = client.grep_files("ERROR", file_ids=["logs/app.log"], max_bytes_per_file=4096)
    cur, found = g3, list(g3["matches"])
    for _ in range(50):
        if not cur.get("next_cursor"): break
        cur = client.grep_files("ERROR", file_ids=["logs/app.log"], max_bytes_per_file=4096, cursor=cur["next_cursor"]); found += cur["matches"]
    assert len(found) == 1, found; print(" resumable grep found:", len(found))
    miss = client.grep_files("x", file_ids=["nope.txt"]); print(" missing ->", {k: v for k, v in miss.items() if "skip" in k})
    v = client.file_version("readme.md"); assert v; print(" version:", v)
    data, name, mime = client.read_raw_bytes("readme.md"); assert data == FILES["readme.md"]
    for bad in ["../x", "/etc/passwd", "sales/../../x"]:
        try: client.read_file(bad); 
        except Exception as e: pass
    tabs = client.get_schemas(); print(" catalog:", len(tabs), "rows; keywords on notes:", [t.metadata_json for t in tabs if t.name.endswith("notes.txt")][0][client.metadata_key].get("keywords")[:5])
    scoped = type(client)(**{**client._init_kwargs, "prefix": "sales/"}) if hasattr(client, "_init_kwargs") else None
    print(" OK")

if __name__ == "__main__":
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[3] / "backend"))
    seed_gcs(); seed_az()
    from app.data_sources.clients.gcs_client import GCSClient
    from app.data_sources.clients.azure_blob_client import AzureBlobClient
    exercise(GCSClient(bucket="bow-test", endpoint_url="http://127.0.0.1:4443", project="test"), "gcs")
    exercise(AzureBlobClient(container="bow-test", connection_string=AZ_CONN), "azure_blob")
    # prefix confinement on a real store
    c = GCSClient(bucket="bow-test", prefix="sales", endpoint_url="http://127.0.0.1:4443", project="test")
    ids = sorted(f["id"] for f in c.list_files()); print("gcs scoped:", ids); assert ids == ["2025/notes.txt", "2025/q1.csv"]
    a = AzureBlobClient(container="bow-test", prefix="sales/", connection_string=AZ_CONN, include_globs="**/*.csv")
    ids = sorted(f["id"] for f in a.list_files()); print("az scoped+glob:", ids); assert ids == ["2025/q1.csv"]
    try: a.read_file("2025/notes.txt"); raise SystemExit("glob scope not enforced!")
    except Exception as e: print("az off-glob denied:", type(e).__name__)
    print("ALL OK")

def changed_mid_scan(client, rewrite):
    first = client.grep_files("NEVER", file_ids=["logs/app.log"], max_bytes_per_file=4096)
    assert first.get("next_cursor"), first
    rewrite()
    second = client.grep_files("NEVER", file_ids=["logs/app.log"], max_bytes_per_file=4096, cursor=first["next_cursor"])
    print(" after rewrite:", {k: second.get(k) for k in ("stop_reason", "skipped_files", "coverage_complete")}, "matches", len(second["matches"]))
    return second

if __name__ == "__main__":
    from google.cloud import storage
    from google.auth.credentials import AnonymousCredentials
    from azure.storage.blob import BlobServiceClient
    g = GCSClient(bucket="bow-test", endpoint_url="http://127.0.0.1:4443", project="test")
    gc = storage.Client(project="test", credentials=AnonymousCredentials(), client_options={"api_endpoint": "http://127.0.0.1:4443"})
    print("== gcs changed mid-scan"); changed_mid_scan(g, lambda: gc.bucket("bow-test").blob("logs/app.log").upload_from_string(b"rewritten\n" * 3000))
    a = AzureBlobClient(container="bow-test", connection_string=AZ_CONN); print("az scope:", a.description)
    cc = BlobServiceClient.from_connection_string(AZ_CONN).get_container_client("bow-test")
    print("== az changed mid-scan"); changed_mid_scan(a, lambda: cc.upload_blob("logs/app.log", b"rewritten\n" * 3000, overwrite=True))
    seed_gcs(); seed_az()
