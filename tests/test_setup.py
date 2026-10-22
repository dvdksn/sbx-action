import contextlib
import importlib.util
import io
import hashlib
import json
import tempfile
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
spec = importlib.util.spec_from_file_location('sbx_setup', Path(__file__).parents[1] / 'src/setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTest(unittest.TestCase):
    def test_existing_install_login_keeps_secrets_out_of_environment_and_argv(self):
        env = {'RUNNER_OS': 'Linux', 'SBXA_DOCKER_USERNAME': 'user', 'SBXA_DOCKER_TOKEN': 'private-token'}
        calls = []
        with patch.object(setup.shutil, 'which', return_value='/usr/bin/sbx'), contextlib.redirect_stdout(io.StringIO()):
            setup.setup(env, lambda argv, **kw: calls.append((argv, kw)))
        self.assertEqual(calls[0][0], ['sbx', 'version'])
        self.assertEqual(calls[1][0], ['sbx', 'login', '--username', 'user', '--password-stdin'])
        self.assertEqual(calls[1][1]['input'], 'private-token')
        self.assertFalse(any(key.startswith('SBXA_') for key in calls[1][1]['env']))
        self.assertEqual(len(calls), 2)

    def test_credentials_validated_before_install(self):
        for env in [
            {'SBXA_DOCKER_TOKEN': 'secret'},
            {'SBXA_DOCKER_USERNAME': 'user'},
            {'SBXA_DOCKER_USERNAME': 'user', 'SBXA_DOCKER_TOKEN': 'secret', 'GITHUB_EVENT_NAME': 'pull_request'},
        ]:
            with self.subTest(env=env), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                setup.setup(env, lambda *a, **kw: self.fail('must fail before subprocess'))

    def test_unsupported_install_and_invalid_version_fail_before_download(self):
        with patch.object(setup.shutil, 'which', return_value=None):
            for extra in [{'SBXA_VERSION': '../invalid'}, {'RUNNER_ARCH': 'ARM64'}]:
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    setup.setup({'RUNNER_OS': 'Linux', **extra}, lambda *a, **kw: self.fail('must fail before subprocess'))

    def test_latest_and_pinned_releases_verify_resolved_package(self):
        package = b'sbx package'
        digest = hashlib.sha256(package).hexdigest()
        for requested, endpoint in [('latest', 'latest'), ('v0.49.2', 'tags/v0.49.2')]:
            with self.subTest(requested=requested), tempfile.TemporaryDirectory() as directory:
                metadata = {'tag_name': 'v0.49.2', 'assets': [{'name': setup.ASSET, 'digest': f'sha256:{digest}'}]}
                calls = []
                with patch.object(setup.shutil, 'which', return_value=None), \
                     patch.object(setup.Path, 'read_text', return_value='ID=ubuntu\nVERSION_ID="24.04"'), \
                     patch.object(setup.Path, 'is_char_device', return_value=True), \
                     patch.object(setup.urllib.request, 'urlopen', side_effect=[io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(package)]) as download:
                    setup.setup({'RUNNER_OS': 'Linux', 'RUNNER_ARCH': 'X64', 'RUNNER_TEMP': directory, 'SBXA_VERSION': requested}, lambda argv, **kw: calls.append(argv))
                self.assertEqual(download.call_args_list[0].args[0].full_url, f'https://api.github.com/repos/docker/sbx-releases/releases/{endpoint}')
                self.assertEqual(download.call_args_list[1].args[0], f'https://github.com/docker/sbx-releases/releases/download/v0.49.2/{setup.ASSET}')
                self.assertIn(['sbx', 'policy', 'init', 'deny-all'], calls)
