"""Private recording storage. Only object keys are persisted (ADR-0005)."""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote

PRESIGN_TTL = 900


def validate_key(key: str) -> str:
    parts = PurePosixPath(key).parts
    if not parts or key.startswith("/") or "\\" in key or ":" in key or any(p in {".", ".."} for p in key.split("/")):
        raise ValueError("invalid object key")
    return key


def recording_key(session_id: str) -> str:
    from .recording import validate_session_id
    return f"sessions/{validate_session_id(session_id)}/student.mp4"


class Storage(Protocol):
    def put_file(self, key: str, path: Path, checksum: str) -> None: ...
    def exists(self, key: str, checksum: str | None = None) -> bool: ...
    def presign_get(self, key: str, ttl: int = PRESIGN_TTL) -> str: ...
    def public_url(self, key: str) -> str: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("object outside storage root")
        return path

    def put_file(self, key: str, path: Path, checksum: str) -> None:
        dest = self.path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_name(dest.name + ".part")
        try:
            shutil.copyfile(path, temp)
            if file_sha256(temp) != checksum:
                raise ValueError("storage checksum mismatch")
            temp.replace(dest)
        finally:
            temp.unlink(missing_ok=True)

    def exists(self, key: str, checksum: str | None = None) -> bool:
        path = self.path(key)
        return path.is_file() and (checksum is None or file_sha256(path) == checksum)

    def presign_get(self, key: str, ttl: int = PRESIGN_TTL) -> str:
        validate_key(key)
        parts = key.split("/")
        if len(parts) != 3 or parts[0] != "sessions" or parts[2] != "student.mp4":
            raise ValueError("only recording objects can be signed")
        expires = int(time.time()) + min(ttl, PRESIGN_TTL)
        signature = local_signature(key, expires)
        return f"/recording-media/{quote(parts[1], safe='')}?expires={expires}&signature={signature}"

    def public_url(self, key: str) -> str:
        return "/media/" + quote(validate_key(key), safe="/")

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def local_signature(key: str, expires: int) -> str:
    secret = os.environ.get("MINDPROBE_WS_TOKEN_SECRET", "")
    if not secret:
        raise RuntimeError("media signing secret is not configured")
    return hmac.new(secret.encode(), f"recording-get\n{key}\n{expires}".encode(), hashlib.sha256).hexdigest()


def valid_local_signature(key: str, expires: int, signature: str) -> bool:
    now = int(time.time())
    return (now < expires <= now + PRESIGN_TTL
            and hmac.compare_digest(local_signature(key, expires).encode(), signature.encode()))


class S3Storage:
    def __init__(self, bucket: str, *, client=None, public_base: str = ""):
        if not bucket:
            raise ValueError("recordings bucket is required")
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client(
                "s3", endpoint_url=os.environ.get("MINDPROBE_S3_ENDPOINT") or None,
                region_name=os.environ.get("MINDPROBE_S3_REGION", "auto"),
                aws_access_key_id=os.environ.get("MINDPROBE_S3_ACCESS_KEY_ID"),
                aws_secret_access_key=os.environ.get("MINDPROBE_S3_SECRET_ACCESS_KEY"),
                config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=30,
                              retries={"max_attempts": 2}),
            )
        self.client, self.bucket, self.public_base = client, bucket, public_base.rstrip("/")

    def put_file(self, key: str, path: Path, checksum: str) -> None:
        from boto3.s3.transfer import TransferConfig
        self.client.upload_file(str(path), self.bucket, validate_key(key),
                                ExtraArgs={"Metadata": {"sha256": checksum}},
                                Config=TransferConfig(use_threads=False))

    def exists(self, key: str, checksum: str | None = None) -> bool:
        from botocore.exceptions import ClientError
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=validate_key(key))
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return checksum is None or head.get("Metadata", {}).get("sha256") == checksum

    def presign_get(self, key: str, ttl: int = PRESIGN_TTL) -> str:
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": validate_key(key)},
                                                  ExpiresIn=min(ttl, PRESIGN_TTL))

    def public_url(self, key: str) -> str:
        if not self.public_base:
            raise ValueError("public media base is not configured")
        return self.public_base + "/" + quote(validate_key(key), safe="/")

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=validate_key(key))


def get_storage() -> Storage:
    backend = os.environ.get("MINDPROBE_STORAGE_BACKEND", "local")
    if backend == "local":
        return LocalStorage(Path(os.environ.get("MINDPROBE_LOCAL_STORAGE_DIR", "./objects")))
    if backend == "s3":
        return S3Storage(os.environ.get("MINDPROBE_RECORDINGS_BUCKET", ""),
                         public_base=os.environ.get("MINDPROBE_MEDIA_BASE_URL", ""))
    raise ValueError("unknown storage backend")
