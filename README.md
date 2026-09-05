# ros2inspector

**Static ROS 2 architecture analysis, visualization, and policy checks.**

`ros2inspector` scans supported Python, C++, interface, and launch-file patterns in a ROS 2 workspace. It builds an evidence-backed architecture graph, renders that graph in several formats, and enforces architecture policies in CI.

The current alpha ships seven commands: `scan`, `packages`, `nodes`, `graph`, `viz`, `audit`, and `validate`.

**Website and documentation:** [ros2-inspector-theta.vercel.app](https://ros2-inspector-theta.vercel.app/)

## Features

- Static node and endpoint discovery with Python AST and Tree-sitter C++ parsers
- Separate source-node definitions and launch deployment instances in the architecture graph
- Python `console_scripts`/entry-point mapping for evidence-based launch executable matching
- `.msg`, `.srv`, and `.action` interface parsing and type resolution
- Python, XML, and YAML launch-file analysis with per-node remapping
- NetworkX Unified Architecture Model for packages, nodes, topics, services, actions, and interfaces
- Content-fingerprinted incremental analysis cache backed by diskcache
- Rich table, JSON, YAML, Mermaid, DOT, and interactive HTML output
- Built-in connectivity audit and YAML policy enforcement

## Analysis scope

ROS 2 Inspector analyzes source files without executing or building the workspace. Unsupported or ambiguous constructs are reported through evidence, confidence, dynamic-name flags, or diagnostics rather than presented as complete architectural truth. Launch substitutions and unresolved interface types remain explicitly unresolved instead of being guessed from similar names.

## Installation

Requires Python 3.10 or newer. A ROS 2 installation is not required for source analysis.

For a globally available CLI in an isolated environment, use `pipx`:

```bash
pipx install ros2inspector
```

Run it once without keeping it installed:

```bash
pipx run ros2inspector --help
```

Inside an existing Python virtual environment, use `pip`:

```bash
pip install ros2inspector
```

Install from source for development:

```bash
git clone https://github.com/aminebensaid66/ros2_inspector
cd ros2_inspector
pip install -e ".[dev]"
```

## Quickstart

```bash
# Workspace summary
ros2inspector scan

# Package metadata and dependencies
ros2inspector packages --show-deps

# Nodes and their connections
ros2inspector nodes --show-connections

# Communication graph as Mermaid
ros2inspector graph comms --format mermaid

# Interactive HTML graph
ros2inspector viz --no-open

# Built-in architecture checks
ros2inspector audit

# Policy enforcement
ros2inspector validate --policy ros2inspector_policy.yaml
```

## Commands

### `scan`

```bash
ros2inspector scan [PATH] [--format table|json|yaml]
```

Discovers ROS 2 packages and displays a health-scored workspace summary.

### `packages`

```bash
ros2inspector packages [-C PATH] [--filter cpp|python|meta] [--sort name|score|version] [--show-deps] [--format table|json|yaml]
```

Shows package metadata, type, license, maintainers, health score, and optional dependency details.

### `nodes`

```bash
ros2inspector nodes [-C PATH] [--format table|json|yaml] [-p PACKAGE] [-o FILE] [--show-connections] [--no-cache]
```

Lists discovered nodes and their publishers, subscriptions, services, clients, action servers, and action clients. `--show-connections` includes resolved peer connections and interface types.

### `graph`

```bash
ros2inspector graph [deps|comms|full] [-C PATH] [--format mermaid|dot|json] [-p PACKAGE] [-o FILE] [--no-cache]
```

Graph views:

| View | Contents |
|---|---|
| `deps` | Package dependencies |
| `comms` | Node-to-topic, service, and action communication |
| `full` | Dependency and communication relationships together |

Examples:

```bash
ros2inspector graph deps --format json
ros2inspector graph comms --format mermaid -o architecture.mmd
ros2inspector graph full --format dot | dot -Tsvg -o architecture.svg
```

### `viz`

```bash
ros2inspector viz [-C PATH] [-o FILE] [--open|--no-open] [--no-cache]
```

Generates a self-contained interactive Cytoscape.js HTML graph.

### `audit`

```bash
ros2inspector audit [PATH] [--format table|json|yaml] [--fail-on error|warning|info] [--strict] [--exclude NAME] [--no-cache]
```

Runs built-in connectivity checks for orphan topics, dead outputs, isolated nodes, and unmatched service or action endpoints.

### `validate`

```bash
ros2inspector validate [PATH] --policy FILE [--fail-on error|warning|info] [--format table|json|yaml] [--no-cache]
```

Validates the workspace against a YAML policy file.

## Global options

```bash
ros2inspector --version
ros2inspector --quiet scan
```

`--version` prints the package version. `--quiet` suppresses diagnostic headers and progress output.

## Output formats

`scan`, `packages`, `nodes`, `audit`, and `validate` support table, JSON, and YAML output where applicable. `graph` supports Mermaid, DOT, and JSON. `viz` writes interactive HTML.

Machine-readable records go to standard output while diagnostics go to standard error, which keeps pipelines clean:

```bash
ros2inspector nodes --format json | jq '.[] | select(.package == "my_pkg")'
ros2inspector audit --format yaml > audit.yaml
ros2inspector graph comms --format mermaid -o architecture.mmd
```

## Policy files

Create a `ros2inspector_policy.yaml` file:

```yaml
version: 1
rules:
  - type: health_threshold
    min_score: 70
    severity: warning

  - type: license
    allowed: [Apache-2.0, MIT]
    severity: error

  - type: naming
    packages:
      pattern: '^[a-z][a-z0-9_]*$'
      severity: warning
    nodes:
      pattern: '^[A-Z][a-zA-Z0-9]*Node$'
      severity: info

  - type: dependency
    forbidden:
      - { from: perception, to: planner, severity: error }
    required:
      - { package: navigation, depends_on: nav_core, severity: warning }

  - type: no_circular_deps
    severity: error

  - type: topic_connectivity
    no_publisher: true
    no_subscriber: true
    exclude: [/rosout, /clock]

  - type: maintainer_required
    require_email: true
    severity: warning
```

Supported rule types are `health_threshold`, `license`, `naming`, `dependency`, `no_circular_deps`, `topic_connectivity`, `node_isolation`, `service_connectivity`, `action_connectivity`, `maintainer_required`, and `version_not_default`.

## CI integration

```yaml
- name: Install ROS 2 Inspector
  run: pip install ros2inspector

- name: Architecture audit
  run: ros2inspector audit --fail-on warning --format json > audit.json

- name: Policy validation
  run: ros2inspector validate --policy .ros2inspector_policy.yaml --fail-on error
```

Relevant exit codes:

| Code | Meaning |
|---|---|
| `0` | Success or no findings at the configured threshold |
| `1` | Findings at or above the configured threshold |
| `2` | Invalid invocation or policy configuration |
| `3` | Workspace or policy file not found |

## Architecture

```text
ros2inspector/
├── cli/           # scan, packages, nodes, graph, viz, audit, validate
├── discovery/     # workspace, package, and interface discovery
├── static/        # Python, C++, interface, launch, package, and health parsers
├── model/         # Pydantic schemas and the NetworkX UAM
├── graph/         # Mermaid, DOT, and JSON renderers
├── viz/           # interactive HTML renderer and bundled Cytoscape.js
├── policy/        # policy loader, engine, and rules
├── cache/         # diskcache-backed incremental analysis
├── output/        # output package plumbing
└── utils/         # shared utilities
```

The Unified Architecture Model is a `networkx.MultiDiGraph`. Graph nodes represent packages, ROS nodes, topics, services, actions, and interfaces. Edges carry relationships such as `depends_on`, `publishes`, `subscribes`, `provides`, `calls`, `defined_in`, and `uses_interface`.


### Ignoring generated or vendor trees

Package scans automatically prune common generated directories such as `build/`, `install/`, `log/`, `.git/`, virtual environments, `node_modules/`, and `__pycache__/`. For large package-local vendor or generated trees, add a `.ros2inspectorignore` file at the package root. It accepts simple shell-style glob patterns; a pattern ending in `/` prunes that entire subtree before traversal.

```text
vendor/
third_party/generated/
*.generated.py
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check ros2inspector/
mypy ros2inspector/
```

The test suite uses fixture workspaces under `tests/fixtures/` and does not require a live ROS 2 environment.

## Publishing

Maintainers should follow [`RELEASING.md`](RELEASING.md). Publishing is performed by
GitHub Actions through PyPI Trusted Publishing when a GitHub Release is published.

## License

MIT. See [`LICENSE`](LICENSE).
