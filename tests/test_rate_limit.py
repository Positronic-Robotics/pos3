import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import pos3 as s3
from pos3.rate_limit import ByteRateLimiter


class _FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class TestByteRateLimiter:
    def test_callers_queue_on_one_timeline(self):
        fake = _FakeTime()
        limiter = ByteRateLimiter(100, clock=fake.clock, sleep=fake.sleep)

        for _ in range(3):
            limiter.consume(100)

        assert fake.sleeps == [1.0, 2.0, 3.0]

    def test_an_idle_limiter_builds_no_credit(self):
        fake = _FakeTime()
        limiter = ByteRateLimiter(100, clock=fake.clock, sleep=fake.sleep)
        limiter.consume(100)

        fake.now = 60.0
        limiter.consume(50)

        assert fake.sleeps == [1.0, 0.5]

    def test_a_rewind_is_not_paced(self):
        fake = _FakeTime()
        limiter = ByteRateLimiter(100, clock=fake.clock, sleep=fake.sleep)

        limiter.consume(-4096)
        limiter.consume(0)

        assert fake.sleeps == []

    def test_a_rate_must_be_positive(self):
        with pytest.raises(ValueError, match="positive"):
            ByteRateLimiter(0)


class _S3Stub(BaseHTTPRequestHandler):
    """Just enough of the S3 API for one upload sync: an empty listing, then PUTs."""

    received: dict[str, int]
    # HTTP/1.1 answers botocore's `Expect: 100-continue` at once; 1.0 makes each PUT wait a second for it.
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_HEAD(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        body = b'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult><KeyCount>0</KeyCount></ListBucketResult>'
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        self.received[self.path] = len(self._read_body())
        self.send_response(200)
        self.send_header("ETag", '"stub"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self) -> bytes:
        if "chunked" not in self.headers.get("Transfer-Encoding", ""):
            return self.rfile.read(int(self.headers["Content-Length"]))
        body = b""
        while True:
            size = int(self.rfile.readline().split(b";")[0], 16)
            body += self.rfile.read(size)
            self.rfile.readline()
            if size == 0:
                return body


@pytest.fixture
def s3_stub():
    received: dict[str, int] = {}
    handler = type("Handler", (_S3Stub,), {"received": received})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = s3.Profile(
        local_name="stub",
        endpoint=f"http://127.0.0.1:{server.server_address[1]}",
        region="us-east-1",
        access_key="stub-key",
        secret_key="stub-secret",
    )
    yield profile, received
    server.shutdown()


def _upload_sync_seconds(tmp_path: Path, profile: s3.Profile, max_upload_bytes_per_second: float | None) -> float:
    started = time.monotonic()
    with s3.mirror(
        cache_root=str(tmp_path / "cache"),
        show_progress=False,
        max_workers=8,
        max_upload_bytes_per_second=max_upload_bytes_per_second,
    ):
        s3.upload("s3://bucket/episodes", local=tmp_path / "episodes", interval=None, profile=profile)
    return time.monotonic() - started


def test_the_cap_holds_across_parallel_files(tmp_path, s3_stub):
    profile, received = s3_stub
    episodes = tmp_path / "episodes"
    episodes.mkdir()
    file_size, file_count, cap = 256 * 1024, 6, 1024 * 1024
    for i in range(file_count):
        (episodes / f"{i}.bin").write_bytes(b"x" * file_size)

    elapsed = _upload_sync_seconds(tmp_path, profile, max_upload_bytes_per_second=cap)

    assert {path: size for path, size in received.items() if path.endswith(".bin")} == {
        f"/bucket/episodes/{i}.bin": file_size for i in range(file_count)
    }
    at_the_cap = file_size * file_count / cap
    # Eight workers each at the cap would finish in one eighth of this.
    assert 0.95 * at_the_cap <= elapsed < 2 * at_the_cap


def test_no_cap_uploads_at_full_speed(tmp_path, s3_stub):
    profile, received = s3_stub
    episodes = tmp_path / "episodes"
    episodes.mkdir()
    (episodes / "0.bin").write_bytes(b"x" * 1024 * 1024)

    elapsed = _upload_sync_seconds(tmp_path, profile, max_upload_bytes_per_second=None)

    assert received["/bucket/episodes/0.bin"] == 1024 * 1024
    assert elapsed < 0.5
