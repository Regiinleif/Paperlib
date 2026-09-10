# CI/CD for PaperLib

A beginner-friendly guide to the automated pipeline that tests PaperLib on
every push. This document describes *this* project's actual setup, defined in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

---

## 1. What is CI/CD?

**CI** stands for **Continuous Integration**. The idea is simple: every time
someone pushes code, a robot automatically builds the project and runs the
tests. You don't have to remember to do it, and you don't have to trust that
"it works on my machine." The robot checks *every* change.

Why it matters:

- **Catch breakage early.** If a change breaks something, you find out within
  minutes, not weeks later when a user hits the bug.
- **Every push is tested the same way.** The tests run in a clean, predictable
  environment, so results don't depend on what happens to be installed on your
  laptop.
- **Confidence to change code.** When the tests are green, you know the core
  logic still works.

**CD** stands for **Continuous Delivery** (or **Continuous Deployment**). This
is the next step *after* the tests pass: automatically packaging and shipping
the software, for example building an installer or publishing a release. The
"delivery" flavor prepares a release for a human to click "ship"; the
"deployment" flavor ships it automatically.

> PaperLib now has **both halves**. CI runs the test suite on every push and
> pull request. CD then builds the web app into a Docker image, pushes it to
> the GitHub Container Registry (ghcr.io), and — on pushes to `main` —
> automatically deploys that image to Azure Container Apps. See
> [Section 5](#5-the-cd-half-build--deploy) for how the build-and-deploy jobs
> work.

---

## 2. This project's pipeline

The whole pipeline lives in one file: `.github/workflows/ci.yml`. GitHub reads
this file and runs it for you. Let's walk through it top to bottom.

### The name

```yaml
name: CI
```

This is the label you'll see in GitHub's **Actions** tab. Our pipeline is
simply called **CI**.

### When it runs (the `on:` triggers)

```yaml
on:
  push:
    branches: [ main ]        # on every push to main
  pull_request:               # and on every pull request
  workflow_dispatch:          # and a manual "Run workflow" button in the UI
```

Three things can trigger a run:

1. **`push` to `main`** — every time code lands on the `main` branch.
2. **`pull_request`** — every time someone opens or updates a pull request, so
   you can see if a change is safe *before* it's merged.
3. **`workflow_dispatch`** — a manual "Run workflow" button in the GitHub UI,
   for when you just want to kick off a run yourself.

### Where it runs

```yaml
jobs:
  test:
    name: Run tests
    runs-on: windows-latest
```

The job is named **Run tests** and it runs on `windows-latest` — a fresh
Windows machine provided by GitHub.

**Why Windows?** PaperLib is a **Tkinter desktop app built for Windows**. Its
target operating system is Windows, so it makes sense to test on the same OS
its users will run it on. (Many projects test on Linux because that's cheapest,
but here Windows matches reality.)

### Testing multiple Python versions (the matrix)

```yaml
strategy:
  fail-fast: false
  matrix:
    python-version: ["3.12", "3.13"]
```

A **matrix** tells GitHub to run the whole job more than once — here, once for
**Python 3.12** and once for **Python 3.13**. This catches bugs that only
appear on a specific Python version.

`fail-fast: false` means: if the 3.12 run fails, don't cancel the 3.13 run.
Let both finish so you can see the full picture.

### The steps (in order)

The job runs these five steps, top to bottom. The names below are quoted
exactly from the file.

**Step 1 — "Check out the repository"**

```yaml
- name: Check out the repository
  uses: actions/checkout@v4
```

Downloads (checks out) the project's code onto the runner. Nothing can happen
until the code is actually there.

**Step 2 — "Set up Python ${{ matrix.python-version }}"**

```yaml
- name: Set up Python ${{ matrix.python-version }}
  uses: actions/setup-python@v5
  with:
    python-version: ${{ matrix.python-version }}
    cache: pip
```

Installs the Python version for this matrix run. `cache: pip` saves downloaded
packages between runs so future installs are faster.

**Step 3 — "Install dependencies"**

```yaml
- name: Install dependencies
  run: |
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    pip install pytest
```

Upgrades `pip`, then installs the app's dependencies from `requirements.txt`
(that's `anthropic`, `pypdf`, and `tkinterdnd2`), and finally installs
`pytest`, the tool that runs the tests. Note `pytest` is installed here rather
than being listed in `requirements.txt`, because it's only needed for testing,
not for running the app.

**Step 4 — "Syntax check"**

```yaml
- name: Syntax check
  run: python -m compileall paperlib run.py
```

A quick gate before the real tests: `compileall` tries to compile every source
file in the `paperlib` package and `run.py`. If there's a typo or a syntax
error anywhere, this fails immediately with a clear message — no need to run
the whole suite to discover a stray missing colon.

**Step 5 — "Run pytest"**

```yaml
- name: Run pytest
  run: pytest
```

Runs the test suite. `pytest` reads its settings from `pytest.ini`:

```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -v
```

So it looks in the `tests/` folder, collects every `test_*.py` file, and runs
verbosely (`-v`). The suite is **18 tests** spread across three files:

- `tests/test_extract.py` — 7 tests (PDF text extraction logic)
- `tests/test_rag.py` — 6 tests (retrieval / RAG logic)
- `tests/test_library.py` — 5 tests (library management logic)

If all 18 pass, the job goes green.

---

## 3. Why the tests need no API key

PaperLib uses the Anthropic Claude API for its RAG chat feature, which normally
requires a secret API key. So you might expect CI to need that key configured
as a GitHub secret — but it doesn't.

The reason: the **test suite only exercises logic**, never the live API. The
tests import and check `paperlib`'s own functions — text extraction, the RAG
retrieval logic, and library management — none of which make a network call to
Anthropic. You can confirm this yourself: the test files import only
`paperlib` modules (`extract`, `rag`, `config`, `library`) and `pytest`, and
never construct an Anthropic client or read an API key.

Because nothing in the tests calls out to a paid, authenticated service, CI
needs **no secrets at all**. This keeps the pipeline simple, fast, free, and
safe to run on every pull request (including from outside contributors, who
should never see your secrets).

---

## 4. How to watch it

### On GitHub (the web UI)

Go to the repository on GitHub and click the **Actions** tab. You'll see a list
of runs, each labeled **CI** (our `name:`). Click any run to expand its jobs
and steps.

- A **green check** ✅ means everything passed.
- A **red X** ❌ means something failed — click into the run, open the failing
  step, and read the log to see exactly what broke.

Because we use a matrix, each run shows two jobs: one for Python 3.12 and one
for Python 3.13.

### From the command line (GitHub CLI)

If you have the [GitHub CLI](https://cli.github.com/) (`gh`) installed, you can
watch runs without leaving your terminal:

```bash
gh run list          # show recent runs and their status
gh run watch         # follow the latest run live until it finishes
gh run view --log    # print the full logs of a run
```

`gh run list` is the CLI equivalent of glancing at the Actions tab;
`gh run watch` streams progress as the pipeline goes.

---

## 5. The CD half (build & deploy)

After the `test` job goes green, two more jobs run the "delivery" half of the
pipeline.

### The `docker` job — build & push the image

`needs: test` ties it to the tests, so it runs **only if the tests passed**. It
builds the web app into a Docker image and, **on pushes to `main` only** (not on
pull requests, so a fork's PR can never publish), pushes it to the GitHub
Container Registry at `ghcr.io/<owner>/paperlib`. Each image is tagged both
`latest` and `sha-<short commit>` (e.g. `sha-55b6630`), so every commit has a
uniquely addressable image.

### The `deploy` job — ship it to Azure

`needs: docker` and `if: github.event_name == 'push'`, so it runs only after a
successful build on `main`. It logs in to Azure using the `AZURE_CREDENTIALS`
secret (a service principal scoped to the `paperlib` resource group), then runs
`az containerapp update` to point the live app at the image tagged with this
commit's `sha-<short>`. Azure already holds the ghcr.io pull credential and the
app's environment + Key Vault config, so only the image reference changes.

The net effect: **push to `main` → tests → image built → live site updated**,
with no manual step. The manual fallback still works if you ever need it:

```bash
az containerapp update -n paperlib -g paperlib \
  --image ghcr.io/<owner>/paperlib:sha-$(git rev-parse --short HEAD)
```

---

## 6. Ideas to extend it further

Natural next steps as you learn more — both strengthen CI.

### Add linting (e.g. ruff)

A linter catches style problems and likely bugs (unused imports, undefined
names) without running the code. You could add a step before the tests:

```yaml
- name: Lint with ruff
  run: |
    pip install ruff
    ruff check paperlib run.py
```

### Add a coverage report

Coverage measures *how much* of your code the tests actually run. This shows
you which parts are untested:

```yaml
- name: Run tests with coverage
  run: |
    pip install pytest-cov
    pytest --cov=paperlib --cov-report=term-missing
```

### Package the desktop client as an `.exe`

The deployed web app covers the server side. For the legacy Tkinter desktop
client, a release job could bundle it into a single `.exe` with PyInstaller and
attach it to a GitHub Release, typically triggered only on a version tag (like
`v1.0.0`) so releases are deliberate:

```yaml
on:
  push:
    tags: [ 'v*' ]
```

---

*This document reflects `.github/workflows/ci.yml` as it exists in the repo.
If you change the pipeline, update this guide to match.*
