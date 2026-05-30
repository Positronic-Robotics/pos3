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

    @patch(BOTO3_PATCH_TARGET)
    def test_ls_trailing_slash_forces_directory_listing(self, mock_boto_client, capsys):
        """`pos3 ls s3://bucket/data/` must list the data/ contents even if
        an object exactly named 'data' also exists. ls() used to strip the
        trailing slash via _normalize_s3_url, letting head_object('data')
        win and hide the directory contents."""
        mock_s3 = _setup_s3_mock(
            mock_boto_client,
            [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}],
        )
        # Pretend an object exactly named 'data' ALSO exists — without the
        # fix, head_object('data') would return this and the directory
        # listing would be skipped.
        mock_s3.head_object.side_effect = None
        mock_s3.head_object.return_value = {"ContentLength": 100}

        rc = main(["ls", "s3://bucket/data/"])

        captured = capsys.readouterr()
        assert rc == 0
        lines = captured.out.strip().splitlines()
        # Should be the directory contents, not the exact-key 'data' object.
        assert lines == ["s3://bucket/data/file.txt"]

    @patch(BOTO3_PATCH_TARGET)
    def test_ls_single_object(self, mock_boto_client, capsys):
        """`pos3 ls s3://bucket/file.json` on an exact object key must return
        the object URL, not an empty list. ls() used to force a trailing
        slash, suppressing the head_object exact-key probe."""
        mock_s3 = _setup_s3_mock(mock_boto_client)
        # Override the default 404: this key IS an exact S3 object.
        mock_s3.head_object.side_effect = None
        mock_s3.head_object.return_value = {"ContentLength": 42}

        rc = main(["ls", "s3://bucket/results.json"])

        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "s3://bucket/results.json"


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
    def test_download_single_object_calls_download_file(self, mock_boto_client, capsys, tmp_path):
        """`pos3 download s3://bucket/results.json` must actually fetch the
        object, not silently mkdir the destination and exit 0."""
        mock_s3 = _setup_s3_mock(mock_boto_client)
        mock_s3.head_object.side_effect = None
        mock_s3.head_object.return_value = {"ContentLength": 42, "Size": 42}

        local = tmp_path / "results.json"
        rc = main(["download", "s3://bucket/results.json", "--local", str(local)])

        assert rc == 0
        assert mock_s3.download_file.call_count == 1
        # Stdout still emits the local path on success.
        captured = capsys.readouterr()
        assert captured.out.strip() == str(local)

    @patch(BOTO3_PATCH_TARGET)
    def test_download_unknown_profile_returns_error(self, mock_boto_client, capsys):
        _setup_s3_mock(mock_boto_client)

        rc = main(["download", "s3://bucket/data", "--profile", "no-such-profile"])

        captured = capsys.readouterr()
        assert rc == 1
        assert "Unknown profile" in captured.err


class TestCliUrlValidation:
    @patch(BOTO3_PATCH_TARGET)
    def test_download_rejects_non_s3_url(self, mock_boto_client, capsys):
        mock_s3 = _setup_s3_mock(mock_boto_client)

        rc = main(["download", "bucket/data", "--local", "/tmp/ignored"])

        captured = capsys.readouterr()
        assert rc == 1
        assert "must be an s3:// URL" in captured.err
        # Nothing should have happened on stdout, nothing on the wire.
        assert captured.out == ""
        mock_s3.download_file.assert_not_called()

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_rejects_non_s3_url(self, mock_boto_client, capsys, tmp_path):
        mock_s3 = _setup_s3_mock(mock_boto_client)
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("x")

        rc = main(["upload", str(tmp_path / "dst"), "--local", str(src)])

        captured = capsys.readouterr()
        assert rc == 1
        assert "must be an s3:// URL" in captured.err
        mock_s3.upload_file.assert_not_called()
        # The bogus "destination" must not have been mkdir'd as a side effect.
        assert not (tmp_path / "dst").exists()


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
    def test_upload_uploads_existing_local_source(self, mock_boto_client, capsys):
        mock_s3 = _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src"
            src.mkdir()
            (src / "file.txt").write_text("content")

            rc = main(["upload", "s3://bucket/data", "--local", str(src)])

        captured = capsys.readouterr()
        assert rc == 0
        assert mock_s3.upload_file.call_count >= 1
        # upload's success path is silent on stdout: no path, no progress.
        # Progress bars and logs go to stderr. This is the counterpart to
        # download's "exactly one line = local path" contract.
        assert captured.out == ""

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


class TestCliTransferFailures:
    @patch(BOTO3_PATCH_TARGET)
    def test_download_returns_nonzero_when_worker_fails(self, mock_boto_client, capsys, tmp_path):
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)
        mock_s3.download_file.side_effect = RuntimeError("boom")

        local_dir = tmp_path / "dst"
        rc = main(["download", "s3://bucket/data", "--local", str(local_dir)])

        captured = capsys.readouterr()
        assert rc == 1
        # The success-path stdout (the local cache path) MUST NOT be printed
        # on failure, since `data_dir=$(pos3 download …)` would treat the
        # path as valid and downstream reads would silently use a partial cache.
        assert captured.out == ""
        assert "Download" in captured.err
        assert "boom" in captured.err

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_returns_nonzero_when_worker_fails(self, mock_boto_client, capsys, tmp_path):
        mock_s3 = _setup_s3_mock(mock_boto_client)
        mock_s3.upload_file.side_effect = RuntimeError("boom")

        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("content")

        rc = main(["upload", "s3://bucket/data", "--local", str(src)])

        captured = capsys.readouterr()
        assert rc == 1
        assert "Upload" in captured.err
        assert "boom" in captured.err


class TestCliDryRun:
    @patch(BOTO3_PATCH_TARGET)
    def test_download_dry_run_prints_plan_and_does_not_transfer(self, mock_boto_client, capsys):
        paginate = [
            {
                "Contents": [
                    {"Key": "data/file.txt", "Size": 5},
                    {"Key": "data/sub/nested.txt", "Size": 7},
                ]
            }
        ]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "dst"
            rc = main(["download", "-n", "s3://bucket/data", "--local", str(local_dir)])

        captured = capsys.readouterr()
        assert rc == 0
        # No actual download was performed.
        mock_s3.download_file.assert_not_called()
        out_lines = captured.out.strip().splitlines()
        # Two files planned, no extra trailing local-path line.
        copy_lines = [line for line in out_lines if line.startswith("download:")]
        assert len(copy_lines) == 2
        assert any("s3://bucket/data/file.txt" in line for line in copy_lines)
        assert any("s3://bucket/data/sub/nested.txt" in line for line in copy_lines)
        assert all(" to " in line for line in copy_lines)
        # No delete lines without --delete.
        assert not any(line.startswith("delete:") for line in out_lines)

    @patch(BOTO3_PATCH_TARGET)
    def test_download_dry_run_with_delete_emits_delete_lines(self, mock_boto_client, capsys):
        paginate = [{"Contents": [{"Key": "data/keep.txt", "Size": 5}]}]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "keep.txt").write_bytes(b"12345")
            orphan = local_dir / "orphan.txt"
            orphan.write_text("orphan")

            rc = main(
                ["download", "-n", "s3://bucket/data", "--local", str(local_dir), "--delete"]
            )

            assert rc == 0
            # Dry-run must not touch the filesystem.
            assert orphan.exists()

        captured = capsys.readouterr()
        delete_lines = [
            line for line in captured.out.splitlines() if line.startswith("delete:")
        ]
        assert any(str(orphan) in line for line in delete_lines)
        mock_s3.download_file.assert_not_called()

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_dry_run_prints_plan_and_does_not_transfer(self, mock_boto_client, capsys):
        mock_s3 = _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src"
            src.mkdir()
            (src / "file.txt").write_text("content")

            rc = main(["upload", "-n", "s3://bucket/data", "--local", str(src)])

        captured = capsys.readouterr()
        assert rc == 0
        mock_s3.upload_file.assert_not_called()
        upload_lines = [
            line for line in captured.out.splitlines() if line.startswith("upload:")
        ]
        assert len(upload_lines) == 1
        assert "s3://bucket/data/file.txt" in upload_lines[0]
        assert str(src / "file.txt") in upload_lines[0]

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_dry_run_with_delete_emits_remote_delete_lines(self, mock_boto_client, capsys):
        paginate = [{"Contents": [{"Key": "data/remote_only.txt", "Size": 5}]}]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src"
            src.mkdir()
            (src / "file.txt").write_text("content")

            rc = main(
                ["upload", "-n", "s3://bucket/data", "--local", str(src), "--delete"]
            )

        captured = capsys.readouterr()
        assert rc == 0
        mock_s3.upload_file.assert_not_called()
        mock_s3.delete_object.assert_not_called()
        delete_lines = [
            line for line in captured.out.splitlines() if line.startswith("delete:")
        ]
        assert any("s3://bucket/data/remote_only.txt" in line for line in delete_lines)

    def test_dry_run_not_accepted_on_ls(self):
        with pytest.raises(SystemExit) as exc:
            main(["ls", "-n", "s3://bucket/data"])
        assert exc.value.code != 0


class TestCliEntry:
    def test_no_subcommand_exits_with_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code != 0

    def test_unknown_flag_uses_subcommand_usage(self, capsys):
        """Typoing a subcommand flag (e.g. `--dry_run` instead of `--dry-run`)
        must produce the SUBCOMMAND's usage, not the top-level one, so the
        user can see which flags actually exist for what they ran."""
        with pytest.raises(SystemExit) as exc:
            main(["download", "s3://bucket/key", "--dry_run"])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        # The error must address the subcommand, not the top-level parser.
        assert "pos3 download" in captured.err
        # And it must show download's actual flags so the user can spot `-n`.
        assert "[-n]" in captured.err or "--dry-run" in captured.err
        # Sanity: NOT the top-level usage.
        assert "{ls,download,upload}" not in captured.err


class TestCliBotoErrors:
    @patch(BOTO3_PATCH_TARGET)
    def test_ls_handles_client_error(self, mock_boto_client, capsys):
        """A non-404 ClientError from _list_s3_objects (e.g. 403 access
        denied) must produce the `pos3 ls: ...` error + exit 1, not a
        Python traceback."""
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
        )

        rc = main(["ls", "s3://bucket/key"])

        captured = capsys.readouterr()
        assert rc == 1
        assert "pos3 ls:" in captured.err
        assert "ClientError" in captured.err
        assert "403" in captured.err

    @patch(BOTO3_PATCH_TARGET)
    def test_download_handles_client_error_during_scan(self, mock_boto_client, capsys, tmp_path):
        """ClientError raised from _scan_s3 (the pre-transfer scan inside
        Mirror.download) must be caught by main(), not escape as a
        traceback."""
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
        )

        rc = main(["download", "s3://bucket/data", "--local", str(tmp_path / "dst")])

        captured = capsys.readouterr()
        assert rc == 1
        assert "pos3 download:" in captured.err
        assert captured.out == ""

    @patch(BOTO3_PATCH_TARGET)
    def test_dry_run_handles_client_error_during_plan(self, mock_boto_client, capsys, tmp_path):
        """plan_download propagates ClientError too — it goes through
        _scan_s3 the same way."""
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
        )

        rc = main(["download", "-n", "s3://bucket/data", "--local", str(tmp_path / "dst")])

        captured = capsys.readouterr()
        assert rc == 1
        assert "pos3 download:" in captured.err
