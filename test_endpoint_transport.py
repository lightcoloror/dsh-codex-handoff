"""Offline protocol tests; no Harness/model calls or user credentials."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import threading
import unittest
from unittest.mock import patch
import uuid

import cli
import endpoint_transport as e


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.root = cli.ROOT / '.tmp' / ('endpoint-' + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.cfgfile = self.root / 'config.json'
        self.endpoint = self.root / 'endpoint.json'
        self.calls = []; self.mode = 'ok'
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                case.calls.append((self.path, self.headers.get('Authorization')))
                if self.path == '/bridge/health':
                    value = {'bridge':'ready','pid':os.getpid(),'protocolVersion':1,
                             'routePrefix':'/bridge','permissions':{'autoApprove':False}}
                    if case.mode == 'wrong_health': value['pid'] = -1
                else:
                    value = {'items':[{'workspaceId':'one','path':str(case.root)}]}
                    if case.mode == 'wide_scope': value['items'].append({'workspaceId':'other','path':str(case.root.parent)})
                    if case.mode == 'changed':
                        record=e.load(case.endpoint);record['instance_id']=str(uuid.uuid4());cli.save(case.endpoint,record)
                raw = json.dumps({'ok':True,'value':value}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length',0)))
                case.calls.append((self.path,self.headers.get('Authorization')))
                # Lost response after accepted input: caller must not retry.
                self.close_connection=True

        self.handler=Handler
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda: (server.shutdown(),server.server_close(),thread.join()))
        self.record={'schema':1,'status':'ready','instance_id':str(uuid.uuid4()),'pid':os.getpid(),
                     'executable':sys.executable,'module':'file:///test/plugin.mjs',
                     'loaded_at':datetime.now(timezone.utc).isoformat(),
                     'base_url':f'http://127.0.0.1:{server.server_port}/bridge'}
        cli.save(self.endpoint,self.record)
        self.cfg={'base_url':'http://127.0.0.1:1/bridge','workspace_id':'one','workspace_path':str(self.root),
                  'endpoint_discovery':{'file':str(self.endpoint),'expected_executable':sys.executable,
                                        'expected_module':self.record['module']}}
        cli.save(self.cfgfile,self.cfg)
        self.env=patch.dict(os.environ,{},clear=False);self.env.start();self.addCleanup(self.env.stop)
        os.environ.pop('DSH_CODEX_BRIDGE_URL',None)
        self.auth=patch.object(cli.CLIENT,'connection',return_value=(self.cfg['base_url'],'synthetic-token'))
        self.auth.start();self.addCleanup(self.auth.stop)
        self.real_peer=e.verify_peer
        self.peer=patch.object(e,'verify_peer');self.peer.start();self.addCleanup(self.peer.stop)
        e._validated.clear()

    def cleanup(self):
        assert self.root.resolve().is_relative_to((cli.ROOT/'.tmp').resolve())
        shutil.rmtree(self.root)

    def request(self,method='GET',route='/health',body=None):
        return e.request(self.cfgfile,cli.CLIENT,method,route,body=body,timeout=2,
                         validate_scope=cli.validate_workspace_scope)

    def test_stale_config_port_is_ignored_and_new_publication_is_read_each_time(self):
        self.assertTrue(self.request()[1]['ok'])
        old = self.record['instance_id']
        replacement=ThreadingHTTPServer(('127.0.0.1',0),self.handler)
        thread=threading.Thread(target=replacement.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(replacement.shutdown(),replacement.server_close(),thread.join()))
        self.record.update(instance_id=str(uuid.uuid4()),base_url=f'http://127.0.0.1:{replacement.server_port}/bridge')
        cli.save(self.endpoint,self.record)
        self.assertNotEqual(e.discover(self.cfg)['instance_id'],old)
        self.assertTrue(self.request()[1]['ok'])
        self.assertEqual(self.cfg['base_url'],'http://127.0.0.1:1/bridge')

    def test_missing_starting_stopped_remote_module_and_override_fail_closed(self):
        for changes in ({'status':'starting'},{'status':'stopped'}, {'base_url':'http://example.com/bridge'},
                        {'module':'file:///wrong/plugin.mjs'},{'executable':'other.exe'}):
            cli.save(self.endpoint,{**self.record,**changes})
            with self.subTest(changes=changes),self.assertRaises((ValueError,e.EndpointUnavailable)):
                self.request()
        cli.save(self.endpoint,self.record)
        os.environ['DSH_CODEX_BRIDGE_URL']=self.cfg['base_url']
        with self.assertRaisesRegex(ValueError,'conflicts'):self.request()
        self.assertEqual(self.calls,[])

    def test_untrusted_socket_receives_no_authorization_bytes(self):
        with patch.object(e,'verify_peer',side_effect=e.EndpointUnavailable('wrong owner')):
            with self.assertRaisesRegex(e.EndpointUnavailable,'wrong owner'):self.request()
        self.assertEqual(self.calls,[])

    def test_health_and_scope_must_match_before_mutation(self):
        for mode in ('wrong_health','wide_scope'):
            self.mode=mode
            with self.subTest(mode=mode),self.assertRaises((ValueError,e.EndpointUnavailable)):
                self.request('POST','/tasks/append',{'requestId':'stable'})
        self.assertFalse(any(path.endswith('append') for path,_ in self.calls))

    def test_changed_generation_during_preflight_does_not_submit(self):
        self.mode='changed'
        with self.assertRaisesRegex(e.EndpointUnavailable,'changed before request'):
            self.request('POST','/tasks/append',{'requestId':'stable'})
        self.assertFalse(any(path.endswith('append') for path,_ in self.calls))

    def test_incomplete_restart_task_restore_blocks_mutations(self):
        self.record['restored_task_count']=10;cli.save(self.endpoint,self.record)
        with self.assertRaises(e.EndpointUnavailable):
            self.request('POST','/tasks/append',{'requestId':'stable'})
        self.assertFalse(any(path.endswith('append') for path,_ in self.calls))

    def test_lost_mutation_response_is_uncertain_and_never_replayed(self):
        with self.assertRaises(e.DeliveryUncertain):
            self.request('POST','/tasks/append',{'requestId':'stable','text':'one'})
        self.assertEqual(sum(path.endswith('append') for path,_ in self.calls),1)

    @unittest.skipUnless(os.name=='nt','Windows connected peer API')
    def test_real_windows_socket_owner_and_process_generation(self):
        port=e.local_url(self.record['base_url']).port
        with socket.create_connection(('127.0.0.1',port),timeout=2) as sock:
            # Call the real verifier, bypassing only the fixture HTTP hook.
            self.assertEqual(e.windows_socket_owner(sock),os.getpid())
            executable,started=e.windows_process(os.getpid())
            self.assertTrue(e.same_path(executable,sys.executable))
            self.assertLessEqual(started,e.stamp(self.record['loaded_at']))
            self.real_peer(sock,self.record)
            with patch.object(e,'windows_socket_owner',return_value=-1):
                with self.assertRaises(e.EndpointUnavailable):
                    self.real_peer(sock,self.record)
            with patch.object(e,'windows_process',return_value=(executable,e.stamp(self.record['loaded_at'])+1)):
                with self.assertRaises(e.EndpointUnavailable):self.real_peer(sock,self.record)


if __name__=='__main__':unittest.main()
