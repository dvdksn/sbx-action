"""Install the tested SBX distribution and optionally authenticate Docker."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request

from main import mask

DEFAULT_VERSION = 'v0.48.0-rc4'
DEFAULT_SHA256 = 'd2a3d90545f7fe242b6bf412ce2afe198cda33f399e2612b91a3418bf13a96e3'
ASSET = 'DockerSandboxes-linux-amd64-ubuntu2404.deb'


def setup(env, runner=subprocess.run):
    username = env.get('SBXA_DOCKER_USERNAME', '')
    token = env.get('SBXA_DOCKER_TOKEN', '')
    if token:
        mask(token)
    if bool(username) != bool(token):
        raise ValueError('provide both docker-username and docker-token')
    if token and env.get('GITHUB_EVENT_NAME') in {'pull_request', 'pull_request_target'}:
        raise ValueError('Docker login is not supported on pull request events')
    clean = {k: v for k, v in env.items() if not k.startswith('SBXA_')}
    def call(argv, **kwargs):
        runner(argv, env=clean, check=True, **kwargs)
    if env.get('RUNNER_OS') != 'Linux':
        raise ValueError('this action currently supports Linux runners only')
    if not shutil.which('sbx'):
        release = env.get('SBXA_VERSION', DEFAULT_VERSION)
        if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', release):
            raise ValueError('sbx-version must be a release tag such as v0.48.0-rc4')
        distro = dict(line.strip().split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
        if env.get('RUNNER_ARCH') != 'X64' or distro.get('ID', '').strip('"') != 'ubuntu' or distro.get('VERSION_ID', '').strip('"') != '24.04':
            raise ValueError('automatic SBX installation requires Ubuntu 24.04 x86-64; provision SBX yourself on other Linux runners')
        if not Path('/dev/kvm').is_char_device():
            raise ValueError('SBX requires /dev/kvm; use a runner with hardware virtualization')
        call(['sudo', 'setfacl', '-m', f'u:{os.getuid()}:rw', '/dev/kvm'])
        digest = DEFAULT_SHA256
        if release != DEFAULT_VERSION:
            request = urllib.request.Request(f'https://api.github.com/repos/docker/sbx-releases/releases/tags/{release}', headers={'Accept': 'application/vnd.github+json'})
            with urllib.request.urlopen(request, timeout=60) as response:
                assets = json.load(response)['assets']
            asset = next((a for a in assets if a['name'] == ASSET), {})
            digest = asset.get('digest', '').removeprefix('sha256:')
            if not re.fullmatch(r'[a-f0-9]{64}', digest):
                raise ValueError('SBX release does not provide a SHA256 digest for the Ubuntu 24.04 package')
        with tempfile.TemporaryDirectory(prefix='sbx-install-', dir=env['RUNNER_TEMP']) as directory:
            package = Path(directory) / 'sbx.deb'
            with urllib.request.urlopen(f'https://github.com/docker/sbx-releases/releases/download/{release}/{ASSET}', timeout=60) as response, package.open('wb') as output:
                shutil.copyfileobj(response, output)
            with package.open('rb') as downloaded:
                actual_digest = hashlib.file_digest(downloaded, 'sha256').hexdigest()
            if actual_digest != digest:
                raise ValueError('SBX package checksum mismatch')
            # apt's unprivileged downloader must be able to traverse the directory.
            Path(directory).chmod(0o755)
            call(['sudo', 'apt-get', 'update', '-qq'])
            call(['sudo', 'apt-get', 'install', '-y', str(package)])
        call(['sbx', 'policy', 'init', 'deny-all'])
    call(['sbx', 'version'])
    if token:
        call(['sbx', 'login', '--username', username, '--password-stdin'], input=token, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    try:
        setup(dict(os.environ))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        # Login diagnostics may contain secrets; only print our own validation errors.
        print(f'::error::{error if isinstance(error, ValueError) else "SBX installation or authentication failed."}', flush=True)
        raise SystemExit(1)
