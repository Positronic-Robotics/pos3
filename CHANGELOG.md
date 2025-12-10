# Changelog

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
