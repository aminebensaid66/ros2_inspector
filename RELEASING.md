# Releasing ros2inspector

Releases are published to PyPI from GitHub Actions using Trusted Publishing. No long-lived
PyPI token is stored in the repository.

## One-time PyPI setup

1. Create the `ros2inspector` project on PyPI, or configure a pending trusted publisher for
   the first release.
2. Add a GitHub trusted publisher with these values:

   - Owner: `aminebensaid66`
   - Repository: `ros2_inspector`
   - Workflow: `publish.yml`
   - Environment: `pypi`

3. In GitHub, create an environment named `pypi`. Protection rules should require approval
   before a package is uploaded.

## Release checklist

1. Update `project.version` in `pyproject.toml`. This is the single source of truth for the
   installed `ros2inspector.__version__`.
2. Refresh and verify the lock file:

   ```bash
   uv lock
   uv lock --check
   ```

3. Run the checks locally:

   ```bash
   python -m pip install -e ".[dev]"
   pytest -m "not requires_ros2"
   ruff check .
   mypy ros2inspector
   python -m pip install --upgrade build twine
   python -m build
   python -m twine check --strict dist/*
   ```

4. If ROS 2 Jazzy is available, run the static/runtime differential test:

   ```bash
   pytest -m requires_ros2 tests/integration/test_ros2_differential.py --no-cov
   ```

   GitHub CI runs this gate independently on every push and pull request.

5. Test the built wheel in a clean virtual environment. Do not test only the source tree:

   ```bash
   python -m venv /tmp/ros2inspector-release-test
   /tmp/ros2inspector-release-test/bin/pip install dist/ros2inspector-*.whl
   cd /tmp
   /tmp/ros2inspector-release-test/bin/ros2inspector --version
   /tmp/ros2inspector-release-test/bin/ros2inspector --help
   ```

6. Commit and push the version change only after CI is green.
7. Create a GitHub Release with a tag that exactly matches `v<project.version>`, for example
   `v0.1.3`. The publish workflow rejects mismatched tags.
8. Confirm the package on PyPI, then test the public installation:

   ```bash
   pipx install ros2inspector==0.1.3
   ros2inspector --version
   ```

PyPI does not allow replacing an uploaded distribution. If a release is incorrect, increment
the version and publish a new release.
