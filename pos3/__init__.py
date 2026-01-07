"""Global S3 mirror API with dynamic registration."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from tqdm import tqdm

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Profile:
    """Configuration for an S3-compatible endpoint.

    Attributes:
        local_name: Identifier used in cache path (e.g., 'nebius'). Cannot be '_' (reserved).
        endpoint: S3 endpoint URL (e.g., 'https://storage.eu-north1.nebius.cloud').
        public: If True, use anonymous access (no credentials required).
        region: Optional AWS region name.
    """

    local_name: str
    endpoint: str
    public: bool = False
    region: str | None = None

    def __post_init__(self):
        if self.local_name == "_":
            raise ValueError("Profile local_name cannot be '_' (reserved for default)")
        if not self.local_name or not all(c.isalnum() or c in "-_" for c in self.local_name):
            raise ValueError(f"Invalid local_name '{self.local_name}': use only alphanumeric, dash, underscore")


_PROFILES: dict[str, Profile] = {}


def register_profile(
    name: str,
    endpoint: str,
    public: bool = False,
    region: str | None = None,
    local_name: str | None = None,
) -> None:
    """Register a named profile for S3 access.

    Creates a Profile with the given parameters. See Profile class for field details.
    The `local_name` defaults to the profile `name` if not specified.
    """
    config = Profile(local_name=local_name or name, endpoint=endpoint, public=public, region=region)
    existing = _PROFILES.get(name)
    if existing is not None and existing != config:
        raise ValueError(f"Profile '{name}' already registered with different config")
    _PROFILES[name] = config


def _resolve_profile(profile: str | Profile | None) -> Profile | None:
    """Resolve a profile name to a Profile object.

    Args:
        profile: None, registered profile name (string), or Profile object.

    Returns:
        Profile object or None.

    Raises:
        ValueError: If profile is a string that is not registered.
    """
    if profile is None or isinstance(profile, Profile):
        return profile
    if profile not in _PROFILES:
        raise ValueError(f"Unknown profile: '{profile}'. Register with pos3.register_profile() first.")
    return _PROFILES[profile]


def _create_s3_client(profile: Profile | None = None):
    """Create boto3 S3 client, optionally using a profile.

    Args:
        profile: None (use boto3 defaults) or Profile config.
    """
    if profile is None:
        return boto3.client("s3")

    kwargs: dict[str, Any] = {"endpoint_url": profile.endpoint}

    if profile.region:
        kwargs["region_name"] = profile.region

    if profile.public:
        kwargs["config"] = Config(signature_version=UNSIGNED)

    return boto3.client("s3", **kwargs)


class _NullTqdm(nullcontext):
    def update(self, *_args: Any, **_kwargs: Any) -> None:  # pragma: no cover - trivial
        pass

    def __enter__(self) -> _NullTqdm:
        return self


def _parse_s3_url(s3_url: str) -> tuple[str, str]:
    parsed = urlparse(s3_url)
    if parsed.scheme != "s3":
        raise ValueError(f"Not an S3 URL: {s3_url}")
    return parsed.netloc, parsed.path.lstrip("/")


def _normalize_s3_url(s3_url: str) -> str:
    bucket, key = _parse_s3_url(s3_url)
    key = key.strip("/")
    return f"s3://{bucket}/{key}" if key else f"s3://{bucket}"


def _is_s3_path(path: str) -> bool:
    return path.startswith("s3://")


def _s3_paths_conflict(left: str, right: str) -> bool:
    left_norm = left.rstrip("/")
    right_norm = right.rstrip("/")
    if left_norm == right_norm:
        return True
    return left_norm.startswith(right_norm + "/") or right_norm.startswith(left_norm + "/")


def _process_futures(futures, operation: str) -> None:
    for future in futures:
        try:
            future.result()
        except Exception as exc:
            logger.error("%s failed: %s", operation, exc)


@dataclass(frozen=True)
class FileInfo:
    """Represents a file or directory with metadata for sync operations."""

    relative_path: str  # Relative path from root (empty string for root file/dir)
    size: int  # File size in bytes, 0 for directories
    is_dir: bool  # True if this represents a directory


def _scan_local(path: Path) -> Iterator[FileInfo]:
    if not path.exists():
        return

    base = path
    stack = [path]
    # path => relative
    while stack:
        p = stack.pop()
        if p.is_symlink():
            continue

        relative = p.relative_to(base).as_posix() if p != base else ""
        if p.is_dir():
            # Always yield directories, including the root (relative_path='')
            yield FileInfo(relative_path=relative, size=0, is_dir=True)
            stack.extend(p.iterdir())
        else:
            yield FileInfo(relative_path=relative, size=p.stat().st_size, is_dir=False)


def _filter_fileinfo(fileinfo_iter: Iterator[FileInfo], exclude: list[str] | None) -> Iterator[FileInfo]:
    """
    Filter FileInfo objects based on glob patterns.

    Args:
        fileinfo_iter: Iterator of FileInfo objects to filter
        exclude: List of glob patterns to exclude (supports *, **, ?, [...])

    Yields:
        FileInfo objects that don't match any exclude patterns
    """
    if not exclude:
        yield from fileinfo_iter
        return
    excluded_dirs: set[str] = set()

    for info in fileinfo_iter:
        # Skip if any parent directory was excluded
        if any(info.relative_path == ed or info.relative_path.startswith(ed + "/") for ed in excluded_dirs):
            continue

        # Check if this path matches any exclude pattern
        path = PurePosixPath(info.relative_path) if info.relative_path else PurePosixPath(".")
        if any(path.match(pattern) for pattern in exclude):
            if info.is_dir:
                excluded_dirs.add(info.relative_path)
        else:
            yield info


def _compute_sync_diff(source: Iterator[FileInfo], target: Iterator[FileInfo]) -> tuple[list[FileInfo], list[FileInfo]]:
    source_map: dict[str, FileInfo] = {info.relative_path: info for info in source}
    target_map: dict[str, FileInfo] = {info.relative_path: info for info in target}

    to_copy, to_delete = [], []

    for relative_path, source_info in source_map.items():
        target_info = target_map.get(relative_path)

        if target_info is None:
            to_copy.append(source_info)
        elif source_info.is_dir != target_info.is_dir:
            to_delete.append(target_info)
            to_copy.append(source_info)
        elif not source_info.is_dir and source_info.size != target_info.size:
            to_copy.append(source_info)

    for relative_path, target_info in target_map.items():
        if relative_path not in source_map:
            to_delete.append(target_info)

    return to_copy, to_delete


@dataclass
class _Options:
    cache_root: str = "~/.cache/positronic/s3/"
    show_progress: bool = True
    max_workers: int = 10
    default_profile: Profile | None = None

    def cache_path_for(self, remote: str, profile: Profile | None = None) -> Path:
        bucket, key = _parse_s3_url(remote)
        cache_root = Path(self.cache_root).expanduser().resolve()
        local_name = profile.local_name if profile else "_"
        return cache_root / local_name / bucket / key


@dataclass
class _DownloadRegistration:
    remote: str
    local_path: Path
    delete: bool
    exclude: list[str] | None
    profile: Profile | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    error: Exception | None = None

    def __eq__(self, other):
        if not isinstance(other, _DownloadRegistration):
            return False
        return (
            self.remote == other.remote
            and self.local_path == other.local_path
            and self.delete == other.delete
            and self.exclude == other.exclude
            and self.profile == other.profile
        )


@dataclass
class _UploadRegistration:
    remote: str
    local_path: Path
    interval: int | None
    delete: bool
    sync_on_error: bool
    exclude: list[str] | None
    profile: Profile | None = None
    last_sync: float = 0.0

    def __eq__(self, other):
        if not isinstance(other, _UploadRegistration):
            return False
        return (
            self.remote == other.remote
            and self.local_path == other.local_path
            and self.interval == other.interval
            and self.delete == other.delete
            and self.sync_on_error == other.sync_on_error
            and self.exclude == other.exclude
            and self.profile == other.profile
        )


_ACTIVE_MIRROR: ContextVar[_Mirror | None] = ContextVar("_ACTIVE_MIRROR", default=None)
_GLOBAL_MIRROR_LOCK = threading.RLock()
_GLOBAL_ACTIVE_MIRROR: _Mirror | None = None


class _Mirror:
    def __init__(self, options: _Options):
        self.options = options
        self.cache_root = Path(self.options.cache_root).expanduser().resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)

        self._default_profile = options.default_profile
        self._clients: dict[Profile | None, Any] = {}

        self._downloads: dict[tuple[str, Profile | None], _DownloadRegistration] = {}
        self._uploads: dict[tuple[str, Profile | None], _UploadRegistration] = {}
        self._lock = threading.RLock()

        self._stop_event: threading.Event | None = None
        self._sync_thread: threading.Thread | None = None

    def _effective_profile(self, profile: str | Profile | None) -> Profile | None:
        """Resolve profile name and substitute default if None."""
        resolved = _resolve_profile(profile)
        return resolved if resolved is not None else self._default_profile

    def _get_client(self, profile: Profile | None = None) -> Any:
        """Get or create S3 client for the given profile."""
        effective_profile = profile if profile is not None else self._default_profile
        if effective_profile not in self._clients:
            self._clients[effective_profile] = _create_s3_client(effective_profile)
        return self._clients[effective_profile]

    @property
    def running(self) -> bool:
        return self._stop_event is not None

    def start(self) -> None:
        if not self.running:
            self._stop_event = threading.Event()

    def stop(self, had_error: bool = False) -> None:
        if self.running:
            self._stop_event.set()

            if self._sync_thread:
                self._sync_thread.join(timeout=60)
                self._sync_thread = None
            self._final_sync(had_error=had_error)
            self._stop_event = None

    def download(
        self,
        remote: str,
        local: str | Path | None,
        delete: bool,
        exclude: list[str] | None = None,
        profile: str | Profile | None = None,
    ) -> Path:
        """
        Register (and perform if needed) a download from a remote S3 bucket path to a local directory or file.

        Args:
            remote (str): Source S3 URL (e.g., "s3://bucket/key/prefix") or local path.
                If a local path is provided, it is validated and returned directly.
            local (str | Path | None): Local directory or file destination. If None, uses cache path from options.
            delete (bool): If True, deletes local files not present in S3.
            exclude (list[str] | None): List of glob patterns to exclude from download.
            profile: S3 profile name or Profile config for custom endpoints.

        Returns:
            Path: The canonical local path associated with this download registration.

        Raises:
            FileNotFoundError: If remote is a local path that does not exist.
            ValueError: If download registration conflicts with an existing download or upload or parameters differ.
        """
        effective_profile = self._effective_profile(profile)

        if not _is_s3_path(remote):
            path = Path(remote).expanduser().resolve()
            return path

        normalized = _normalize_s3_url(remote)
        local_path = (
            self.options.cache_path_for(remote, effective_profile)
            if local is None
            else Path(local).expanduser().resolve()
        )
        new_registration = _DownloadRegistration(
            remote=normalized, local_path=local_path, delete=delete, exclude=exclude, profile=effective_profile
        )

        with self._lock:
            reg_key = (normalized, effective_profile)
            existing = self._downloads.get(reg_key)
            if existing:
                if existing != new_registration:
                    raise ValueError(f"Download for '{normalized}' already registered with different parameters")
                registration = existing
                need_download = False
            else:
                self._check_download_conflicts(normalized, effective_profile)
                self._downloads[reg_key] = new_registration
                registration = new_registration
                need_download = True

        if need_download:
            try:
                self._perform_download(normalized, local_path, delete, exclude, effective_profile)
            except Exception as exc:
                registration.error = exc
                registration.ready.set()
                with self._lock:
                    self._downloads.pop(reg_key, None)
                raise
            else:
                registration.ready.set()
        else:
            registration.ready.wait()
            if registration.error is not None:
                raise registration.error

        return local_path

    def upload(
        self,
        remote,
        local,
        interval,
        delete,
        sync_on_error,
        exclude: list[str] | None = None,
        profile: str | Profile | None = None,
    ) -> Path:
        """
        Register (and perform if needed) an upload from a local directory or file to a remote S3 bucket path.

        Args:
            remote (str): Destination S3 URL (e.g., "s3://bucket/key/prefix")
            local (str | Path | None): Local directory or file to upload. If None, determines default from options.
            interval (int | None): If set, enables periodic background uploads (seconds between syncs).
            delete (bool): If True, deletes remote files not present locally.
            sync_on_error (bool): If True, attempts to sync files even when encountering errors.
            exclude (list[str] | None): List of glob patterns to exclude from upload.
            profile: S3 profile name or Profile config for custom endpoints.

        Returns:
            Path: The canonical local path associated with this upload registration.

        Raises:
            ValueError: If upload registration conflicts with an existing download or upload or parameters differ.
        """
        effective_profile = self._effective_profile(profile)

        if not _is_s3_path(remote):
            path = Path(remote).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            return path

        normalized = _normalize_s3_url(remote)
        local_path = (
            self.options.cache_path_for(remote, effective_profile)
            if local is None
            else Path(local).expanduser().resolve()
        )

        new_registration = _UploadRegistration(
            remote=normalized,
            local_path=local_path,
            interval=interval,
            delete=delete,
            sync_on_error=sync_on_error,
            exclude=exclude,
            profile=effective_profile,
            last_sync=0,
        )

        with self._lock:
            reg_key = (normalized, effective_profile)
            existing = self._uploads.get(reg_key)
            if existing:
                if existing != new_registration:
                    raise ValueError(f"Upload for '{normalized}' already registered with different parameters")
                return existing.local_path

            self._check_upload_conflicts(new_registration)
            self._uploads[reg_key] = new_registration
            if interval is not None:
                self._ensure_background_thread_unlocked()

        return local_path

    def sync(
        self,
        remote: str,
        local: str | Path | None,
        interval: int | None,
        delete_local: bool,
        delete_remote: bool,
        sync_on_error: bool,
        exclude: list[str] | None = None,
        profile: str | Profile | None = None,
    ) -> Path:
        # Let download() and upload() handle profile resolution and normalization
        local_path = self.download(remote, local, delete_local, exclude, profile)
        if not _is_s3_path(remote):
            return local_path

        normalized = _normalize_s3_url(remote)
        effective_profile = self._effective_profile(profile)
        # Unregister the download to allow upload registration for the same remote
        self._downloads.pop((normalized, effective_profile), None)
        return self.upload(remote, local_path, interval, delete_remote, sync_on_error, exclude, profile)

    def ls(self, prefix: str, recursive: bool = False, profile: str | Profile | None = None) -> list[str]:
        """Lists objects under the given prefix, working for both local directories and S3 prefixes."""
        effective_profile = self._effective_profile(profile)

        if _is_s3_path(prefix):
            normalized = _normalize_s3_url(prefix)
            bucket, key = _parse_s3_url(normalized)
            # Ensure directory-like listing by appending '/' to avoid spurious prefix matches
            if key:
                key = key + "/"
            items = []
            for info in self._scan_s3(bucket, key, effective_profile):
                if info.relative_path:
                    # Skip nested items if not recursive
                    if not recursive and "/" in info.relative_path:
                        continue
                    # Reconstruct the full S3 key
                    if key:
                        s3_key = key.rstrip("/") + "/" + info.relative_path
                    else:
                        s3_key = info.relative_path
                    items.append(f"s3://{bucket}/{s3_key}")
            return items
        else:
            display_path = Path(prefix).expanduser()
            scan_path = display_path.resolve()
            items = []
            for info in _scan_local(scan_path):
                if info.relative_path:
                    # Skip nested items if not recursive
                    if not recursive and "/" in info.relative_path:
                        continue
                    items.append(str(display_path.joinpath(Path(info.relative_path))))
            return items

    def _check_download_conflicts(self, candidate: str, profile: Profile | None) -> None:
        for (upload_remote, upload_profile), _reg in self._uploads.items():
            if upload_profile == profile and _s3_paths_conflict(candidate, upload_remote):
                raise ValueError(f"Conflict: download '{candidate}' overlaps with upload '{upload_remote}'")

    def _check_upload_conflicts(self, new_registration) -> None:
        candidate = new_registration.remote
        candidate_profile = new_registration.profile
        for (download_remote, download_profile), _reg in self._downloads.items():
            if download_profile == candidate_profile and _s3_paths_conflict(candidate, download_remote):
                raise ValueError(f"Conflict: upload '{candidate}' overlaps with download '{download_remote}'")
        for (upload_remote, upload_profile), reg in self._uploads.items():
            if upload_profile == candidate_profile and _s3_paths_conflict(candidate, upload_remote):
                same_remote = candidate == upload_remote
                if not same_remote or reg != new_registration:
                    raise ValueError(f"Conflict: upload '{candidate}' overlaps with upload '{upload_remote}'")

    def _ensure_background_thread_unlocked(self) -> None:
        assert self.running, "The mirror must be started before performing any uploads"
        if not self._sync_thread or not self._sync_thread.is_alive():
            thread = threading.Thread(target=self._background_worker, name="positronic-s3-sync", daemon=True)
            thread.start()
            self._sync_thread = thread

    def _background_worker(self) -> None:
        while not self._stop_event.wait(1):
            now = time.monotonic()
            due: list[_UploadRegistration] = []
            with self._lock:
                for registration in self._uploads.values():
                    if registration.interval is not None and now - registration.last_sync >= registration.interval:
                        registration.last_sync = now
                        due.append(registration)

            self._sync_uploads(due)

    def _final_sync(self, had_error: bool = False) -> None:
        with self._lock:
            uploads = list(self._uploads.values())
        if had_error:
            uploads = [u for u in uploads if u.sync_on_error]
        self._sync_uploads(uploads)

    def _sync_uploads(self, registrations: Iterable[_UploadRegistration]) -> None:
        tasks: list[tuple[str, Path, bool, list[str] | None, Profile | None]] = []
        for registration in registrations:
            if registration.local_path.exists():
                tasks.append(
                    (
                        registration.remote,
                        registration.local_path,
                        registration.delete,
                        registration.exclude,
                        registration.profile,
                    )
                )

        if not tasks:
            return

        to_put: list[tuple[FileInfo, Path, str, str, Profile | None]] = []
        to_remove: list[tuple[str, str, Profile | None]] = []
        total_bytes = 0

        for remote, local_path, delete, exclude, profile in tasks:
            logger.debug("Syncing upload: %s from %s (delete=%s)", remote, local_path, delete)
            bucket, prefix = _parse_s3_url(remote)
            to_copy, to_delete = _compute_sync_diff(
                _filter_fileinfo(_scan_local(local_path), exclude),
                _filter_fileinfo(self._scan_s3(bucket, prefix, profile), exclude),
            )

            for info in to_copy:
                s3_key = prefix + ("/" + info.relative_path if info.relative_path else "")
                to_put.append((info, local_path, bucket, s3_key, profile))
                total_bytes += info.size

            for info in to_delete if delete else []:
                s3_key = prefix + ("/" + info.relative_path if info.relative_path else "")
                to_remove.append((bucket, s3_key, profile))

        if to_put:
            with (
                self._progress_bar(total_bytes, f"Uploading {remote}") as pbar,
                ThreadPoolExecutor(max_workers=self.options.max_workers) as executor,
            ):
                futures = [
                    executor.submit(self._put_to_s3, info, local_path, bucket, key, pbar, profile)
                    for info, local_path, bucket, key, profile in to_put
                ]
                _process_futures(as_completed(futures), "Upload")

        if to_remove:
            to_remove_sorted = sorted(to_remove, key=lambda x: x[1].count("/"), reverse=True)
            with ThreadPoolExecutor(max_workers=self.options.max_workers) as executor:
                futures = [
                    executor.submit(self._remove_from_s3, bucket, key, profile)
                    for bucket, key, profile in to_remove_sorted
                ]
                iterator = as_completed(futures)
                if self.options.show_progress:
                    iterator = tqdm(
                        iterator,
                        total=len(to_remove_sorted),
                        desc=f"Deleting in {remote}",
                    )
                _process_futures(iterator, "Delete")

    def _perform_download(
        self,
        remote: str,
        local_path: Path,
        delete: bool,
        exclude: list[str] | None,
        profile: Profile | None = None,
    ) -> None:
        bucket, prefix = _parse_s3_url(remote)
        logger.debug(
            "Performing download: s3://%s/%s to %s (delete=%s)",
            bucket,
            prefix,
            local_path,
            delete,
        )
        to_copy, to_delete = _compute_sync_diff(
            _filter_fileinfo(self._scan_s3(bucket, prefix, profile), exclude),
            _filter_fileinfo(_scan_local(local_path), exclude),
        )

        to_put: list[tuple[FileInfo, str, str, Path]] = []
        to_remove: list[Path] = []
        total_bytes = 0

        for info in to_copy:
            s3_key = prefix + ("/" + info.relative_path if info.relative_path else "")
            to_put.append((info, bucket, s3_key, local_path))
            total_bytes += info.size

        if delete:
            logger.debug("Will delete %d local items not in S3", len(to_delete))
        for info in to_delete if delete else []:
            target = local_path / info.relative_path if info.relative_path else local_path
            logger.debug("Marking for local deletion: %s", target)
            to_remove.append(target)

        if to_put:
            with (
                self._progress_bar(total_bytes, f"Downloading {remote}") as pbar,
                ThreadPoolExecutor(max_workers=self.options.max_workers) as executor,
            ):
                futures = [executor.submit(self._put_locally, *args, pbar, profile) for args in to_put]
                _process_futures(as_completed(futures), "Download")

        if to_remove:
            to_remove_sorted = sorted(to_remove, key=lambda x: len(x.parts), reverse=True)
            iterator = to_remove_sorted
            if self.options.show_progress:
                iterator = tqdm(iterator, desc=f"Deleting in {remote}")
            for path in iterator:
                self._remove_locally(path)

    def _list_s3_objects(self, bucket: str, key: str, profile: Profile | None = None) -> Iterator[dict]:
        logger.debug("Listing S3 objects: bucket=%s, key=%s", bucket, key)
        client = self._get_client(profile)
        # Skip head_object for directory-like keys ending with '/'
        # as we want to list contents, not check if the directory marker exists
        if not key.endswith("/"):
            try:
                obj = client.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                error_code = exc.response["Error"]["Code"]
                if error_code != "404":
                    raise
            else:
                logger.debug("Found single object via head_object: %s", key)
                if "ContentLength" in obj and "Size" not in obj:
                    obj["Size"] = obj["ContentLength"]
                yield {**obj, "Key": key}
                return

        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=key):
            objects = page.get("Contents", [])
            logger.debug("Listed %d objects with prefix %s", len(objects), key)
            yield from objects

    def _scan_s3(self, bucket: str, prefix: str, profile: Profile | None = None) -> Iterator[FileInfo]:
        logger.debug("Scanning S3: s3://%s/%s", bucket, prefix)
        seen_dirs: set[str] = set()
        has_content = False

        for obj in self._list_s3_objects(bucket, prefix, profile):
            has_content = True
            key = obj["Key"]
            relative = key[len(prefix) :].lstrip("/")

            if key.endswith("/"):
                relative = relative.rstrip("/")
                if relative:
                    yield FileInfo(relative_path=relative, size=0, is_dir=True)
                    seen_dirs.add(relative)
            else:
                yield FileInfo(relative_path=relative, size=obj["Size"], is_dir=False)

                if "/" in relative:
                    parts = relative.split("/")
                    for i in range(len(parts) - 1):
                        dir_path = "/".join(parts[: i + 1])
                        if dir_path and dir_path not in seen_dirs:
                            yield FileInfo(relative_path=dir_path, size=0, is_dir=True)
                            seen_dirs.add(dir_path)

        if has_content:
            yield FileInfo(
                relative_path="", size=0, is_dir=True
            )  # Yield root directory marker for symmetry with _scan_local

    def _progress_bar(self, total_bytes: int, desc: str):
        if not self.options.show_progress:
            return _NullTqdm()
        return tqdm(total=total_bytes, unit="B", unit_scale=True, unit_divisor=1024, desc=desc)

    def _put_to_s3(
        self, info: FileInfo, local_path: Path, bucket: str, key: str, pbar, profile: Profile | None = None
    ) -> None:
        try:
            client = self._get_client(profile)
            if info.is_dir:
                key += "/" if not key.endswith("/") else ""
                client.put_object(Bucket=bucket, Key=key, Body=b"")
            else:
                file_path = local_path / info.relative_path if info.relative_path else local_path
                client.upload_file(str(file_path), bucket, key, Callback=pbar.update)
        except Exception as exc:
            logger.error("Failed to put %s to %s/%s: %s", local_path, bucket, key, exc)
            raise

    def _remove_from_s3(self, bucket: str, key: str, profile: Profile | None = None) -> None:
        try:
            client = self._get_client(profile)
            client.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:
            logger.error("Failed to remove %s/%s: %s", bucket, key, exc)
            raise

    def _put_locally(
        self, info: FileInfo, bucket: str, key: str, local_path: Path, pbar, profile: Profile | None = None
    ) -> None:
        try:
            target = local_path / info.relative_path if info.relative_path else local_path
            if info.is_dir:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                client = self._get_client(profile)
                client.download_file(bucket, key, str(target), Callback=pbar.update)
        except Exception as exc:
            logger.error("Failed to put %s locally: %s", key, exc)
            raise

    def _remove_locally(self, path: Path) -> None:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except Exception as exc:
            logger.error("Failed to remove %s: %s", path, exc)
            raise


@contextmanager
def mirror(
    cache_root: str = "~/.cache/positronic/s3/",
    show_progress: bool = True,
    max_workers: int = 10,
    default_profile: str | Profile | None = None,
):
    """
    Context manager that activates the sync environment.

    Args:
        cache_root: Base directory for caching downloaded files.
        show_progress: Display tqdm progress bars.
        max_workers: Threads for parallel S3 operations.
        default_profile: Default S3 profile for all operations in this context.
    """
    global _GLOBAL_ACTIVE_MIRROR
    resolved_default_profile = _resolve_profile(default_profile)
    options = _Options(
        cache_root=cache_root,
        show_progress=show_progress,
        max_workers=max_workers,
        default_profile=resolved_default_profile,
    )

    with _GLOBAL_MIRROR_LOCK:
        if _GLOBAL_ACTIVE_MIRROR is not None:
            raise RuntimeError("Mirror already active")

        mirror_obj = _Mirror(options)
        mirror_obj.start()
        _GLOBAL_ACTIVE_MIRROR = mirror_obj

    token = _ACTIVE_MIRROR.set(mirror_obj)
    had_error = False
    try:
        yield
    except Exception:
        had_error = True
        raise
    finally:
        try:
            mirror_obj.stop(had_error=had_error)
        finally:
            with _GLOBAL_MIRROR_LOCK:
                _GLOBAL_ACTIVE_MIRROR = None
            _ACTIVE_MIRROR.reset(token)


def with_mirror(
    cache_root: str = "~/.cache/positronic/s3/",
    show_progress: bool = True,
    max_workers: int = 10,
    default_profile: str | Profile | None = None,
):
    """
    Decorator equivalent of mirror() for wrapping functions.
    See mirror() for argument details.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Resolve profile at call time, not decoration time
            with mirror(
                cache_root=cache_root,
                show_progress=show_progress,
                max_workers=max_workers,
                default_profile=default_profile,
            ):
                return func(*args, **kwargs)

        return wrapper

    return decorator


def _require_active_mirror() -> _Mirror:
    mirror_obj = _ACTIVE_MIRROR.get()
    if mirror_obj is not None:
        return mirror_obj

    global _GLOBAL_ACTIVE_MIRROR
    if _GLOBAL_ACTIVE_MIRROR is not None:
        return _GLOBAL_ACTIVE_MIRROR

    raise RuntimeError("No active mirror context")


def download(
    remote: str,
    local: str | Path | None = None,
    delete: bool = True,
    exclude: list[str] | None = None,
    profile: str | Profile | None = None,
) -> Path:
    """
    Register a path for download. Ensures local copy matches S3 immediately.

    Args:
        remote: S3 URL or local path.
        local: Explicit local destination. Defaults to standard cache path.
        delete: If True (default), deletes local files NOT in S3 ("mirror" behavior).
        exclude: List of glob patterns to skip.
        profile: S3 profile name or Profile config for custom endpoints.

    Returns:
        Path to the local directory/file.
    """
    mirror_obj = _require_active_mirror()
    return mirror_obj.download(remote, local, delete, exclude, profile)


def upload(
    remote: str,
    local: str | Path | None = None,
    interval: int | None = 300,
    delete: bool = True,
    sync_on_error: bool = False,
    exclude: list[str] | None = None,
    profile: str | Profile | None = None,
) -> Path:
    """
    Register a local path for upload. Uploads on exit and optionally in background.

    Args:
        remote: Destination S3 URL.
        local: Local source path. Auto-resolved from cache path if None.
        interval: Seconds between background syncs. None for exit-only.
        delete: If True (default), deletes S3 files NOT present locally.
        sync_on_error: If True, syncs even if the context exits with an exception.
        profile: S3 profile name or Profile config for custom endpoints.

    Returns:
        Path to the local directory/file.
    """
    mirror_obj = _require_active_mirror()
    return mirror_obj.upload(remote, local, interval, delete, sync_on_error, exclude, profile)


def sync(
    remote: str,
    local: str | Path | None = None,
    interval: int | None = 300,
    delete_local: bool = True,
    delete_remote: bool = True,
    sync_on_error: bool = False,
    exclude: list[str] | None = None,
    profile: str | Profile | None = None,
) -> Path:
    """
    Bi-directional helper. Performs download() then registers upload().

    Args:
        delete_local: Cleanup local files during download.
        delete_remote: Cleanup remote files during upload.
        profile: S3 profile name or Profile config for custom endpoints.

    Returns:
        Path to the local directory/file.
    """
    mirror_obj = _require_active_mirror()
    return mirror_obj.sync(remote, local, interval, delete_local, delete_remote, sync_on_error, exclude, profile)


def ls(prefix: str, recursive: bool = False, profile: str | Profile | None = None) -> list[str]:
    """
    Lists files/objects in a directory or S3 prefix.

    Args:
        prefix: S3 URL or local path.
        recursive: List subdirectories if True.
        profile: S3 profile name or Profile config for custom endpoints.

    Returns:
        List of full S3 URLs or local paths.
    """
    mirror_obj = _require_active_mirror()
    return mirror_obj.ls(prefix, recursive, profile)


__all__ = ["mirror", "with_mirror", "download", "upload", "sync", "ls", "register_profile", "Profile", "_parse_s3_url"]
