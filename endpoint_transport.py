"""Restart-safe discovery with Windows socket-owner verification before credentials.

No port scan, proxy, redirect, process launch, configuration rewrite or request replay.
The manifest is host-owned rendezvous data; Windows authenticates the connected peer.
"""
from datetime import datetime, timezone
import ctypes
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import struct
import time
from urllib.parse import urlencode, urlsplit
import uuid


class EndpointUnavailable(RuntimeError):
    pass


class DeliveryUncertain(RuntimeError):
    pass


_validated = set()


def load(path):
    p = Path(path)
    if p.stat().st_size > 65536:
        raise ValueError('Endpoint/config record exceeds size limit')
    return json.loads(p.read_text(encoding='utf-8-sig'))


def stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Endpoint timestamp must include timezone')
    return parsed.timestamp()


def same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def local_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r'/[A-Za-z0-9_/-]+', parsed.path.rstrip('/'))
            or '..' in parsed.path):
        raise ValueError('Discovery requires a literal IPv4 loopback HTTP endpoint and a bridge prefix')
    return parsed


def discover(config):
    policy = config['endpoint_discovery']
    p = Path(policy['file'])
    if not p.is_absolute() or not same_path(p.resolve(), p):
        raise ValueError('Linked or relative endpoint record denied')
    try:
        record = load(p)
    except FileNotFoundError as exc:
        raise EndpointUnavailable('Bridge discovery missing; start Harness with the installed handoff plugin') from exc
    if record.get('schema') != 1 or record.get('status') != 'ready':
        raise EndpointUnavailable('Bridge is not ready: ' + str(record.get('status', 'invalid_manifest')))
    if (type(record.get('pid')) is not int or record['pid'] <= 0
            or str(uuid.UUID(record['instance_id'])) != record['instance_id']):
        raise ValueError('Invalid bridge process or instance identity')
    if (not same_path(record['executable'], policy['expected_executable'])
            or record['module'].lower() != policy['expected_module'].lower()):
        raise ValueError('Endpoint executable/module differs from the paired installation')
    if stamp(record['loaded_at']) > time.time() + 2:
        raise ValueError('Endpoint load time is in the future')
    url = local_url(record['base_url'])
    configured = local_url(config['base_url'])
    if url.path.rstrip('/') != configured.path.rstrip('/'):
        raise ValueError('Endpoint bridge prefix differs from paired configuration')
    override = os.environ.get('DSH_CODEX_BRIDGE_URL')
    if override and override.rstrip('/') != record['base_url'].rstrip('/'):
        raise ValueError('Explicit DSH_CODEX_BRIDGE_URL conflicts with current discovery; remove stale process override')
    return record


def windows_process(pid):
    """Read exact executable and creation time without CIM, PowerShell or admin."""
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise EndpointUnavailable('Cannot query Harness peer process identity')
    try:
        size = w.DWORD(32768); name = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            raise EndpointUnavailable('Cannot resolve Harness peer executable')
        times = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise EndpointUnavailable('Cannot read Harness peer creation time')
        created = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10000000 - 11644473600
        return name.value, created
    finally:
        kernel.CloseHandle(handle)


def windows_socket_owner(sock):
    """Match the established server-side TCP tuple of this exact connection."""
    from ctypes import wintypes as w
    api = ctypes.WinDLL('iphlpapi', use_last_error=True)
    get = api.GetExtendedTcpTable
    get.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD), w.BOOL, w.ULONG, ctypes.c_int, w.ULONG]
    get.restype = w.DWORD
    local = sock.getsockname(); peer = sock.getpeername()
    for attempt in range(5):
        size = w.DWORD()
        code = get(None, ctypes.byref(size), False, socket.AF_INET, 5, 0)
        if code != 122:
            raise EndpointUnavailable('Cannot read Windows TCP ownership table')
        data = ctypes.create_string_buffer(size.value)
        code = get(data, ctypes.byref(size), False, socket.AF_INET, 5, 0)
        if code == 122:
            continue
        if code:
            raise EndpointUnavailable('Cannot verify Windows TCP ownership')
        count, = struct.unpack_from('<I', data)
        owners = set()
        for i in range(count):
            state, la, lp, ra, rp, pid = struct.unpack_from('<6I', data, 4 + i * 24)
            addr = lambda n: socket.inet_ntoa(struct.pack('<I', n))
            if (state == 5 and (addr(la), socket.ntohs(lp & 65535)) == peer
                    and (addr(ra), socket.ntohs(rp & 65535)) == local):
                owners.add(pid)
        if len(owners) == 1:
            return owners.pop()
        if owners:
            raise EndpointUnavailable('Ambiguous connected peer ownership')
        if attempt < 4:
            time.sleep(0.01)
    raise EndpointUnavailable('Connected Harness socket ownership was not found')


def verify_peer(sock, record):
    if os.name != 'nt':
        raise EndpointUnavailable('Automatic endpoint authentication is currently implemented for Windows only')
    if windows_socket_owner(sock) != record['pid']:
        raise EndpointUnavailable('Connected socket does not belong to the published Harness process')
    executable, started = windows_process(record['pid'])
    if not same_path(executable, record['executable']) or started > stamp(record['loaded_at']):
        raise EndpointUnavailable('Harness executable or process generation changed; wait for fresh discovery')


class Connection(http.client.HTTPConnection):
    def __init__(self, record, timeout):
        url = local_url(record['base_url'])
        super().__init__(url.hostname, url.port, timeout=timeout)
        self.record = record
        self.sent = False

    def connect(self):
        super().connect()
        try:
            verify_peer(self.sock, self.record)
        except Exception:
            self.close()
            raise

    def send(self, data):
        if self.sock is None:
            self.connect()
        self.sent = True
        return super().send(data)


def exchange(conn, config, record, token, method, route, body=None, query=None):
    # Check that publication has not changed since the connection was resolved.
    current = discover(config)
    if current != record:
        raise EndpointUnavailable('Harness instance changed before request; retry observation with the original job IDs')
    url = local_url(record['base_url']).path.rstrip('/') + route
    if query:
        url += '?' + urlencode(query)
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    conn.sent = False
    try:
        conn.request(method, url, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError('Bridge response exceeds limit')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Bridge returned a non-object JSON response')
        return response.status, result
    except (OSError, http.client.HTTPException, ValueError) as exc:
        if method == 'POST' and route != '/tasks/wait' and conn.sent:
            raise DeliveryUncertain('Bridge response lost/invalid; delivery is uncertain. Preserve job/session/request IDs and reconcile; no automatic replay.') from exc
        raise


def request(config_path, client, method, route, body=None, query=None, timeout=40, validate_scope=None):
    cfg = load(config_path)
    if not cfg.get('endpoint_discovery'):
        return client.request(config_path, method, route, body=body, query=query, timeout=timeout)
    if route not in client.ROUTES.get(method, set()):
        raise ValueError('Unsupported JSON bridge route')
    if (method == 'GET' and body is not None) or (method == 'POST' and not isinstance(body, dict)):
        raise ValueError('Invalid bridge request body')
    if route in ('/workspaces/create', '/tasks/create', '/tasks/append') and not isinstance(body.get('requestId'), str):
        raise ValueError('Mutation requires the original stable requestId')
    if route == '/tasks/wait' and not 0 <= body.get('waitMs', 25000) <= 30000:
        raise ValueError('Use a bounded waitMs')
    record = discover(cfg)
    _, token = client.connection(config_path)  # Read only; never writes/prints credentials.
    conn = Connection(record, timeout)
    key = (str(config_path), json.dumps(cfg, sort_keys=True), json.dumps(record, sort_keys=True), hashlib.sha256(token.encode()).digest())
    try:
        if key not in _validated:
            status, health = exchange(conn, cfg, record, token, 'GET', '/health')
            value = health.get('value', {})
            if (status != 200 or health.get('ok') is not True or value.get('bridge') != 'ready'
                    or value.get('pid') != record['pid'] or value.get('protocolVersion') != 1
                    or value.get('trackedTasks', 0) < record.get('restored_task_count', 0)
                    or value.get('routePrefix') != local_url(record['base_url']).path.rstrip('/')
                    or value.get('permissions', {}).get('autoApprove') is not False):
                raise EndpointUnavailable('Authenticated bridge health/identity does not match paired runtime')
            if validate_scope:
                status, spaces = exchange(conn, cfg, record, token, 'GET', '/workspaces')
                if status != 200 or spaces.get('ok') is not True:
                    raise EndpointUnavailable('Cannot validate paired workspace scope')
                validate_scope(cfg, spaces['value']['items'])
            if len(_validated) > 32:
                _validated.clear()
            _validated.add(key)
            if method == 'GET' and route == '/health':
                return 200, health
        return exchange(conn, cfg, record, token, method, route, body, query)
    finally:
        conn.close()
