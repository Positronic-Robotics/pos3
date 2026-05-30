# Changelog

## [0.3.1] - 2026-05-21

### Added
- `pos3` console-script entry point with `ls`, `download`, `upload` subcommands
  ([#10](https://github.com/Positronic-Robotics/pos3/issues/10)).
  - `pos3 ls <prefix> [-r] [--profile NAME]` lists objects, one full `s3://` URL
    per line on stdout.
  - `pos3 download <url> [--local PATH] [--delete] [--exclude PATTERN]... [--profile NAME]`
    prints only the resulting local path to stdout; progress and logs go to
    stderr, so `data_dir=$(pos3 download s3://bucket/dataset/)` is safe.
  - `pos3 upload <url> [--local PATH] [--delete] [--exclude PATTERN]... [--profile NAME]`
    is one-shot (no background loop or interval). Source defaults to the cache
    path `pos3 download` would have produced; errors if the source doesn't
    exist.
  - `--delete` defaults OFF for both `download` and `upload` (the Python API
    defaults to `True`; the CLI is more conservative for interactive use).
  - `--profile` is supported alongside the URL form `s3://<profile>@bucket/...`;
    the URL form wins on conflict, matching the Python precedence.
  - `-n` / `--dry-run` on `download` and `upload` prints the planned per-file
    actions to stdout (in `aws s3 sync --dryrun` style) and performs no
    transfers, no deletes, and no directory creation.
  - `download` and `upload` require an `s3://` URL. The Python API's
    local-path passthrough still works in code; the CLI rejects non-S3
    inputs with a clear error so a typo can't silently succeed. `ls` is
    unchanged and still accepts both forms.
- `pos3.TransferPlan` dataclass plus module-level `pos3.plan_download(remote, ...)`
  and `pos3.plan_upload(remote, ...)` wrappers: compute the set of
  `(source, destination)` copies and target deletes a real call would
  perform, without performing any of them. Same calling pattern as
  `pos3.download` / `pos3.upload` — use them inside a `with pos3.mirror():`
  block. The CLI's `-n` / `--dry-run` is implemented on top of these.
- `TransferError` and `TransferPlan` are now in `pos3.__all__`.

### Changed
- Per-object transfer failures now raise `pos3.TransferError` instead of
  being logged and swallowed. Previously, if any worker in a download or
  upload batch failed, the error was sent to the logger and the call
  returned normally — `data_dir = pos3.download(...)` could return a path
  to a partial cache, and `pos3 download` exited 0 after a failed S3 GET.
  Both now propagate. The new exception exposes `.operation` and
  `.failures` (list of the underlying per-worker exceptions). The CLI
  catches it and exits 1 with the failure on stderr. **Background
  interval syncs** (`upload(..., interval=N)`) are best-effort: a
  `TransferError` from one tick is logged and the daemon continues so
  the next interval can retry. Only the final sync on context exit and
  any one-shot call propagate. **Cleanup on error** — when the
  `mirror()` body is unwinding with an exception, a `TransferError` from
  the `sync_on_error=True` cleanup sync is logged but swallowed, so the
  original application exception remains the visible cause.
- `pos3.mirror()` no longer creates the cache root directory eagerly on
  context entry. The leaf directory is still created on demand when a
  file is actually downloaded, so the visible behavior of `download()` is
  unchanged. Dry-run and `plan_*` paths are now genuinely side-effect
  free on the local filesystem.

## [0.3.0] - 2026-05-19

### Added
- Explicit per-path S3 profile selection via the URL userinfo slot:
  `s3://<profile>@bucket/key` ([#8](https://github.com/Positronic-Robotics/pos3/issues/8)).
  A profile in the URL takes precedence over the `profile=` argument and works
  for every pos3-powered CLI without code or env-var changes.
- Name-keyed local profile registry auto-loaded from
  `~/.config/pos3/profiles.toml` (override with `POS3_PROFILES_FILE`).
  Non-secret config (`endpoint`/`region`/`local_name`/`public`) is kept
  separate from a secret `credentials_file`.
- Profiles with explicit credentials build their own isolated `boto3.Session`,
  never reading or mutating the user's ambient AWS configuration.
- Unknown profile names (in URL or argument) are a hard error with no silent
  fallback to the default credential chain.

### Changed
- **Python 3.11+ is now required** (`requires-python = ">=3.11"`). The TOML
  profile registry uses the standard-library `tomllib`; the previously
  declared 3.9/3.10 support was never exercised by CI.
- Profile logic (the `Profile` dataclass, registry loading, resolution, and
  client creation) moved into the internal `pos3.profiles` module. The public
  API (`pos3.Profile`, `pos3.register_profile`) is unchanged.

## [0.2.2] - 2026-02-06

### Fixed
- Fixed S3 key construction for directory markers being inconsistent across code paths. Consolidated all key building into a single `_make_s3_key(prefix, info)` helper, ensuring trailing `/` is always appended for directories. Previously, the upload-copies path relied on a separate fixup in `_put_to_s3`, while the delete path had its own inline fix — a pattern that caused the v0.2.1 bug.

## [0.2.1] - 2026-01-14

### Fixed
- Fixed S3 prefix matching bug where paths like `s3://bucket/data/` would incorrectly match adjacent paths like `s3://bucket/data_backup/`. The `_list_s3_objects()` function now ensures directory prefixes always end with `/` to prevent spurious matches at path boundaries.

## [0.2.0] - 2026-01-07

### Added
- Profile system for S3-compatible endpoints (MinIO, Nebius, etc.) ([#4](https://github.com/Positronic-Robotics/pos3/pull/4))
- `pos3.Profile` dataclass for endpoint configuration
- `pos3.register_profile()` for named profile registration
- `profile` parameter on `download()`, `upload()`, `sync()`, `ls()`
- `default_profile` parameter on `mirror()` and `with_mirror()`
- Support for anonymous/public bucket access via `public=True`
- Multiple profiles can be used simultaneously in the same context
- Cache path isolation per profile via `local_name`

## [0.1.0] - 2025-12-10

### Added
- Initial extraction of `pos3` from the `positronic` codebase as a standalone library.
- `pos3.mirror()` context manager for seamlessly syncing S3 files to local storage.
- Drop-in compatibility for third-party scripts (e.g., OpenCV, Pandas) that require local file paths.
- `pos3.download()`: Fetch S3 files to local cache with mirroring logic.
- `pos3.upload()`: Register local outputs for automatic background and exit-time synchronous upload.
- `pos3.sync()`: Bi-directional helper for resume-and-update workflows (download inputs -> run -> upload outputs).
- `pos3.ls()`: List files in S3 or local paths.
- Thread-safe, differential transfer logic (only syncs changes).
- CLI and Python API support.
