# pos3

**PO**sitronic **S3** — Make using S3 as simple as using local files.

`pos3` provides a Pythonic context manager for syncing directories and files with S3. It is designed for data processing pipelines and machine learning workflows where you want to work with local files but persist data in S3.

## Core Concepts

- **Context Manager**: All operations run within a `with pos3.mirror():` block.
    - **Enter**: Initializes the sync environment (threads, cache).
    - **Body**: You explicitly call `pos3.download()` to fetch files and `pos3.upload()` to register outputs.
    - **Exit**: Uploads registered output paths (mirroring local to S3).
- **Lazy & Efficient**: Only transfers files that have changed (based on size/presence).
- **Local Paths**: All API calls return a `pathlib.Path` to the local file/directory. If you pass a local path instead of an S3 URL, it is passed through unchanged (no copy).
- **Background Sync**: Can optionally upload changes in the background (e.g., every 60s) for long-running jobs.

## Quick Start

The primary API is the `pos3.mirror()` context manager.

```python
import pos3

# 1. Start the context
with pos3.mirror(cache_root='~/.cache/positronic/s3'):

    # 2. Download Input
    #    - Downloads s3://bucket/data to cache
    #    - Deletes local files that don't exist in S3 (mirroring)
    #    - Returns local Path object
    dataset_path = pos3.download('s3://bucket/data')

    # 3. Sync Output (Resume & Upload)
    #    - Downloads existing checkpoints (to resume)
    #    - Registers path for background uploads
    checkpoints_path = pos3.sync('s3://bucket/ckpt', interval=60, delete_remote=False)

    # 4. Upload Logs (Write-only)
    #    - Creates local directory
    #    - Uploads new files to S3 on exit/interval
    logs_path = pos3.upload('s3://bucket/logs', interval=30)

    # 5. Use standard local file paths
    print(f"Reading from {dataset_path}")      # -> ~/.cache/positronic/s3/bucket/data
    print(f"Writing to {checkpoints_path}")    # -> ~/.cache/positronic/s3/bucket/ckpt
    print(f"Logging to {logs_path}")           # -> ~/.cache/positronic/s3/bucket/logs

    train(dataset_path, checkpoints_path, logs_path)
```

## API Guide

> **Note**: All operational methods (`download`, `upload`, `sync`, `ls`) must be called within an active `pos3.mirror()` context. Calling them outside will raise a `RuntimeError`.

### `pos3.mirror(...)` / `@pos3.with_mirror(...)`

Context manager (or decorator) that activates the sync environment.

**Parameters:**
- `cache_root` (default: `'~/.cache/positronic/s3/'`): Base directory for caching downloaded files.
- `show_progress` (default: `True`): Display tqdm progress bars.
- `max_workers` (default: `10`): Threads for parallel S3 operations.

**Decorator Example:**

```python
@pos3.with_mirror(cache_root='/tmp/cache')
def main():
    # Only works when called!
    data_path = pos3.download('s3://bucket/data')
    train(data_path)

if __name__ == "__main__":
    main()
```

### `pos3.download(remote, local=None, delete=True, exclude=None)`

Registers a path for download. Ensures local copy matches S3 immediately.
- `remote`: S3 URL (e.g., `s3://bucket/key`) or local path.
- `local`: Explicit local destination. Defaults to standard cache path.
- `delete`: If `True` (default), deletes local files NOT in S3 ("mirror" behavior).
- `exclude`: List of glob patterns to skip.

**Returns**: `pathlib.Path` to the local directory/file.

### `pos3.upload(remote, local=None, interval=300, delete=True, sync_on_error=False, exclude=None)`

Registers a local path for upload. Uploads on exit and optionally in background.
- `remote`: Destination S3 URL.
- `local`: Local source path. Auto-resolved from cache path if `None`.
- `interval`: Seconds between background syncs. `None` for exit-only.
- `delete`: If `True` (default), deletes S3 files NOT present locally.
- `sync_on_error`: If `True`, syncs even if the context exits with an exception.

**Returns**: `pathlib.Path` to the local directory/file.

### `pos3.sync(remote, local=None, interval=300, delete_local=True, delete_remote=True, sync_on_error=False, exclude=None)`

Bi-directional helper. Performs `download()` then registers `upload()`. Useful for jobs that work on existing files, like when you resume training from a checkpoint.
- `delete_local`: Cleanup local files during download.
- `delete_remote`: Cleanup remote files during upload. carefully consider setting to `False` when resuming jobs to avoid deleting history.

**Returns**: `pathlib.Path` to the local directory/file.

### `pos3.ls(prefix, recursive=False)`

Lists files/objects in a directory or S3 prefix.
- `prefix`: S3 URL or local path.
- `recursive`: List subdirectories if `True`.

**Returns**: List of full S3 URLs or local paths.

## Comparison with Libraries

Why use `pos3` instead of other Python libraries?

| Feature | `pos3` | `boto3` | `s3fs` / `fsspec` |
| :--- | :--- | :--- | :--- |
| **Abstraction Level** | **High** (Context Manager) | **Low** (API Client) | **Medium** (File System) |
| **Sync Logic** | **Built-in** (Differential) | Manual Implementation | `put`/`get` (Recursive) |
| **Lifecycle** | **Automated** (Open/Close) | Manual | Manual |
| **Background Upload** | **Yes** (Non-blocking) | Manual Threading | No (Blocking) |
| **Local I/O Speed** | **Native** (SSD) | Native | Network Bound (Virtual FS) |
| **Use Case** | **ML / Pipelines** | App Development | DataFrames / Interactive |

- **vs `boto3`**: `boto3` is the raw AWS SDK. `pos3` wraps it to provide "mirroring" logic, threading, and diffing out of the box.
- **vs `s3fs`**: `s3fs` treats S3 as a filesystem. `pos3` treats S3 as a persistence layer for your high-speed local storage, ensuring you always get native IO performance.
