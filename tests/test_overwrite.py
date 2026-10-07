from pathlib import Path

import pytest
from botocore.exceptions import ClientError

import pos3


@pytest.fixture
def storage(monkeypatch):
    class Storage:
        def __init__(self):
            self.objects = {}
            self.uploads = []
            self.deletes = []

        def head_object(self, *, Bucket, Key):
            if (Bucket, Key) not in self.objects:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return {"ContentLength": len(self.objects[Bucket, Key])}

        def get_paginator(self, operation):
            assert operation == "list_objects_v2"
            return self

        def paginate(self, *, Bucket, Prefix):
            yield {
                "Contents": [
                    {"Key": key, "Size": len(body)}
                    for (bucket, key), body in self.objects.items()
                    if bucket == Bucket and key.startswith(Prefix)
                ]
            }

        def upload_file(self, filename, bucket, key, *, Callback):
            body = Path(filename).read_bytes()
            self.objects[bucket, key] = body
            self.uploads.append(key)
            Callback(len(body))

        def put_object(self, *, Bucket, Key, Body):
            self.objects[Bucket, Key] = Body

        def download_file(self, bucket, key, filename, *, Callback):
            body = self.objects[bucket, key]
            Path(filename).write_bytes(body)
            Callback(len(body))

        def delete_object(self, *, Bucket, Key):
            self.deletes.append(Key)
            del self.objects[Bucket, Key]

    result = Storage()
    monkeypatch.setattr(pos3._Mirror, "_get_client", lambda self, profile=None: result)
    return result


@pytest.mark.parametrize("method", ["upload", "sync"])
@pytest.mark.parametrize("patterns", [None, [], [".recording-attempts.sqlite", "run_metadata_*.yaml"]])
def test_selected_same_size_files_are_overwritten_without_deleting_remote_objects(tmp_path, storage, method, patterns):
    names = [".recording-attempts.sqlite", "run_metadata_1.yaml", "episode.parquet"]
    for name in names:
        (tmp_path / name).write_bytes(b"new")
        storage.objects["bucket", "run/" + name] = b"old"
    storage.objects["bucket", "run/remote-only"] = b"keep"
    with pos3.mirror(show_progress=False):
        plan = pos3.plan_upload("s3://bucket/run/", tmp_path, overwrite=patterns)
        kwargs = {"delete": False} if method == "upload" else {"delete_remote": False}
        getattr(pos3, method)("s3://bucket/run/", tmp_path, interval=None, overwrite=patterns, **kwargs)
        expected = names[:2] if patterns else []
        assert {remote for _, remote in plan.to_copy} == {"s3://bucket/run/" + name for name in expected}
        assert storage.uploads == []
    assert set(storage.uploads) == {"run/" + name for name in expected}
    assert storage.deletes == []
    assert storage.objects["bucket", "run/remote-only"] == b"keep"
    for name in names:
        assert storage.objects["bucket", "run/" + name] == (b"new" if name in expected else b"old")
    with pos3.mirror(show_progress=False):
        readback = pos3.download("s3://bucket/run/", local=tmp_path / "readback")
    for name in names:
        assert (readback / name).read_bytes() == (b"new" if name in expected else b"old")


def test_overwrite_selection_survives_registration_replay_and_respects_exclusion(tmp_path, storage):
    (tmp_path / "journal").write_bytes(b"new")
    storage.objects["bucket", "run/journal"] = b"old"
    with pos3.mirror(show_progress=False):
        pos3.upload(
            "s3://bucket/run/", tmp_path, interval=None, delete=False, overwrite=["journal"], exclude=["journal"]
        )
        pos3.upload(
            "s3://bucket/run/", tmp_path, interval=None, delete=False, overwrite=["journal"], exclude=["journal"]
        )
        with pytest.raises(ValueError, match="different parameters"):
            pos3.upload("s3://bucket/run/", tmp_path, interval=None, delete=False, overwrite=[], exclude=["journal"])
        assert pos3.plan_upload("s3://bucket/run/", tmp_path, overwrite=["journal"], exclude=["journal"]).to_copy == []
    assert storage.uploads == []
    assert storage.objects["bucket", "run/journal"] == b"old"


def test_sync_overwrite_keeps_size_only_download_and_uploads_selected_local_bytes(tmp_path, storage):
    local = tmp_path / "journal"
    local.write_bytes(b"local")
    storage.objects["bucket", "run/journal"] = b"other"
    with pos3.mirror(show_progress=False):
        pos3.sync("s3://bucket/run/", tmp_path, interval=None, delete_remote=False, overwrite=["journal"])
        assert local.read_bytes() == b"local"
        assert storage.objects["bucket", "run/journal"] == b"other"
    assert storage.objects["bucket", "run/journal"] == b"local"


def test_directory_name_does_not_select_its_contents(tmp_path, storage):
    directory = tmp_path / "state"
    directory.mkdir()
    (directory / "journal").write_bytes(b"new")
    storage.objects["bucket", "run/state/journal"] = b"old"
    storage.objects["bucket", "run/state/"] = b""
    with pos3.mirror(show_progress=False):
        assert pos3.plan_upload("s3://bucket/run/", tmp_path, overwrite=["state"]).to_copy == []
        pos3.upload("s3://bucket/run/", tmp_path, interval=None, delete=False, overwrite=["state"])
    assert storage.objects["bucket", "run/state/journal"] == b"old"


@pytest.mark.parametrize("method", ["upload", "sync"])
@pytest.mark.parametrize("patterns", [["journal"], ["state/journal"]])
def test_right_relative_patterns_select_and_copy_nested_same_size_files(tmp_path, storage, method, patterns):
    directory = tmp_path / "state"
    directory.mkdir()
    for name in ["journal", "other"]:
        (directory / name).write_bytes(b"new")
        storage.objects["bucket", "run/state/" + name] = b"old"
    storage.objects["bucket", "run/state/remote-only"] = b"keep"
    with pos3.mirror(show_progress=False):
        plan = pos3.plan_upload("s3://bucket/run/", tmp_path, overwrite=patterns)
        assert plan.to_copy == [(str(directory / "journal"), "s3://bucket/run/state/journal")]
        kwargs = {"delete": False} if method == "upload" else {"delete_remote": False}
        getattr(pos3, method)("s3://bucket/run/", tmp_path, interval=None, overwrite=patterns, **kwargs)
        assert (directory / "journal").read_bytes() == b"new"
        assert storage.objects["bucket", "run/state/journal"] == b"old"
    assert storage.uploads == ["run/state/journal"]
    assert storage.objects["bucket", "run/state/journal"] == b"new"
    assert storage.objects["bucket", "run/state/other"] == b"old"
    assert storage.objects["bucket", "run/state/remote-only"] == b"keep"
    assert storage.deletes == []


@pytest.mark.parametrize("method", ["upload", "sync"])
@pytest.mark.parametrize(
    "patterns,selected",
    [(None, False), ([], False), (["*"], True), (["journal"], True), (["remote-journal"], False), (["state/journal"], False)],
)
def test_single_file_overwrite_matches_source_basename_and_retains_remote_objects(
    tmp_path, storage, method, patterns, selected
):
    local = tmp_path / "journal"
    local.write_bytes(b"new")
    remote = "s3://bucket/run/remote-journal"
    storage.objects["bucket", "run/remote-journal"] = b"old"
    storage.objects["bucket", "run/remote-only"] = b"keep"
    with pos3.mirror(show_progress=False):
        plan = pos3.plan_upload(remote, local, overwrite=patterns)
        assert plan.to_copy == ([(str(local), remote)] if selected else [])
        assert plan.to_delete == []
        assert storage.uploads == []
        assert storage.deletes == []
        kwargs = {"delete": False} if method == "upload" else {"delete_remote": False}
        getattr(pos3, method)(remote, local, interval=None, overwrite=patterns, **kwargs)
        assert local.read_bytes() == b"new"
        assert storage.objects["bucket", "run/remote-journal"] == b"old"
    assert storage.uploads == (["run/remote-journal"] if selected else [])
    assert storage.deletes == []
    assert storage.objects["bucket", "run/remote-journal"] == (b"new" if selected else b"old")
    assert storage.objects["bucket", "run/remote-only"] == b"keep"
    with pos3.mirror(show_progress=False):
        readback = pos3.download(remote, local=tmp_path / "readback")
    assert readback.read_bytes() == (b"new" if selected else b"old")

@pytest.mark.parametrize(
    "upload_exclude,expected", [(None, {"upload"}), ([], {"general", "upload"}), (["upload"], {"general"})]
)
def test_sync_upload_exclusions_keep_the_download_exclusions(tmp_path, storage, upload_exclude, expected):
    storage.objects["bucket", "run/general"] = b"excluded download"
    storage.objects["bucket", "run/baseline"] = b"retained"
    with pos3.mirror(show_progress=False):
        pos3.sync(
            "s3://bucket/run/",
            tmp_path,
            interval=None,
            delete_remote=False,
            exclude=["general"],
            upload_exclude=upload_exclude,
        )
        assert not (tmp_path / "general").exists()
        assert (tmp_path / "baseline").read_bytes() == b"retained"
        (tmp_path / "general").write_bytes(b"new")
        (tmp_path / "upload").write_bytes(b"new")
    assert set(storage.uploads) == {"run/" + name for name in expected}
    assert storage.deletes == []


def test_sync_can_download_baseline_markers_without_uploading_new_open_markers(tmp_path, storage):
    storage.objects["bucket", "run/baseline/.unfinished"] = b"unfinished"
    storage.objects["bucket", "run/foreign"] = b"retained"
    with pos3.mirror(show_progress=False):
        pos3.sync("s3://bucket/run/", tmp_path, interval=None, delete_remote=False, upload_exclude=[".unfinished"])
        assert (tmp_path / "baseline/.unfinished").read_bytes() == b"unfinished"
        (tmp_path / "new").mkdir()
        (tmp_path / "new/.unfinished").write_bytes(b"unfinished")
        (tmp_path / "new/data").write_bytes(b"recording")
    assert ("bucket", "run/new/.unfinished") not in storage.objects
    assert storage.objects["bucket", "run/new/data"] == b"recording"
    assert storage.objects["bucket", "run/baseline/.unfinished"] == b"unfinished"
    assert storage.objects["bucket", "run/foreign"] == b"retained"
    assert storage.deletes == []

    storage.objects["bucket", "run/late-foreign"] = b"arrived after phase one"
    assert not (tmp_path / "late-foreign").exists()
    with pos3.mirror(show_progress=False):
        pos3.upload("s3://bucket/run/", tmp_path, interval=None, delete=False)
    assert storage.objects["bucket", "run/new/.unfinished"] == b"unfinished"
    assert storage.objects["bucket", "run/baseline/.unfinished"] == b"unfinished"
    assert storage.objects["bucket", "run/foreign"] == b"retained"
    assert storage.objects["bucket", "run/late-foreign"] == b"arrived after phase one"
    assert storage.deletes == []


@pytest.mark.parametrize("delete_remote", [False, True])
@pytest.mark.parametrize("upload_exclude", [None, []])
def test_sync_narrowed_upload_exclusions_obey_remote_deletion(tmp_path, storage, delete_remote, upload_exclude):
    storage.objects["bucket", "run/general"] = b"excluded download"
    storage.objects["bucket", "run/baseline"] = b"retained"
    with pos3.mirror(show_progress=False):
        pos3.sync(
            "s3://bucket/run/",
            tmp_path,
            interval=None,
            delete_remote=delete_remote,
            exclude=["general"],
            upload_exclude=upload_exclude,
        )
        assert not (tmp_path / "general").exists()
        assert (tmp_path / "baseline").read_bytes() == b"retained"
    if delete_remote and upload_exclude == []:
        assert ("bucket", "run/general") not in storage.objects
        assert storage.deletes == ["run/general"]
    else:
        assert storage.objects["bucket", "run/general"] == b"excluded download"
        assert storage.deletes == []
    assert storage.objects["bucket", "run/baseline"] == b"retained"
    assert storage.uploads == []


def test_registered_overwrite_selection_survives_caller_list_mutation(tmp_path, storage):
    for name in ["journal", "other"]:
        (tmp_path / name).write_bytes(b"new")
        storage.objects["bucket", "run/" + name] = b"old"
    patterns = ["journal"]
    with pos3.mirror(show_progress=False):
        pos3.upload("s3://bucket/run/", tmp_path, interval=None, delete=False, overwrite=patterns)
        patterns[:] = ["other"]
        with pytest.raises(ValueError, match="different parameters"):
            pos3.upload("s3://bucket/run/", tmp_path, interval=None, delete=False, overwrite=["other"])
    assert storage.uploads == ["run/journal"]
    assert storage.objects["bucket", "run/journal"] == b"new"
    assert storage.objects["bucket", "run/other"] == b"old"
    assert storage.deletes == []
