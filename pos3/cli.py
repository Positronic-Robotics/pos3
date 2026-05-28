"""Command-line interface for pos3.

Three subcommands — ``ls``, ``download``, ``upload`` — each running inside a
short-lived ``pos3.mirror()`` context. ``upload`` is one-shot: no background
interval, no sync loop.

CLI defaults intentionally differ from the Python API: ``--delete`` defaults
to OFF for both ``download`` and ``upload``, because the API's ``True``
default is too destructive for interactive shell use. ``download`` writes
only the resulting local path to stdout so the output is safe to capture in
``$(pos3 download ...)``; progress bars and logs go to stderr.

``--dry-run`` / ``-n`` (download and upload only) prints the planned
per-file actions to stdout in ``aws s3 sync --dryrun`` style and performs
no transfers and no deletes. (The cache root directory is initialized the
same way it is for any pos3 invocation.)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import (
    _compute_sync_diff,
    _filter_fileinfo,
    _is_s3_path,
    _make_s3_key,
    _parse_s3_url,
    _require_active_mirror,
    _scan_local,
    download,
    ls,
    mirror,
    upload,
)
from .profiles import _resolve_profile, _url_profile


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pos3", description="pos3 command-line interface.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_profile(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--profile",
            metavar="NAME",
            help="Named pos3 profile. Overridden by the URL form s3://<profile>@bucket/key.",
        )

    def add_transfer_args(p: argparse.ArgumentParser, local_help: str) -> None:
        p.add_argument("--local", metavar="PATH", help=local_help)
        p.add_argument(
            "--delete",
            action="store_true",
            help="Delete files not present at the other end. Defaults to OFF in the CLI.",
        )
        p.add_argument(
            "--exclude",
            metavar="PATTERN",
            action="append",
            default=None,
            help="Glob pattern to skip. May be passed multiple times.",
        )
        p.add_argument(
            "-n",
            "--dry-run",
            action="store_true",
            help="Print the planned actions and exit without transferring or deleting anything.",
        )
        add_profile(p)

    p_ls = subparsers.add_parser("ls", help="List objects under a prefix.")
    p_ls.add_argument("prefix", help="S3 prefix (s3://bucket/key) or local path.")
    p_ls.add_argument("-r", "--recursive", action="store_true", help="List subdirectories recursively.")
    add_profile(p_ls)

    p_dl = subparsers.add_parser("download", help="Download an S3 prefix or object to a local path.")
    p_dl.add_argument("url", help="Source S3 URL (s3://bucket/key).")
    add_transfer_args(p_dl, local_help="Destination path. Defaults to the cache path.")

    p_up = subparsers.add_parser("upload", help="One-shot upload of a local path to S3.")
    p_up.add_argument("url", help="Destination S3 URL (s3://bucket/key).")
    add_transfer_args(p_up, local_help="Source path. Defaults to the cache path.")

    return parser


def _cmd_ls(args: argparse.Namespace) -> int:
    with mirror(show_progress=False):
        for item in ls(args.prefix, recursive=args.recursive, profile=args.profile):
            print(item)
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    if not _is_s3_path(args.url):
        print(
            f"pos3 download: url must be an s3:// URL, got: {args.url}",
            file=sys.stderr,
        )
        return 1
    if args.dry_run:
        with mirror(show_progress=False):
            _print_download_plan(args)
        return 0
    with mirror(show_progress=True):
        local_path = download(
            args.url,
            local=args.local,
            delete=args.delete,
            exclude=args.exclude,
            profile=args.profile,
        )
    print(str(local_path))
    return 0


def _resolve_upload_source(args: argparse.Namespace) -> Path | None:
    """Resolve the local source path, mirroring the precedence used by upload().

    Prints an error and returns None if the source path does not exist.
    """
    mirror_obj = _require_active_mirror()
    if args.local:
        source = Path(args.local).expanduser().resolve()
    else:
        # Resolve the same profile precedence (URL > --profile > context default)
        # the upload() call will use, so the cache path we check matches the one
        # upload() would target.
        url_profile = _url_profile(args.url)
        profile = url_profile if url_profile is not None else args.profile
        effective_profile = _resolve_profile(profile) or mirror_obj.options.default_profile
        source = mirror_obj.options.cache_path_for(args.url, effective_profile)
    if not source.exists():
        print(f"pos3 upload: source path does not exist: {source}", file=sys.stderr)
        return None
    return source


def _cmd_upload(args: argparse.Namespace) -> int:
    if not _is_s3_path(args.url):
        print(
            f"pos3 upload: url must be an s3:// URL, got: {args.url}",
            file=sys.stderr,
        )
        return 1
    with mirror(show_progress=not args.dry_run):
        source = _resolve_upload_source(args)
        if source is None:
            return 1
        if args.dry_run:
            _print_upload_plan(args, source)
            return 0
        upload(
            args.url,
            local=source,
            interval=None,
            delete=args.delete,
            exclude=args.exclude,
            profile=args.profile,
        )
    return 0


def _print_download_plan(args: argparse.Namespace) -> None:
    mirror_obj = _require_active_mirror()
    profile = mirror_obj._effective_profile(args.profile, args.url)
    local_path = (
        mirror_obj.options.cache_path_for(args.url, profile)
        if args.local is None
        else Path(args.local).expanduser().resolve()
    )
    bucket, prefix = _parse_s3_url(args.url)
    to_copy, to_delete = _compute_sync_diff(
        _filter_fileinfo(mirror_obj._scan_s3(bucket, prefix, profile), args.exclude),
        _filter_fileinfo(_scan_local(local_path), args.exclude),
    )
    # Skip synthesized directory entries: only file-level actions matter for the user.
    for info in to_copy:
        if info.is_dir:
            continue
        s3_key = _make_s3_key(prefix, info)
        dst = local_path / info.relative_path if info.relative_path else local_path
        print(f"download: s3://{bucket}/{s3_key} to {dst}")
    if args.delete:
        for info in to_delete:
            if info.is_dir:
                continue
            target = local_path / info.relative_path if info.relative_path else local_path
            print(f"delete: {target}")


def _print_upload_plan(args: argparse.Namespace, source: Path) -> None:
    mirror_obj = _require_active_mirror()
    profile = mirror_obj._effective_profile(args.profile, args.url)
    bucket, prefix = _parse_s3_url(args.url)
    to_copy, to_delete = _compute_sync_diff(
        _filter_fileinfo(_scan_local(source), args.exclude),
        _filter_fileinfo(mirror_obj._scan_s3(bucket, prefix, profile), args.exclude),
    )
    for info in to_copy:
        if info.is_dir:
            continue
        s3_key = _make_s3_key(prefix, info)
        local = source / info.relative_path if info.relative_path else source
        print(f"upload: {local} to s3://{bucket}/{s3_key}")
    if args.delete:
        for info in to_delete:
            if info.is_dir:
                continue
            s3_key = _make_s3_key(prefix, info)
            print(f"delete: s3://{bucket}/{s3_key}")


_COMMANDS = {
    "ls": _cmd_ls,
    "download": _cmd_download,
    "upload": _cmd_upload,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except ValueError as exc:
        print(f"pos3 {args.command}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
