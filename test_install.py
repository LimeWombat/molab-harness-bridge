import json
import os
import plistlib
import stat
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


class InstallTest(unittest.TestCase):
    def test_restarts_existing_service_without_unregistering_it(self):
        import install
        from types import SimpleNamespace
        with patch.object(install.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            install.start_service('gui/123', Path('/tmp/agent.plist'))
        self.assertEqual(run.call_args_list[-1].args[0], ['launchctl', 'kickstart', '-k', 'gui/123/io.local.molab-bridge'])

    def test_imports_current_pair_prompt_into_private_session_file(self):
        try:
            import install
        except ModuleNotFoundError:
            self.fail('install module does not exist yet')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            runtime, _ = install.install(Path(__file__).parent, root / 'app', root / 'agents')
            prompt = 'Connect to the notebook at: https://sb-test.sb.molab.run/\nUse `execute-code.sh --url https://sb-test.sb.molab.run/ --token sample-token`.'
            install.import_session(runtime, prompt)
            session = runtime / 'session.json'
            self.assertEqual(json.loads(session.read_text())['token'], 'sample-token')
            self.assertEqual(stat.S_IMODE(session.stat().st_mode), 0o600)

    def test_installs_portable_runtime_and_preserves_existing_session(self):
        try:
            import install
        except ModuleNotFoundError:
            self.fail('install module does not exist yet')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / 'Application Support' / 'MolabBridge'
            target.joinpath('bridge').mkdir(parents=True)
            session = target / 'bridge' / 'session.json'
            session.write_text('{"keep":"my-existing-session"}')
            session.chmod(0o600)
            agents = root / 'LaunchAgents'
            agents.mkdir()
            install.install(Path(__file__).parent, target, agents)
            self.assertEqual(json.loads(session.read_text()), {'keep': 'my-existing-session'})
            self.assertEqual(stat.S_IMODE(session.stat().st_mode), 0o600)
            self.assertTrue((target / 'bridge' / 'bridge.py').is_file())
            self.assertTrue((target / 'jailbreaks' / 'glm-5.2.md').is_file())
            with (agents / 'io.local.molab-bridge.plist').open('rb') as f:
                agent = plistlib.load(f)
            self.assertEqual(agent['ProgramArguments'][1], str(target / 'bridge' / 'bridge.py'))
            self.assertEqual(agent['ProgramArguments'][-2:], ['--port', '49876'])
            self.assertNotIn('/Users/another-user/', str(agent))


if __name__ == '__main__':
    unittest.main()
