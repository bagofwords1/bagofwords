"""Salesforce data-source client.

Authentication (auto-detected from the fields the constructor receives):

- **JWT Bearer** (`consumer_key` + `private_key` + `username`) — the OAuth 2.0
  `urn:ietf:params:oauth:grant-type:jwt-bearer` server-to-server flow. A
  Connected App's certificate signs a short-lived JWT that is exchanged for an
  access token; no interactive login, no stored password. This is the
  recommended path and mirrors the customer's existing ETL setup.
- **Access token** (`access_token` + `instance_url`) — a pre-obtained session,
  used by the per-user delegated OAuth path and by tests.
- **Username / password** (`username` + `password` [+ `security_token`]) — the
  legacy SOAP login, kept for backwards compatibility.

Schema discovery enumerates the org's queryable objects dynamically (standard
AND custom `__c`), filtering system/noise objects — replacing the previous
hardcoded five-object list. Field types are mapped to canonical dtypes and
reference fields become foreign keys.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from functools import cached_property
from typing import Generator, List, Optional

import pandas as pd
import requests
from simple_salesforce import Salesforce

from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import ProgressCallback, make_reporter
from app.ai.prompt_formatters import ForeignKey, Table, TableColumn, ServiceFormatter

logger = logging.getLogger(__name__)

# Hard cap on rows returned by a single execute_query. SOQL is model-generated
# and may omit LIMIT; `query_all` would otherwise stream an entire object into
# memory. We paginate up to this ceiling and stop.
MAX_ROWS = 10_000

# Upper bound on objects indexed when discovering dynamically, so a large org
# (which can expose 1000+ sobjects) can't produce a runaway catalog. Curated
# CRM objects are prioritized so they survive the cap; the dropped count is
# logged (never silently truncated).
MAX_INDEX_OBJECTS = 500

JWT_EXP_SECONDS = 180
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# Objects a CRM user almost always cares about — surfaced first so they win the
# MAX_INDEX_OBJECTS cap, and used as the fallback set if discovery is blocked.
PRIORITY_OBJECTS = [
    "Account", "Contact", "Lead", "Opportunity", "Case", "Campaign",
    "User", "Task", "Event", "Contract", "Order", "Product2", "Pricebook2",
    "Quote", "OpportunityLineItem", "CampaignMember",
]

# Suffixes of Salesforce system/plumbing objects that are queryable but carry no
# analytical value (change-data-capture, sharing rows, field history, chatter).
_NOISE_SUFFIXES = (
    "Share", "History", "Feed", "ChangeEvent", "Tag", "EventRelation",
    "StatusUpdate",
)

# Salesforce field type -> canonical dtype used across the catalog.
_SF_TYPE_MAP = {
    "id": "str",
    "reference": "reference",
    "string": "str",
    "textarea": "str",
    "picklist": "str",
    "multipicklist": "str",
    "combobox": "str",
    "email": "str",
    "phone": "str",
    "url": "str",
    "encryptedstring": "str",
    "base64": "str",
    "address": "str",
    "location": "str",
    "boolean": "bool",
    "int": "int",
    "long": "int",
    "double": "float",
    "currency": "float",
    "percent": "float",
    "date": "date",
    "datetime": "datetime",
    "time": "time",
    "anyType": "str",
}


class SalesforceClient(DataSourceClient):
    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        security_token: Optional[str] = None,
        domain: Optional[str] = "login",
        sandbox: bool = False,
        consumer_key: Optional[str] = None,
        private_key: Optional[str] = None,
        access_token: Optional[str] = None,
        instance_url: Optional[str] = None,
        objects: Optional[str] = None,
    ):
        self.username = username
        self.password = password
        self.security_token = security_token
        self.domain = (domain or "login").strip()
        self.sandbox = bool(sandbox)
        self.consumer_key = consumer_key
        self.private_key = private_key
        self.access_token = access_token
        self.instance_url = (instance_url or "").rstrip("/") or None
        # Optional explicit object allowlist (comma-separated) — overrides
        # dynamic discovery when set.
        self.objects = [o.strip() for o in objects.split(",") if o.strip()] if objects else None

    # ── auth ─────────────────────────────────────────────────────────────────

    @property
    def _auth_mode(self) -> str:
        if self.access_token and self.instance_url:
            return "token"
        if self.consumer_key and self.private_key and self.username:
            return "jwt"
        if self.username and self.password:
            return "userpass"
        return "none"

    def _login_url(self) -> str:
        """Base OAuth host for the token endpoint / JWT audience."""
        if self.sandbox:
            return "https://test.salesforce.com"
        # A custom My Domain (anything other than the login/test aliases) logs
        # in against that host; otherwise production login.
        if self.domain and self.domain not in ("login", "test"):
            return f"https://{self.domain}.my.salesforce.com"
        return "https://login.salesforce.com"

    def _sf_domain(self) -> str:
        """`domain` kwarg for simple_salesforce's SOAP (username/password) login.
        Fixes the long-standing bug where sandbox/domain were captured but never
        forwarded, so sandbox and My-Domain logins silently hit production.
        """
        if self.sandbox:
            return "test"
        return self.domain or "login"

    def _jwt_access(self) -> tuple[str, str]:
        """Run the JWT Bearer flow; return (access_token, instance_url)."""
        import jwt  # PyJWT

        login_url = self._login_url()
        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": self.consumer_key,
                "sub": self.username,
                "aud": login_url,
                "exp": now + JWT_EXP_SECONDS,
            },
            self.private_key,
            algorithm="RS256",
        )
        resp = requests.post(
            f"{login_url}/services/oauth2/token",
            data={"grant_type": JWT_GRANT, "assertion": assertion},
            timeout=60,
        )
        if resp.status_code != 200:
            detail = resp.text[:500]
            try:
                body = resp.json()
                detail = f"{body.get('error')}: {body.get('error_description', detail)}"
            except Exception:
                pass
            raise RuntimeError(
                f"Salesforce JWT authentication failed ({resp.status_code}): {detail}. "
                "Check the Connected App consumer key, the certificate, the user "
                "(sub), and that the app is admin pre-authorized for that user."
            )
        data = resp.json()
        return data["access_token"], data["instance_url"].rstrip("/")

    @cached_property
    def sf(self) -> Salesforce:
        mode = self._auth_mode
        if mode == "token":
            return Salesforce(instance_url=self.instance_url, session_id=self.access_token)
        if mode == "jwt":
            token, instance_url = self._jwt_access()
            return Salesforce(instance_url=instance_url, session_id=token)
        if mode == "userpass":
            return Salesforce(
                username=self.username,
                password=self.password,
                security_token=self.security_token or "",
                domain=self._sf_domain(),
            )
        raise RuntimeError(
            "Salesforce client has no usable credentials: provide a Connected App "
            "(consumer_key + private_key + username), an access_token + instance_url, "
            "or username + password."
        )

    @contextmanager
    def connect(self) -> Generator[Salesforce, None, None]:
        """Yield a connection to Salesforce."""
        try:
            yield self.sf
        except Exception as e:
            raise RuntimeError(f"Error while connecting to Salesforce: {e}")

    def test_connection(self):
        """Test the Salesforce connection with a lightweight authenticated call."""
        try:
            with self.connect() as sf:
                sf.limits()
                return {"success": True, "message": "Connected to Salesforce"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── schema discovery ─────────────────────────────────────────────────────

    def _discover_object_names(self) -> List[str]:
        """Enumerate queryable standard + custom objects, minus system/noise.

        Priority objects come first so they survive the MAX_INDEX_OBJECTS cap.
        """
        if self.objects:
            return self.objects

        global_desc = self.sf.describe()
        candidates: List[str] = []
        for obj in global_desc.get("sobjects", []):
            name = obj.get("name")
            if not name:
                continue
            if not obj.get("queryable"):
                continue
            if obj.get("deprecatedAndHidden") or obj.get("customSetting"):
                continue
            if any(name.endswith(suffix) for suffix in _NOISE_SUFFIXES):
                continue
            candidates.append(name)

        candidate_set = set(candidates)
        ordered = [n for n in PRIORITY_OBJECTS if n in candidate_set]
        ordered += sorted(n for n in candidate_set if n not in set(PRIORITY_OBJECTS))

        if len(ordered) > MAX_INDEX_OBJECTS:
            logger.warning(
                "Salesforce discovery found %d indexable objects; capping at %d "
                "(set the connection's `objects` list to index specific ones). "
                "Dropped %d objects.",
                len(ordered), MAX_INDEX_OBJECTS, len(ordered) - MAX_INDEX_OBJECTS,
            )
            ordered = ordered[:MAX_INDEX_OBJECTS]
        return ordered

    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        """Discover object schemas dynamically (standard + custom)."""
        names = self._discover_object_names()
        reporter = make_reporter(progress_callback)
        reporter.phase("Indexing Salesforce objects", total=len(names))

        schemas: List[Table] = []
        for name in names:
            try:
                schemas.append(self.get_schema(name))
            except Exception as e:
                # A single unreadable object (field-level security, odd metadata)
                # must not abort the whole catalog.
                logger.warning("Skipping Salesforce object %s: %s", name, e)
            reporter.tick(name)
        return schemas

    def get_schema(self, object_name: str) -> Table:
        """Get schema for a specific Salesforce object, with FKs and dtypes."""
        with self.connect() as sf:
            describe = sf.__getattr__(object_name).describe()

        columns: List[TableColumn] = []
        fks: List[ForeignKey] = []
        for field in describe["fields"]:
            fname = field["name"]
            ftype = field.get("type", "string")
            dtype = _SF_TYPE_MAP.get(ftype, "str")
            column = TableColumn(name=fname, dtype=dtype, description=field.get("label"))
            columns.append(column)

            if ftype == "reference":
                references = [r for r in (field.get("referenceTo") or []) if r]
                if references:
                    fks.append(ForeignKey(
                        column=column,
                        references_name=references[0],
                        references_column=TableColumn(name="Id", dtype="str"),
                    ))

        return Table(
            name=object_name,
            columns=columns,
            pks=[TableColumn(name="Id", dtype="str")],
            fks=fks,
        )

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    # ── querying ─────────────────────────────────────────────────────────────

    def execute_query(self, query: str) -> pd.DataFrame:
        """Execute a SOQL query and return results as a DataFrame.

        Paginates up to MAX_ROWS so a LIMIT-less query can't stream an entire
        object into memory.
        """
        try:
            with self.connect() as sf:
                result = sf.query(query)
                records = list(result.get("records", []))
                while not result.get("done") and len(records) < MAX_ROWS:
                    result = sf.query_more(result["nextRecordsUrl"], identifier_is_url=True)
                    records.extend(result.get("records", []))
                records = records[:MAX_ROWS]

                df = pd.DataFrame(records)
                if not df.empty and "attributes" in df.columns:
                    df = df.drop("attributes", axis=1)
                return df
        except Exception as e:
            raise RuntimeError(f"Error executing Salesforce query: {e}")

    # ── prompts / description ────────────────────────────────────────────────

    def system_prompt(self):
        """Provide a detailed system prompt for LLM integration."""
        text = """
        ## System Prompt for Salesforce Integration
        This service allows querying Salesforce data using SOQL (Salesforce Object Query Language).
        Use `execute_query` to run SOQL queries.

        Example:
        ```python
        df = client.execute_query("SELECT Id, Name, Type, Industry FROM Account LIMIT 10")
        df_lead = client.execute_query("SELECT Id, FirstName, LastName, Company, Status FROM Lead LIMIT 10")
        df_case = client.execute_query("SELECT Id, CaseNumber, Status, Priority, Subject FROM Case LIMIT 10")
        ```

        SOQL is similar to SQL but has some differences:
        1. FROM clause comes immediately after SELECT
        2. Supports relationship queries using dot notation (e.g. Contact.Account.Name)
        3. No table aliases or joins (use relationship queries instead)
        4. Always include a LIMIT — result sets are capped for performance

        Both standard objects (Account, Contact, Opportunity, Lead, Case, ...) and
        custom objects (suffixed `__c`) are available; consult the indexed schema
        for the exact objects and fields in this org.
        """
        return text

    @property
    def description(self):
        text = "Salesforce Client, execute SOQL queries to retrieve Salesforce data."
        return text + "\n\n" + self.system_prompt()
