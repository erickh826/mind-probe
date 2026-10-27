"""Private recording storage. Only object keys are persisted (ADR-0005).

``MINDPROBE_PRESIGN_TTL_SECONDS`` (default 900, allowed 1–86400) is the
presigned GET lifetime. ``MINDPROBE_MEDIA_BUCKET`` is the bucket for
``cases/`` keys on the S3 adapter; empty keeps those keys on the recordings
bucket, which is the previous single-bucket behavior.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote

logger = logging.getLogger(__name__)

#: Default and the historical constant. Call :func:`presign_ttl_seconds` for
#: the value actually in force; a default argument would freeze this at import.
DEFAULT_PRESIGN_TTL_SECONDS = 900
PRESIGN_TTL = DEFAULT_PRESIGN_TTL_SECONDS
PRESIGN_TTL_ENV = "MINDPROBE_PRESIGN_TTL_SECONDS"
MEDIA_BUCKET_ENV = "MINDPROBE_MEDIA_BUCKET"
_MAX_PRESIGN_TTL_SECONDS = 24 * 60 * 60


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
    def presign_get(self, key: str, ttl: int | None = None) -> str: ...
    def public_url(self, key: str) -> str: ...
    def delete(self, key: str) -> None: ...


def presign_ttl_seconds() -> int:
    """Lifetime of a presigned GET, in seconds.

    Unset matches the previous hard-coded 15 minutes. Invalid values fall
    back to that default so a typo cannot mint a year-long URL or a zero TTL.
    """
    raw = os.environ.get(PRESIGN_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_PRESIGN_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s.", PRESIGN_TTL_ENV, raw, DEFAULT_PRESIGN_TTL_SECONDS)
        return DEFAULT_PRESIGN_TTL_SECONDS
    if not 1 <= value <= _MAX_PRESIGN_TTL_SECONDS:
        logger.warning(
            "Ignoring out-of-range %s=%r; using %s.", PRESIGN_TTL_ENV, raw, DEFAULT_PRESIGN_TTL_SECONDS
        )
        return DEFAULT_PRESIGN_TTL_SECONDS
    return value


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

    def presign_get(self, key: str, ttl: int | None = None) -> str:
        validate_key(key)
        parts = key.split("/")
        if len(parts) != 3 or parts[0] != "sessions" or parts[2] != "student.mp4":
            raise ValueError("only recording objects can be signed")
        limit = presign_ttl_seconds()
        expires_in = limit if ttl is None else min(ttl, limit)
        expires = int(time.time()) + expires_in
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
    limit = presign_ttl_seconds()
    return (now < expires <= now + limit
            and hmac.compare_digest(local_signature(key, expires).encode(), signature.encode()))


class S3Storage:
    def __init__(self, bucket: str, *, client=None, public_base: str = "", media_bucket: str = ""):
        if not bucket:
            raise ValueError("recordings bucket is required")
        if client is None:
            import boto3
            from botocore.config import Config
            # A custom endpoint (R2) signs path-style URLs on that host:
            # https://<account>.r2.cloudflarestorage.com/<bucket>/<key>
            # so the browser CSP can allow *.r2.cloudflarestorage.com without
            # embedding an account id. AWS's own endpoints keep virtual-hosted
            # style when MINDPROBE_S3_ENDPOINT is unset.
            endpoint = os.environ.get("MINDPROBE_S3_ENDPOINT") or None
            s3_options = {"addressing_style": "path"} if endpoint else None
            client = boto3.client(
                "s3", endpoint_url=endpoint,
                region_name=os.environ.get("MINDPROBE_S3_REGION", "auto"),
                aws_access_key_id=os.environ.get("MINDPROBE_S3_ACCESS_KEY_ID"),
                aws_secret_access_key=os.environ.get("MINDPROBE_S3_SECRET_ACCESS_KEY"),
                config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=30,
                              retries={"max_attempts": 2},
                              **({"s3": s3_options} if s3_options else {})),
            )
        self.client, self.bucket, self.public_base = client, bucket, public_base.rstrip("/")
        self.media_bucket = media_bucket.strip()

    def bucket_for(self, key: str) -> str:
        """Recordings and backups stay private. ``cases/`` may use the media bucket."""
        key = validate_key(key)
        if self.media_bucket and key.startswith("cases/"):
            return self.media_bucket
        return self.bucket

    def put_file(self, key: str, path: Path, checksum: str) -> None:
        from boto3.s3.transfer import TransferConfig
        key = validate_key(key)
        self.client.upload_file(str(path), self.bucket_for(key), key,
                                ExtraArgs={"Metadata": {"sha256": checksum}},
                                Config=TransferConfig(use_threads=False))

    def exists(self, key: str, checksum: str | None = None) -> bool:
        from botocore.exceptions import ClientError
        key = validate_key(key)
        try:
            head = self.client.head_object(Bucket=self.bucket_for(key), Key=key)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return checksum is None or head.get("Metadata", {}).get("sha256") == checksum

    def presign_get(self, key: str, ttl: int | None = None) -> str:
        key = validate_key(key)
        limit = presign_ttl_seconds()
        expires_in = limit if ttl is None else min(ttl, limit)
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket_for(key), "Key": key}, ExpiresIn=expires_in
        )

    def public_url(self, key: str) -> str:
        if not self.public_base:
            raise ValueError("public media base is not configured")
        return self.public_base + "/" + quote(validate_key(key), safe="/")

    def delete(self, key: str) -> None:
        key = validate_key(key)
        self.client.delete_object(Bucket=self.bucket_for(key), Key=key)


def get_storage() -> Storage:
    backend = os.environ.get("MINDPROBE_STORAGE_BACKEND", "local")
    if backend == "local":
        return LocalStorage(Path(os.environ.get("MINDPROBE_LOCAL_STORAGE_DIR", "./objects")))
    if backend == "s3":
        return S3Storage(
            os.environ.get("MINDPROBE_RECORDINGS_BUCKET", ""),
            public_base=os.environ.get("MINDPROBE_MEDIA_BASE_URL", ""),
            media_bucket=os.environ.get(MEDIA_BUCKET_ENV, ""),
        )
    raise ValueError("unknown storage backend")
