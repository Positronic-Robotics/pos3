# Changelog

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
