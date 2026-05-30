import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError

import pos3 as s3

BOTO3_PATCH_TARGET = "pos3.profiles.boto3.client"
SESSION_PATCH_TARGET = "pos3.profiles.boto3.session.Session"


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


class TestMakeS3Key:
    def test_file_with_prefix(self):
        info = s3.FileInfo(relative_path="file.txt", size=100, is_dir=False)
        assert s3._make_s3_key("data", info) == "data/file.txt"

    def test_dir_with_prefix(self):
        info = s3.FileInfo(relative_path="subdir", size=0, is_dir=True)
        assert s3._make_s3_key("data", info) == "data/subdir/"

    def test_root_dir(self):
        info = s3.FileInfo(relative_path="", size=0, is_dir=True)
        assert s3._make_s3_key("data", info) == "data/"

    def test_empty_prefix_file(self):
        info = s3.FileInfo(relative_path="file.txt", size=100, is_dir=False)
        assert s3._make_s3_key("", info) == "file.txt"

    def test_empty_prefix_dir(self):
        info = s3.FileInfo(relative_path="subdir", size=0, is_dir=True)
        assert s3._make_s3_key("", info) == "subdir/"

    def test_nested_path(self):
        info = s3.FileInfo(relative_path="a/b/c.txt", size=50, is_dir=False)
        assert s3._make_s3_key("prefix", info) == "prefix/a/b/c.txt"

    def test_nested_dir(self):
        info = s3.FileInfo(relative_path="a/b", size=0, is_dir=True)
        assert s3._make_s3_key("prefix", info) == "prefix/a/b/"

    def test_dir_already_trailing_slash_prefix(self):
        """Prefix with trailing slash in relative_path shouldn't double-slash."""
        info = s3.FileInfo(relative_path="sub/", size=0, is_dir=True)
        result = s3._make_s3_key("data", info)
        assert result == "data/sub/"
        assert "//" not in result


class TestS3URLParsing:
    def test_parse_s3_url_valid(self):
        assert s3._parse_s3_url("s3://bucket/path/to/data") == (
            "bucket",
            "path/to/data",
        )
        assert s3._parse_s3_url("s3://bucket/") == ("bucket", "")

    def test_parse_s3_url_invalid_scheme(self):
        with pytest.raises(ValueError, match="Not an S3 URL"):
            s3._parse_s3_url("http://bucket/path")


class TestMirrorLifecycle:
    def test_download_requires_active_mirror(self):
        with pytest.raises(RuntimeError, match="No active mirror"):
            s3.download("s3://bucket/data")

    def test_nested_mirror_fails(self):
        with s3.mirror(show_progress=False):
            with pytest.raises(RuntimeError, match="Mirror already active"):
                with s3.mirror():
                    pass


class TestDownload:
    @patch(BOTO3_PATCH_TARGET)
    def test_download_deduplicated(self, mock_boto_client):
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                path1 = s3.download("s3://bucket/data")
                path2 = s3.download("s3://bucket/data")

        assert path1 == path2
        assert mock_s3.download_file.call_count == 1

    @patch(BOTO3_PATCH_TARGET)
    def test_download_local_override_conflict(self, mock_boto_client):
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            custom_a = Path(tmpdir) / "custom_a"
            custom_b = Path(tmpdir) / "custom_b"

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", local=custom_a)
                with pytest.raises(ValueError, match="already registered"):
                    s3.download("s3://bucket/data", local=custom_b)

    def test_download_local_passthrough(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = Path(tmpdir) / "data"
            local_path.mkdir()

            with s3.mirror(show_progress=False):
                resolved = s3.download(str(local_path))

        assert resolved == local_path.resolve()

    @patch(BOTO3_PATCH_TARGET)
    def test_thread_safe_download(self, mock_boto_client):
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):

                def _do_download(_):
                    return s3.download("s3://bucket/data")

                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(_do_download, range(4)))

        assert len(set(results)) == 1
        assert mock_s3.download_file.call_count == 1


class TestUpload:
    @patch(BOTO3_PATCH_TARGET)
    def test_upload_conflict_with_download(self, mock_boto_client):
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data")
                with pytest.raises(ValueError, match="Conflict"):
                    s3.upload("s3://bucket/data/subdir")

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_deduplicated(self, mock_boto_client):
        _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                path1 = s3.upload("s3://bucket/output")
                path2 = s3.upload("s3://bucket/output")

        assert path1 == path2

    def test_upload_local_passthrough(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = Path(tmpdir) / "output"

            with s3.mirror(show_progress=False):
                resolved = s3.upload(str(local_path))

            assert resolved == local_path.resolve()
            assert local_path.exists()

    @patch(BOTO3_PATCH_TARGET)
    def test_final_sync_upload(self, mock_boto_client):
        paginate = [{"Contents": [{"Key": "output/existing.txt", "Size": 5}]}]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "output"
            output.mkdir()
            (output / "new.txt").write_text("content")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.upload("s3://bucket/output", local=output, interval=None)

        assert mock_s3.upload_file.call_count >= 1
        assert mock_s3.delete_object.call_count == 1

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_delete_directory_marker_trailing_slash(self, mock_boto_client):
        """Test that deleting a directory from S3 uses trailing slash to match directory markers."""
        # S3 has a directory marker "output/subdir/" and a file "output/file.txt"
        paginate = [
            {
                "Contents": [
                    {"Key": "output/subdir/", "Size": 0},  # Directory marker with trailing slash
                    {"Key": "output/file.txt", "Size": 5},
                ]
            }
        ]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "output"
            output.mkdir()
            # Local only has file.txt - subdir was deleted locally
            (output / "file.txt").write_text("content")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.upload("s3://bucket/output", local=output, interval=None)

        # The directory marker should be deleted with trailing slash
        delete_calls = mock_s3.delete_object.call_args_list
        deleted_keys = [call[1]["Key"] for call in delete_calls]
        assert "output/subdir/" in deleted_keys, (
            f"Expected delete of 'output/subdir/' but got: {deleted_keys}"
        )

    @patch(BOTO3_PATCH_TARGET)
    def test_background_sync_uploads_repeatedly(self, mock_boto_client):
        mock_s3 = _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "output"
            output.mkdir()
            (output / "data.txt").write_text("content")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.upload("s3://bucket/output", local=output, interval=1)
                time.sleep(2.5)

        assert mock_s3.upload_file.call_count >= 2

    @patch(BOTO3_PATCH_TARGET)
    def test_background_worker_survives_transfer_error(self, mock_boto_client):
        """A TransferError from one interval-sync iteration must not kill the
        daemon thread; subsequent ticks should keep retrying."""
        mock_s3 = _setup_s3_mock(mock_boto_client)

        call_count = {"n": 0}

        def upload_side_effect(*_args, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient")
            # Subsequent calls succeed (no return value needed).

        mock_s3.upload_file.side_effect = upload_side_effect

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "output"
            output.mkdir()
            (output / "data.txt").write_text("content")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.upload("s3://bucket/output", local=output, interval=1)
                time.sleep(2.5)

        # If the worker had died after the first failure, count would stay at 1.
        # Surviving means at least one more attempt happened on a later tick.
        assert call_count["n"] >= 2

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_no_sync_on_error(self, mock_boto_client):
        """Test that uploads with sync_on_error=False don't sync when context exits with error."""
        mock_s3 = _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "output"
            output.mkdir()
            (output / "data.txt").write_text("content")

            try:
                with s3.mirror(cache_root=tmpdir, show_progress=False):
                    s3.upload(
                        "s3://bucket/output",
                        local=output,
                        interval=None,
                        sync_on_error=False,
                    )
                    raise RuntimeError("Test error")
            except RuntimeError:
                pass

        # Should not have synced because sync_on_error=False and context exited with error
        assert mock_s3.upload_file.call_count == 0

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_sync_on_error_true(self, mock_boto_client):
        """Test that uploads with sync_on_error=True do sync when context exits with error."""
        mock_s3 = _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "output"
            output.mkdir()
            (output / "data.txt").write_text("content")

            try:
                with s3.mirror(cache_root=tmpdir, show_progress=False):
                    s3.upload(
                        "s3://bucket/output",
                        local=output,
                        interval=None,
                        sync_on_error=True,
                    )
                    raise RuntimeError("Test error")
            except RuntimeError:
                pass

        # Should have synced because sync_on_error=True
        assert mock_s3.upload_file.call_count >= 1


class TestDownloadSync:
    @patch(BOTO3_PATCH_TARGET)
    def test_download_delete_removes_orphaned_files(self, mock_boto_client):
        """Test that download with delete=True removes local files not in S3."""
        # S3 only has file1.txt
        paginate = [{"Contents": [{"Key": "data/file1.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            # Create local files - file1.txt (exists in S3) and file2.txt (orphan)
            (local_dir / "file1.txt").write_bytes(b"12345")
            orphan_file = local_dir / "file2.txt"
            orphan_file.write_text("orphan")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", local=local_dir, delete=True)

            # file1.txt should exist (no need to download, same size)
            assert (local_dir / "file1.txt").exists()
            # file2.txt should be deleted
            assert not orphan_file.exists()

    @patch(BOTO3_PATCH_TARGET)
    def test_download_no_delete_preserves_orphaned_files(self, mock_boto_client):
        """Test that download with delete=False preserves local files not in S3."""
        # S3 only has file1.txt
        paginate = [{"Contents": [{"Key": "data/file1.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            # Create local files
            (local_dir / "file1.txt").write_bytes(b"12345")
            orphan_file = local_dir / "file2.txt"
            orphan_file.write_text("orphan")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", local=local_dir, delete=False)

            # Both files should exist
            assert (local_dir / "file1.txt").exists()
            assert orphan_file.exists()

    @patch(BOTO3_PATCH_TARGET)
    def test_download_syncs_directories(self, mock_boto_client):
        """Test that download syncs directory structure including empty dirs."""
        # S3 has a directory marker
        paginate = [
            {
                "Contents": [
                    {"Key": "data/subdir/", "Size": 0},  # Directory marker
                    {"Key": "data/subdir/file.txt", "Size": 5},
                ]
            }
        ]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", local=local_dir, delete=True)

            # Directory should be created
            assert (local_dir / "subdir").is_dir()
            # File download should have been attempted
            assert mock_s3.download_file.call_count >= 1

    @patch(BOTO3_PATCH_TARGET)
    def test_download_delete_parameter_conflict(self, mock_boto_client):
        """Test that registering same download with different delete param raises error."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", delete=True)
                with pytest.raises(ValueError, match="already registered with different parameters"):
                    s3.download("s3://bucket/data", delete=False)

    @patch(BOTO3_PATCH_TARGET)
    def test_download_delete_empty_s3_removes_all_local(self, mock_boto_client):
        """Test that download with delete=True removes all local files, dirs, and root when S3 is empty."""
        # S3 is completely empty
        paginate = [{"Contents": []}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            # Create nested directory structure with files
            (local_dir / "file.txt").write_text("content")
            subdir = local_dir / "subdir"
            subdir.mkdir()
            (subdir / "nested.txt").write_text("nested content")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", local=local_dir, delete=True)

            # When S3 is completely empty, the local directory itself should be removed
            assert not local_dir.exists()

    @patch(BOTO3_PATCH_TARGET)
    def test_download_no_delete_empty_s3_preserves_local(self, mock_boto_client):
        """Test that download with delete=False preserves local dir when S3 is empty."""
        # S3 is completely empty
        paginate = [{"Contents": []}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "file.txt").write_text("content")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", local=local_dir, delete=False)

            # With delete=False, local directory and its contents should be preserved
            assert local_dir.exists()
            assert (local_dir / "file.txt").exists()


class TestSync:
    @patch(BOTO3_PATCH_TARGET)
    def test_sync_requires_active_mirror(self, mock_boto_client):
        """Test that sync requires an active mirror context."""
        with pytest.raises(RuntimeError, match="No active mirror"):
            s3.sync("s3://bucket/data")

    @patch(BOTO3_PATCH_TARGET)
    def test_sync_basic_functionality(self, mock_boto_client):
        """Test that sync performs download then upload and allows same remote path."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            # Add a new file that doesn't exist in S3 to ensure upload happens
            (local_dir / "new_file.txt").write_text("new content")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                result_path = s3.sync(
                    "s3://bucket/data",
                    local=local_dir,
                    interval=None,
                    delete_local=False,
                )

            assert result_path == local_dir.resolve()
            # Should have downloaded
            assert mock_s3.download_file.call_count >= 1
            # Should have uploaded (at least the new file)
            assert mock_s3.upload_file.call_count >= 1

    @patch(BOTO3_PATCH_TARGET)
    def test_sync_local_passthrough(self, mock_boto_client):
        """Test that sync with local path just returns the path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = Path(tmpdir) / "data"
            local_path.mkdir()

            with s3.mirror(show_progress=False):
                resolved = s3.sync(str(local_path))

            assert resolved == local_path.resolve()

    @patch(BOTO3_PATCH_TARGET)
    def test_sync_delete_flags(self, mock_boto_client):
        """Test that delete_local and delete_remote flags work correctly."""
        # Test delete_local: S3 only has file1.txt
        paginate_local = [{"Contents": [{"Key": "data/file1.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate_local)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "file1.txt").write_bytes(b"12345")
            orphan_local = local_dir / "file2.txt"
            orphan_local.write_text("orphan")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                # delete_local=True should remove orphaned local files
                s3.sync(
                    "s3://bucket/data",
                    local=local_dir,
                    delete_local=True,
                    delete_remote=False,
                    interval=None,
                )

            assert (local_dir / "file1.txt").exists()
            assert not orphan_local.exists()

        # Test delete_local=False: preserve orphaned files
        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "file1.txt").write_bytes(b"12345")
            orphan_local = local_dir / "file2.txt"
            orphan_local.write_text("orphan")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.sync(
                    "s3://bucket/data",
                    local=local_dir,
                    delete_local=False,
                    delete_remote=False,
                    interval=None,
                )

            assert (local_dir / "file1.txt").exists()
            assert orphan_local.exists()

        # Test delete_remote: S3 has file1.txt and file2.txt, local only has file1.txt
        paginate_remote = [
            {
                "Contents": [
                    {"Key": "data/file1.txt", "Size": 5},
                    {"Key": "data/file2.txt", "Size": 5},
                ]
            }
        ]
        mock_s3_remote = _setup_s3_mock(mock_boto_client, paginate_remote)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "file1.txt").write_bytes(b"12345")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                # delete_remote=True should remove orphaned S3 files
                s3.sync(
                    "s3://bucket/data",
                    local=local_dir,
                    delete_local=False,
                    delete_remote=True,
                    interval=None,
                )

            assert mock_s3_remote.delete_object.call_count >= 1

        # Test delete_remote=False: preserve orphaned S3 files
        mock_s3_no_delete = _setup_s3_mock(mock_boto_client, paginate_remote)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "file1.txt").write_bytes(b"12345")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.sync(
                    "s3://bucket/data",
                    local=local_dir,
                    delete_local=False,
                    delete_remote=False,
                    interval=None,
                )

            assert mock_s3_no_delete.delete_object.call_count == 0

    @patch(BOTO3_PATCH_TARGET)
    def test_sync_background_sync(self, mock_boto_client):
        """Test that sync with interval enables background syncing."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "file.txt").write_text("content")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.sync("s3://bucket/data", local=local_dir, interval=1)
                time.sleep(2.5)

            # Should have synced multiple times in background
            assert mock_s3.upload_file.call_count >= 2

    @patch(BOTO3_PATCH_TARGET)
    def test_sync_on_error_flag(self, mock_boto_client):
        """Test that sync_on_error flag controls syncing on context exit with error."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        mock_s3_no_sync = _setup_s3_mock(mock_boto_client, paginate)

        # Test sync_on_error=False: should not sync on error exit
        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "file.txt").write_text("content")

            try:
                with s3.mirror(cache_root=tmpdir, show_progress=False):
                    s3.sync(
                        "s3://bucket/data",
                        local=local_dir,
                        interval=None,
                        sync_on_error=False,
                    )
                    raise RuntimeError("Test error")
            except RuntimeError:
                pass

            assert mock_s3_no_sync.download_file.call_count >= 1

        # Test sync_on_error=True: should sync on error exit
        mock_s3_with_sync = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "file.txt").write_text("content")

            try:
                with s3.mirror(cache_root=tmpdir, show_progress=False):
                    s3.sync(
                        "s3://bucket/data",
                        local=local_dir,
                        interval=None,
                        sync_on_error=True,
                    )
                    raise RuntimeError("Test error")
            except RuntimeError:
                pass

            # Should have synced because sync_on_error=True
            assert mock_s3_with_sync.upload_file.call_count >= 1

    @patch(BOTO3_PATCH_TARGET)
    def test_sync_empty_s3_deletes_local_directories(self, mock_boto_client):
        """Test that sync with empty S3 deletes local directory completely."""
        # S3 is completely empty
        paginate = [{"Contents": []}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            # Create nested directory structure
            (local_dir / "file.txt").write_text("content")
            subdir = local_dir / "subdir"
            subdir.mkdir()
            (subdir / "nested.txt").write_text("nested")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                # sync with empty S3 should delete everything local including root
                s3.sync(
                    "s3://bucket/data",
                    local=local_dir,
                    interval=None,
                    delete_local=True,
                    delete_remote=False,
                )

            # When S3 is completely empty, the local directory itself should be removed
            assert not local_dir.exists()

    @patch(BOTO3_PATCH_TARGET)
    def test_sync_conflicts(self, mock_boto_client):
        """Test that sync conflicts with existing registrations and second sync call."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        # Test conflict with existing download
        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data/subdir")
                with pytest.raises(ValueError, match="Conflict"):
                    s3.sync("s3://bucket/data", interval=None)

        # Test conflict with existing upload
        _setup_s3_mock(mock_boto_client)
        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.upload("s3://bucket/data/subdir")
                with pytest.raises(ValueError, match="Conflict"):
                    s3.sync("s3://bucket/data", interval=None)

        # Test second sync call conflicts (upload already registered)
        _setup_s3_mock(mock_boto_client, paginate)
        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                path1 = s3.sync("s3://bucket/data", interval=None)
                assert path1 is not None
                # Second sync call tries to download, which conflicts with existing upload
                with pytest.raises(ValueError, match="Conflict"):
                    s3.sync("s3://bucket/data", interval=None)


class TestPlanPublicAPI:
    """The plan API is callable via the public pos3.plan_download /
    pos3.plan_upload module-level wrappers, the same way pos3.download /
    pos3.upload are. Users following the README must not need to reach
    into pos3._require_active_mirror()."""

    def test_plan_download_and_upload_are_module_level(self):
        # Sanity: they exist on the package surface.
        assert callable(s3.plan_download)
        assert callable(s3.plan_upload)
        # And in __all__ so `from pos3 import *` includes them.
        assert "plan_download" in s3.__all__
        assert "plan_upload" in s3.__all__
        assert "TransferPlan" in s3.__all__
        assert "TransferError" in s3.__all__

    @patch(BOTO3_PATCH_TARGET)
    def test_plan_download_via_public_wrapper(self, mock_boto_client):
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "dst"
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                plan = s3.plan_download("s3://bucket/data", local=str(local_dir))

        assert isinstance(plan, s3.TransferPlan)
        sources = [src for src, _ in plan.to_copy]
        assert "s3://bucket/data/file.txt" in sources


class TestPlan:
    """plan_download / plan_upload return what a real call would do, without
    transferring, deleting, or creating directories."""

    @patch(BOTO3_PATCH_TARGET)
    def test_plan_download_lists_files_to_copy(self, mock_boto_client):
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
            with s3.mirror(cache_root=tmpdir, show_progress=False) as _:
                from pos3 import _require_active_mirror

                plan = _require_active_mirror().plan_download(
                    "s3://bucket/data", local=str(local_dir)
                )

        sources = [src for src, _ in plan.to_copy]
        assert "s3://bucket/data/file.txt" in sources
        assert "s3://bucket/data/sub/nested.txt" in sources
        # No real transfer happened.
        mock_s3.download_file.assert_not_called()
        # Directory entries are filtered out — file-level only.
        assert all(not src.endswith("/") for src in sources)

    @patch(BOTO3_PATCH_TARGET)
    def test_plan_download_lists_orphans_in_to_delete(self, mock_boto_client):
        paginate = [{"Contents": [{"Key": "data/keep.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "keep.txt").write_bytes(b"12345")
            orphan = local_dir / "orphan.txt"
            orphan.write_text("x")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                from pos3 import _require_active_mirror

                plan = _require_active_mirror().plan_download(
                    "s3://bucket/data", local=str(local_dir)
                )

            # Dry-plan is read-only.
            assert orphan.exists()

        assert str(orphan) in plan.to_delete

    @patch(BOTO3_PATCH_TARGET)
    def test_plan_upload_lists_files_to_copy(self, mock_boto_client):
        mock_s3 = _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src"
            src.mkdir()
            (src / "file.txt").write_text("content")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                from pos3 import _require_active_mirror

                plan = _require_active_mirror().plan_upload(
                    "s3://bucket/data", local=str(src)
                )

        destinations = [dst for _, dst in plan.to_copy]
        assert destinations == ["s3://bucket/data/file.txt"]
        mock_s3.upload_file.assert_not_called()

    def test_plan_download_rejects_non_s3_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                from pos3 import _require_active_mirror

                with pytest.raises(ValueError, match="s3:// URL"):
                    _require_active_mirror().plan_download("/local/path")

    def test_plan_upload_rejects_non_s3_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                from pos3 import _require_active_mirror

                with pytest.raises(ValueError, match="s3:// URL"):
                    _require_active_mirror().plan_upload("/local/path", local=tmpdir)

    @patch(BOTO3_PATCH_TARGET)
    def test_plan_download_normalizes_trailing_slash_in_url(self, mock_boto_client):
        """A trailing slash on the input URL must not produce s3://bucket/data//file.txt
        in the plan — Mirror.download normalizes before parsing and plan_* must agree."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "dst"
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                from pos3 import _require_active_mirror

                plan = _require_active_mirror().plan_download(
                    "s3://bucket/data/", local=str(local_dir)
                )

        sources = [src for src, _ in plan.to_copy]
        assert sources == ["s3://bucket/data/file.txt"]
        assert all("//" not in src.replace("s3://", "") for src in sources)

    @patch(BOTO3_PATCH_TARGET)
    def test_plan_download_trailing_slash_forces_directory_listing(self, mock_boto_client):
        """`pos3 download -n s3://bucket/data/` must plan the directory
        contents even if an exact object `data` also exists. Pre-fix,
        plan_download normalized away the slash, head_object('data') won,
        and the plan reported the exact-object copy instead of `data/*`."""
        mock_s3 = _setup_s3_mock(
            mock_boto_client,
            [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}],
        )
        # Exact 'data' object ALSO exists — without preserving the slash,
        # head_object('data') would win and shadow the directory contents.
        mock_s3.head_object.side_effect = None
        mock_s3.head_object.return_value = {"ContentLength": 100}

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                from pos3 import _require_active_mirror

                plan = _require_active_mirror().plan_download(
                    "s3://bucket/data/", local=str(Path(tmpdir) / "dst")
                )

        sources = [src for src, _ in plan.to_copy]
        assert sources == ["s3://bucket/data/file.txt"]

    @patch(BOTO3_PATCH_TARGET)
    def test_plan_upload_normalizes_trailing_slash_in_url(self, mock_boto_client):
        paginate = [{"Contents": [{"Key": "data/orphan.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "src"
            src.mkdir()
            (src / "file.txt").write_text("content")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                from pos3 import _require_active_mirror

                plan = _require_active_mirror().plan_upload(
                    "s3://bucket/data/", local=str(src)
                )

        destinations = [dst for _, dst in plan.to_copy]
        assert destinations == ["s3://bucket/data/file.txt"]
        # And the delete list, which also goes through _make_s3_key for upload.
        assert plan.to_delete == ["s3://bucket/data/orphan.txt"]


class TestTrailingSlashRealTransfers:
    """The real (non-dry-run) download() and upload() paths must preserve
    the user's trailing-slash intent. Mirror.download / _sync_uploads used
    to normalize the URL before scanning, letting head_object('data') win
    over the requested data/ directory listing."""

    @patch(BOTO3_PATCH_TARGET)
    def test_download_trailing_slash_transfers_directory_contents(self, mock_boto_client):
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3
        # Both: an exact 'data' object AND objects under 'data/' exist.
        mock_s3.head_object.return_value = {"ContentLength": 100}
        mock_paginator = Mock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Contents": [{"Key": "data/file.txt", "Size": 5}]}
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            local = Path(tmpdir) / "dst"
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data/", local=str(local), delete=False)

        # The directory content must be the one downloaded, not the exact
        # 'data' object that head_object would have picked up.
        keys = [c[0][1] for c in mock_s3.download_file.call_args_list]
        assert keys == ["data/file.txt"]
        assert "data" not in keys

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_trailing_slash_scans_directory_for_delete(self, mock_boto_client):
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3
        # Exact 'data' object AND an orphan under 'data/' both exist.
        mock_s3.head_object.return_value = {"ContentLength": 100}
        mock_paginator = Mock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Contents": [{"Key": "data/orphan.txt", "Size": 5}]}
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "src"
            source.mkdir()
            (source / "file.txt").write_text("x")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.upload(
                    "s3://bucket/data/",
                    local=str(source),
                    interval=None,
                    delete=True,
                )

        # The directory orphan must be the one deleted, not the exact
        # 'data' object.
        deleted_keys = [c[1]["Key"] for c in mock_s3.delete_object.call_args_list]
        assert "data/orphan.txt" in deleted_keys
        assert "data" not in deleted_keys

    @patch(BOTO3_PATCH_TARGET)
    def test_plan_upload_empty_when_source_missing(self, mock_boto_client):
        """Real _sync_uploads skips registrations whose local_path doesn't
        exist (no transfers, no deletes). plan_upload must mirror that
        instead of reporting every remote object as 'would delete'."""
        _setup_s3_mock(
            mock_boto_client,
            [{"Contents": [{"Key": "data/orphan.txt", "Size": 5}]}],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "does-not-exist"
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                from pos3 import _require_active_mirror

                plan = _require_active_mirror().plan_upload(
                    "s3://bucket/data", local=str(missing)
                )

        assert plan.to_copy == []
        assert plan.to_delete == []


class TestSingleObjectDownload:
    """Downloading an exact S3 object key (not a prefix) must actually fetch
    the file. _scan_s3 used to emit a root directory marker that collided
    with the file in _compute_sync_diff's dict, causing download() to mkdir
    the local path instead of calling download_file."""

    @patch(BOTO3_PATCH_TARGET)
    def test_download_single_object_calls_download_file(self, mock_boto_client):
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3
        # head_object 200 → _list_s3_objects yields the exact object.
        mock_s3.head_object.return_value = {"ContentLength": 42, "Size": 42}
        mock_paginator = Mock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Contents": []}]

        with tempfile.TemporaryDirectory() as tmpdir:
            local = Path(tmpdir) / "results.json"
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/results.json", local=str(local))

        assert mock_s3.download_file.call_count == 1
        args = mock_s3.download_file.call_args[0]
        assert args[0] == "bucket"
        assert args[1] == "results.json"
        assert args[2] == str(local)


class TestMirrorConstructorIsSideEffectFree:
    def test_constructing_mirror_does_not_create_cache_root(self):
        """Constructing a Mirror (entering pos3.mirror()) must not mkdir the
        cache root — dry-run and planning paths rely on this."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_root = Path(tmpdir) / "nested" / "cache" / "root"
            assert not cache_root.exists()

            with s3.mirror(cache_root=str(cache_root), show_progress=False):
                # Just entering the context must not have created cache_root.
                assert not cache_root.exists()


class TestFinalSyncPreservesOriginalException:
    @patch(BOTO3_PATCH_TARGET)
    def test_app_exception_survives_failed_cleanup_sync(self, mock_boto_client):
        """When the mirror body raises AND a sync_on_error upload's cleanup
        sync also fails, the user must see the app exception, not the
        TransferError from cleanup."""
        mock_s3 = _setup_s3_mock(mock_boto_client)
        mock_s3.upload_file.side_effect = RuntimeError("cleanup upload failed")

        class AppError(Exception):
            pass

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "output"
            output.mkdir()
            (output / "data.txt").write_text("content")

            with pytest.raises(AppError, match="the real failure"):
                with s3.mirror(cache_root=tmpdir, show_progress=False):
                    s3.upload(
                        "s3://bucket/output",
                        local=output,
                        interval=None,
                        sync_on_error=True,
                    )
                    raise AppError("the real failure")


class TestLs:
    def test_ls_local_non_recursive(self):
        """Test non-recursive listing excludes nested items."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "file.txt").write_text("x")
            (base / "dir").mkdir()
            (base / "dir" / "nested.txt").write_text("x")

            with s3.mirror(show_progress=False):
                items = s3._require_active_mirror().ls(str(base), recursive=False)

            assert str(base / "dir" / "nested.txt") not in items
            assert str(base / "file.txt") in items

    def test_ls_local_recursive(self):
        """Test recursive listing includes nested items."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "dir").mkdir()
            (base / "dir" / "nested.txt").write_text("x")

            with s3.mirror(show_progress=False):
                items = s3._require_active_mirror().ls(str(base), recursive=True)

            assert str(base / "dir" / "nested.txt") in items

    @patch(BOTO3_PATCH_TARGET)
    def test_ls_s3_non_recursive(self, mock_boto_client):
        """Test non-recursive S3 listing excludes nested items."""
        paginate = [
            {
                "Contents": [
                    {"Key": "data/file.txt", "Size": 5},
                    {"Key": "data/sub/nested.txt", "Size": 10},
                ]
            }
        ]
        _setup_s3_mock(mock_boto_client, paginate)

        with s3.mirror(show_progress=False):
            items = s3._require_active_mirror().ls("s3://bucket/data", recursive=False)

        assert "s3://bucket/data/sub/nested.txt" not in items
        assert "s3://bucket/data/file.txt" in items

    @patch(BOTO3_PATCH_TARGET)
    def test_ls_s3_recursive(self, mock_boto_client):
        """Test recursive S3 listing includes nested items."""
        paginate = [{"Contents": [{"Key": "data/sub/nested.txt", "Size": 10}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with s3.mirror(show_progress=False):
            items = s3._require_active_mirror().ls("s3://bucket/data", recursive=True)

        assert "s3://bucket/data/sub/nested.txt" in items

    @patch(BOTO3_PATCH_TARGET)
    def test_ls_s3_no_spurious_prefix_match(self, mock_boto_client):
        """Test that listing s3://bucket/data doesn't match s3://bucket/data-other."""
        paginate = [
            {
                "Contents": [
                    {"Key": "data/file.txt", "Size": 5},
                    {"Key": "data-other/file.txt", "Size": 10},
                ]
            }
        ]
        _setup_s3_mock(mock_boto_client, paginate)

        with s3.mirror(show_progress=False):
            items = s3._require_active_mirror().ls("s3://bucket/data", recursive=False)

        assert "s3://bucket/data/file.txt" in items
        assert "s3://bucket/data-other/file.txt" not in items


class TestExclude:
    @patch(BOTO3_PATCH_TARGET)
    def test_download_exclude_simple_pattern(self, mock_boto_client):
        """Test that exclude filters out files matching simple patterns."""
        # S3 has file.txt and file.log
        paginate = [
            {
                "Contents": [
                    {"Key": "data/file.txt", "Size": 5},
                    {"Key": "data/file.log", "Size": 10},
                ]
            }
        ]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", local=local_dir, exclude=["*.log"])

            # Should only download file.txt, not file.log
            assert mock_s3.download_file.call_count == 1
            call_args = mock_s3.download_file.call_args_list[0][0]
            assert "file.txt" in call_args[1]  # S3 key
            assert "file.log" not in str(call_args)

    @patch(BOTO3_PATCH_TARGET)
    def test_download_exclude_recursive_pattern(self, mock_boto_client):
        """Test that exclude with ** filters recursively."""
        # S3 has files in nested directories
        paginate = [
            {
                "Contents": [
                    {"Key": "data/file.txt", "Size": 5},
                    {"Key": "data/logs/error.log", "Size": 10},
                    {"Key": "data/logs/debug.log", "Size": 10},
                    {"Key": "data/sub/logs/info.log", "Size": 10},
                ]
            }
        ]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", local=local_dir, exclude=["**/*.log"])

            # Should only download file.txt, not any .log files
            assert mock_s3.download_file.call_count == 1
            call_args = mock_s3.download_file.call_args_list[0][0]
            assert "file.txt" in call_args[1]

    @patch(BOTO3_PATCH_TARGET)
    def test_download_exclude_directory(self, mock_boto_client):
        """Test that excluding a directory excludes all its contents."""
        # S3 has files in multiple directories
        paginate = [
            {
                "Contents": [
                    {"Key": "data/file.txt", "Size": 5},
                    {"Key": "data/logs/", "Size": 0},  # Directory marker
                    {"Key": "data/logs/error.log", "Size": 10},
                    {"Key": "data/logs/debug.log", "Size": 10},
                ]
            }
        ]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", local=local_dir, exclude=["logs"])

            # Should only download file.txt, not logs directory or its contents
            assert mock_s3.download_file.call_count == 1
            call_args = mock_s3.download_file.call_args_list[0][0]
            assert "file.txt" in call_args[1]

    @patch(BOTO3_PATCH_TARGET)
    def test_upload_exclude_pattern(self, mock_boto_client):
        """Test that exclude filters out files during upload."""
        mock_s3 = _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "file.txt").write_text("content")
            (local_dir / "file.log").write_text("log content")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.upload(
                    "s3://bucket/data",
                    local=local_dir,
                    interval=None,
                    exclude=["*.log"],
                )

            # Should only upload file.txt, not file.log
            assert mock_s3.upload_file.call_count == 1
            call_args = mock_s3.upload_file.call_args_list[0][0]
            assert "file.txt" in str(call_args[0])  # Local file path
            assert "file.log" not in str(call_args)

    @patch(BOTO3_PATCH_TARGET)
    def test_sync_exclude_pattern(self, mock_boto_client):
        """Test that exclude filters files during sync in both directions."""
        # S3 has file.txt and remote.log
        paginate = [
            {
                "Contents": [
                    {"Key": "data/file.txt", "Size": 5},
                    {"Key": "data/remote.log", "Size": 10},
                ]
            }
        ]
        mock_s3 = _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            local_dir = Path(tmpdir) / "data"
            local_dir.mkdir()
            (local_dir / "file.txt").write_bytes(b"12345")  # Same size as S3
            (local_dir / "local.log").write_text("local log")

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.sync(
                    "s3://bucket/data",
                    local=local_dir,
                    interval=None,
                    exclude=["*.log"],
                )

            # Should not download remote.log or upload local.log
            # Only file.txt should be considered (and it's already synced)
            assert mock_s3.download_file.call_count == 0  # file.txt already exists with same size
            assert mock_s3.upload_file.call_count == 0  # file.txt already synced, *.log excluded

    @patch(BOTO3_PATCH_TARGET)
    def test_download_exclude_parameter_conflict(self, mock_boto_client):
        """Test that registering download with different exclude param raises error."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", exclude=["*.log"])
                with pytest.raises(ValueError, match="already registered with different parameters"):
                    s3.download("s3://bucket/data", exclude=["*.txt"])

    @patch(BOTO3_PATCH_TARGET)
    def test_exclude_multiple_patterns(self, mock_boto_client):
        """Test that multiple exclude patterns work together."""
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
            local_dir = Path(tmpdir) / "data"

            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", local=local_dir, exclude=["*.log", "*.tmp"])

            # Should only download file.txt
            assert mock_s3.download_file.call_count == 1
            call_args = mock_s3.download_file.call_args_list[0][0]
            assert "file.txt" in call_args[1]


class TestPrefixBoundaryMatching:
    @patch(BOTO3_PATCH_TARGET)
    def test_prefix_boundary_prevents_spurious_matches(self, mock_boto_client):
        """Test that S3 prefix matching respects path boundaries.

        When downloading s3://bucket/data/, should NOT match s3://bucket/data_backup/
        This is a regression test for the bug where "droid/recovery" matched "droid/recovery_towels"
        """
        mock_s3 = _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                mirror_obj = s3._require_active_mirror()

                # Simulate listing objects - should add "/" to prefix when key doesn't end with "/"
                _ = list(mirror_obj._list_s3_objects("bucket", "data", None))

                # Verify that paginate was called with "data/" (with trailing slash)
                paginator_calls = mock_s3.get_paginator.return_value.paginate.call_args_list
                assert len(paginator_calls) == 1
                call_kwargs = paginator_calls[0][1]
                assert (
                    call_kwargs["Prefix"] == "data/"
                ), f"Expected Prefix='data/' but got Prefix='{call_kwargs['Prefix']}'"

    @patch(BOTO3_PATCH_TARGET)
    def test_prefix_boundary_with_trailing_slash(self, mock_boto_client):
        """Test that keys already ending with '/' don't get double slashes."""
        mock_s3 = _setup_s3_mock(mock_boto_client)

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                mirror_obj = s3._require_active_mirror()

                # List with trailing slash already present
                _ = list(mirror_obj._list_s3_objects("bucket", "data/", None))

                # Should use "data/" as-is, not "data//"
                paginator_calls = mock_s3.get_paginator.return_value.paginate.call_args_list
                assert len(paginator_calls) == 1
                call_kwargs = paginator_calls[0][1]
                assert call_kwargs["Prefix"] == "data/"

    @patch(BOTO3_PATCH_TARGET)
    def test_single_file_download_bypasses_list(self, mock_boto_client):
        """Test that single file downloads use head_object and don't list with prefix."""
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3

        # Mock head_object to return a valid file
        mock_s3.head_object.return_value = {"ContentLength": 1234, "ETag": "abc123"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                mirror_obj = s3._require_active_mirror()

                # List a single file (no trailing slash)
                results = list(mirror_obj._list_s3_objects("bucket", "data/file.txt", None))

                # Should have called head_object and returned the file
                assert mock_s3.head_object.call_count == 1
                assert len(results) == 1
                assert results[0]["Key"] == "data/file.txt"
                assert results[0]["Size"] == 1234

                # Should NOT have called paginate
                assert mock_s3.get_paginator.call_count == 0

    @patch(BOTO3_PATCH_TARGET)
    def test_directory_without_trailing_slash_gets_slash_added(self, mock_boto_client):
        """Test that downloading a directory without trailing slash still works correctly.

        User scenario: download('s3://bucket/my_dir') where my_dir is a directory.
        The fix should:
        1. Try head_object('my_dir') first
        2. Get 404 (not a single file)
        3. Add trailing slash and list with Prefix='my_dir/'
        4. Only match 'my_dir/*', NOT 'my_dir_backup/*'
        """
        mock_s3 = _setup_s3_mock(mock_boto_client)

        # Mock paginator to return directory contents
        mock_paginator = Mock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Contents": [{"Key": "my_dir/file1.txt", "Size": 100}, {"Key": "my_dir/file2.txt", "Size": 200}]}
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                mirror_obj = s3._require_active_mirror()

                # User downloads directory without trailing slash (after normalization: key="my_dir")
                results = list(mirror_obj._list_s3_objects("bucket", "my_dir", None))

                # Should have tried head_object first
                assert mock_s3.head_object.call_count == 1
                head_call_key = mock_s3.head_object.call_args[1]["Key"]
                assert head_call_key == "my_dir"

                # After getting 404, should have listed with trailing slash
                paginate_calls = mock_paginator.paginate.call_args_list
                assert len(paginate_calls) == 1
                prefix_used = paginate_calls[0][1]["Prefix"]
                assert prefix_used == "my_dir/", f"Expected 'my_dir/' but got '{prefix_used}'"

                # Should have returned the directory contents
                assert len(results) == 2


class TestProfile:
    def setup_method(self):
        """Clear registered profiles before each test."""
        s3._PROFILES.clear()

    def test_register_profile_success(self):
        """Test that register_profile stores the profile correctly."""
        s3.register_profile(
            "test-profile",
            endpoint="https://storage.example.com",
            public=True,
            region="us-west-2",
        )

        assert "test-profile" in s3._PROFILES
        profile = s3._PROFILES["test-profile"]
        assert profile.endpoint == "https://storage.example.com"
        assert profile.public is True
        assert profile.region == "us-west-2"

    def test_register_profile_duplicate_same_config(self):
        """Test that registering same profile with identical config is no-op."""
        s3.register_profile("test-profile", endpoint="https://storage.example.com", public=True)
        # Should not raise
        s3.register_profile("test-profile", endpoint="https://storage.example.com", public=True)

        assert "test-profile" in s3._PROFILES

    def test_register_profile_duplicate_different_config(self):
        """Test that registering same profile with different config raises error."""
        s3.register_profile("test-profile", endpoint="https://storage.example.com", public=True)

        with pytest.raises(ValueError, match="already registered with different config"):
            s3.register_profile("test-profile", endpoint="https://other.example.com", public=True)

    def test_profile_local_name_underscore_reserved(self):
        """Test that local_name='_' is reserved and raises error."""
        from pos3 import Profile

        with pytest.raises(ValueError, match="reserved for default"):
            Profile(local_name="_", endpoint="https://storage.example.com")

    def test_profile_local_name_invalid_chars_rejected(self):
        """Test that local_name with invalid characters is rejected."""
        from pos3 import Profile

        invalid_names = ["../escape", "/absolute", "sub/dir", "with space", "with.dot", ""]
        for name in invalid_names:
            with pytest.raises(ValueError, match="Invalid local_name"):
                Profile(local_name=name, endpoint="https://storage.example.com")

        # Valid names should work
        Profile(local_name="valid-name", endpoint="https://storage.example.com")
        Profile(local_name="valid_name", endpoint="https://storage.example.com")
        Profile(local_name="ValidName123", endpoint="https://storage.example.com")

    def test_profile_partial_credentials_rejected(self):
        """Half-set access_key/secret_key would fall back to ambient creds; reject at construction."""
        from pos3 import Profile

        common = {"local_name": "p", "endpoint": "https://s.example.com"}
        with pytest.raises(ValueError, match="must be set together"):
            Profile(**common, access_key="AKIA")
        with pytest.raises(ValueError, match="must be set together"):
            Profile(**common, secret_key="shh")
        # Both set or both absent is fine.
        Profile(**common, access_key="AKIA", secret_key="shh")
        Profile(**common)

    def test_create_client_unknown_profile(self):
        """Test that using unknown profile raises error."""
        with pytest.raises(ValueError, match="Unknown profile"):
            s3._resolve_profile("nonexistent-profile")

    @patch(BOTO3_PATCH_TARGET)
    def test_create_client_with_public_profile(self, mock_boto_client):
        """Test that public profile creates client with UNSIGNED signature."""
        from botocore import UNSIGNED

        from pos3 import Profile

        profile = Profile(local_name="test", endpoint="https://storage.example.com", public=True)
        s3._create_s3_client(profile)

        mock_boto_client.assert_called_once()
        call_kwargs = mock_boto_client.call_args[1]
        assert call_kwargs["endpoint_url"] == "https://storage.example.com"
        assert call_kwargs["config"].signature_version == UNSIGNED

    @patch(BOTO3_PATCH_TARGET)
    def test_create_client_with_profile_object(self, mock_boto_client):
        """Test that inline Profile object works without registration."""
        from pos3 import Profile

        profile = Profile(local_name="inline", endpoint="https://storage.example.com", public=False, region="eu-west-1")
        s3._create_s3_client(profile)

        mock_boto_client.assert_called_once()
        call_kwargs = mock_boto_client.call_args[1]
        assert call_kwargs["endpoint_url"] == "https://storage.example.com"
        assert call_kwargs["region_name"] == "eu-west-1"
        assert "config" not in call_kwargs  # Not public, no UNSIGNED

    @patch(BOTO3_PATCH_TARGET)
    def test_download_with_profile(self, mock_boto_client):
        """Test that download with profile uses correct S3 client."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        s3.register_profile("test-profile", endpoint="https://storage.example.com", public=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                s3.download("s3://bucket/data", profile="test-profile")

        # Client should have been created with profile settings
        assert mock_boto_client.call_count >= 1
        # Find the call with our endpoint
        found_profile_call = False
        for call in mock_boto_client.call_args_list:
            if call[1].get("endpoint_url") == "https://storage.example.com":
                found_profile_call = True
                break
        assert found_profile_call, "Expected client to be created with profile endpoint"

    @patch(BOTO3_PATCH_TARGET)
    def test_mirror_default_profile(self, mock_boto_client):
        """Test that default_profile on mirror() is used when no profile specified."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        s3.register_profile("default-test", endpoint="https://default.example.com", public=False)

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False, default_profile="default-test"):
                s3.download("s3://bucket/data")

        # Client should have been created with default profile settings
        found_default_call = False
        for call in mock_boto_client.call_args_list:
            if call[1].get("endpoint_url") == "https://default.example.com":
                found_default_call = True
                break
        assert found_default_call, "Expected client to be created with default profile endpoint"

    @patch(BOTO3_PATCH_TARGET)
    def test_profile_override_default(self, mock_boto_client):
        """Test that explicit profile parameter overrides default_profile."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        s3.register_profile("default-test", endpoint="https://default.example.com")
        s3.register_profile("override-test", endpoint="https://override.example.com")

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False, default_profile="default-test"):
                s3.download("s3://bucket/data", profile="override-test")

        # Client should have been created with override profile, not default
        found_override_call = False
        for call in mock_boto_client.call_args_list:
            if call[1].get("endpoint_url") == "https://override.example.com":
                found_override_call = True
                break
        assert found_override_call, "Expected client to be created with override profile endpoint"

    @patch(BOTO3_PATCH_TARGET)
    def test_implicit_and_explicit_default_profile_no_conflict(self, mock_boto_client):
        """Test that implicit None and explicit default profile don't conflict."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        s3.register_profile("my-profile", endpoint="https://storage.example.com")

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False, default_profile="my-profile"):
                # First call with implicit profile (None -> uses default)
                path1 = s3.download("s3://bucket/data")
                # Second call with explicit default profile - should NOT conflict
                path2 = s3.download("s3://bucket/data", profile="my-profile")

                assert path1 == path2

    @patch(BOTO3_PATCH_TARGET)
    def test_same_url_different_profiles_no_conflict(self, mock_boto_client):
        """Test that same S3 URL with different profiles doesn't conflict."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        s3.register_profile("profile-a", endpoint="https://a.example.com")
        s3.register_profile("profile-b", endpoint="https://b.example.com")

        with tempfile.TemporaryDirectory() as tmpdir:
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                # Same S3 URL but different profiles - should NOT conflict
                path_a = s3.download("s3://bucket/data", profile="profile-a")
                path_b = s3.download("s3://bucket/data", profile="profile-b")

                # Different cache paths due to different local_names
                assert path_a != path_b
                assert "profile-a" in str(path_a)
                assert "profile-b" in str(path_b)

    @patch(BOTO3_PATCH_TARGET)
    def test_with_mirror_resolves_profile_at_call_time(self, mock_boto_client):
        """Test that with_mirror resolves profile when function is called, not at decoration."""
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        # Define decorated function BEFORE registering profile
        @s3.with_mirror(show_progress=False, default_profile="late-profile")
        def do_download():
            return s3.download("s3://bucket/data")

        # Register profile AFTER decoration
        s3.register_profile("late-profile", endpoint="https://late.example.com")

        # Should work - profile resolved at call time
        with tempfile.TemporaryDirectory():
            do_download()

        # Verify the late-registered profile was used
        found_late_call = False
        for call in mock_boto_client.call_args_list:
            if call[1].get("endpoint_url") == "https://late.example.com":
                found_late_call = True
                break
        assert found_late_call, "Expected profile to be resolved at call time"


class TestUrlProfileParsing:
    def test_url_profile_extracted(self):
        assert s3._url_profile("s3://acme@bucket/key") == "acme"

    def test_url_profile_absent(self):
        assert s3._url_profile("s3://bucket/key") is None

    def test_url_profile_non_s3(self):
        assert s3._url_profile("/local/path") is None

    def test_empty_url_profile_selector_raises(self):
        """An '@' with no profile name (e.g. from a template variable that expanded empty)
        must fail loudly, not silently fall back to arg/default profile."""
        with pytest.raises(ValueError, match="Empty profile selector"):
            s3._url_profile("s3://@bucket/key")
        with pytest.raises(ValueError, match="Empty profile selector"):
            s3._url_profile("s3://:token@bucket/key")

    def test_parse_strips_userinfo(self):
        assert s3._parse_s3_url("s3://acme@bucket/path/to/data") == ("bucket", "path/to/data")

    def test_normalize_strips_userinfo(self):
        assert s3._normalize_s3_url("s3://acme@bucket/path/") == "s3://bucket/path"


class TestProfileRegistry:
    def setup_method(self):
        s3._PROFILES.clear()
        s3.profiles._REGISTRY_PROFILES.clear()
        s3.profiles._REGISTRY_LOADED = False
        self._saved_env = os.environ.get("POS3_PROFILES_FILE")

    def teardown_method(self):
        s3._PROFILES.clear()
        s3.profiles._REGISTRY_PROFILES.clear()
        s3.profiles._REGISTRY_LOADED = False
        if self._saved_env is None:
            os.environ.pop("POS3_PROFILES_FILE", None)
        else:
            os.environ["POS3_PROFILES_FILE"] = self._saved_env

    def _write_registry(self, tmpdir, body):
        path = Path(tmpdir) / "profiles.toml"
        path.write_text(body)
        os.environ["POS3_PROFILES_FILE"] = str(path)
        return path

    def test_registry_loaded_lazily(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_registry(
                tmpdir,
                '[profiles.acme]\nendpoint = "https://s3.acme.example.com"\nregion = "us-east-1"\n',
            )
            profile = s3._resolve_profile("acme")

        assert profile.endpoint == "https://s3.acme.example.com"
        assert profile.region == "us-east-1"
        assert profile.local_name == "acme"

    def test_unknown_profile_hard_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_registry(tmpdir, '[profiles.known]\nendpoint = "https://k.example.com"\n')
            with pytest.raises(ValueError, match="Unknown profile"):
                s3._resolve_profile("missing")

    def test_missing_endpoint_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_registry(tmpdir, '[profiles.bad]\nregion = "us-east-1"\n')
            with pytest.raises(ValueError, match="missing required 'endpoint'"):
                s3._resolve_profile("bad")

    def test_programmatic_registration_wins(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_registry(tmpdir, '[profiles.dup]\nendpoint = "https://from-file.example.com"\n')
            s3.register_profile("dup", endpoint="https://from-code.example.com")
            profile = s3._resolve_profile("dup")

        assert profile.endpoint == "https://from-code.example.com"

    def test_code_override_after_registry_load(self):
        """Code-precedence must hold even when the registry was loaded first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_registry(
                tmpdir,
                '[profiles.dup]\nendpoint = "https://from-file.example.com"\n'
                '[profiles.other]\nendpoint = "https://other.example.com"\n',
            )
            # Resolving any name first triggers registry load, populating "dup".
            assert s3._resolve_profile("other").endpoint == "https://other.example.com"
            # Now overriding "dup" in code must succeed and win.
            s3.register_profile("dup", endpoint="https://from-code.example.com")
            assert s3._resolve_profile("dup").endpoint == "https://from-code.example.com"

    def test_credentials_file_relative_to_registry(self):
        """A relative credentials_file path must resolve next to profiles.toml, not CWD."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "acme.creds").write_text(
                "[acme]\naws_access_key_id = AKIAEXAMPLE\naws_secret_access_key = secret123\n"
            )
            # Bare filename — would fail against CWD, must resolve against the registry dir.
            self._write_registry(
                tmpdir,
                '[profiles.acme]\nendpoint = "https://s.example.com"\ncredentials_file = "acme.creds"\n',
            )
            saved_cwd = os.getcwd()
            try:
                os.chdir(Path(tmpdir).parent)  # any dir that doesn't contain acme.creds
                profile = s3._resolve_profile("acme")
            finally:
                os.chdir(saved_cwd)

        assert profile.access_key == "AKIAEXAMPLE"
        assert profile.secret_key == "secret123"

    def test_forced_reload_drops_stale_entries(self):
        """force=True must replace the registry snapshot, not merge into it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_registry(
                tmpdir,
                '[profiles.alpha]\nendpoint = "https://a.example.com"\n'
                '[profiles.beta]\nendpoint = "https://b.example.com"\n',
            )
            assert s3._resolve_profile("alpha").endpoint == "https://a.example.com"
            assert s3._resolve_profile("beta").endpoint == "https://b.example.com"

            # Rewrite the registry without 'beta' and force a reload.
            self._write_registry(tmpdir, '[profiles.alpha]\nendpoint = "https://a2.example.com"\n')
            s3.profiles._load_profile_registry(force=True)

            assert s3._resolve_profile("alpha").endpoint == "https://a2.example.com"
            with pytest.raises(ValueError, match="Unknown profile"):
                s3._resolve_profile("beta")

    def test_credentials_file_missing_secret_raises(self):
        """A half-populated credentials file must fail loudly, not fall back to ambient creds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            creds = Path(tmpdir) / "acme.creds"
            creds.write_text("[acme]\naws_access_key_id = AKIAEXAMPLE\n")  # no secret
            self._write_registry(
                tmpdir,
                f'[profiles.acme]\nendpoint = "https://s.example.com"\ncredentials_file = "{creds}"\n',
            )
            with pytest.raises(ValueError, match="aws_access_key_id.*aws_secret_access_key"):
                s3._resolve_profile("acme")

    def test_credentials_file_wrong_section_raises(self):
        """A typo in the section name must not silently bind credentials from an unrelated section."""
        with tempfile.TemporaryDirectory() as tmpdir:
            creds = Path(tmpdir) / "acme.creds"
            # Typo: section says 'acne' instead of 'acme'; no [default] either.
            creds.write_text("[acne]\naws_access_key_id = WRONG\naws_secret_access_key = wrong\n")
            self._write_registry(
                tmpdir,
                f'[profiles.acme]\nendpoint = "https://s.example.com"\ncredentials_file = "{creds}"\n',
            )
            with pytest.raises(ValueError, match=r"\[acme\] or \[default\] section"):
                s3._resolve_profile("acme")

    def test_load_failure_is_not_sticky(self):
        """A malformed registry must not flip _REGISTRY_LOADED on; subsequent loads should retry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_registry(tmpdir, '[profiles.bad]\nregion = "us-east-1"\n')  # missing endpoint
            with pytest.raises(ValueError, match="missing required 'endpoint'"):
                s3._resolve_profile("bad")
            assert s3.profiles._REGISTRY_LOADED is False

            # Repair the registry and retry without manual reset.
            self._write_registry(tmpdir, '[profiles.bad]\nendpoint = "https://fixed.example.com"\n')
            profile = s3._resolve_profile("bad")
            assert profile.endpoint == "https://fixed.example.com"

    def test_credentials_file_isolated_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            creds = Path(tmpdir) / "acme.creds"
            creds.write_text("[acme]\naws_access_key_id = AKIAEXAMPLE\naws_secret_access_key = secret123\n")
            self._write_registry(
                tmpdir,
                f'[profiles.acme]\nendpoint = "https://s3.acme.example.com"\n'
                f'region = "eu-west-1"\ncredentials_file = "{creds}"\n',
            )
            profile = s3._resolve_profile("acme")
            assert profile.access_key == "AKIAEXAMPLE"
            assert profile.secret_key == "secret123"

            with patch(SESSION_PATCH_TARGET) as mock_session:
                s3._create_s3_client(profile)

            mock_session.assert_called_once()
            session_kwargs = mock_session.call_args[1]
            assert session_kwargs["aws_access_key_id"] == "AKIAEXAMPLE"
            assert session_kwargs["aws_secret_access_key"] == "secret123"
            assert session_kwargs["region_name"] == "eu-west-1"
            client_kwargs = mock_session.return_value.client.call_args[1]
            assert client_kwargs["endpoint_url"] == "https://s3.acme.example.com"

    def test_secrets_not_in_repr(self):
        profile = s3.Profile(
            local_name="acme",
            endpoint="https://s3.acme.example.com",
            access_key="AKIAEXAMPLE",
            secret_key="topsecret",
        )
        assert "topsecret" not in repr(profile)
        assert "AKIAEXAMPLE" not in repr(profile)

    @patch(BOTO3_PATCH_TARGET)
    def test_url_profile_takes_precedence_over_argument(self, mock_boto_client):
        paginate = [{"Contents": [{"Key": "data/file.txt", "Size": 5}]}]
        _setup_s3_mock(mock_boto_client, paginate)

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_registry(
                tmpdir,
                '[profiles.url_prof]\nendpoint = "https://url.example.com"\n'
                '[profiles.arg_prof]\nendpoint = "https://arg.example.com"\n',
            )
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                path = s3.download("s3://url_prof@bucket/data", profile="arg_prof")

            # Cache path keyed by the URL profile's local_name, not the argument.
            assert "url_prof" in str(path)
            assert "arg_prof" not in str(path)

        endpoints = [c[1].get("endpoint_url") for c in mock_boto_client.call_args_list]
        assert "https://url.example.com" in endpoints
        assert "https://arg.example.com" not in endpoints

    @patch(BOTO3_PATCH_TARGET)
    def test_url_unknown_profile_hard_error(self, mock_boto_client):
        _setup_s3_mock(mock_boto_client)
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_registry(tmpdir, '[profiles.known]\nendpoint = "https://k.example.com"\n')
            with s3.mirror(cache_root=tmpdir, show_progress=False):
                with pytest.raises(ValueError, match="Unknown profile"):
                    s3.download("s3://ghost@bucket/data")
