# Contributing

The workflow (packages, plans, reviews) is in [CLAUDE.md](CLAUDE.md); the commands are in the
[README](README.md#quick-start). This file holds the rules for dependencies.

## Dependencies

- **14-day rule.** A new or upgraded dependency, direct or transitive, uses its newest release
  that is at least 14 days old (supply-chain caution): a fresh release gets two weeks for a
  compromise to be found. Direct dependencies record it with a pin and a comment in
  `pyproject.toml`.
- **Upgrading the lock.** `uv lock --upgrade` (and `--upgrade-package NAME`) takes the newest
  release of everything it touches. Check the result with `uv lock --dry-run --upgrade` first and
  hold back what is too new with `uv lock --upgrade-package NAME==VERSION`. Transitive packages
  that the lock already holds back are listed in `[tool.uv] exclude-newer-package` with the span
  `"14 days"`, so an upgrade keeps them to releases at least 14 days old; uv re-evaluates the span
  on each new resolution and does not treat the lock as outdated while time passes. A global
  `exclude-newer = "14 days"` does not fit: it applies to every package, also to dependencies
  whose required minimum is younger than 14 days, and such a lock does not resolve.
- **yt-dlp, the exception.** The video platforms change their players without notice, and a
  pinned yt-dlp they broke stops the subtitles of a whole platform. Then yt-dlp (and with it
  `yt-dlp-ejs`, at the version that yt-dlp release asks for) is replaced by its newest stable
  release, however young, in a small PR of its own that says which platform broke and shows the
  fixed extraction (the live media set, `tests/live/urls/media.csv`). Its other dependencies
  keep the 14-day rule. youtube-transcript-api is not covered: it follows the rule.
- **The extra `asr`.** yt-dlp is installed without its `default` extra (it pulls in GPL
  `mutagen`); the ASR image takes `yt-dlp[deno]` and pins `yt-dlp-ejs`. PyAV's PyPI wheel bundles
  GPL `libx264` and `libx265`, which its metadata does not show: the ASR image builds PyAV from
  its sdist against a pinned LGPL FFmpeg (decision D5), and the worker refuses to start on any
  other build. Moving that FFmpeg pin means changing the release, version, series and both
  SHA-256 sums in the `Dockerfile` together.
- **Licences.** No GPL or AGPL dependency (CLAUDE.md rule 7). `scripts/check_licenses.py` checks
  the metadata of every installed distribution; CI runs it on the default environment, in the
  job `documents` on the environment with the extra `docling`, and in the job `media` on the one
  with the extra `asr`:

  ```bash
  uv run python scripts/check_licenses.py --lock uv.lock --extra browser
  ```

  LGPL and missing metadata are warnings. A false positive goes into
  `scripts/license-allowlist.toml` (`[[allow]]`, at its version) after a review; a package of
  `uv.lock` that Linux never installs (for example `pyreadline3` and `colorama`, Windows only)
  goes under `[[not_installed]]` with the licence from its PyPI metadata.
- **Torch.** The extra `docling` takes torch and torchvision from the PyTorch CPU index
  (`https://download.pytorch.org/whl/cpu`, a uv index with `explicit = true`), never the CUDA
  wheels.
