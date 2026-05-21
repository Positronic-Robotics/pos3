"""Command-line interface for pos3.

Three subcommands — ``ls``, ``download``, ``upload`` — each running inside a
short-lived ``pos3.mirror()`` context. ``upload`` is one-shot: no background
interval, no sync loop.

CLI defaults intentionally differ from the Python API: ``--delete`` defaults
to OFF for both ``download`` and ``upload``, because the API's ``True``
default is too destructive for interactive shell use. ``download`` writes
only the resulting local path to stdout so the output is safe to capture in
``$(pos3 download ...)``; progress bars and logs go to stderr.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import _is_s3_path, _require_active_mirror, download, ls, mirror, upload
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


def _cmd_upload(args: argparse.Namespace) -> int:
    with mirror(show_progress=True):
        mirror_obj = _require_active_mirror()
        if args.local:
            source = Path(args.local).expanduser().resolve()
        else:
            # Resolve the same profile precedence (URL > --profile > context default)
            # the upload() call will use, so the cache path we check matches the one
            # upload() would target.
            profile: str | None = args.profile
            if _is_s3_path(args.url):
                url_profile = _url_profile(args.url)
                if url_profile is not None:
                    profile = url_profile
            effective_profile = _resolve_profile(profile) or mirror_obj.options.default_profile
            source = mirror_obj.options.cache_path_for(args.url, effective_profile)
        if not source.exists():
            print(f"pos3 upload: source path does not exist: {source}", file=sys.stderr)
            return 1
        upload(
            args.url,
            local=source,
            interval=None,
            delete=args.delete,
            exclude=args.exclude,
            profile=args.profile,
        )
    return 0


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
