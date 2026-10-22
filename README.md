# sbx-action

Run a command in a Docker Sandbox with your repository as its workspace. The
Action installs SBX, authenticates Docker, creates an environment, executes your
command, and removes the sandbox and its scoped secrets when finished.

`run` uses **`sbx env exec`**, executing `bash -lc` without a TTY. Supply the CLI
and its headless flags explicitly, such as `claude -p "…"`. It does not use
`sbx run` to forward prompt arguments to a kit's default workload command.

Requires a Linux runner with KVM access and Python 3 with venv/pip. On
Ubuntu 24.04 x86-64, the Action installs a checksum-verified SBX release and
initializes deny-all policy. Existing SBX installations and their policy are
reused. Other Linux runners must have SBX provisioned already. This is a prototype.

## Usage

```yaml
on: workflow_dispatch
permissions:
  contents: read
jobs:
  review:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v5
        with:
          persist-credentials: false
      - uses: dvdksn/sbx-action@main # Pin a reviewed commit in production.
        with:
          docker-username: ${{ secrets.DOCKER_USERNAME }}
          docker-token: ${{ secrets.DOCKER_PAT }}
          environment-file: sbxenv.yaml
          credentials: |
            anthropic: ${{ toJSON(secrets.ANTHROPIC_API_KEY) }}
          run: claude -p "Review the documentation and write review.md"
      - uses: actions/upload-artifact@v4
        with:
          name: review
          path: review.md
          if-no-files-found: error
```

For the built-in Claude workload, `sbxenv.yaml` can be:

```yaml
schemaVersion: "1"
agent: claude
bindings:
  anthropic:
    apiKey:
      domains: [api.anthropic.com]
```

Use your own [SBX environment file](https://docs.docker.com/ai/sandboxes/configuration/environment-files/)
to select kits and workload settings. For a single workload kit, provide `kit`
instead of `environment-file`; its workload name is inferred from the OCI
repository basename.

## Inputs

| Input | Description |
| --- | --- |
| `sbx-version` | Release installed when SBX is absent; defaults to `latest` (latest stable release). Set an explicit tag such as `v0.49.2` to pin |
| `docker-username` | Docker login username; requires `docker-token` |
| `docker-token` | Docker access token; requires `docker-username` |
| `environment-file` | Environment file relative to the checkout; mutually exclusive with `kit` |
| `kit` | Workload kit OCI reference; mutually exclusive with `environment-file` |
| `run` | Required Bash command executed inside the sandbox workspace |
| `workspace` | Path relative to the checkout; defaults to `.` |
| `clone` | Private Git clone for the primary workspace; defaults to `false` |
| `environment-args` | YAML mapping of environment arguments to runtime values |
| `credentials` | YAML mapping of SBX service names to secret values |
| `bindings` | Optional credential approval mapping layered over environment bindings |
| `allow-host-commands` | Permit trusted environment host lifecycle/resolver commands; defaults to `false` |

`sandbox-name` is an output for diagnostics. The sandbox is removed before the
Action returns, including after command failure. A cleanup failure also fails the
Action. Forced runner termination can interrupt cleanup.

## Workspace and credentials

By default, the checkout is mounted directly: generated files and edits are
available to subsequent workflow steps. Upload artifacts with ordinary Actions
steps, as above.

Set `clone: true` to isolate primary workspace edits in an SBX Git clone. The
workspace root must be a Git checkout; edits are discarded during cleanup.
Additional workspaces retain their SBX mount settings. The Action does not collect
or publish output.

Provide `docker-username` and `docker-token` through GitHub secrets; the Action
authenticates with `sbx login --password-stdin`. These inputs are separate from
workload `credentials` and are not passed to the sandbox. The Action leaves runner
authentication in place; use ephemeral runners. Custom kit registries may require
runner `kit.allowedSources` configuration.

Each supplied credential requires an explicit `apiKey.domains` binding and a kit
that supports that service. Values are masked and supplied through sandbox-scoped
SBX snapshots. Use `toJSON` to quote secret values safely for YAML. For GitHub App
credentials, pass the installation token from an earlier step as the `github`
service value.

Use trusted kits and environment files on ephemeral runners. Secret-bearing tasks
on pull request events are rejected; host commands require explicit opt-in. SBX
owns capabilities and network policy. GitHub permissions, triggers, and artifact
handling belong to your workflow. Concurrent jobs sharing an SBX daemon are not
supported.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```
