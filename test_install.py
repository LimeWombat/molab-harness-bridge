import json
import importlib.util
import io
import os
import plistlib
import stat
import tempfile
import subprocess
import sys
import unittest
import urllib.request
from unittest.mock import patch
from pathlib import Path


class InstallTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux') or sys.platform == 'win32', 'foreground installation is for Linux and Windows')
    def test_platform_installer_starts_gateway_and_lists_models(self):
        import install
        prompt = 'Connect to the notebook at: https://sb-test.sb.molab.run/\nUse `execute-code.sh --url https://sb-test.sb.molab.run/ --token sample-token`.'
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'app'
            with patch.object(install, 'install_target', return_value=target), \
                 patch.object(sys, 'argv', ['install.py', '--prompt-stdin']), \
                 patch.object(sys, 'stdin', io.StringIO(prompt)), patch('builtins.print'):
                install.main()
            bridge = target / 'bridge' / 'bridge.py'
            key = subprocess.check_output([sys.executable, str(bridge), 'key'], text=True).strip()
            server = subprocess.Popen([sys.executable, str(bridge), 'serve', '--port', '0'],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                line = server.stdout.readline()
                self.assertIn('Listening on http://127.0.0.1:', line)
                url = line.strip().split()[-1] + '/models'
                request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key})
                with urllib.request.urlopen(request, timeout=10) as response:
                    self.assertTrue(json.load(response)['data'])
            finally:
                server.terminate()
                server.communicate(timeout=10)

    def test_install_without_launch_agent_supports_other_platforms(self):
        import install
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            runtime, agent = install.install(Path(__file__).parent, root / 'app', None)
            self.assertIsNone(agent)
            self.assertTrue((runtime / 'bridge.py').is_file())
            self.assertTrue((root / 'app' / 'jailbreaks' / 'Unified.md').is_file())

    def test_non_mac_default_install_paths(self):
        import install
        home = Path('/example/home')
        self.assertEqual(install.install_target('linux', home, {}), home / '.local' / 'share' / 'molab-bridge')
        self.assertEqual(install.install_target('win32', home, {'LOCALAPPDATA': str(home / 'AppData' / 'Local')}), home / 'AppData' / 'Local' / 'MolabBridge')

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
            spec = importlib.util.spec_from_file_location('installed_bridge_test', runtime / 'bridge.py')
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
            self.assertEqual(bridge.read_session(session)['token'], 'sample-token')
            if os.name != 'nt':
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
            if os.name != 'nt':
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
