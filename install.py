#!/usr/bin/env python3
"""Install the Molab bridge on macOS, Linux, or Windows."""

import argparse
import importlib.util
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


LABEL = 'io.local.molab-bridge'


def install_target(platform, home, environ):
    if platform == 'darwin':
        return home / 'Library' / 'Application Support' / 'MolabBridge'
    if platform == 'win32':
        return Path(environ.get('LOCALAPPDATA') or home / 'AppData' / 'Local') / 'MolabBridge'
    if platform.startswith('linux'):
        return Path(environ.get('XDG_DATA_HOME') or home / '.local' / 'share') / 'molab-bridge'
    raise ValueError('Unsupported operating system')


def install(source, target, agents):
    prompts = source / 'jailbreaks'
    if not (prompts / 'Unified.md').is_file():
        raise ValueError('Missing jailbreaks submodule: run git submodule update --init')
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime = target / 'bridge'
    runtime.mkdir(mode=0o700, exist_ok=True)
    prompt_target = target / 'jailbreaks'
    prompt_target.mkdir(mode=0o700, exist_ok=True)
    for name in ('bridge.py', 'models.json'):
        shutil.copy2(source / 'bridge' / name, runtime / name)
    for prompt in prompts.glob('*.md'):
        shutil.copy2(prompt, prompt_target / prompt.name)
    if agents is None:
        return runtime, None
    agents.mkdir(mode=0o700, parents=True, exist_ok=True)
    agent = {'Label': LABEL, 'ProgramArguments': ['/usr/bin/python3', str(runtime / 'bridge.py'),
             'serve', '--port', '49876'], 'WorkingDirectory': str(runtime), 'RunAtLoad': True,
             'KeepAlive': True, 'ThrottleInterval': 30, 'StandardOutPath': '/dev/null',
             'StandardErrorPath': str(target / 'bridge-error.log')}
    path = agents / (LABEL + '.plist')
    with path.open('wb') as f:
        plistlib.dump(agent, f)
    os.chmod(path, 0o600)
    return runtime, path


def import_session(runtime, prompt):
    spec = importlib.util.spec_from_file_location('molab_bridge_runtime', runtime / 'bridge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_session(runtime / 'session.json', module.parse_pair_prompt(prompt))


def start_service(domain, agent):
    service = f'{domain}/{LABEL}'
    loaded = subprocess.run(['launchctl', 'print', service], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if loaded.returncode == 0:
        subprocess.run(['launchctl', 'kickstart', '-k', service], check=True)
    else:
        subprocess.run(['launchctl', 'bootstrap', domain, str(agent)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true', help='Import a new Molab Pair prompt from the clipboard')
    parser.add_argument('--prompt-stdin', action='store_true', help='Read the Pair prompt from standard input')
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    target = install_target(sys.platform, Path.home(), os.environ)
    agents = Path.home() / 'Library' / 'LaunchAgents' if sys.platform == 'darwin' else None
    runtime, agent = install(source, target, agents)
    session = runtime / 'session.json'
    if args.refresh or not session.is_file():
        if args.prompt_stdin:
            prompt = sys.stdin.read()
        elif sys.platform == 'darwin':
            prompt = subprocess.run(['pbpaste'], capture_output=True, text=True, check=True).stdout
        else:
            raise SystemExit('Provide the copied Molab Pair prompt using --prompt-stdin; see README.md.')
        try:
            import_session(runtime, prompt)
        except ValueError:
            raise SystemExit('Copy the Molab Pair prompt first, then run this installer again.')
    if agent:
        start_service(f'gui/{os.getuid()}', agent)
        print('Molab bridge installed and started at http://127.0.0.1:49876/v1')
    else:
        print('Molab bridge installed. Start it using the command for your platform in README.md.')
    print(f'The local client key is stored in {session} (local_key).')
    print('Add a custom OpenAI Chat Completions provider in DeepSeek Harness; see README.md.')


if __name__ == '__main__':
    main()
