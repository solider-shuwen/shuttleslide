# Publishing to PyPI

This document is for maintainers who want to publish a new version of Shuttleslide to [PyPI](https://pypi.org/project/shuttleslide/). End users do **not** need this — they install via `pip install shuttleslide`.

## Prerequisites (one-time)

1. **PyPI account** — register at <https://pypi.org/account/register/>
2. **API token** — create one at <https://pypi.org/manage/account/token/> with scope "Entire account" (first release) or restricted to the `shuttleslide` project (subsequent releases).
3. **TestPyPI account** (optional but recommended for first-time publishers) — <https://test.pypi.org/account/register/>
4. **Build tools** on your machine:

   ```bash
   pip install --upgrade build twine
   ```

5. **2FA enabled** on your PyPI account (required to publish).

## Authenticate twine

You have two equivalent options:

### Option A — environment variables (recommended for CI)

```bash
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx   # your PyPI token
```

(On Windows PowerShell: `$env:TWINE_PASSWORD = "pypi-..."`.)

### Option B — `~/.pypirc`

```ini
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
username = __token__
password = pypi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

`~/.pypirc` should be `chmod 600`. Don't commit it.

## Build the wheel and sdist

```bash
# 1. From the repo root, with the shuttleslide conda env active
conda activate shuttleslide
pip install --upgrade build twine

# 2. Bump version (if needed) in:
#    - pyproject.toml        (version = "...")
#    - src/shuttleslide/__init__.py  (__version__ = "...")
#    - src/shuttleslide/cli.py       (version_option(version="..."))

# 3. Clean previous builds
rm -rf dist/ build/ src/shuttleslide.egg-info/

# 4. Build
python -m build

# 5. Sanity check
twine check dist/*
```

`dist/` will contain:

- `shuttleslide-<version>-py3-none-any.whl` — the wheel (binary distribution, what `pip` installs by default)
- `shuttleslide-<version>.tar.gz` — the sdist (source distribution, fallback)

`twine check` should report `PASSED` for both. If it warns about a missing README, ensure `readme = "README.md"` is set in `pyproject.toml`.

## Test on TestPyPI first (recommended)

```bash
# Upload to TestPyPI
twine upload --repository testpypi dist/*

# Install from TestPyPI in a clean environment
conda create -n test-install python=3.11 -y
conda activate test-install
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ shuttleslide

# Verify
slidecraft --version
slidecraft --help
python -c "from shuttleslide.pptx_to_html import PPTXParser; print('OK')"

# Test extras
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ "shuttleslide[ai,review]"
python -c "import fastapi, openai; print('extras OK')"
```

If anything fails here, fix it, bump the version, rebuild, retest. TestPyPI lets you iterate without polluting the real index.

## Publish to PyPI

```bash
twine upload dist/*
```

This uploads both the wheel and the sdist. PyPI cannot overwrite a version — every upload must be a new version number.

## Verify the release

Wait a few minutes for indexing, then:

```bash
# Fresh environment
conda create -n verify-install python=3.11 -y
conda activate verify-install
pip install shuttleslide
slidecraft --version          # prints the new version
pip show shuttleslide         # metadata, URLs, etc.
```

Open <https://pypi.org/project/shuttleslide/> and check:

- README renders correctly (badges, tables, code blocks)
- All metadata fields (license, classifiers, URLs) are populated
- The `slidecraft` console script is registered

## Tag the release in git

```bash
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin v0.1.0

# On GitHub, the tag will appear at github.com/solider-shuwen/shuttleslide/releases
# Add release notes summarizing changes, then "Publish release".
```

## Rollback / yank

You **cannot delete** a version from PyPI, but you can **yank** it:

```bash
pip install pep517
python -m pep517  # not actually needed — use the web UI:
```

Open <https://pypi.org/manage/project/shuttleslide/releases/> and click **Yank** on the broken version. Yanked versions still resolve for projects that pin them but won't be picked up by fresh `pip install` calls.

If the release is *catastrophically* broken (broken wheel, security issue), contact the PyPI admins at <https://github.com/pypi/support/issues> to request file deletion.

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `twine upload` → 403 Forbidden | Wrong token / token not scoped | Regenerate token, ensure `TWINE_USERNAME=__token__` |
| `InvalidDistribution: ... long description` | README has bad markup | `twine check dist/*` locally first |
| `slidecraft` command not found after install | `[project.scripts]` missing or wrong | Verify `slidecraft = "shuttleslide.cli:main"` in pyproject.toml |
| Wheel installs but templates missing | `package-data` not configured | Verify `[tool.setuptools.package-data]` includes the `agent/templates` and `agent/review/static` globs |
| Playwright Chromium missing at runtime | User hasn't run `playwright install` | Document it (README + cli-reference) — this is expected, not a bug |

## CI publishing (optional, future)

Once the manual flow is proven, automate via GitHub Actions using `pypa/gh-action-pypi-publish`. Trigger on git tag pushes, scope the PyPI token to the project, and store it as a repository secret.
