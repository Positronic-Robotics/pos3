import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError

from pos3.cli import main

BOTO3_PATCH_TARGET = "pos3.profiles.boto3.client"


def _make_404_error(*_args, **_kwargs):
    raise ClientError({"Error": {"Code": "404"}}, "head_object")


def _setup_s3_mock(mock_boto_client, paginate_return_value=None):
    mock_s3 = Mock()
    mock_boto_client.return_value = mock_s3
    mock_s3.head_object.side_effect = _make_404_error
    mock_paginator = Mock()
    mock_s3.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = paginate_return_value or [{"Contents": []}]
    return mock_s3


class TestCliLs:
    @patch(BOTO3_PATCH_TARGET)
    def test_ls_prints_full_s3_urls(self, mock_boto_client, capsys):
        paginate = [
            {
                "Contents": [
                    {"Key": "data/file.txt", "Size": 5},
                    {"Key": "data/sub/nested.txt", "Size": 10},
                ]
            }
        ]
        _setup_s3_mock(mock_boto_client, paginate)

        rc = main(["ls", "s3://bucket/data"])

        captured = capsys.readouterr()
        assert rc == 0
        lines = captured.out.strip().splitlines()
        assert "s3://bucket/data/file.txt" in lines
        # Non-recursive excludes nested items
        assert "s3://bucket/data/sub/nested.txt" not in lines

    @patch(BOTO3_PATCH_TARGET)
    def test_ls_recursive(self, mock_boto_client, capsys):
        paginate = [{"Contents": [{"Key": "data/sub/nested.txt", "Size": 10}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        rc = main(["ls", "-r", "s3://bucket/data"])

        captured = capsys.readouterr()
        assert rc == 0
        assert "s3://bucket/data/sub/nested.txt" in captured.out

    def test_ls_local_path(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "file.txt").write_text("x")

            rc = main(["ls", str(base)])

        captured = capsys.readouterr()
        assert rc == 0
        assert str(base / "file.txt") in captured.out


class TestCliDownload:
    @patch(BOTO3_PATCH_TARGET)
    def test_download_prints_only_local_path_to_stdout(self, mock_boto_client, capsys):
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "dst"
            rc = main(["download", "s3://bucket/data", "--local", str(local_dir)])

        captured = capsys.readouterr()
        assert rc == 0
        # Exactly one line on stdout: the resulting local path.
        lines = captured.out.strip().splitlines()
        assert len(lines) == 1
        assert lines[0] == str(local_dir.resolve())

    @patch(BOTO3_PATCH_TARGET)
    def test_download_default_does_not_delete(self, mock_boto_client):
        """CLI default for --delete is OFF; orphan local files survive."""
        paginate = [{"Contents": [{"Key": "data/file1.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "file1.txt").write_bytes(b"12345")
            orphan = local_dir / "orphan.txt"
            orphan.write_text("orphan")

            rc = main(["download", "s3://bucket/data", "--local", str(local_dir)])

            assert rc == 0
            assert orphan.exists()

    @patch(BOTO3_PATCH_TARGET)
    def test_download_delete_flag_removes_orphans(self, mock_boto_client):
        paginate = [{"Contents": [{"Key": "data/file1.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "file1.txt").write_bytes(b"12345")
            orphan = local_dir / "orphan.txt"
            orphan.write_text("orphan")

            rc = main(["download", "s3://bucket/data", "--local", str(local_dir), "--delete"])

            assert rc == 0
            assert not orphan.exists()

    @patch(BOTO3_PATCH_TARGET)
    def test_download_exclude_multiple_patterns(self, mock_boto_client):
        paginate = [
            {
                "Contents": [
                    {"Key": "data/file.txt", "Size": 5},
                    {"Key": "data/file.log", "Size": 10},
                    {"Key": "data/file.tmp", "Size": 10},
                ]
            }
        ]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "dst"
            rc = main(
                [
                    "download",
                    "s3://bucket/data",
                    "--local",
                    str(local_dir),
                    "--exclude",
                    "*.log",
                    "--exclude",
                    "*.tmp",
                ]
            )

        assert rc == 0
        assert mock_s3.download_file.call_count == 1
        downloaded_key = mock_s3.download_file.call_args_list[0][0][1]
        assert "file.txt" in downloaded_key

    @patch(BOTO3_PATCH_TARGET)
    def test_download_unknown_profile_returns_error(self, mock_boto_client, capsys):
        _setup_s3_mock(mock_boto_client)

        rc = main(["download", "s3://bucket/data", "--profile", "no-such-profile"])

        captured = capsys.readouterr()
        assert rc == 1
        assert "Unknown profile" in captured.err


class TestCliUpload:
    @patch(BOTO3_PATCH_TARGET)
    def test_upload_errors_when_source_missing(self, mock_boto_client, capsys):
        _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "does-not-exist"
            rc = main(["upload", "s3://bucket/data", "--local", str(missing)])

        captured = capsys.readouterr()
        assert rc == 1
        assert "does not exist" in captured.err
        # Nothing should have been written to S3.
        mock_boto_client.return_value.upload_file.assert_not_called()

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_uploads_existing_local_source(self, mock_boto_client):
        mock_s3 = _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src"
            src.mkdir()
            (src / "file.txt").write_text("content")

            rc = main(["upload", "s3://bucket/data", "--local", str(src)])

        assert rc == 0
        assert mock_s3.upload_file.call_count >= 1

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_default_source_is_cache_path(self, mock_boto_client):
        """When --local is omitted, source defaults to the same cache path pos3 download
        would have produced."""
        mock_s3 = _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_root = Path(tmpdir)
            cache_path = cache_root / "_" / "bucket" / "data"
            cache_path.mkdir(parents=True)
            (cache_path / "file.txt").write_text("hi")

            # Override the CLI's mirror() to anchor cache_root at our tmpdir, so
            # the no-`--local` path resolves under it.
            import pos3

            real_mirror = pos3.mirror

            def fixed_mirror(**_kwargs):
                return real_mirror(cache_root=str(cache_root), show_progress=False)

            with patch("pos3.cli.mirror", side_effect=fixed_mirror):
                rc = main(["upload", "s3://bucket/data"])

            assert rc == 0
            assert mock_s3.upload_file.call_count >= 1

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_default_does_not_delete(self, mock_boto_client):
        """CLI default for --delete is OFF; remote orphans survive."""
        # S3 has remote_only.txt; local has file.txt. Without --delete, remote should stay.
        paginate = [{"Contents": [{"Key": "data/remote_only.txt", "Size": 5}]}]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src"
            src.mkdir()
            (src / "file.txt").write_text("content")

            rc = main(["upload", "s3://bucket/data", "--local", str(src)])

        assert rc == 0
        # No deletes should have been issued.
        assert mock_s3.delete_object.call_count == 0

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_delete_flag_removes_remote_orphans(self, mock_boto_client):
        paginate = [{"Contents": [{"Key": "data/remote_only.txt", "Size": 5}]}]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src"
            src.mkdir()
            (src / "file.txt").write_text("content")

            rc = main(["upload", "s3://bucket/data", "--local", str(src), "--delete"])

        assert rc == 0
        assert mock_s3.delete_object.call_count >= 1


class TestCliEntry:
    def test_no_subcommand_exits_with_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code != 0
