"""Read-only Fabric OS RCA connector. Vendor requests are explicitly allowlisted.

The bundled 9.1.0b YANG baseline is reconciled with live module discovery;
customer FOS 9.1.1c compatibility still requires a real-switch acceptance pass.
"""
import base64
import json
import math
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pandas as pd
import requests
from defusedxml import ElementTree
from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import ProgressReporter
from app.data_sources.clients.brocade_catalog import (
    PRIMARY, EVIDENCE, alias, definitions, resource_catalog,
)


class BrocadeError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'{code}: {message}')


def _fail(code, message):
    raise BrocadeError(code, message)


def _list(value):
    return [] if value is None else value if isinstance(value, list) else [value]


def _at(row, path):
    if not path:
        return row
    head, _, tail = path.partition('.')
    if isinstance(row, list):
        return [_at(v, path) for v in row]
    if not isinstance(row, dict):
        return None
    return _at(row.get(head), tail) if tail else row.get(head)


def _xml_value(element):
    if not len(element):
        return element.text or None
    result = {}
    for child in element:
        name = child.tag.rsplit('}', 1)[-1]
        value = _xml_value(child)
        if name in result:
            if not isinstance(result[name], list):
                result[name] = [result[name]]
            result[name].append(value)
        else:
            result[name] = value
    return result


def _timestamp(value):
    if isinstance(value, bool):
        _fail('InvalidTime', 'Expected an offset-aware timestamp.')
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        _fail('InvalidTime', 'Expected an ISO timestamp with UTC offset.')
    if dt.tzinfo is None:
        _fail('InvalidTime', 'Timestamp must include a UTC offset.')
    return dt.astimezone(timezone.utc)


def _coerce(value, spec):
    if value is None:
        return None
    if spec.get('dtype') == 'array':
        item = dict(spec, dtype=spec.get('item_type', 'string'))
        return [_coerce(v, spec if isinstance(v, list) else item) for v in _list(value)]
    dtype = spec.get('dtype')
    try:
        if dtype == 'integer':
            if isinstance(value, bool) or not re.fullmatch(r'-?\d+', str(value)):
                raise ValueError()
            return int(value)
        if dtype == 'number':
            number = float(value)
            if not math.isfinite(number):
                raise ValueError()
            return number
        if dtype == 'boolean':
            if value is True or value == 'true':
                return True
            if value is False or value == 'false':
                return False
            raise ValueError()
        if isinstance(value, (dict, list)):
            _fail('InvalidResponse', 'Expected a scalar field.')
        return str(value)
    except (ValueError, TypeError, OverflowError):
        _fail('InvalidResponse', 'A source field does not match its declared type.')


class BrocadeClient(DataSourceClient):
    relative_date_hint = 'Use lookback="30m" or "24h" on events/congestion_samples; port_statistics has no arbitrary history.'

    def __init__(self, url, username, password, vf_ids='', include_advanced=False,
                 verify_ssl=True, ca_bundle=None, allow_http=False, login_scheme='Custom_Basic',
                 timeout=30, max_rows=10000, max_response_bytes=8388608):
        parsed = urlsplit(str(url))
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            _fail('InvalidConfig', 'Supply a switch origin URL without credentials or path.')
        if parsed.scheme == 'http' and (not allow_http or parsed.hostname not in ('localhost', '127.0.0.1', '::1')):
            _fail('InvalidConfig', 'HTTP is restricted to explicitly enabled loopback simulators.')
        if login_scheme not in ('Basic', 'Custom_Basic'):
            _fail('InvalidConfig', 'Unsupported login scheme.')
        for value, low, high in ((timeout, 1, 60), (max_rows, 1, 100000), (max_response_bytes, 1024, 67108864)):
            if type(value) is not int or not low <= value <= high:
                _fail('InvalidConfig', 'Invalid request budget.')
        if not username or not password:
            _fail('InvalidConfig', 'Username and password are required.')
        try:
            self.vf_ids = {int(v.strip()) for v in str(vf_ids).split(',') if v.strip()}
        except ValueError:
            _fail('InvalidConfig', 'Fabric IDs must be comma-separated integers.')
        if any(v < 1 or v > 128 for v in self.vf_ids):
            _fail('InvalidConfig', 'Fabric IDs must be between 1 and 128.')
        self.url = str(url).rstrip('/')
        self.username, self.password = username, password
        self.include_advanced = include_advanced
        self.verify = ca_bundle or verify_ssl
        self.login_scheme, self.timeout = login_scheme, timeout
        self.max_rows, self.max_response_bytes = max_rows, max_response_bytes
        self._definitions = definitions()
        self._lock = threading.Lock()

    @property
    def description(self):
        return ('Brocade Fabric OS RCA. execute_query accepts a JSON string/dict (also sql= for tool compatibility), '
                'not SQL. Example: {"table":"port_statistics","scope":{"vf_id":10},"filter":{"port":"0/12"}}. '
                'Omit fields for every reviewed diagnostic column; filters and limits still apply. '
                'Use get_tables(include_advanced=True) or get_schema(name) for advanced diagnostics. '
                'Queries require explicit vf_id on logical-switch tables, never default to 128. '
                'Events/congestion contain retained observations only, not guaranteed time coverage. '
                'Join WWPNs across connections in Python; never interpret cumulative errors as recent rates. '
                'bb_credit_zero counts transitions, not stall duration; total_up_time is percent. '
                'Defined zoning does not prove effective access. Current observations do not prove past topology. '
                'Read-only; no raw URLs, methods, SSH or switch changes. FOS baseline 9.1.0b; target 9.1.1c is not live-verified.')

    def _request(self, session, method, path, deadline, vf_id=None):
        allowed = {r['uri'] for r in resource_catalog()['resources'].values()}
        if (method == 'GET' and path not in allowed) or (method == 'POST' and path not in ('/rest/login', '/rest/logout')) or method not in ('GET', 'POST'):
            _fail('ForbiddenOperation', 'The request is outside the diagnostic allowlist.')
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail('QueryDeadlineExceeded', 'Narrow the query and retry.')
            try:
                with session.request(method, self.url + path,
                        params={'vf-id': vf_id} if vf_id is not None else None,
                        timeout=min(self.timeout, remaining), verify=self.verify,
                        allow_redirects=False, stream=True) as response:
                    body = bytearray()
                    for chunk in response.iter_content(65536):
                        if time.monotonic() >= deadline:
                            _fail('QueryDeadlineExceeded', 'Narrow the query and retry.')
                        body.extend(chunk)
                        if len(body) > self.max_response_bytes:
                            _fail('ResponseLimitExceeded', 'Response exceeds the configured byte budget.')
                    status, headers = response.status_code, response.headers
            except requests.RequestException:
                _fail('TransportError', 'Could not complete the HTTPS request; check reachability and certificate trust.')
            if status == 401:
                _fail('AuthenticationFailed', 'Credentials or session are not accepted.')
            if status == 403:
                _fail('PermissionDenied', 'The account cannot read the requested resource or fabric.')
            if 300 <= status < 400:
                _fail('RemoteError', 'Switch redirects are not followed; use the management origin.')
            if status in (502, 503, 504) and method == 'GET' and attempt < 2:
                time.sleep(min(0.2 * (2 ** attempt), max(0, deadline - time.monotonic())))
                continue
            data = {}
            if body:
                try:
                    root = ElementTree.fromstring(body)
                    data = _xml_value(root)
                    if not isinstance(data, dict):
                        data = {}
                except Exception:
                    if status in (400, 404, 405):
                        _fail('UnsupportedResource', 'The resource or request is not supported.')
                    if status >= 400:
                        _fail('RemoteError', f'The switch returned HTTP {status}.')
                    _fail('InvalidResponse', 'Expected a valid XML response.')
            errors = _at(data, 'errors.error') or data.get('error')
            if 200 <= status < 300 and not errors:
                return data, headers
            messages = {str(e.get('error-message', '')) for e in _list(errors) if isinstance(e, dict)}
            # Only endpoint-specific, known no-record conditions are empty results.
            empty = {
                'fibrechannel-name-server': {'No entries in Name Server'},
                'trunk': {'No trunking Links'}, 'trunk-area': {'No ports have Trunk Area enabled'},
                'dashboard-rule': {'No Rule violations found'},
                'syslog-server': {'No syslog servers are configured'},
            }
            if status == 404 and messages and messages <= empty.get(path.rsplit('/', 1)[-1], set()):
                return {path.rsplit('/', 1)[-1]: []}, headers
            if status in (400, 404, 405):
                _fail('UnsupportedResource', 'The resource, mode or request is not supported; it is not an empty result.')
            _fail('RemoteError', f'The switch returned HTTP {status}; no complete result is available.')

    @contextmanager
    def _session(self):
        with self._lock:
            deadline = time.monotonic() + 120
            with requests.Session() as session:
                session.trust_env = False
                token = base64.b64encode(f'{self.username}:{self.password}'.encode()).decode()
                session.headers.update({'Authorization': self.login_scheme + ' ' + token,
                    'Accept': 'application/yang-data+xml', 'Content-Type': 'application/yang-data+xml',
                    'User-Agent': 'BagOfWords-Brocade-RCA'})
                _, headers = self._request(session, 'POST', '/rest/login', deadline)
                auth = headers.get('Authorization')
                if not auth:
                    _fail('InvalidResponse', 'The switch did not return a session authorization header.')
                session.headers['Authorization'] = auth
                try:
                    yield session, deadline
                finally:
                    # Session release is the only other allowed POST; never hides a query failure.
                    try:
                        self._request(session, 'POST', '/rest/logout', time.monotonic() + 5)
                    except BrocadeError:
                        pass

    def _read(self, session, resource, deadline, vf_id=None):
        r = resource_catalog()['resources'][resource]
        data, _ = self._request(session, 'GET', r['uri'], deadline, vf_id)
        name = r['object']
        if name not in data:
            # Some servers retain the module container inside Response.
            nested = data.get(r['module'])
            if isinstance(nested, dict):
                data = nested
        if name not in data:
            _fail('InvalidResponse', 'The response is missing the expected resource container.')
        rows = _list(data[name])
        if any(not isinstance(row, dict) for row in rows):
            _fail('InvalidResponse', 'Expected resource records, not scalar values.')
        if any(any(row.get(key) is None for key in r['keys']) for row in rows):
            _fail('InvalidResponse', 'A resource record is missing its declared identity key.')
        if len(rows) > self.max_rows:
            _fail('ResultLimitExceeded', 'The source collection exceeds the configured row budget.')
        return rows

    def _discover(self, session, deadline, vf_id=None):
        if vf_id is None and self.vf_ids:
            vf_id = min(self.vf_ids)
        modules = self._read(session, 'brocade-module-version/module', deadline)
        advertised = set()
        for module in modules:
            name = module.get('name')
            for obj in _list(_at(module, 'objects.object')):
                if isinstance(name, str) and isinstance(obj, str):
                    advertised.add(name + '/' + obj)
        if not advertised:
            _fail('InvalidResponse', 'Module discovery did not report supported objects.')
        switches = self._read(session, 'brocade-fibrechannel-switch/fibrechannel-switch', deadline, vf_id)
        if len(switches) != 1 or not switches[0].get('name') or not switches[0].get('firmware-version'):
            _fail('InvalidResponse', 'Cannot establish the source switch identity and firmware.')
        return advertised, switches[0]

    def get_schemas(self, progress_callback=None):
        reporter = ProgressReporter(progress_callback)
        reporter.phase("Discovering Brocade diagnostics")
        tables = self.get_tables(include_advanced=self.include_advanced)
        reporter.set_total(len(tables))
        for table in tables:
            reporter.item(table.name)
        reporter.done()
        return tables

    def get_tables(self, include_advanced=False):
        with self._session() as (session, deadline):
            advertised, switch = self._discover(session, deadline)
        tables = []
        for name, d in self._definitions.items():
            if not d['primary'] and not include_advanced:
                continue
            supported = all(r in advertised for r, _, _ in d['parts'])
            columns = [TableColumn(name=n, dtype=f['dtype'],
                description=('Units: ' + f['units']) if f.get('units') else None,
                metadata={k: v for k, v in f.items() if k in ('path', 'units', 'enum')}) for n, f in d['columns'].items()]
            tables.append(Table(name=name, columns=columns, pks=[], fks=[],
                description=d['description'], metadata_json={
                    'schema': 'primary' if d['primary'] else 'advanced',
                    'availability': 'advertised' if supported else 'unsupported',
                    'scope': d['scope'], 'allowed_vf_ids': sorted(self.vf_ids),
                    'firmware': switch['firmware-version'], 'baseline': '9.1.0b',
                    'live_verified': False, 'default_fields': 'all_reviewed',
                    'identity_fields': d['identity_fields'], 'time_field': d['time_field'], 'source_resources': [r for r, _, _ in d['parts']],
                    'description': d['description']}))
        return tables

    def get_schema(self, table_name):
        if table_name not in self._definitions:
            _fail('InvalidQuery', 'Unknown table.')
        return next(t for t in self.get_tables(include_advanced=True) if t.name == table_name)

    def prompt_schema(self):
        return TableFormatter(self.get_schemas()).table_str

    def test_connection(self):
        try:
            with self._session() as (session, deadline):
                _, switch = self._discover(session, deadline)
            return {'success': True, 'message': 'Connected to Brocade FOS ' + str(switch['firmware-version'])}
        except BrocadeError as exc:
            return {'success': False, 'message': str(exc)}

    def _validate(self, query):
        if not isinstance(query, dict) or set(query) - {'table', 'scope', 'keys', 'fields', 'filter', 'lookback', 'start_time', 'end_time', 'order_by', 'limit'}:
            _fail('InvalidQuery', 'Supply a query object with supported properties.')
        name = query.get('table')
        if not isinstance(name, str) or name not in self._definitions:
            _fail('InvalidQuery', 'Unknown table; inspect get_tables or get_schema.')
        d = self._definitions[name]
        scope = query.get('scope', {})
        if not isinstance(scope, dict) or set(scope) - {'vf_id'}:
            _fail('InvalidQuery', 'Scope supports only vf_id.')
        vf = scope.get('vf_id')
        if d['scope'] == 'logical_switch':
            if type(vf) is not int or not 1 <= vf <= 128:
                _fail('InvalidQuery', 'An explicit vf_id between 1 and 128 is required.')
            if self.vf_ids and vf not in self.vf_ids:
                _fail('PermissionDenied', 'Fabric ID is outside the configured connection scope.')
        elif scope:
            _fail('InvalidQuery', 'This resource uses chassis scope; omit scope.')
        fields = query.get('fields')
        if fields is not None and (not isinstance(fields, list) or not fields or any(not isinstance(f, str) or f not in d['columns'] for f in fields) or len(set(fields)) != len(fields)):
            _fail('InvalidQuery', 'fields must be a nonempty list of declared columns.')
        filters = {}
        for prop in ('filter', 'keys'):
            value = query.get(prop, {})
            if not isinstance(value, dict):
                _fail('InvalidQuery', prop + ' must be an object.')
            if filters.keys() & value.keys():
                _fail('InvalidQuery', 'Do not specify the same field in keys and filter.')
            if prop == 'keys' and any(k not in d['identity_fields'] or isinstance(v, (dict, list)) or v is None for k, v in value.items()):
                _fail('InvalidQuery', 'keys accepts scalar equality on declared identity fields only.')
            filters.update(value)
        for field, condition in filters.items():
            if field not in d['columns'] or field.startswith('_bow_'):
                _fail('InvalidQuery', 'Unknown or non-filterable field.')
            operators = condition if isinstance(condition, dict) else {'eq': condition}
            if not operators or set(operators) - {'eq', 'ne', 'in', 'gt', 'gte', 'lt', 'lte'}:
                _fail('InvalidQuery', 'Unsupported filter operator.')
            for op, value in operators.items():
                values = value if op == 'in' else [value]
                if not isinstance(values, list) or not values or len(values) > 1000:
                    _fail('InvalidQuery', 'in expects a bounded nonempty list.')
                for v in values:
                    dtype = d['columns'][field]['dtype']
                    if v is None and op in ('eq', 'ne', 'in'):
                        continue
                    valid = ((dtype == 'string' and isinstance(v, str)) or
                             (dtype == 'boolean' and type(v) is bool) or
                             (dtype == 'integer' and type(v) is int) or
                             (dtype == 'number' and type(v) in (int, float) and math.isfinite(v)))
                    if not valid or (op in ('gt', 'gte', 'lt', 'lte') and dtype not in ('integer', 'number')):
                        _fail('InvalidQuery', 'Filter value/operator does not match the column type.')
        limit = query.get('limit', self.max_rows)
        if type(limit) is not int or not 1 <= limit <= self.max_rows:
            _fail('InvalidQuery', 'limit must be a positive integer within the connection budget.')
        order = query.get('order_by', [])
        if not isinstance(order, list):
            _fail('InvalidQuery', 'order_by must be a list.')
        for item in order:
            if not isinstance(item, dict) or set(item) != {'field', 'direction'} or not isinstance(item['field'], str) or item['field'] not in d['columns'] or item['direction'] not in ('asc', 'desc') or d['columns'][item['field']]['dtype'] == 'array':
                _fail('InvalidQuery', 'Invalid ordering specification.')
        bounds = None
        if any(k in query for k in ('lookback', 'start_time', 'end_time')):
            if not d['time_field']:
                _fail('HistoryUnavailable', 'This table has no supported timestamp window.')
            if 'lookback' in query:
                if 'start_time' in query or 'end_time' in query:
                    _fail('InvalidQuery', 'Choose relative or absolute time bounds.')
                match = re.fullmatch(r'([1-9]\d{0,4})([smhd])', str(query['lookback']))
                if not match:
                    _fail('InvalidQuery', 'lookback must be a positive duration such as 30m or 24h.')
                end = datetime.now(timezone.utc)
                bounds = (end - timedelta(seconds=int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]), end)
            else:
                if not {'start_time', 'end_time'} <= query.keys():
                    _fail('InvalidQuery', 'Supply both start_time and end_time.')
                bounds = (_timestamp(query['start_time']), _timestamp(query['end_time']))
                if bounds[0] >= bounds[1]:
                    _fail('InvalidQuery', 'start_time must precede end_time.')
        return name, d, vf, fields, filters, limit, order, bounds

    def _normalize(self, name, definition, resource, selector, tags, rows):
        spec = resource_catalog()['resources'][resource]
        for parent in rows:
            for raw in _list(_at(parent, selector)):
                if not isinstance(raw, dict):
                    _fail('InvalidResponse', 'Expected a nested record.')
                if name == 'fabric_links' and raw.get('port-type-string') not in ('e-port', 'ex-port') and not raw.get('neighbor-switch-user-friendly-name'):
                    continue
                if name == 'health':
                    for f in spec['fields']:
                        value = _at(raw, f['path'])
                        if value is not None:
                            yield {'component': f['path'].removesuffix('-health').replace('-', '_'), 'status': str(value)}
                    continue
                row = dict(tags)
                for f in spec['fields']:
                    path = f['path']
                    if selector:
                        if not path.startswith(selector + '.'):
                            continue
                        path = path[len(selector) + 1:]
                    col = alias(name, path) if definition['primary'] else path
                    if col not in definition['columns']:
                        continue
                    fs = dict(f, dtype=f['item_type']) if selector else f
                    row[col] = _coerce(_at(raw, path), fs)
                if name == 'zone_members':
                    for key, role in [('entry-name', 'member'), ('principal-entry-name', 'principal')]:
                        for member in _list(_at(raw, 'member-entry.' + key)):
                            if not isinstance(member, str):
                                _fail('InvalidResponse', 'Zone member must be a string.')
                            kind = 'wwpn' if re.fullmatch(r'(?:[0-9a-fA-F]{2}:){7}[0-9a-fA-F]{2}', member) else 'domain_index' if re.fullmatch(r'\d+,\d+', member) else 'alias'
                            yield dict(row, member=member.lower() if kind == 'wwpn' else member, member_role=role, member_kind=kind)
                else:
                    yield row

    def execute_query(self, query=None, sql=None, **kwargs):
        if query is not None and sql is not None:
            _fail('InvalidQuery', 'Supply query or sql, not both.')
        value = query if query is not None else sql
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                _fail('InvalidQuery', 'Expected JSON, not SQL.')
        if value is None:
            value = kwargs
        elif kwargs:
            _fail('InvalidQuery', 'Do not combine query JSON and keyword properties.')
        name, d, vf, fields, filters, limit, order, bounds = self._validate(value)
        query_id = str(uuid.uuid4())
        result = []
        with self._session() as (session, deadline):
            advertised, switch = self._discover(session, deadline, vf)
            for resource, selector, tags in d['parts']:
                # A discriminator equality can select one constituent without hiding failures in another selected constituent.
                if any(k in filters and not self._matches(v, filters[k]) for k, v in tags.items()):
                    continue
                if resource not in advertised:
                    _fail('UnsupportedResource', 'A requested view constituent is not advertised by the switch.')
                rows = self._read(session, resource, deadline, vf if d['scope'] == 'logical_switch' else None)
                for row in self._normalize(name, d, resource, selector, tags, rows):
                    if len(result) >= self.max_rows:
                        _fail('ResultLimitExceeded', 'The normalized result exceeds the row budget.')
                    # Normalize endpoint WWPN values without changing non-WWPN strings.
                    for col, val in row.items():
                        if isinstance(val, str) and re.fullmatch(r'(?:[0-9a-fA-F]{2}:){7}[0-9a-fA-F]{2}', val):
                            row[col] = val.lower()
                    row.update({'_bow_query_id': query_id, '_bow_source_url': self.url,
                        '_bow_switch_wwn': switch['name'], '_bow_vf_id': vf,
                        '_bow_retrieved_at': datetime.now(timezone.utc).isoformat(),
                        '_bow_source_resource': resource, '_bow_firmware': switch['firmware-version'],
                        '_bow_history_coverage': 'retention_unknown' if d['time_field'] else 'current_observation',
                        '_bow_complete': True})
                    result.append(row)
        filtered = []
        for row in result:
            if not all(self._matches(row.get(k), v) for k, v in filters.items()):
                continue
            if bounds:
                raw_time = row.get(d['time_field'])
                if raw_time is None:
                    _fail('InvalidResponse', 'Cannot apply a time window to a record without a timestamp.')
                if not bounds[0] <= _timestamp(raw_time) < bounds[1]:
                    continue
            filtered.append(row)
        if len(filtered) > limit:
            _fail('ResultLimitExceeded', 'Result exceeds limit; narrow the query. No rows were silently truncated.')
        for item in reversed(order):
            field = item['field']
            present = [r for r in filtered if r.get(field) is not None]
            absent = [r for r in filtered if r.get(field) is None]
            filtered = sorted(present, key=lambda r: _timestamp(r[field]) if field == d['time_field'] else r[field], reverse=item['direction'] == 'desc') + absent
        columns = list(d['columns']) if fields is None else list(dict.fromkeys(fields + d['identity_fields'] + d['timestamp_fields'] + list(EVIDENCE)))
        # Object dtype prevents nullable uint64 observations being converted to lossy floats.
        frame = pd.DataFrame(filtered, columns=columns, dtype=object)
        frame.attrs['evidence'] = {'query_id': query_id, 'complete': True, 'firmware': switch['firmware-version'],
            'history_coverage': 'retention_unknown' if d['time_field'] else 'current_observation', 'live_verified': False}
        return frame

    @staticmethod
    def _matches(actual, condition):
        def canonical(value):
            return value.lower() if isinstance(value, str) and re.fullmatch(r'(?:[0-9a-fA-F]{2}:){7}[0-9a-fA-F]{2}', value) else value
        actual = canonical(actual)
        operators = condition if isinstance(condition, dict) else {'eq': condition}
        for op, value in operators.items():
            value = [canonical(v) for v in value] if op == 'in' else canonical(value)
            if op == 'eq' and actual != value:
                return False
            if op == 'ne' and actual == value:
                return False
            if op == 'in' and actual not in value:
                return False
            if op in ('gt', 'gte', 'lt', 'lte'):
                if actual is None:
                    return False
                if not {'gt': lambda: actual > value, 'gte': lambda: actual >= value,
                        'lt': lambda: actual < value, 'lte': lambda: actual <= value}[op]():
                    return False
        return True
