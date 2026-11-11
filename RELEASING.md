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

## One-time website setup

The documentation website reads the latest version from PyPI when it builds. To rebuild it
automatically after each release:

1. In the Vercel project for the website, open **Settings → Git → Deploy Hooks** and create a
   hook for the production branch (`release/v0.1.0`).
2. In this repository, add the hook URL as an Actions secret named `VERCEL_DEPLOY_HOOK`.

The `refresh-website` job in `publish.yml` calls the hook after a successful PyPI upload. If the
secret is not set, the job is skipped without failing the release.

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
   `v0.2.0`. The publish workflow rejects mismatched tags.
8. Confirm the package on PyPI, then test the public installation:

   ```bash
   pipx install ros2inspector==0.2.0
   ros2inspector --version
   ```

PyPI does not allow replacing an uploaded distribution. If a release is incorrect, increment
the version and publish a new release.

## apt repository

`.github/workflows/apt.yml` builds a self-contained `.deb` for Ubuntu 22.04 (jammy), 24.04
(noble) and 26.04 (resolute) on amd64 and arm64. Each package bundles ros2inspector and the
dependencies pinned in `uv.lock` in a private virtual environment under `/opt/ros2inspector`,
with `/usr/bin/ros2inspector` linked to it. Every package is installed, run against the test
fixture workspace and removed again in a clean container before anything is published.

The workflow runs on pull requests that touch `packaging/deb/` (build and test only), on every
published GitHub Release (build, test and publish), and by hand from the Actions tab, where
`publish` and a Debian `revision` can be set. Bump the revision to re-publish the same upstream
version with packaging fixes; a published file is never replaced.

The packages are published to a separate repository, served by GitHub Pages, that holds only
the signed apt indexes and `.deb` files, never source code.

### One-time setup

1. Create a public repository named `ros2inspector-apt`, initialised with a README so it has
   a `main` branch. To use another name, set the Actions variable `APT_REPOSITORY_NAME` in
   this repository.
2. In `ros2inspector-apt`, open **Settings → Pages** and serve the `main` branch from `/ (root)`.
3. Create a deploy key so this repository's workflow can push there:

   ```bash
   ssh-keygen -t ed25519 -N "" -C "ros2inspector apt publish" -f apt-deploy-key
   ```

   Add `apt-deploy-key.pub` to `ros2inspector-apt` under **Settings → Deploy keys** with
   **Allow write access**. Add the private key `apt-deploy-key` to this repository as the
   Actions secret `APT_REPO_DEPLOY_KEY`.
4. Create a signing key without a passphrase, used only for this repository:

   ```bash
   export GNUPGHOME="$(mktemp -d)"
   gpg --batch --passphrase '' --quick-gen-key "ros2inspector apt repository <aminebensaid66@gmail.com>" ed25519 sign 5y
   gpg --armor --export-secret-keys > apt-signing-key.asc
   ```

   Add the contents of `apt-signing-key.asc` as the Actions secret `APT_SIGNING_KEY`, keep an
   offline backup, then delete both local key files. If the key is ever replaced, users must
   download the new public key again.
5. Optionally add protection rules to the `apt` environment that the publish job uses.

### Checking a release

After the workflow finishes, test the public repository on a clean machine or container with
the install commands in the README, then run `ros2inspector --version`.
