# Releasing

Releases are GitHub Releases cut from `vMAJOR.MINOR.PATCH` tags. Pushing such
a tag runs [`.github/workflows/release.yml`](../.github/workflows/release.yml),
which publishes the release; nothing is built or uploaded by hand.

## The version

`version` in `pyproject.toml` is the single source of truth.

- The engine reads it from the installed package's metadata
  (`better_captions.__version__`, `better-captions --version`, `doctor`); a
  source tree that isn't installed reports `0+unknown`.
- The Resolve plugin runs on Resolve's own Python without the package
  installed, so `resolve_plugin/src/better_captions_plugin/__init__.py` keeps a
  copy in `__version__`. It must be bumped together with `pyproject.toml`;
  `tests/test_release.py` and the release workflow both fail when they differ.

Versions follow [semantic versioning](https://semver.org): bump PATCH for
fixes, MINOR for new features, MAJOR for changes that break existing installs,
settings or the engine/plugin contract.

## Cutting a release

1. On an up-to-date `main` with green CI, bump the version in both places:
   - `pyproject.toml`: `version = "0.2.0"`
   - `resolve_plugin/src/better_captions_plugin/__init__.py`: `__version__ = "0.2.0"`
2. Refresh the lockfile and run the release checks:

   ```sh
   uv lock
   uv run pytest tests/test_release.py -q
   uv run python .github/scripts/check_release_version.py v0.2.0
   ```

3. Commit and push: `git commit -am "Release 0.2.0" && git push`.
4. Tag that commit and push the tag:

   ```sh
   git tag -a v0.2.0 -m "Better Captions 0.2.0"
   git push origin v0.2.0
   ```

5. Watch the **Release** workflow in the Actions tab. It:
   1. runs the full CI workflow (`ci.yml`: tests on Linux, macOS and Windows,
      the Windows installer end to end, the package build);
   2. checks that the tag is `v` + the `pyproject.toml` version (and that the
      plugin's copy matches), failing before anything is published if not;
   3. builds the wheel and sdist with `uv build`, plus a source zip
      (`git archive`);
   4. writes release notes from the commit subjects since the previous
      `v*.*.*` tag (merge commits left out);
   5. creates the GitHub Release `Better Captions 0.2.0` with the wheel,
      sdist, source zip, `install.sh` and `install.ps1` attached.

Once published, the installers pick it up: with no `BETTER_CAPTIONS_REF` set
they ask GitHub's `releases/latest` API for the newest release and install
that tag. Users can still pin a release (`BETTER_CAPTIONS_REF=v0.1.0`), a
commit SHA, or track `main` (`BETTER_CAPTIONS_REF=main`). If there is no
release yet, or the API fails or is rate-limited, they warn and install `main`.

## If something goes wrong

- **The workflow failed before "Create GitHub Release"**: nothing was
  published. Fix the problem on `main`, then move the tag to the fixed commit:

  ```sh
  git tag -d v0.2.0 && git push origin :refs/tags/v0.2.0
  git tag -a v0.2.0 -m "Better Captions 0.2.0" && git push origin v0.2.0
  ```

- **The tag doesn't match the version**: the "Check the tag matches the
  package version" step says what it expected. Either bump the version and
  re-tag as above, or delete the wrong tag.
- **A bad release was published**: don't reuse its version. Delete it or mark
  it as a pre-release on GitHub (the installers then skip it, since
  `releases/latest` ignores pre-releases and drafts), and release a new PATCH
  version with the fix.
