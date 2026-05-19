# Changelog

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
