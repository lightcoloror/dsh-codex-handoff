from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import MagicMock,patch
import uuid
import setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).resolve().parent/'.tmp'/('pairing-'+uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        def cleanup():
            assert self.root.resolve().is_relative_to((Path(__file__).resolve().parent/'.tmp').resolve())
            shutil.rmtree(self.root)
        self.addCleanup(cleanup)
        self.package=self.root/'package';self.package.mkdir()
        for rel in setup.CLIENT_FILES:(self.package/rel).write_text('{}' if rel.endswith('.json') else '# fixture\n')
        (self.package/'vendor-manifest.json').write_text('{}')
        (self.package/'plugin.mjs').write_text('// fixture\n')
        self.home=self.root/'host';self.workspace=self.root/'workspace';self.workspace.mkdir()
        self.endpoint=self.home/'.state/endpoint.json'
        self.record={'schema':1,'status':'ready','base_url':'http://127.0.0.1:1251/bridge',
            'pid':os.getpid(),'instance_id':str(uuid.uuid4()),'executable':sys.executable,
            'module':(self.package/'plugin.mjs').as_uri(),'loaded_at':datetime.now(timezone.utc).isoformat(),
            'workspaces':[{'workspace_id':'registered-one','workspace_path':str(self.workspace)}]}
        setup.write(self.endpoint,self.record)
        for p in (patch.object(setup,'PACKAGE',self.package),patch.object(setup.socket,'create_connection',return_value=MagicMock()),
                  patch.object(setup.transport,'verify_peer')):
            p.start();self.addCleanup(p.stop)

    def run_setup(self):return setup.setup(self.home,self.workspace,sys.executable)

    def test_exact_scoped_pairing_repeat_and_changed_port_do_not_rotate_credentials(self):
        self.assertTrue(self.run_setup()['paired'])
        token=self.home/'.client/bridge.token';before=token.read_bytes()
        store=json.loads((self.home/'.state/forward/tokens.json').read_text())
        entry=store['tokens']['codex-handoff-default']
        self.assertEqual(entry['workspaceIds'],['registered-one']);self.assertFalse(entry['allowWorkspaceCreate'])
        self.assertEqual(entry['hash'],hashlib.sha256(before.strip()).hexdigest())
        self.record['base_url']='http://127.0.0.1:8877/bridge';setup.write(self.endpoint,self.record)
        self.assertTrue(self.run_setup()['paired']);self.assertEqual(token.read_bytes(),before)

    def test_unconfigured_workspace_does_not_create_client_or_token(self):
        self.record['workspaces']=[];setup.write(self.endpoint,self.record)
        with self.assertRaisesRegex(ValueError,'Register this workspace'):self.run_setup()
        self.assertFalse((self.home/'.client').exists());self.assertFalse((self.home/'cli.py').exists())

    def test_changed_client_is_retained_without_credential_or_code_overwrite(self):
        target=self.home/'cli.py';target.write_text('# user changes\n')
        with self.assertRaisesRegex(ValueError,'Existing client file differs'):self.run_setup()
        self.assertEqual(target.read_text(),'# user changes\n');self.assertFalse((self.home/'.client').exists())


if __name__=='__main__':unittest.main()
