"""Small SBX orchestration adapter. Requires a provisioned local Linux runner."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

import yaml


def mapping(raw, label):
    try:
        value = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError:
        # Parser errors can include secret-bearing source lines.
        raise ValueError(f"{label} must be a valid YAML mapping") from None
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a YAML mapping")
    return value


def mask(value):
    escaped = value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print(f"::add-mask::{escaped}", flush=True)
    for line in value.splitlines():
        if line:
            print(f"::add-mask::{line.replace('%', '%25')}", flush=True)


def configuration(env):
    root = Path(env['GITHUB_WORKSPACE']).resolve()
    workspace = (root / env.get('SBXA_WORKSPACE', '.')).resolve()
    if not workspace.is_dir():
        raise ValueError('workspace must be an existing directory; run actions/checkout first')
    clone = env.get('SBXA_CLONE', 'false')
    if clone not in {'true', 'false'}:
        raise ValueError('clone must be true or false')
    if clone == 'true' and not (workspace / '.git').exists():
        raise ValueError('clone requires a Git checkout at the workspace root')
    source, kit = env.get('SBXA_ENVIRONMENT_FILE', ''), env.get('SBXA_KIT', '')
    if bool(source) == bool(kit):
        raise ValueError('provide exactly one of environment-file or kit')
    document = mapping((root / source).read_text(), 'environment-file') if source else {
        'schemaVersion': '1', 'agent': kit.rsplit('/', 1)[-1].split('@', 1)[0].split(':', 1)[0], 'kits': [{'source': kit}]}
    credentials = mapping(env.get('SBXA_CREDENTIALS', ''), 'credentials')
    if credentials and env.get('GITHUB_EVENT_NAME') in {'pull_request', 'pull_request_target'}:
        raise ValueError('secret-bearing tasks are not supported on pull request events; use a trusted manual workflow')
    allow_host = env.get('SBXA_ALLOW_HOST_COMMANDS', 'false')
    if allow_host not in {'true', 'false'}:
        raise ValueError('allow-host-commands must be true or false')
    def has_command(value):
        if isinstance(value, dict):
            return 'command' in value or any(has_command(v) for v in value.values())
        return isinstance(value, list) and any(has_command(v) for v in value)
    if allow_host != 'true' and (
        document.get('lifecycle') or has_command(document.get('secrets', {}))
        or has_command(document.get('registries', {})) or has_command(document.get('mcp', {}))
    ):
        raise ValueError('environment host commands require allow-host-commands: true')
    bindings = mapping(env.get('SBXA_BINDINGS', ''), 'bindings')
    existing = document.get('bindings', {})
    if not isinstance(existing, dict):
        raise ValueError('environment bindings must be a mapping')
    approvals = {**existing, **bindings}
    for service, value in credentials.items():
        if not isinstance(service, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]+', service):
            raise ValueError('credential service names must use letters, numbers, dots, underscores or hyphens')
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'credential {service} must be a non-empty string')
        mask(value)
        approval = approvals.get(service, {})
        key = approval.get('apiKey', {}) if isinstance(approval, dict) else {}
        domains = key.get('domains') if isinstance(key, dict) else None
        if not isinstance(domains, list) or not domains or not all(isinstance(d, str) and d for d in domains):
            raise ValueError(f'credential {service} requires explicit apiKey.domains bindings')
    arguments = mapping(env.get('SBXA_ARGUMENTS', ''), 'environment-args')
    argv = []
    for name, value in arguments.items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]+', name) or isinstance(value, (dict, list)) or value is None:
            raise ValueError('environment-args must map argument names to scalar values')
        argv += ['--env-arg', f'{name}={str(value).lower() if isinstance(value, bool) else value}']
    if not env.get('SBXA_RUN', '').strip():
        raise ValueError('run is required')
    return root, workspace, source, document, credentials, bindings, argv


def execute(env, runner=subprocess.run):
    root, workspace, source, document, credentials, bindings, arguments = configuration(env)
    additional = document.get('additionalWorkspaces', [])
    if not isinstance(additional, list):
        raise ValueError('additionalWorkspaces must be a list')
    name = 'gha-' + uuid.uuid4().hex[:20]
    temp = Path(tempfile.mkdtemp(prefix='sbx-action-', dir=env.get('RUNNER_TEMP')))
    # Host resolver and inputs must never be in a writable sandbox mount.
    mounts = [workspace]
    source_dir = (root / source).resolve().parent if source else temp
    for entry in additional:
        raw = entry.get('path', '') if isinstance(entry, dict) else ''
        if not isinstance(raw, str) or not raw:
            shutil.rmtree(temp)
            raise ValueError('additionalWorkspaces entries require a path')
        raw = raw.replace('${{ env.fileDir }}', str(source_dir)).replace('${{ env.projectDir }}', str(source_dir))
        if '${{' in raw:
            shutil.rmtree(temp)
            raise ValueError('parameterized additional workspace paths are not supported by this prototype')
        mounts.append((source_dir / raw).resolve())
    if any(temp.is_relative_to(mount) for mount in mounts):
        shutil.rmtree(temp)
        raise ValueError('RUNNER_TEMP must be outside every sandbox workspace')
    def call(args):
        clean = {k: v for k, v in env.items() if not k.startswith('SBXA_')}
        # Treat sandbox output as data, never GitHub workflow commands.
        token = uuid.uuid4().hex
        print(f'::stop-commands::{token}', flush=True)
        try:
            return runner(['sbx', *args], cwd=root, env=clean, stdin=subprocess.DEVNULL, check=False).returncode
        except OSError:
            return 1
        finally:
            print(f'::{token}::', flush=True)
    attempted = False
    result = 1
    try:
        overlay = {'schemaVersion': '1', 'workspace': {'path': str(workspace), 'clone': env.get('SBXA_CLONE', 'false') == 'true'}}
        if bindings:
            overlay['bindings'] = bindings
        if credentials:
            values = temp / 'values.json'
            values.write_text(json.dumps(credentials))
            values.chmod(0o600)
            helper = temp / 'resolve.py'
            helper.write_text('import json,sys\nfrom pathlib import Path\nprint(json.loads(Path(__file__).with_name("values.json").read_text())[sys.argv[1]])\n')
            overlay['secrets'] = {service: {'command': shlex.join([sys.executable, str(helper), service]), 'snapshot': True} for service in credentials}
        overlay_path = temp / 'overlay.yaml'
        overlay_path.write_text(yaml.safe_dump(overlay))
        overlay_path.chmod(0o600)
        if source:
            # Keep the original path so SBX resolves relative references correctly.
            files = [str((root / source).resolve()), str(overlay_path)]
        else:
            base = temp / 'base.yaml'
            base.write_text(yaml.safe_dump(document))
            files = [str(base), str(overlay_path)]
        if env.get('GITHUB_OUTPUT'):
            with open(env['GITHUB_OUTPUT'], 'a') as output:
                output.write(f'sandbox-name={name}\n')
        attempted = True
        result = call(['env', 'create', '--name', name, '--force', *arguments, *files])
        if result == 0:
            if credentials:
                values.unlink()  # Snapshot resolution is complete; discard host plaintext now.
            result = call(['env', 'exec', '--name', name, '--workdir', str(workspace), *arguments, *files, '--', 'bash', '-lc', env['SBXA_RUN']])
    finally:
        if attempted:
            cleanup = call(['rm', '--force', name])
            for service in credentials:
                cleanup |= call(['secret', 'rm', service, '--sandbox', name, '--force'])
            if cleanup:
                print('::warning::SBX cleanup was incomplete; inspect the runner before reuse.', flush=True)
                if result == 0:
                    result = 1
        shutil.rmtree(temp)
    return result


if __name__ == '__main__':
    try:
        if sys.platform != 'linux':
            raise ValueError('this prototype currently supports Linux runners only')
        if not shutil.which('sbx'):
            raise ValueError('install and authenticate SBX on the runner before using this action')
        sys.exit(execute(dict(os.environ)))
    except ValueError as error:
        print(f'::error::{error}', flush=True)
        raise SystemExit(1)
    except OSError:
        print('::error::Could not read configuration or prepare private runner files.', flush=True)
        raise SystemExit(1)
