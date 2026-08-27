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

3. In GitHub, create an environment named `pypi`. Optional protection rules can require
   approval before a package is uploaded.

## Release checklist

1. Update `project.version` in `pyproject.toml`. This is the single source of truth for the
   installed `ros2inspector.__version__`.
2. Refresh the lock file:

   ```bash
   uv lock
   ```

3. Run the checks locally:

   ```bash
   python -m pip install -e ".[dev]"
   pytest
   ruff check .
   mypy ros2inspector
   python -m pip install --upgrade build twine
   python -m build
   python -m twine check dist/*
   ```

4. Test the built wheel in a clean virtual environment. Do not test only the source tree:

   ```bash
   python -m venv /tmp/ros2inspector-release-test
   /tmp/ros2inspector-release-test/bin/pip install dist/ros2inspector-*.whl
   cd /tmp
   /tmp/ros2inspector-release-test/bin/ros2inspector --version
   /tmp/ros2inspector-release-test/bin/ros2inspector --help
   ```

5. Commit and push the version change.
6. Create a GitHub Release with a tag that exactly matches `v<project.version>`, for example
   `v0.1.1`. The workflow rejects mismatched tags.
7. Confirm the package on PyPI, then test the public installation:

   ```bash
   pipx install ros2inspector
   ros2inspector --version
   ```

PyPI does not allow replacing an uploaded distribution. If a release is incorrect, increment
the version and publish a new release.
