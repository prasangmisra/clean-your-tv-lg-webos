"""Exercise the actual observer against modeled SDK metadata; no TV writes."""
import json
import os
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from cytv import no_root_observer as observer
from cytv.errors import UserError


class DeveloperLayoutTests(unittest.TestCase):
    def check_layout(self, changes=None, root=observer.ROOT, guards_only=False, action='install', legacy_access=False):
        metadata = {
            '/': dict(uid=5938, gid=5000, mode=0o755),
            '/media': dict(uid=5938, gid=5000, mode=0o555),
            '/media/developer': dict(uid=0, gid=0, mode=0o775),
            '/media/developer/temp': dict(uid=0, gid=0, mode=0o777),
        }
        for name, fields in (changes or {}).items():
            metadata.setdefault(name, {}).update(fields)
        prefix = r'''
var fixtureFs=require('fs'), metadata=METADATA;
process.getuid=function(){return 5938;};
fixtureFs.lstatSync=function(name){
 var item=metadata[name];
 if(!item){var e=Error('absent fixture path');e.code='ENOENT';throw e;}
 return {uid:item.uid,gid:item.gid,mode:item.mode,
  isDirectory:function(){return item.directory!==false;},
  isSymbolicLink:function(){return item.symlink===true;}};
};
fixtureFs.accessSync=function(name){
 if(!metadata[name]||metadata[name].writable===false){var e=Error('not writable');e.code='EACCES';throw e;}
};
['mkdirSync','writeFileSync','openSync','chmodSync','renameSync','unlinkSync'].forEach(function(name){
 fixtureFs[name]=function(){throw Error('unexpected filesystem mutation');};
});
'''.replace('METADATA', json.dumps(metadata))
        if legacy_access:
            prefix += "fixtureFs.accessSync=undefined;process.getgroups=function(){return [5000];};"
        request = dict(command='preflight', root=root, public_root=observer.PUBLIC_ROOT, action=action)
        program = observer.PROGRAM
        if guards_only:
            program = program.split("if(typeof c.O_NOFOLLOW", 1)[0]+"ancestry(path.dirname(root));ancestry(publicRoot,true);process.stdout.write('checked');}());"
        return subprocess.run([shutil.which('node'), '-e', prefix+program, json.dumps(request)],
                              capture_output=True, timeout=5)

    def test_directory_guards_accept_sdk_metadata(self):
        result = self.check_layout(guards_only=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, b'checked')

    def test_removal_does_not_require_install_staging_access(self):
        result = self.check_layout({'/media/developer/temp':dict(mode=0o755),
                                    observer.ROOT:dict(uid=5938,gid=5000,mode=0o700)}, action='remove')
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_preflight_checks_kernel_write_access_without_creating_files(self):
        result = self.check_layout({'/media/developer/temp':dict(writable=False)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'EACCES', result.stderr)

    def test_legacy_runtime_write_check_rejects_protected_parent_and_accepts_staging(self):
        result = self.check_layout(root='/media/developer/.cytv-installer', legacy_access=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'directory-not-writable', result.stderr)
        result = self.check_layout(legacy_access=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_known_sdk_containers_are_read_only_compatible_on_configured_runtimes(self):
        runtimes = [shutil.which('node')]+[os.environ[name] for name in ('NODE010','NODE01025','NODE012') if os.environ.get(name)]
        for runtime in runtimes:
            with self.subTest(runtime=runtime), unittest.mock.patch('shutil.which', return_value=runtime):
                result = self.check_layout()
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(json.loads(result.stdout), {'preflight':'ready'})

    def test_sdk_exception_does_not_allow_foreign_or_changed_containers(self):
        for name, fields in [
            ('/media/developer', dict(uid=1234)),
            ('/media/developer', dict(gid=1234)),
            ('/media/developer', dict(mode=0o773)),
            ('/media/developer', dict(mode=0o2775)),
            ('/media/developer/temp', dict(symlink=True)),
            ('/media/developer/temp', dict(directory=False)),
            ('/media', dict(uid=0, gid=0, mode=0o777)),
            (observer.ROOT, dict(uid=5938, gid=5000, mode=0o775)),
        ]:
            with self.subTest(name=name, fields=fields):
                result = self.check_layout({name:fields})
                self.assertNotEqual(result.returncode, 0)

    def test_known_sdk_modes_are_not_an_exception_for_other_directories(self):
        result = self.check_layout({'/media/developer/other':dict(uid=0,gid=0,mode=0o777)},
                                   root='/media/developer/other/private')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'writable-directory', result.stderr)

    def test_invalid_preflight_reply_never_records_an_operation(self):
        for reply in ('', '{}', '{"preflight":"unknown"}', '[]'):
            with self.subTest(reply=reply):
                installer = SimpleNamespace(remote=Mock(), store=Mock())
                installer.remote.run.return_value=reply
                with self.assertRaises(UserError):
                    observer.begin(installer, 'install', {}, {}, {}, {})
                installer.store.begin.assert_not_called()

    def test_prepare_streams_bound_source_instead_of_a_large_ssh_argument(self):
        remote=Mock()
        remote.run.return_value=json.dumps({'operation':'a'*32})
        plan=observer.make_plan('a'*32,'b'*32,'install',{'helper.ipk':b'helper','launcher.ipk':b'home'},
                                dict(zip(observer.APPS,['1','2'])))
        observer.invoke(remote,plan,'prepare')
        args,kwargs=remote.run.call_args
        self.assertEqual(args,('env -u NODE_OPTIONS -u NODE_PATH /usr/bin/node',))
        prefix,program=kwargs['data'].decode().split('\n',1)
        request=json.loads(json.loads(prefix[len('process.argv.push('):-2]))
        self.assertEqual(program,observer.PROGRAM)
        self.assertEqual(request['source'],observer.PROGRAM)
        self.assertEqual(request['plan'],plan)
        self.assertEqual(request['command'],'prepare')
