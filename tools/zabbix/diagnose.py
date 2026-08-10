#!/usr/bin/env python3
"""Diagnose a Zabbix connection that "connects and shows a schema" but returns
no rows.

Schema discovery in the Zabbix connector is a catalog declared in code, so it
succeeds even when the API is unreachable or the credential can see nothing.
A green connection plus an empty schema-looking UI therefore proves very
little. This script exercises the paths the connector actually uses at query
time and prints which one is failing.

Run it from wherever the customer's network can reach Zabbix:

    python diagnose.py --url https://zabbix.example.com --token <API_TOKEN>
    python diagnose.py --url https://zabbix.example.com --user <U> --password <P>

Add --insecure for self-signed certs. It only issues read-only *.get calls.
"""
import argparse
import json
import os
import sys

import requests


def rpc(endpoint, method, params, *, bearer=None, auth_field=None, verify=True):
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    if auth_field:
        payload["auth"] = auth_field
    headers = {"Content-Type": "application/json-rpc"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    resp = requests.post(endpoint, data=json.dumps(payload), headers=headers,
                         timeout=60, verify=verify)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        err = body["error"]
        raise RuntimeError(f"[{err.get('code')}] {err.get('message')}: {err.get('data')}")
    return body.get("result")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="Zabbix frontend URL or full /api_jsonrpc.php")
    ap.add_argument("--token")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    endpoint = base if base.endswith("/api_jsonrpc.php") else f"{base}/api_jsonrpc.php"
    verify = not args.insecure

    print(f"endpoint : {endpoint}")
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        if os.environ.get(var):
            print(f"proxy env: {var}={os.environ[var]}")

    # 1. Reachability + version (unauthenticated).
    try:
        version = rpc(endpoint, "apiinfo.version", [], verify=verify)
    except Exception as e:
        print(f"\nFAIL  apiinfo.version — the API is not reachable: {e}")
        print("      A proxy, WAF, or wrong path (some installs live under "
              "/zabbix/api_jsonrpc.php) will stop here.")
        return 1
    print(f"version  : {version}")
    major, minor = (list(map(int, str(version).split(".")[:2])) + [0, 0])[:2]

    # 2. Authentication. On 6.4+ a user.login session token goes in the
    #    Authorization header; 7.2 removed the `auth` body property entirely.
    bearer, auth_field = args.token, None
    if not bearer:
        if not (args.user and args.password):
            print("\nFAIL  no credentials given (--token, or --user/--password)")
            return 1
        try:
            token = rpc(endpoint, "user.login",
                        {"username": args.user, "password": args.password}, verify=verify)
        except Exception as e:
            print(f"\nFAIL  user.login: {e}")
            return 1
        if (major, minor) >= (6, 4):
            bearer = token
            print("auth     : user.login session token via Authorization: Bearer")
        else:
            auth_field = token
            print("auth     : user.login session token via body `auth` (pre-6.4)")
    else:
        print("auth     : API token via Authorization: Bearer")

    def call(label, method, params):
        try:
            result = rpc(endpoint, method, params, bearer=bearer,
                         auth_field=auth_field, verify=verify)
        except Exception as e:
            print(f"FAIL  {label:<24} {e}")
            return None
        n = len(result) if isinstance(result, list) else result
        flag = "  <-- EMPTY" if isinstance(result, list) and not result else ""
        print(f"ok    {label:<24} {n}{flag}")
        return result

    print("\n--- visibility ---")
    hosts = call("host.get", "host.get", {"output": ["hostid", "host", "name"], "limit": 5})
    call("hostgroup.get", "hostgroup.get", {"output": ["groupid", "name"], "limit": 5})
    call("problem.get", "problem.get", {"output": "extend", "limit": 5})
    call("problem.get sev 4-5", "problem.get", {"output": "extend", "severities": [4, 5], "limit": 5})
    call("trigger.get", "trigger.get", {"output": ["triggerid", "description"], "limit": 5})

    if hosts is not None and not hosts:
        print("\nDIAGNOSIS: authentication works but this credential can see 0 hosts.")
        print("  Zabbix silently filters every *.get to the API user's permitted host")
        print("  groups — no error, just []. Grant the user's user group read access on")
        print("  the relevant host groups (Users -> User groups -> Host permissions),")
        print("  or use a Super admin role. Note Super admin *role* is what matters;")
        print("  an Admin-role token is still filtered.")
        return 1

    # 3. The column that no longer exists. Zabbix does not reject it — it just
    #    omits the key, so the column silently vanishes from every row.
    print("\n--- host availability columns (removed from host object in 6.0) ---")
    for col in ("available", "active_available"):
        res = call(f"host.get + {col}", "host.get", {"output": ["hostid", col], "limit": 1})
        if res:
            print(f"      {col!r} present in row: {col in res[0]}")

    # 4. history.get value-type trap: the API default is 3 (unsigned int), so a
    #    float item returns [] with no error unless `history` is set to 0.
    print("\n--- history value types ---")
    items = call("item.get", "item.get",
                 {"output": ["itemid", "name", "value_type"],
                  "hostids": [h["hostid"] for h in (hosts or [])[:1]] or None,
                  "limit": 5})
    float_item = next((i for i in (items or []) if str(i.get("value_type")) == "0"), None)
    if float_item:
        iid = float_item["itemid"]
        print(f"      using float item {iid} ({float_item.get('name')})")
        call("history.get default", "history.get",
             {"output": "extend", "itemids": [iid], "limit": 3})
        call("history.get history=0", "history.get",
             {"output": "extend", "itemids": [iid], "history": 0, "limit": 3})
        print("      if 'default' is empty and 'history=0' is not, that is the "
              "value-type trap.")
    else:
        print("      no float item found to test with")

    print("\nDone. Any FAIL or EMPTY line above is the failing path.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
