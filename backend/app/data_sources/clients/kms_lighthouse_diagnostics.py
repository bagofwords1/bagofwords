"""On-site diagnostics for the KMS Lighthouse connector.

No real KMS account exists before a client install, and the client network is
unknown until then. One run answers the Live-probe checklist in
docs/feedback-loops/kms-lighthouse-connector.md as far as one account can, and
says where a failure is: network, TLS, login or KMS behaviour.

The safety rules, each held by this module:

1. Read-only: one login POST and the GETs the connector already makes, through
   the client's own ``_send`` (same session setup, TLS verification and redirect
   refusal). The ``connector_*`` rows call the public ``list_files``,
   ``search_files`` and ``read_file``, because only the connector can say whether
   a tenant works for it: an answer it would refuse must never read as a pass.
2. One login at most, never with a hold in force. A hold or a rejection ends the
   run. The public methods log in again after a refused session, so they run
   inside ``kc.proof_calls``, where a login is refused.
3. At most ``_MAX_CALLS`` calls and ``_MAX_SECONDS`` seconds. The clock is read
   before every call and each call's socket timeouts are cut to fit, connect and
   read together, so a server that goes silent cannot overrun it by more than the
   one-second timeout floor. A server that keeps trickling bytes inside the socket
   timeout can: nothing short of a second thread could interrupt that read.
   ``_PROOF_CALLS`` is the share of the connector rows, so a large tenant cannot
   take the calls the other checks need.
4. No content leaves: rows hold key names, type names, counts, status codes and
   booleans. ``_closed`` is the chokepoint; any other string becomes ``"other"``.
   Summaries are fixed text with numbers.
5. One check failing never stops the others, unless login failed. An unexpected
   exception becomes that check's ``fail`` with its type name only.
6. Sampling is small and fixed: ``_MAX_ITEMS`` items and ``_MAX_SCENARIOS`` scenarios.

Only a call the connector really makes with this configuration can fail a run
or end it. Every other call goes through ``_Run.observe``: the budgets count it,
and whatever it gets back is a word in its row.
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
from collections import Counter
from datetime import datetime, timezone
from itertools import zip_longest
from urllib.parse import urlsplit

from app.core.url_safety import UnsafeURLError
from app.data_sources.clients import kms_lighthouse_client as kc
from app.data_sources.clients.base import SearchStopped
from app.settings.config import settings

logger = logging.getLogger(__name__)

_REPORT_VERSION = 1
_MAX_CALLS = 60
_MAX_SECONDS = 90
_ROUTE_GRACE_SECONDS = 5
_CALL_SECONDS = 20            # one slow endpoint must not use up the whole run
_MAX_ITEMS = 5
_MAX_SCENARIOS = 2
_TREE_WALK_CALLS = 4          # folders opened to find items and scenarios
_STEP_FETCHES = 3             # per sampled scenario
_MAX_SCENARIO_NODES = 400
_MAX_NAMES = 40
_PROOF_CHECKS = ("connector_browse", "connector_search", "connector_read")
_PROOF_CALLS = 21             # 60 less the 39 the other checks can need at most
_PROOF_ENTRIES = 10           # one tree call per listed folder at most, so at most 11 calls
_PROOF_HITS = 5
_PROOF_ITEMS = 2
_PROOF_SCENARIOS = 1
# The connector needs a query; tenants differ in language, so try a few fixed ones.
_SAMPLE_QUERIES = ("a", "*", "ה")
_TITLE_WORD_RE = re.compile(r"\w{3,}")
_NO_HIT_QUERY = "zqxbowdiagnosticsnohit"
_LONGER_QUERIES = (500, 1000)
_SHORTER_QUERIES = (100, 50)
_QUERY_REJECTIONS = (400, 413, 414)   # the only answers that mean "too long"
_ALT_PREFIXES = {"kms_lh_api": "/kms/lh/api", "api": "/api"}
_MANUAL_CHECKS = ("ai_elements",)                 # always warns: only a second, restricted account can settle it
_ADMIN_CHECKS = ("concurrent_sessions", "logout") # always skipped: a question for the KMS admin
_TREE = "/cm/tree/branch/view"
_SEARCH = "/search/full"
_ROWS = 50
_DATE_KEYS = ("updateDate", "updatedDate", "createDate", "createdDate")
_STEP_TEXT_KEYS = ("text", "stepName", "stepRemark", "stepNodeText")
_TAG_RE = re.compile(r"<[A-Za-z/!?]")
_TLS_FAILURES = ("untrusted_certificate", "tls_failed")
_SLOWER = "slower_than_limit"         # a timeout this run cut below the connector's: KMS may only be slow
_REACH_ROWS = ("network", "tls", "expiry")   # all three judge the one call with no session
# What may appear as a string in a report: a plain identifier.
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,47}")
_HOST_RE = re.compile(r"[a-z0-9.\[\]:-]{1,253}")

_LOGIN_ENDED = "Skipped: the login did not succeed, so nothing else was sent."
_SESSION_ENDED = "Skipped: KMS refused the session or the token, so nothing else was sent."
_UNREACHABLE_ENDED = "Skipped: KMS could not be reached, so nothing else was sent."
_BUDGET_ENDED = "Skipped: the run used up its budget of calls or time."
_PROOF_SPENT = "Skipped: the connector rows used up their share of the calls, so this was not proved."
_CLIENT_ENDED = "Skipped: the run could not start."
_CUT_OFF_ENDED = "Skipped: the run was cut off; calls may have been sent."
_NO_REPLY = "Skipped: the first call got no reply to judge."
_CA_BUNDLE_ENDED = "Skipped: the CA bundle cannot be read, so nothing was sent."
_NO_REACH = "Skipped: the request with no session got no answer."
_REACH_ONLY = "The request with no session got no answer, but later calls reached KMS."
_SLOWER_SUMMARY = ("KMS did not answer within this run's shorter time limit. KMS may still work for users: "
                   "the connector waits longer.")
_FAILURE_SUMMARIES = {
    "dns": "The KMS host name did not resolve. Check the URL and this server's DNS.",
    "refused": "The KMS host refused the connection. Check the URL, the port and the firewall.",
    "timeout": "KMS did not answer in time. Check the firewall and the route from this server.",
    "untrusted_certificate": "The KMS certificate is not trusted here. Add the CA to REQUESTS_CA_BUNDLE.",
    "tls_failed": "The TLS handshake with KMS failed. Check for a device that intercepts TLS.",
    "connection_failed": "The connection to KMS failed. Note that a proxy is not supported.",
    "blocked_route": "There is no open route to KMS from this server. Check the firewall and the routing.",
    "ca_bundle_unreadable": "The file named by REQUESTS_CA_BUNDLE cannot be read. Check its path and permissions.",
}
_NO_CLIENT_SUMMARIES = {
    "egress_blocked": "The destination is blocked by this server's egress rules.",
    "run_timed_out": "The run went past its time limit, so its results are lost. KMS may be stalling; run it again.",
    "bad_settings": "The connection settings are not valid. Check the URL, the API path prefix and Allow HTTP.",
}
# OpenSSL's verify codes that need a different action from "add the CA".
_CERTIFICATE_SUMMARIES = {
    10: "The KMS certificate has expired. Ask the KMS administrator to renew it.",
    62: "The KMS certificate does not match the host name. Check the URL, and any device that intercepts TLS.",
}


def _closed(value):
    """The only way a value reaches a report: closed by default (safety rule 4).

    Booleans, numbers and ``None`` pass; a string passes only when it is a plain
    name, else it becomes ``"other"``; a dict keeps plain-name keys only.
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return value if _NAME_RE.fullmatch(value) else "other"
    if isinstance(value, dict):
        return {key: _closed(entry) for key, entry in value.items()
                if isinstance(key, str) and _NAME_RE.fullmatch(key)}
    if isinstance(value, (list, tuple)):
        return [_closed(entry) for entry in value[:_MAX_NAMES]]
    return "other"


def _plain_names(names) -> tuple[list, int]:
    """``(the plain names, sorted and capped, how many were left out)``."""
    names = {name for name in names if isinstance(name, str)}
    plain = sorted(name for name in names if _NAME_RE.fullmatch(name))
    shown = plain[:_MAX_NAMES]
    return shown, len(names) - len(shown)


def _value_counts(values) -> dict:
    """How often each value occurs; anything that is not a plain name counts as ``other``."""
    counts: Counter = Counter()
    for value in values:
        if value is None or value == "":
            continue
        counts[str(value) if _NAME_RE.fullmatch(str(value)) else "other"] += 1
    return dict(counts)


def _type_counts(values) -> dict:
    return dict(Counter(type(value).__name__ for value in values))


def _status_key(reply: "_Reply") -> str:
    if reply.failure is not None:
        return reply.failure
    return "kms_error" if reply.kms_error else f"http_{reply.status}"


def _status_counts(replies) -> dict:
    return dict(Counter(_status_key(reply) for reply in replies))


class _Stop(kc.KmsError):
    """The budget ran out. A ``KmsError``, so the connector's own error handling lets it through."""


class _ProofSpent(_Stop):
    """The connector rows used up their share of the budget; the other checks still run."""


class _Reply:
    """One answer: its status, its JSON when there is any, and a fixed word when it failed."""

    def __init__(self, status: int, payload=None, failure: str | None = None, verify_code: int | None = None):
        self.status = status
        self.payload = payload
        self.failure = failure
        self.verify_code = verify_code

    @property
    def kms_error(self) -> bool:
        """KMS's HTTP 200 {"status": "ERROR"}."""
        return (self.status == 200 and isinstance(self.payload, dict)
                and str(self.payload.get("status", "")).upper() == "ERROR")

    @property
    def ok(self) -> bool:
        """HTTP 200 with JSON that is not KMS's {"status": "ERROR"}."""
        return self.status == 200 and self.failure is None and self.payload is not None and not self.kms_error

    def observed(self) -> dict:
        return {"http_status": self.status, **({"failure": self.failure} if self.failure else {}),
                **({"verify_code": self.verify_code} if self.verify_code is not None else {})}


def _is_transport(reply: _Reply) -> bool:
    """No answer came back, also when a stall came after the status. A broken body carries no such word."""
    return reply.failure in _FAILURE_SUMMARIES


def _no_answer(reply: _Reply) -> bool:
    return _is_transport(reply) or reply.failure == _SLOWER


def _body_chars(text: str) -> int:
    """Rendered text below the header: one block of lines, a blank line, then the body. Headings are not text."""
    body = text.partition("\n\n")[2]
    return sum(len(line) for line in body.splitlines() if line.strip() and not line.startswith("#"))


def _transport_stage(failure: str) -> str:
    return "tls" if failure in _TLS_FAILURES else "network"


def _failure_summary(failure: str, verify_code: int | None) -> str:
    return _CERTIFICATE_SUMMARIES.get(verify_code) or _FAILURE_SUMMARIES[failure]


def _record_fault(records) -> str | None:
    """Why the connector would refuse these records (``_tree_children``), or None when it would not."""
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        return "not_records"
    if not all(kc._DIGITS_RE.fullmatch(str(record.get("id", ""))) for record in records):
        return "non_numeric_id"
    return None


def _unusable(records: list) -> int:
    return sum(1 for record in records if not kc._DIGITS_RE.fullmatch(str(record.get("id", ""))))


def _search_fault(records) -> str | None:
    """Why the connector would refuse a search answer, or None.

    Stricter on shape than on ids: one record that is not a dict refuses the
    whole answer (``_search_page``), but a bad id is skipped, and only an answer
    with no numeric id at all is refused (``_refuse_if_none_usable``).
    """
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        return "not_records"
    return "non_numeric_id" if records and _unusable(records) == len(records) else None


def _search_records(reply: _Reply):
    """What a usable search answer holds as its records; a stated total of zero is no hits."""
    block = reply.payload.get("regularSearch") if reply.ok and isinstance(reply.payload, dict) else None
    results = block.get("results") if isinstance(block, dict) else None
    return [] if results is None and isinstance(block, dict) and kc._zero_total(block) else results


def _tree_records(reply: _Reply):
    return reply.payload.get("data") if reply.ok and isinstance(reply.payload, dict) else None


def _search_rows(reply: _Reply) -> list | None:
    """The result records of a search answer, or None when the connector would refuse it."""
    records = _search_records(reply)
    return None if _search_fault(records) else records


def _tree_rows(reply: _Reply) -> list | None:
    records = _tree_records(reply)
    return None if _record_fault(records) else records


def _taking_turns(first: list, second: list) -> list:
    return [entry for pair in zip_longest(first, second) for entry in pair if entry is not None]


def _reason(reply: _Reply, fault: str | None) -> dict:
    """``{"reason": ...}`` when a usable answer held records the connector would refuse."""
    return {"reason": fault} if reply.ok else {}


def _step_usable(reply: _Reply, digits: str, step_id: str) -> bool:
    """The connector's rule for a step call (``_fetch_step``): the step it asked for, of the scenario it asked for."""
    data = reply.payload.get("data") if reply.ok and isinstance(reply.payload, dict) else None
    step = data.get("steps") if isinstance(data, dict) else None
    return (isinstance(step, dict) and kc._same_id(data.get("id"), digits)
            and kc._step_key(step.get("stepId")) == step_id)


class _Run:
    """One diagnostics run: the budget, the samples, and one method per check."""

    def __init__(self, client, deadline=None):
        self.client = client
        self.started = kc._clock()
        latest = self.started + _MAX_SECONDS
        self.deadline = latest if deadline is None else min(deadline, latest)
        self.calls = 0
        self.allowed = 0.0                   # the seconds the latest call was given
        self.ended: str | None = None        # why every later check is skipped
        self.reach: _Reply | None = None     # None until a call came back
        self.reached = False                 # some call got an answer from KMS
        self.ca_unreadable = False
        self.tree_records: list = []
        self.search_records: list = []
        self.search_query: str | None = None  # a query that returned rows
        self.items: list = []                # (digits, payload) of the items read
        self.ai_records: list = []           # the element records of each AI text answer
        self.ai_statuses: dict | None = None # None: no AI text was asked for
        self.ai_common = 0                   # element names the AI text shares with the items
        self.scenarios_read = 0
        self.step_fetches: list = []         # (scenario digits, step id) that need their own call
        self.sent: list = []                 # the answers of the check that is running
        self.proof_calls = 0
        self.proof_listed: list = []         # the items and scenarios the connector listed
        self.proof_found: list = []          # what the connector's search found
        self.proof_read: list = []           # the entry ids it was asked to read
        self.title_word: str | None = None   # from a listed title, for one more query; never reported

    # ------------------------------------------------------------ the budget and the one way out
    def _take_call(self, connector_timeout: bool = False):
        """Count one call, or stop; returns its deadline.

        ``_send`` gives the connect and the read timeout each up to that
        deadline, so the deadline leaves room for both. A reading call gets half
        the time left, so a stalled one cannot overrun. The first call and the
        login are the ones a slow tenant needs to answer for the run to mean
        anything, so they keep the connector's read timeout, less its connect
        timeout, when the time left allows it.
        """
        left = self.deadline - kc._clock()
        if self.calls >= _MAX_CALLS or left <= 0:
            raise _Stop()
        self.calls += 1
        if connector_timeout:
            self.allowed = max(kc._MIN_TIMEOUT_S, min(kc._DEFAULT_TIMEOUT[1], left - kc._DEFAULT_TIMEOUT[0]))
        else:
            self.allowed = min(_CALL_SECONDS, left / 2)
        return kc._clock() + self.allowed

    def _take_proof_call(self):
        if self.proof_calls >= _PROOF_CALLS:
            raise _ProofSpent()
        self.proof_calls += 1
        return self._take_call()

    def closing(self) -> tuple | None:
        """``(summary, observed, stage)`` when what this check's calls saw ends the run.

        The one way out for a dead line (the check's last call got no answer)
        and for a session or token KMS stopped accepting (a call saw it, and no
        call of the check proved the session).
        """
        replies = self.sent
        if replies and _is_transport(replies[-1]):
            last = replies[-1]
            self.ended = _UNREACHABLE_ENDED
            return _failure_summary(last.failure, last.verify_code), last.observed(), _transport_stage(last.failure)
        lost = next((reply for reply in replies if kc._session_lost(reply.status)), None)
        if lost is None or any(reply.ok for reply in replies):
            return None
        self.ended = _SESSION_ENDED
        if self.client.auth_method == "token":
            return "KMS refused the API token.", {**lost.observed(), "reason": "rejected"}, "login"
        return ("KMS accepted the login but refused its session. Ask the KMS admin about API access.",
                {**lost.observed(), "reason": "session_refused"}, "login")

    def get(self, op: str, path: str, **kwargs) -> _Reply:
        """A call the connector makes with this configuration: ``closing`` judges it."""
        reply = self._call(op, path, **kwargs)
        self.sent.append(reply)
        return reply

    def observe(self, op: str, path: str, **kwargs) -> _Reply:
        """Any other call. The budgets count it, but it can neither fail the run nor end it."""
        return self._call(op, path, **kwargs)

    def line_gone(self) -> bool:
        """The latest call of this check lost the line or the session: no point in sending another."""
        return bool(self.sent) and (_is_transport(self.sent[-1]) or kc._session_lost(self.sent[-1].status))

    def slowed(self, status: str) -> bool:
        """This row's outcome comes from a timeout the run cut short, not from an answer of KMS."""
        answered = [reply for reply in self.sent if reply.failure != _SLOWER]
        return len(answered) < len(self.sent) and (status != "fail" or all(reply.ok for reply in answered))

    def _cut_short(self, failed: kc.KmsHTTPError) -> bool:
        """The call timed out, and this run gave it less time than the connector would have."""
        timeout = kc._DEFAULT_TIMEOUT[0 if failed.connect_timeout else 1]
        return failed.transport == "timeout" and self.allowed < timeout

    def _reply_of(self, failed: kc.KmsHTTPError) -> _Reply:
        word = _SLOWER if self._cut_short(failed) else failed.transport or failed.kind
        return _Reply(failed.answered or failed.status, None, word, failed.verify_code)

    def _connector(self, call, *args, **kwargs) -> tuple:
        """``(what a public connector method gave, None)``, or ``(None, the kind it failed with)``."""
        found, failed = None, None
        try:
            with kc.proof_calls(self._take_proof_call):
                found = call(*args, **kwargs)
        except SearchStopped as stop:
            found = stop.files      # a small limit ends in "max_results"
            if stop.stop_reason == "deadline" and not found:
                self.sent.append(_Reply(0, None, _SLOWER))
                return None, "unreachable"
        except kc.KmsHTTPError as exc:
            failed = exc
        if failed is not None:
            self.sent.append(self._reply_of(failed))
            return None, failed.kind
        self.sent.append(_Reply(200, {}))
        return found, None

    def _call(self, op: str, path: str, *, params=None, headers=None, prefix=None, session=None,
              connector_timeout: bool = False) -> _Reply:
        client = self.client
        deadline = self._take_call(connector_timeout)
        failed = None
        try:
            status, body = client._send(session or client._session(), op, "GET", path, params=params, headers=headers,
                                        deadline=deadline, prefix=prefix)
        except kc.KmsHTTPError as exc:
            failed = self._reply_of(exc)
        if failed is not None:
            self.reached = self.reached or not _no_answer(failed)
            return failed
        self.reached = True
        if status != 200 or not body.strip():
            return _Reply(status, None, None if status != 200 else "empty_body")
        try:
            return _Reply(status, json.loads(body))
        except (ValueError, RecursionError):
            return _Reply(status, None, "not_json")

    def _search(self, query: str, *, layout: bool = True, start: int = 0, observation: bool = False) -> _Reply:
        params = {"query": query, "start": str(start), "rows": str(_ROWS)}
        if layout and self.client.layout_id is not None:
            params["layoutId"] = str(self.client.layout_id)
        return (self.observe if observation else self.get)("diagnostics_search", _SEARCH, params=params)

    def _queries(self) -> tuple:
        """The fixed queries, then a word from a listed title when there is one."""
        return _SAMPLE_QUERIES + ((self.title_word,) if self.title_word else ())

    # ------------------------------------------------------------ network, TLS, item 3
    def _ca_bundle_unreadable(self) -> bool:
        bundle = os.environ.get("REQUESTS_CA_BUNDLE")
        if not bundle or not self.client._base.startswith("https://"):
            return False
        try:    # the connector takes the variable as requests does: a file or a directory
            ssl.create_default_context(**{"capath" if os.path.isdir(bundle) else "cafile": bundle})
        except (OSError, ValueError):
            return True
        return False

    def check_network(self):
        if self._ca_bundle_unreadable():
            self.ca_unreadable = True
            return "skipped", "Skipped: nothing is sent until the CA bundle can be read.", {}
        # The first call carries no session and no credential (a new session has neither).
        # The connector never makes it; ``settle_reach`` weighs it against the calls after it.
        self.reach = self.observe("diagnostics_reach", _TREE, session=self.client._new_session(), connector_timeout=True)
        if self.reach.failure == _SLOWER:
            return "warn", _SLOWER_SUMMARY, {"reached": False, "failure": _SLOWER}
        failure = self.reach.failure if _is_transport(self.reach) else None
        if failure is None or failure in _TLS_FAILURES:
            return "pass", "This server reached KMS.", {"reached": True}
        return "fail", _FAILURE_SUMMARIES[failure], {"reached": False, "failure": failure}

    def check_tls(self):
        if self.ca_unreadable:
            self.ended = _CA_BUNDLE_ENDED
            return "fail", _FAILURE_SUMMARIES["ca_bundle_unreadable"], {"failure": "ca_bundle_unreadable"}
        if not self.client._base.startswith("https://"):
            return "skipped", "Skipped: the connection uses plain HTTP.", {"plain_http": True}
        if self.reach is None:
            return "skipped", _NO_REPLY, {}
        failure = self.reach.failure if _is_transport(self.reach) else None
        if failure in _TLS_FAILURES:
            return "fail", _failure_summary(failure, self.reach.verify_code), {
                "failure": failure, "verify_code": self.reach.verify_code}
        if _no_answer(self.reach):
            return "skipped", _NO_REACH, {}
        return "pass", "The KMS certificate is trusted.", {"verified": True}

    def check_expiry(self):
        reply = self.reach
        if reply is None:
            return "skipped", _NO_REPLY, {}
        if _no_answer(reply):
            return "skipped", _NO_REACH, {}
        observed = {"http_status": reply.status, "redirected": 300 <= reply.status < 400,
                    **({"failure": reply.failure} if reply.failure else {})}
        if kc._session_lost(reply.status):
            return "pass", "A request with no session is refused the way the connector expects.", observed
        return "warn", "A request with no session got an answer the connector does not treat as expiry.", observed

    # ------------------------------------------------------------ login, item 2
    def check_login(self):
        # Ended until proved otherwise: an unexpected exception here is a failed login too.
        self.ended = _LOGIN_ENDED
        if self.client.auth_method == "token":
            return self._check_token()
        client, entry, seen = self.client, self.client._entry, {}
        failure, reason = None, None
        with entry.login_lock:
            try:
                client._check_hold(entry)
            except kc.KmsHTTPError:
                reason = "held"
            if reason is None:
                deadline = self._take_call(connector_timeout=True)
                try:
                    client._login(client._new_session(), deadline, seen)
                except kc.KmsHTTPError as exc:
                    failure = exc
        observed = self._login_observed(seen)
        if reason == "held":
            return "fail", "A rejected login is recent, so no login was sent. Wait five minutes.", {
                **observed, "reason": "held"}
        if failure is None:
            self.ended, self.reached = None, True
            return "pass", "KMS accepted the login. One run cannot measure the session lifetime.", observed
        return self._login_failed(failure, observed)

    @staticmethod
    def _login_observed(seen: dict) -> dict:
        payload = seen.get("payload") or {}
        data = payload.get("data")
        keys, left_out = _plain_names(payload)
        cookies, cookies_left_out = _plain_names(seen.get("cookie_names", ()))
        return {"http_status": seen.get("status", 0), "cookie_count": len(seen.get("cookie_names", ())),
                "cookie_names": cookies, "data_token": isinstance(data, dict) and "token" in data,
                "top_level_token": "token" in payload, "response_keys": keys,
                "unlisted_names": left_out + cookies_left_out}

    def _login_failed(self, failure: kc.KmsHTTPError, observed: dict):
        if self._cut_short(failure):
            return "warn", _SLOWER_SUMMARY, {**observed, "failure": _SLOWER}
        answered = failure.transport not in _FAILURE_SUMMARIES
        self.reached = self.reached or answered
        if failure.transport is not None and answered:
            return "fail", "KMS answered the login, but the answer could not be read.", {
                **observed, "http_status": failure.answered, "reason": failure.transport}
        if failure.transport is not None:
            self.ended = _UNREACHABLE_ENDED
            summary = _failure_summary(failure.transport, failure.verify_code)
            return "fail", summary, {**observed, "failure": failure.transport, "verify_code": failure.verify_code}, (
                _transport_stage(failure.transport))
        if failure.kind == "auth":
            return "fail", ("KMS rejected the login. Check the username and password. If they are right, a device in "
                            "front of KMS may have answered, or the login has an unexpected shape."), {
                **observed, "reason": "rejected"}
        reason = "no_session" if str(failure) == kc._NO_SESSION else failure.kind
        return "fail", "The login failed for a reason other than the password.", {**observed, "reason": reason}

    def _check_token(self):
        # The connector uses /sessionInfo for a name only and ignores any answer but a 401
        # (``test_connection``), so that alone ends the run; the tree read decides the rest.
        reply = self.observe("diagnostics_session", "/sessionInfo")
        if reply.status == 401:
            self.ended = _SESSION_ENDED
            return "fail", "KMS refused the API token.", {**reply.observed(), "reason": "rejected"}
        self.ended = None
        if reply.ok:
            return "pass", "An API token never logs in; KMS answered it.", reply.observed()
        return "warn", "/sessionInfo gave no usable answer. It is optional: the next read decides.", reply.observed()

    def check_session(self):
        """One read as the session or token: a lost session here is a login failure, not a browse failure."""
        root = self.client.root_folder_id
        reply = self.get("diagnostics_session", _TREE, params=None if root is None else {"parentId": str(root)})
        if not reply.ok:
            return "warn", "The first read after the login got an unexpected answer.", reply.observed()
        return "pass", "KMS accepted the session or token on a read.", reply.observed()

    # ------------------------------------------------------------ the connector's own proof
    def check_connector_browse(self):
        before = self.calls
        found, kind = self._connector(self.client.list_files, limit=_PROOF_ENTRIES)
        observed = {"calls": self.calls - before}
        if kind:
            return "fail", "The connector could not list the folder tree.", {**observed, "kind": kind}
        self.proof_listed = [entry for entry in found if entry["kind"] != "folder"]
        words = (_TITLE_WORD_RE.search(entry["name"]) for entry in self.proof_listed + found)
        self.title_word = next((word.group() for word in words if word), None)
        capped = len(found) >= _PROOF_ENTRIES
        observed.update(entries=len(found), content_entries=len(self.proof_listed), capped=capped)
        cut = " The listing reached this run's limit, so only part of the tree was proved." if capped else ""
        if not found:
            return "warn", "The connector listed nothing: an agent would see an empty knowledge base.", observed
        if not self.proof_listed:
            return "warn", "The connector listed folders and nothing to read." + cut, observed
        return "pass", "The connector listed the folder tree." + cut, observed

    def check_connector_search(self):
        tried, refused, kind = 0, 0, None
        for query in self._queries():
            tried += 1
            found, kind = self._connector(self.client.search_files, query, limit=_PROOF_HITS)
            if found:
                self.proof_found = found
                observed = {"entries": len(found), "queries_tried": tried, "queries_refused": refused}
                if refused:
                    return "warn", "The connector found results, but KMS refused a fixed query.", observed
                return "pass", "The connector found results.", observed
            refused += 1 if kind else 0
            if self.line_gone():
                break
        observed = {"entries": 0, "queries_tried": tried, "queries_refused": refused}
        if refused == tried:
            return "fail", "The connector could not search.", {**observed, "kind": kind}
        return "warn", "The connector found nothing for the sample queries: every hit was empty or hidden.", observed

    def _proof_sample(self) -> list:
        """A search hit first, then a listed entry, whatever their kinds; then the rest up to the fixed counts."""
        room, sample = Counter(item=_PROOF_ITEMS, scenario=_PROOF_SCENARIOS), []

        def take(entry):
            sample.append(entry["id"])
            room[entry["kind"]] -= 1

        for source in (self.proof_found, self.proof_listed):
            entry = next((entry for entry in source if entry["id"] not in sample), None)
            if entry is not None:
                take(entry)
        for entry in self.proof_found + self.proof_listed:
            if entry["id"] not in sample and room[entry["kind"]] > 0:
                take(entry)
        return sample

    def check_connector_read(self):
        self.proof_read = self._proof_sample()
        kinds, usable, header_only, chars = Counter(), 0, 0, 0
        for entry_id in self.proof_read:
            text, kind = self._connector(self.client.read_file, entry_id)
            if kind:
                kinds[kind] += 1
                continue
            body = _body_chars(text)    # a length is all that stays of a read
            chars += body
            usable += 1 if body else 0
            header_only += 0 if body else 1
        failed = sum(kinds.values())
        observed = {"reads": len(self.proof_read), "usable": usable, "header_only": header_only, "body_chars": chars,
                    "failed_reads": failed, "kinds": dict(kinds)}
        if failed and failed == len(self.proof_read):
            return "fail", "The connector could not read any entry it listed.", observed
        if failed:
            return "warn", "The connector could not read one entry it listed, and read another.", observed
        if not self.proof_read:
            return "warn", "The connector listed nothing to read.", observed
        if header_only:
            return "warn", "The connector read an entry and got its header lines only.", observed
        return "pass", "The connector read the sampled entries as text.", observed

    # ------------------------------------------------------------ browse, items 4, 5, 6
    def _level(self, parent=None) -> tuple[_Reply, list]:
        params = {"parentId": str(parent)} if parent is not None else None
        reply = self.get("diagnostics_tree", _TREE, params=params)
        rows = _tree_rows(reply)
        if rows:
            self.tree_records.extend(rows)
        return reply, rows or []

    def _no_parent_level(self) -> tuple[_Reply, list]:
        """The tree call with no parent, which the connector never makes with a root set: it samples nothing."""
        reply = self.observe("diagnostics_tree", _TREE)
        return reply, _tree_rows(reply) or []

    def _enough_samples(self) -> bool:
        kinds = Counter(kc._kind_of(record) for record in self.tree_records)
        return kinds["item"] >= _MAX_ITEMS and kinds["scenario"] >= _MAX_SCENARIOS

    def _walk(self, rows: list) -> list:
        """Open a few folders from ``rows`` to find items and scenarios; returns the answers."""
        def shown(records):
            return [row for row in records if kc._kind_of(row) == "folder" and not self.client._hidden(row)]

        folders, replies = shown(rows), []
        while folders and len(replies) < _TREE_WALK_CALLS and not self._enough_samples():
            reply, found = self._level(folders.pop(0)["id"])
            replies.append(reply)
            folders += shown(found)
        return replies

    def check_top_level(self):
        root = self.client.root_folder_id
        top, top_rows = self._level() if root is None else self._no_parent_level()
        observed = {**top.observed(), "no_parent_status": _status_key(top), "records": len(top_rows),
                    "folders": sum(1 for row in top_rows if kc._kind_of(row) == "folder")}
        start, start_rows = top, top_rows
        if root is not None:
            start, start_rows = self._level(root)
            observed.update(root_status=start.status, root_records=len(start_rows))
        opened = self._walk(start_rows)
        failed = sum(1 for reply in opened if _tree_rows(reply) is None)
        observed.update(folders_opened=len(opened), folders_failed=failed, folder_statuses=_status_counts(opened))
        # With a root set the connector never makes the call with no parent, so only the root is judged.
        if _tree_rows(start) is None:
            hinted = root is None and (start.failure == "empty_body" or kc._kind_for_status(start.status) == "bad_request")
            return "fail", kc._TOP_LEVEL_HINT if hinted else "The folder tree could not be read.", {
                **observed, **_reason(start, _record_fault(_tree_records(start)))}
        if opened and failed == len(opened):
            return "fail", "No folder could be opened.", observed
        if not start_rows:
            return "warn", kc._TOP_LEVEL_HINT if root is None else "The root folder came back empty.", observed
        if failed:
            return "warn", "Some folders could not be opened.", observed
        if not top_rows:
            return "info", "The root folder answers. The tree call with no parent does not, and is not used here.", observed
        return "pass", "The tree returns its records from where the connector starts.", observed

    def check_types(self):
        tree, search = self.tree_records, self.search_records
        observed = {"tree_types": _value_counts(row.get("type") for row in tree),
                    "tree_sub_types": _value_counts(row.get("subType") for row in tree),
                    "search_types": _value_counts(row.get("type") for row in search),
                    "search_sub_types": _value_counts(row.get("subType") for row in search)}
        return "info", "Counts of the type and subType values seen in the sample.", observed

    def _ids_of(self, kind: str) -> list:
        """Sampled ids of ``kind``, in order, without offline content the connector would hide.

        What the connector rows read comes first: they count towards the same sample
        (rule 6). Then search and the tree take turns, so a small sample holds both.
        """
        seen = [entry_id.partition(":")[2] for entry_id in self.proof_read if entry_id.startswith(f"{kind}:")]
        for record in _taking_turns(self.search_records, self.tree_records):
            raw = str(record.get("id", ""))
            if (kc._kind_of(record) == kind and kc._DIGITS_RE.fullmatch(raw) and raw not in seen
                    and not self.client._hidden(record)):
                seen.append(raw)
        return seen

    def check_id_overlap(self):
        items, scenarios = set(self._ids_of("item")), set(self._ids_of("scenario"))
        if not items and not scenarios:
            return "skipped", "Skipped: the sample holds no item and no scenario.", {}
        observed = {"item_ids": len(items), "scenario_ids": len(scenarios), "shared_ids": len(items & scenarios)}
        if items & scenarios:
            return "warn", "An item and a scenario share an id in the sample.", observed
        return "info", "No shared id in this sample, which does not prove there are none.", observed

    # ------------------------------------------------------------ search, items 8, 9, 17, 18
    def check_search_layout(self):
        tried, refused, reply, rows = 0, 0, _Reply(0), None
        for query in self._queries():
            tried += 1
            reply = self._search(query)
            rows = _search_rows(reply)
            if rows:
                self.search_query = query
                self.search_records = rows
                break
            refused += 1 if rows is None else 0
            if self.line_gone():
                break
        layout_set = self.client.layout_id is not None
        observed = {"layout_set": layout_set, "queries_tried": tried, "queries_refused": refused}
        bare_unclear = False
        if layout_set and not self.line_gone():
            observed.update(with_layout_status=_status_key(reply), with_layout_rows=len(rows or ()))
            bare = self._search(self.search_query or _SAMPLE_QUERIES[0], layout=False, observation=True)
            bare_refused = bare.status in _QUERY_REJECTIONS or bare.kms_error
            bare_unclear = _search_rows(bare) is None and not bare_refused
            observed.update(without_layout_status=_status_key(bare), without_layout_rows=len(_search_rows(bare) or ()),
                            layout_statuses=_status_counts([reply, bare]),
                            layout_required=True if reply.ok and bare_refused else None)
        elif not layout_set:
            observed.update(without_layout_status=_status_key(reply), without_layout_rows=len(rows or ()))
        if refused == tried:
            return "fail", "The search call failed.", {**observed, "failure": reply.failure,
                                                       **_reason(reply, _search_fault(_search_records(reply)))}
        rows = rows or []
        unusable = _unusable(rows)
        observed["unusable_records"] = unusable
        if not rows:
            return "warn", "The search call worked but none of the sample queries found anything.", observed
        if refused:
            return "warn", "The search call returned results, but KMS refused a fixed query.", observed
        if unusable:
            return "warn", "Some search records have no numeric id. The connector skips them.", observed
        if bare_unclear:
            return "info", "The search call returned results. The same search with no layout gave no clear answer.", observed
        return "pass", "The search call returned results.", observed

    def check_query_length(self):
        limit = kc._MAX_QUERY_CHARS
        replies, found = [], {}             # outcome -> the length that last gave it

        def attempt(length: int) -> str:
            replies.append(self._search("a" * length, observation=length > limit))   # the connector cuts those
            answer = replies[-1]
            outcome = ("rejected" if answer.status in _QUERY_REJECTIONS
                       else "accepted" if _search_rows(answer) is not None else "inconclusive")
            found[outcome] = length
            return outcome

        first = attempt(limit)
        # Up from the limit while KMS accepts, down from it while KMS rejects, until the answer changes.
        for length in {"accepted": _LONGER_QUERIES, "rejected": _SHORTER_QUERIES}.get(first, ()):
            if attempt(length) != first:
                break
        longest, unclear = found.get("accepted"), found.get("inconclusive")
        observed = {"connector_limit": limit, "longest_accepted": longest, "shortest_rejected": found.get("rejected"),
                    "calls": len(replies), "inconclusive": unclear is not None, "statuses": _status_counts(replies)}
        if unclear is not None and unclear <= limit:
            return "warn", "KMS gave an answer to a long query that neither accepts nor rejects it.", observed
        if unclear is not None:
            return "info", "KMS accepts every query length the connector sends. A longer one got no clear answer.", observed
        if longest is not None and longest >= limit:
            return "pass", "KMS accepts every query length the connector sends.", observed
        return "warn", "KMS rejected a query the connector could send. Tell the BOW engineers.", observed

    def check_zero_hit(self):
        reply = self._search(_NO_HIT_QUERY)
        payload = reply.payload if isinstance(reply.payload, dict) else {}
        block = payload.get("regularSearch")
        block = block if isinstance(block, dict) else {}
        results = block.get("results")
        keys, left_out = _plain_names(payload)
        block_keys, block_left_out = _plain_names(block)
        understood = reply.ok and (isinstance(results, list) or kc._zero_total(block))
        rows = len(results) if isinstance(results, list) else 0
        observed = {**reply.observed(), "top_level_keys": keys, "search_block_keys": block_keys,
                    "unlisted_names": left_out + block_left_out, "rows": rows, "understood": bool(understood)}
        if understood and not rows:
            return "pass", "The connector can read the answer to a search with no hits.", observed
        return "warn", "The answer to a search meant to find nothing was not what the connector expects.", observed

    def check_page_rows(self):
        if self.search_query is None:
            return "skipped", "Skipped: no search returned rows.", {}
        returned = len(self.search_records)
        observed = {"rows_requested": _ROWS, "rows_returned": returned}
        full = returned >= _ROWS
        # After a full page the connector asks for the next one, unless ``max_results`` already stopped
        # it there. After a short page it stops too (``search_files``). In both those cases the call is
        # an observation: the end of the results, or a cap?
        real = full and self.client.max_results > _ROWS
        reply = self._search(self.search_query, start=_ROWS if full else returned, observation=not real)
        following = _search_rows(reply)
        if following is None:
            observed.update(next_page_status=_status_key(reply), server_cap=False if full else None)
            if real:
                return "fail", "KMS refused the next page of results, which the connector asks for.", observed
            if full:
                return "info", ("A full page came back, so no cap was seen. The next page, which this connection "
                                "never asks for, could not be read."), observed
            return "warn", "The next page of results could not be read, so a server-side cap cannot be ruled out.", observed
        observed.update(next_page_rows=len(following), server_cap=bool(following) and not full)
        if following and not full:
            return "warn", "KMS caps rows per page below what the connector asks for, so paging stops early.", observed
        return "pass", "No server-side cap on rows was seen.", observed

    # ------------------------------------------------------------ read, items 7, 11, 12, 14
    def check_field_types(self):
        ids = self._ids_of("item")[:_MAX_ITEMS]
        if not ids:
            return "skipped", "Skipped: no item in the sample.", {}
        replies = []
        for digits in ids:
            reply = self.get("diagnostics_item", f"/public/item/{digits}")
            replies.append(reply)
            if reply.ok and isinstance(reply.payload, dict) and kc._same_id(reply.payload.get("id"), digits):
                self.items.append((digits, reply.payload))
        tabs, tab_ids = [], []
        for _, item in self.items:
            raw_tabs = item.get("tabs") if isinstance(item.get("tabs"), list) else []
            tabs += [tab.get("id") for tab in raw_tabs if isinstance(tab, dict)]
            elements = item.get("elements") if isinstance(item.get("elements"), dict) else {}
            tab_ids += [element.get("tabId") for element in elements.values() if isinstance(element, dict)]
        observed = {"items_tried": len(ids), "items_read": len(self.items), "statuses": _status_counts(replies),
                    "tabs_id_types": _type_counts(tabs), "element_tab_id_types": _type_counts(tab_ids)}
        if not self.items:
            return "fail", "No sampled item could be read.", observed
        if len(self.items) < len(ids):
            return "warn", "Some sampled items could not be read. See the status codes.", observed
        if not tabs and not tab_ids:
            return "warn", "The sampled items hold no tabs[].id and no elements.*.tabId.", observed
        return "pass", "Python type names of tabs[].id and elements.*.tabId in the sampled items.", observed

    def check_dates(self):
        day_month, other, first_over, second_over = 0, 0, False, False
        records = self.tree_records + self.search_records + [item for _, item in self.items]
        for value in (record.get(key) for record in records for key in _DATE_KEYS):
            if not isinstance(value, str) or not value.strip():
                continue
            match = kc._DAY_FIRST_RE.match(value.strip())
            if match is None:
                other += 1
                continue
            day_month += 1
            first_over = first_over or int(match.group(1)) > 12
            second_over = second_over or int(match.group(2)) > 12
        observed = {"day_month_dates": day_month, "other_dates": other, "first_over_12": first_over,
                    "second_over_12": second_over}
        if second_over:
            return "warn", "A date has a second field over 12: the connector assumes day first.", observed
        if first_over:
            return "pass", "Dates are day first, as the connector assumes.", observed
        return "info", "No date in the sample tells the day from the month.", observed

    def check_ai_elements(self):
        replies, ai_names, item_names = [], set(), set()
        for digits, item in self.items:
            version, language = kc._version(item.get("version")), kc._language(item.get("language"))
            if not version or not language:
                continue
            reply = self.get("diagnostics_ai_text", "/ai_answers_item",
                             params={"itemId": digits, "version": version, "language": language})
            replies.append(reply)
            elements = reply.payload.get("elements") if reply.ok and isinstance(reply.payload, dict) else None
            if isinstance(elements, list):
                records = [element for element in elements if isinstance(element, dict)]
                self.ai_records.append(records)
                ai_names |= {name for name in (element.get("element_name") for element in records) if isinstance(name, str)}
                item_names |= set(kc._elements(item))
        if not replies:
            return "skipped", "Skipped: no sampled item has a version and a language.", {}
        ai_only, ai_left_out = _plain_names(ai_names - item_names)
        item_only, item_left_out = _plain_names(item_names - ai_names)
        observed = {"statuses": _status_counts(replies), "only_in_ai": ai_only, "only_in_item": item_only,
                    "common": len(ai_names & item_names), "unlisted_names": ai_left_out + item_left_out}
        self.ai_statuses, self.ai_common = observed["statuses"], observed["common"]
        return ("warn", "One account cannot prove permission filtering: repeat this run as a restricted user and "
                        "compare the element names.", observed)

    def check_ai_text(self):
        if self.ai_statuses is None:
            return "skipped", "Skipped: no AI text was asked for.", {}
        plain, ambiguous, blank, odd_keys = 0, 0, 0, set()
        for records in self.ai_records:
            for element in records:
                if "element_text" not in element or "element_name" not in element:
                    odd_keys |= set(element)
                text = element.get("element_text")
                text = "" if text is None else str(text)
                if not kc._tidy_lines(text):
                    blank += 1
                elif kc._AMBIGUOUS_AI_TEXT_RE.search(text):
                    ambiguous += 1
                else:
                    plain += 1
        observed = {"statuses": self.ai_statuses, "plain": plain, "ambiguous": ambiguous, "blank": blank,
                    "common": self.ai_common}
        if odd_keys:
            keys, left_out = _plain_names(odd_keys)
            observed.update(element_keys=keys, unlisted_names=left_out)
        if not self.ai_records:
            return "warn", "No AI text came back. See the status codes; the connector falls back to the item.", observed
        if ambiguous:
            return "warn", "Some AI text looks like HTML. The connector renders those elements from the item.", observed
        if not plain:
            return "warn", "The AI answers held no plain text. The connector falls back to the item.", observed
        if not self.ai_common:
            return "warn", "The AI text shares no element with the item, so the connector falls back to the item.", observed
        if len(self.ai_records) < sum(self.ai_statuses.values()):
            return "warn", "Only some sampled items gave AI text. See the status codes.", observed
        return "pass", "The sampled AI text is plain text.", observed

    # ------------------------------------------------------------ read, items 15, 16
    def check_scenario_text(self):
        ids = self._ids_of("scenario")[:_MAX_SCENARIOS]
        if not ids:
            return "skipped", "Skipped: no scenario in the sample.", {}
        replies, read, strings, tags, entities, step_keys = [], 0, 0, 0, 0, set()
        for digits in ids:
            reply = self.get("diagnostics_scenario", f"/public/scenario/{digits}", params=kc._SCENARIO_PARAMS,
                             headers={"Accept": kc._SCENARIO_ACCEPT})
            replies.append(reply)
            data = reply.payload.get("data") if reply.ok and isinstance(reply.payload, dict) else None
            steps = data.get("steps") if isinstance(data, dict) else None
            if not isinstance(steps, dict):
                continue
            read += 1
            step_keys |= set(steps)
            for text in self._scenario_strings(digits, steps):
                strings += 1
                tags += 1 if _TAG_RE.search(text) else 0
                entities += 1 if kc._ENTITY_RE.search(text) else 0
        self.scenarios_read = read
        observed = {"scenarios_tried": len(ids), "scenarios_read": read, "statuses": _status_counts(replies),
                    "strings": strings, "with_tags": tags, "with_entities": entities}
        if not read:
            return "warn", "No sampled scenario could be read.", observed
        if not strings:
            keys, left_out = _plain_names(step_keys)
            return "warn", "The sampled scenarios held no text the connector knows. See the step key names.", {
                **observed, "step_keys": keys, "unlisted_names": left_out}
        if tags or entities:
            return "warn", "Scenario text has tags or entities although plainText=true was sent.", observed
        if read < len(ids):
            return "warn", "Some sampled scenarios could not be read. See the status codes.", observed
        return "pass", "The sampled scenario text is plain text.", observed

    def _scenario_strings(self, digits: str, root: dict) -> list:
        """Every text of the inline steps; steps that need their own call go to ``step_fetches``."""
        strings, stack, visited, fetches = [], [root], 0, []
        while stack and visited < _MAX_SCENARIO_NODES:
            step = stack.pop()
            visited += 1
            strings += [step[key] for key in _STEP_TEXT_KEYS if isinstance(step.get(key), str) and step[key]]
            for is_decision, label, child_id, child in kc._step_branches(step):
                if is_decision and isinstance(label, str):
                    strings.append(label)
                if child is not None:
                    stack.append(child)
                elif child_id is not None and child_id not in fetches:
                    fetches.append(child_id)
        self.step_fetches += [(digits, step_id) for step_id in fetches[:_STEP_FETCHES]]
        return strings

    def check_scenario_steps(self):
        if not self._ids_of("scenario"):
            return "skipped", "Skipped: no scenario in the sample.", {}
        if not self.scenarios_read:
            return "skipped", "Skipped: no sampled scenario could be read.", {}
        if not self.step_fetches:
            return "info", "No sampled step needed its own call.", {"statuses": {}}
        replies, unusable = [], 0
        for digits, step_id in self.step_fetches:
            replies.append(self.get("diagnostics_step", f"/public/scenario/{digits}/step/{step_id}",
                                    params=kc._SCENARIO_PARAMS, headers={"Accept": kc._SCENARIO_ACCEPT}))
            unusable += 0 if _step_usable(replies[-1], digits, step_id) else 1
        observed = {"statuses": _status_counts(replies), "unusable": unusable}
        if not unusable:
            return "pass", "Every sampled step call gave the step the connector asked for.", observed
        return "warn", "A step call gave nothing the connector can use. Such a step shows as not available.", observed

    # ------------------------------------------------------------ item 1
    def check_prefix(self):
        endpoint, params, path = "tree", None, _TREE
        if self.items:
            digits, item = self.items[0]
            version, language = kc._version(item.get("version")), kc._language(item.get("language"))
            if version and language:
                endpoint, path = "ai_answers_item", "/ai_answers_item"
                params = {"itemId": digits, "version": version, "language": language}
        alternatives = {}
        for label, prefix in _ALT_PREFIXES.items():
            if prefix == self.client.api_prefix:
                continue
            reply = self.observe("diagnostics_prefix", path, params=params, prefix=prefix)
            alternatives[label] = {**reply.observed(), "served": reply.ok}
        return "info", "One authenticated GET per other known prefix; no login was sent there.", {
            "endpoint": endpoint, "alternatives": alternatives}


def _check_concurrent_sessions(_run):
    return ("skipped", "Skipped: ask the KMS admin for the concurrent-session limit and the failed-login lockout "
                       "threshold. Failed logins are never sent on purpose.", {})


def _check_logout(_run):
    return "skipped", "Skipped: ask the KMS admin whether KMS has a logout endpoint that ends a login session.", {}


# (id, checklist items, stage, method, runs even when the run has ended)
_CHECKS = (
    ("network", [], "network", _Run.check_network, False),
    ("tls", [], "tls", _Run.check_tls, False),
    ("expiry", [3], "login", _Run.check_expiry, False),
    ("login", [2], "login", _Run.check_login, False),
    ("session", [], "login", _Run.check_session, False),
    ("connector_browse", [], "browse", _Run.check_connector_browse, False),
    ("connector_search", [], "search", _Run.check_connector_search, False),
    ("connector_read", [], "read", _Run.check_connector_read, False),
    ("top_level", [4], "browse", _Run.check_top_level, False),
    ("search_layout", [8], "search", _Run.check_search_layout, False),
    ("types", [5], "browse", _Run.check_types, False),
    ("id_overlap", [6], "browse", _Run.check_id_overlap, False),
    ("query_length", [9], "search", _Run.check_query_length, False),
    ("zero_hit", [17], "search", _Run.check_zero_hit, False),
    ("page_rows", [18], "search", _Run.check_page_rows, False),
    ("field_types", [11], "read", _Run.check_field_types, False),
    ("dates", [7], "browse", _Run.check_dates, False),
    ("ai_elements", [12], "read", _Run.check_ai_elements, False),
    ("ai_text", [14], "read", _Run.check_ai_text, False),
    ("scenario_text", [15], "read", _Run.check_scenario_text, False),
    ("scenario_steps", [16], "read", _Run.check_scenario_steps, False),
    ("prefix", [1], "browse", _Run.check_prefix, False),
    ("concurrent_sessions", [10], "login", _check_concurrent_sessions, True),
    ("logout", [13], "login", _check_logout, True),
)


def _row(check_id: str, checklist: list, stage: str, status: str, summary: str, observed: dict) -> dict:
    return {"id": check_id, "checklist": list(checklist), "stage": stage, "status": status, "summary": summary,
            "observed": _closed(observed)}


def _run_check(run: _Run, check_id: str, checklist: list, stage: str, method) -> dict:
    """One row. The method may name another stage as a fourth value (a login that never reached KMS)."""
    run.sent = []
    try:
        status, summary, observed, *moved = method(run)
    except _ProofSpent:
        return _row(check_id, checklist, stage, "skipped", _PROOF_SPENT, {})
    except _Stop:
        run.ended = _BUDGET_ENDED
        return _row(check_id, checklist, stage, "skipped", _BUDGET_ENDED, {})
    except Exception as exc:
        # The class name only: the exception text may hold a URL or a secret.
        logger.warning("kms_lighthouse diagnostics check %s failed unexpectedly: %s", check_id, type(exc).__name__)
        return _row(check_id, checklist, stage, "fail", "This check stopped unexpectedly.",
                    {"error_type": type(exc).__name__})
    closing = run.closing()
    if closing is not None:
        summary, seen, stage_seen = closing
        return _row(check_id, checklist, stage_seen, "fail", summary, {**observed, **seen})
    if run.slowed(status):
        return _row(check_id, checklist, stage, "warn", _SLOWER_SUMMARY, {**observed, "failure": _SLOWER})
    return _row(check_id, checklist, moved[0] if moved else stage, status, summary, observed)


def _settle_reach(run: _Run, rows: list) -> None:
    """A later answer outweighs the call with no session, which the connector never makes."""
    if run.reached and run.reach is not None and _no_answer(run.reach):
        for row in rows:
            if row["id"] in _REACH_ROWS:
                row["observed"]["observation_failed"] = True
                if row["status"] == "fail":
                    row.update(status="warn", summary=_REACH_ONLY)


def _lowers_overall(row: dict) -> bool:
    """A warning or a skip, unless it says nothing about this run: only a person can answer those."""
    if row["observed"].get("observation_failed"):
        return False
    if row["status"] == "warn":
        return row["id"] not in _MANUAL_CHECKS
    if row["status"] == "skipped":
        return row["id"] not in _ADMIN_CHECKS and not row["observed"].get("plain_http")
    return False


def _overall(rows: list, ended: str | None) -> tuple[str, str | None]:
    failed = next((row["stage"] for row in rows if row["status"] == "fail"), None)
    if failed is not None:
        return "fail", failed
    proved = sum(1 for row in rows if row["id"] in _PROOF_CHECKS and row["status"] == "pass") == len(_PROOF_CHECKS)
    if ended is not None or not proved or any(_lowers_overall(row) for row in rows):
        return "warn", None
    return "pass", None


def _report(host: str, api_prefix: str, auth_method: str, root_folder_set: bool, rows: list, calls: int | None,
            duration_ms: int, ended: str | None) -> dict:
    overall, failed_stage = _overall(rows, ended)
    return {
        "report_version": _REPORT_VERSION, "generated_at": datetime.now(timezone.utc).isoformat(),
        "bow_version": settings.PROJECT_VERSION, "host": host, "api_prefix": api_prefix, "auth_method": auth_method,
        "root_folder_set": root_folder_set, "overall": overall, "failed_stage": failed_stage,
        "manual_checks": [row["id"] for row in rows if row["id"] in _MANUAL_CHECKS and row["status"] == "warn"],
        "calls_made": calls, "calls_unknown": calls is None, "duration_ms": duration_ms, "checks": rows,
    }


def run_diagnostics(client, deadline=None) -> dict:
    run = _Run(client, deadline)
    rows = []
    for check_id, checklist, stage, method, always in _CHECKS:
        if run.ended is not None and not always:
            rows.append(_row(check_id, checklist, stage, "skipped", run.ended, {}))
        else:
            rows.append(_run_check(run, check_id, checklist, stage, method))
    _settle_reach(run, rows)
    duration_ms = max(0, int((kc._clock() - run.started) * 1000))
    return _report(_host_of(client._base), client.api_prefix, client.auth_method, client.root_folder_id is not None,
                   rows, run.calls, duration_ms, run.ended)


def _host_of(base_url) -> str:
    """The host of a config that may not have built a client; never a user name or password."""
    # A password holding "/" or "?" ends the netloc early, so only the raw string shows the "@".
    if not isinstance(base_url, str) or "@" in base_url:
        return ""
    bad, host, port = False, None, None
    try:
        parts = urlsplit(base_url.strip())
        host, port = parts.hostname, parts.port     # ``port`` raises when it is not a number
    except ValueError:
        bad = True
    if bad or not host:
        return ""
    host = f"[{host}]" if ":" in host else host
    shown = host.lower() if port is None else f"{host.lower()}:{port}"
    return shown if _HOST_RE.fullmatch(shown) else ""


def _config_dict(config) -> dict:
    return (json.loads(config) if isinstance(config, str) else config) or {}


def build_failure_report(config, exc: Exception) -> dict:
    """The report for a client that could not be built: the type name of ``exc`` only, never its text."""
    if isinstance(exc, UnsafeURLError):
        return failure_report(_config_dict(config), stage="network", reason="egress_blocked",
                              error_type=type(exc).__name__)
    if isinstance(exc, kc.KmsError) and str(exc) not in (kc._NO_CREDENTIAL, kc.KMS_TOKEN_FORMAT_MESSAGE):
        return failure_report(_config_dict(config), stage="network", reason="bad_settings",
                              error_type=type(exc).__name__)
    return failure_report(_config_dict(config), stage="login", reason="credentials_unavailable",
                          error_type=type(exc).__name__)


def run_deadline() -> float:
    """A run's deadline, read when the request arrives: a thread that starts late cannot outlive the route."""
    return kc._clock() + _MAX_SECONDS


def route_seconds() -> float:
    """How long the route waits: the run's own budget, plus time for a last call to return."""
    return _MAX_SECONDS + _ROUTE_GRACE_SECONDS


def timed_out_report(config) -> dict:
    """The route gave up on a run. A thread cannot be cancelled: it stops at its next ``_take_call``."""
    return failure_report(_config_dict(config), stage="network", reason="run_timed_out", error_type="TimeoutError",
                          ended=_CUT_OFF_ENDED, calls=None, duration_ms=int(route_seconds() * 1000))


def failure_report(config: dict, *, stage: str, reason: str, error_type: str, ended: str = _CLIENT_ENDED,
                   calls: int | None = 0, duration_ms: int = 0) -> dict:
    """The same report when no client could be built, so the installer always gets something to send."""
    prefix = config.get("api_prefix")
    prefix = prefix if isinstance(prefix, str) and kc._PREFIX_RE.fullmatch(prefix) else kc._DEFAULT_PREFIX
    auth_method = "token" if config.get("auth_type") == "token" else "userpass"
    summary = _NO_CLIENT_SUMMARIES.get(reason, "The connection's credentials could not be used.")
    target = "network" if stage == "network" else "login"
    rows = []
    for check_id, checklist, check_stage, method, always in _CHECKS:
        if check_id == target:
            rows.append(_row(check_id, checklist, stage, "fail", summary, {"reason": reason, "error_type": error_type}))
        elif always:
            rows.append(_row(check_id, checklist, check_stage, *method(None)))
        else:
            rows.append(_row(check_id, checklist, check_stage, "skipped", ended, {}))
    return _report(_host_of(config.get("base_url")), prefix, auth_method,
                   config.get("root_folder_id") not in (None, ""), rows, calls, duration_ms, ended)
