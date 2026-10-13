import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import unittest
from types import SimpleNamespace
import yaml

spec = importlib.util.spec_from_file_location('action', Path(__file__).parents[1] / 'src/main.py')
action = importlib.util.module_from_spec(spec)
spec.loader.exec_module(action)


class ActionTest(unittest.TestCase):
    def test_lifecycle_and_secret_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'repo'
            workspace.mkdir()
            env = {**os.environ, 'GITHUB_WORKSPACE': str(workspace), 'RUNNER_TEMP': str(root),
                   'SBXA_KIT': 'ghcr.io/example/workload:sha', 'SBXA_RUN': 'exit 7',
                   'SBXA_CREDENTIALS': 'github: super-secret',
                   'SBXA_BINDINGS': 'github: {apiKey: {domains: [api.github.com]}}',
                   'SBXA_ARGUMENTS': 'model: example'}
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                self.assertNotIn('super-secret', repr(argv))
                self.assertNotIn('SBXA_CREDENTIALS', kwargs['env'])
                if argv[1:3] == ['env', 'create']:
                    overlay = yaml.safe_load(Path(argv[-1]).read_text())
                    self.assertEqual(overlay['workspace'], {'path': str(workspace), 'clone': False})
                    self.assertNotIn('super-secret', Path(argv[-1]).read_text())
                    self.assertTrue(overlay['secrets']['github']['snapshot'])
                return SimpleNamespace(returncode=7 if argv[1:3] == ['env', 'exec'] else 0)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(action.execute(env, run), 7)
            self.assertEqual([c[1:3] for c in calls], [['env', 'create'], ['env', 'exec'], ['rm', '--force'], ['secret', 'rm']])
            self.assertEqual(list(root.iterdir()), [workspace])

    def test_missing_binding_and_empty_secret_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'GITHUB_WORKSPACE': directory, 'SBXA_KIT': 'example', 'SBXA_RUN': 'true'}
            for value in ['github: token', 'github: ""']:
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                    action.configuration({**env, 'SBXA_CREDENTIALS': value})

    def test_source_paths_preserved_and_failed_create_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'repo'
            workspace.mkdir()
            source = workspace / 'environment.yaml'
            source.write_text('schemaVersion: "1"\nagent: shell\n')
            env = {'GITHUB_WORKSPACE': str(workspace), 'RUNNER_TEMP': str(root),
                   'SBXA_ENVIRONMENT_FILE': 'environment.yaml', 'SBXA_RUN': 'true'}
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                return SimpleNamespace(returncode=3 if argv[1:3] == ['env', 'create'] else 0)
            self.assertEqual(action.execute(env, run), 3)
            self.assertIn(str(source), calls[0])
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][1:3], ['rm', '--force'])

    def test_malformed_secret_error_does_not_echo_input(self):
        with self.assertRaises(ValueError) as caught:
            action.mapping('github: [super-secret', 'credentials')
        self.assertNotIn('super-secret', str(caught.exception))

class HardeningTest(unittest.TestCase):
    def test_pull_request_secrets_and_host_commands_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'GITHUB_WORKSPACE': directory, 'SBXA_KIT': 'example', 'SBXA_RUN': 'true'}
            for event in ['pull_request', 'pull_request_target']:
                with self.assertRaises(ValueError):
                    action.configuration({**env, 'GITHUB_EVENT_NAME': event, 'SBXA_CREDENTIALS': 'github: token'})
            source = Path(directory) / 'env.yaml'
            source.write_text('agent: shell\nlifecycle:\n  initialize:\n    - command: touch /tmp/host-file\n')
            env.pop('SBXA_KIT')
            env['SBXA_ENVIRONMENT_FILE'] = str(source)
            with self.assertRaises(ValueError):
                action.configuration(env)
            action.configuration({**env, 'SBXA_ALLOW_HOST_COMMANDS': 'true'})

    def test_additional_mount_cannot_expose_secret_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'repo'
            workspace.mkdir()
            source = workspace / 'env.yaml'
            source.write_text(yaml.safe_dump({'agent': 'shell', 'additionalWorkspaces': [{'path': str(root)}]}))
            env = {'GITHUB_WORKSPACE': str(workspace), 'RUNNER_TEMP': str(root),
                   'SBXA_ENVIRONMENT_FILE': str(source), 'SBXA_RUN': 'true'}
            with self.assertRaises(ValueError):
                action.execute(env, lambda *args, **kwargs: self.fail('must not invoke SBX'))
            self.assertEqual(list(root.iterdir()), [workspace])

    def test_workflow_commands_are_disabled_around_child_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'repo').mkdir()
            env = {'GITHUB_WORKSPACE': str(root / 'repo'), 'RUNNER_TEMP': str(root),
                   'SBXA_KIT': 'example', 'SBXA_RUN': 'true'}
            output = io.StringIO()
            def run(*args, **kwargs):
                print('::error::untrusted sandbox output')
                return SimpleNamespace(returncode=0)
            with contextlib.redirect_stdout(output):
                self.assertEqual(action.execute(env, run), 0)
            lines = output.getvalue().splitlines()
            self.assertTrue(lines[0].startswith('::stop-commands::'))
            token = lines[0].split('::')[-1]
            self.assertEqual(lines[2], f'::{token}::')


class KitSelectionTest(unittest.TestCase):
    def test_workload_name_from_pinned_oci_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'GITHUB_WORKSPACE': directory, 'SBXA_KIT': 'ghcr.io/dvdksn/kit-base@sha256:abc', 'SBXA_RUN': 'true'}
            document = action.configuration(env)[3]
            self.assertEqual(document['agent'], 'kit-base')


class WorkspaceModeTest(unittest.TestCase):
    def test_clone_is_explicit_and_cleanup_remains_automatic(self):
        for clone in ['false', 'true']:
            with self.subTest(clone=clone), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); workspace = root / 'repo'; workspace.mkdir()
                (workspace / '.git').mkdir()
                env = {'GITHUB_WORKSPACE': str(workspace), 'RUNNER_TEMP': directory,
                       'SBXA_KIT': 'example', 'SBXA_RUN': 'true', 'SBXA_CLONE': clone}
                calls = []
                def runner(argv, **kwargs):
                    calls.append(argv)
                    if argv[1:3] == ['env', 'create']:
                        self.assertEqual(yaml.safe_load(Path(argv[-1]).read_text())['workspace']['clone'], clone == 'true')
                    return SimpleNamespace(returncode=0)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(action.execute(env, runner), 0)
                self.assertEqual([c[1:3] for c in calls], [['env', 'create'], ['env', 'exec'], ['rm', '--force']])
                self.assertEqual(calls[1][-4:], ['--', 'bash', '-lc', 'true'])
                self.assertEqual(list(root.iterdir()), [workspace])

    def test_invalid_clone_or_missing_git_checkout_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'GITHUB_WORKSPACE': directory, 'SBXA_KIT': 'example', 'SBXA_RUN': 'true'}
            for clone in ['yes', '', 'true']:
                with self.subTest(clone=clone), self.assertRaises(ValueError):
                    action.configuration({**env, 'SBXA_CLONE': clone})


class PreflightAndCleanupTest(unittest.TestCase):
    def test_malformed_additional_workspaces_do_not_leave_staging(self):
        for additional in [None, 'path', {'path': '/tmp'}]:
            with self.subTest(additional=additional), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); workspace = root / 'repo'; workspace.mkdir()
                (workspace / 'env.yaml').write_text(yaml.safe_dump({'agent': 'shell', 'additionalWorkspaces': additional}))
                env = {'GITHUB_WORKSPACE': str(workspace), 'RUNNER_TEMP': directory,
                       'SBXA_ENVIRONMENT_FILE': 'env.yaml', 'SBXA_RUN': 'true'}
                with self.assertRaises(ValueError):
                    action.execute(env, lambda *args, **kwargs: self.fail('must reject before SBX'))
                self.assertEqual(list(root.iterdir()), [workspace])

    def test_cleanup_failure_fails_successful_task_and_still_removes_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); workspace = root / 'repo'; workspace.mkdir()
            env = {'GITHUB_WORKSPACE': str(workspace), 'RUNNER_TEMP': directory,
                   'SBXA_KIT': 'example', 'SBXA_RUN': 'true', 'SBXA_CREDENTIALS': 'github: token',
                   'SBXA_BINDINGS': 'github: {apiKey: {domains: [api.github.com]}}'}
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                return SimpleNamespace(returncode=1 if argv[1] == 'rm' else 0)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(action.execute(env, runner), 1)
            self.assertEqual(calls[-1][1:3], ['secret', 'rm'])
            self.assertEqual(list(root.iterdir()), [workspace])


if __name__ == '__main__':
    unittest.main()
