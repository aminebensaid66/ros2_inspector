# Changelog

All notable changes to `ros2inspector` are documented here. The project follows
[Semantic Versioning](https://semver.org/); while it is below 1.0, a minor version can add
commands and change output.

## [0.2.0] - 2026-11-08

The first release of the **Package Designer** and three more commands, with a large accuracy
pass over launch, remap and name resolution.

### New commands

- **`design`**: a local, browser-based package designer. Add packages and nodes, give them
  publishers, subscribers, services, clients and action servers or clients, wire topics, then
  generate ready-to-build `ament_python` packages with a README and ament linter tests.
  - Served on `127.0.0.1` only, protected by a per-session token.
  - Regenerating is safe: files you edited are kept, files you added are never touched, and
    renaming a node or package moves its module, folder, `setup.py` registration and launch
    entries. A rename that cannot be completed safely is refused, or rolled back without
    losing your files.
  - Refuses names that ROS rejects at startup, node names that shadow Python modules and
    reserved package names such as `ament_cmake`.
  - Uses the ROS2 Inspector look: brand colours and logo, publishers drawn as cyan dots and
    subscribers as blue rings, and a toolbar that fits phone-width screens.
- **`docs`**: write architecture documentation in Markdown (with Mermaid dependency and
  communication diagrams) or HTML: an index plus one page per package.
- **`doctor`**: read-only diagnostics for versions, source roots, manifests, `ROS_DISTRO` and
  the cache, with `--json` output and a failing exit code for CI.
- **`watch`**: re-run policy validation or the audit whenever sources, launch, config, README
  or test files change, printing only new and resolved findings.

### Analysis accuracy

- Launch: apply `PushRosNamespace` scopes inside groups, carry parent namespaces into included
  deployments, follow namespace changes in action order, and apply literal parameter
  overrides.
- Names: resolve relative topic names (`cmd_vel` and `/cmd_vel` are one topic), match remap
  rules on fully expanded names as `rcl` does, and keep remap order and duplicate keys.
- Nodes: map console-script entry points to the node classes they construct, report
  ambiguous mappings, and resolve node inheritance across workspace packages.
- Graph: report each endpoint's own interface type and flag type mismatches.

### Policy and audit

- Validate rule keys and value types before analysis.
- Enforce declared external dependencies, including conditional and transitive manifest
  dependencies.
- Expose analysis completeness in JSON and SARIF output; keep findings that depend on
  unreadable source marked as uncertain.
- Exclude parse gaps in unrelated test sources from production confidence, while following
  test-helper imports (including relative imports from package `__init__` modules).

### Visualisation

- Honest layouts with unique labels and readable containers and edges; arrows follow the
  data flow; overlapping nodes are pushed apart; the toolbar stays reachable on narrow
  screens. Embedded JSON escapes every markup character.

### Compatibility

- Requires `tree-sitter>=0.25` and `tree-sitter-cpp>=0.23.4`; the incompatible 0.22 runtime is
  excluded.
- Output and generated packages are written correctly on native Windows.
- Tested on Python 3.10 to 3.14, on Linux, macOS and Windows, and against ROS 2 Jazzy.

### Known limitations

- The designer edits one package at a time. Viewing and wiring connections across packages
  is planned ([#17](https://github.com/aminebensaid66/ros2_inspector/issues/17)).

[0.2.0]: https://github.com/aminebensaid66/ros2_inspector/compare/v0.1.5...v0.2.0
