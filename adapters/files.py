"""
File storage adapter
====================
Uploads and outputs are addressed by a *key* relative to the store root, e.g.
"runs/<run>/inputs/ab12cd34_boq.pdf". The database stores only keys, never
absolute paths, so the whole store can move (new drive, network share, object
store) without touching a single database row.

Backends (CONFLICTSCAN_FILES_BACKEND):

    local   a folder on this server (default: <data_dir>), or a network share
            given as a UNC path in CONFLICTSCAN_FILES_ROOT
            (\\\\fileserver\\conflictscan). The service account needs
            modify rights on it.
    s3      any S3-compatible object store (MinIO, Ceph, NetApp StorageGRID):
            CONFLICTSCAN_S3_ENDPOINT, _BUCKET, _PREFIX, _ACCESS_KEY, _SECRET_KEY.
            Needs the boto3 package. Files are mirrored into a local working
            cache because the pipeline reads ordinary files.

Every backend offers the same calls:

    local_path(key)       ordinary file path to read (downloads if needed)
    local_dir(prefix)     ordinary folder to write outputs into
    save(key, src)        store a local file (or file object) under key
    sync(key)             push a file written into local_dir()/local_path() to the store
    exists / size / delete / delete_prefix / copy / iter_keys

Windows compatible.
"""

import os
import shutil
from typing import Iterator, Optional

import config  # noqa: F401

DATA_DIR = os.environ.get("CONFLICTSCAN_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _norm(key: str) -> str:
    key = key.replace("\\", "/").lstrip("/")
    if ".." in key.split("/"):
        raise ValueError(f"invalid storage key: {key}")
    return key


class LocalFileStore:
    backend = "local"

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def describe(self) -> str:
        kind = "network share" if self.root.startswith("\\\\") else "local folder"
        return f"{kind} {self.root}"

    def local_path(self, key: str) -> str:
        return os.path.join(self.root, *_norm(key).split("/"))

    def local_dir(self, prefix: str) -> str:
        path = self.local_path(prefix)
        os.makedirs(path, exist_ok=True)
        return path

    def key_for(self, path: str) -> str:
        """The key of a file inside the store (for paths produced by local_dir/local_path)."""
        rel = os.path.relpath(os.path.abspath(path), self.root)
        return _norm(rel)

    def save(self, key: str, src) -> str:
        dest = self.local_path(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if isinstance(src, str):
            if os.path.abspath(src) != dest:
                shutil.copy2(src, dest)
        else:
            with open(dest, "wb") as f:
                shutil.copyfileobj(src, f)
        return key

    def sync(self, key: str) -> None:
        return None          # already in place

    def exists(self, key: str) -> bool:
        return os.path.isfile(self.local_path(key))

    def size(self, key: str) -> int:
        return os.path.getsize(self.local_path(key))

    def delete(self, key: str) -> None:
        try:
            os.remove(self.local_path(key))
        except OSError:
            pass

    def delete_prefix(self, prefix: str) -> None:
        shutil.rmtree(self.local_path(prefix), ignore_errors=True)

    def copy(self, src_key: str, dst_key: str) -> None:
        src, dst = self.local_path(src_key), self.local_path(dst_key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            os.link(src, dst)                # same volume: no extra disk space
        except OSError:
            shutil.copy2(src, dst)

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        base = self.local_path(prefix) if prefix else self.root
        for folder, _dirs, names in os.walk(base):
            for name in names:
                yield self.key_for(os.path.join(folder, name))

    def health(self) -> dict:
        probe = os.path.join(self.root, ".write_test")
        try:
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            free = shutil.disk_usage(self.root).free
            return {"ok": True, "detail": f"{self.describe()} - writable, {free / 1e9:.0f} GB free"}
        except OSError as exc:
            return {"ok": False, "detail": f"{self.describe()} - not writable: {exc}"}


class S3FileStore(LocalFileStore):
    """S3-compatible object store with a local working cache (the pipeline needs real files)."""
    backend = "s3"

    def __init__(self, endpoint: str, bucket: str, prefix: str, access_key: str, secret_key: str, cache_root: str):
        super().__init__(cache_root)
        import boto3  # noqa: WPS433 - only needed when this backend is chosen
        self.bucket = bucket
        self.prefix = (prefix or "").strip("/")
        self.endpoint = endpoint
        self.client = boto3.client("s3", endpoint_url=endpoint or None, aws_access_key_id=access_key or None,
                                   aws_secret_access_key=secret_key or None)

    def describe(self) -> str:
        return f"object store {self.endpoint or 's3'}/{self.bucket}/{self.prefix}"

    def _obj(self, key: str) -> str:
        return f"{self.prefix}/{_norm(key)}" if self.prefix else _norm(key)

    def local_path(self, key: str) -> str:
        path = super().local_path(key)
        if not os.path.isfile(path):
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                self.client.download_file(self.bucket, self._obj(key), path)
            except Exception:  # noqa: BLE001 - missing object: callers check exists()
                pass
        return path

    def save(self, key: str, src) -> str:
        super().save(key, src)
        self.sync(key)
        return key

    def sync(self, key: str) -> None:
        path = super().local_path(key)
        if os.path.isfile(path):
            self.client.upload_file(path, self.bucket, self._obj(key))

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._obj(key))
            return True
        except Exception:  # noqa: BLE001
            return False

    def size(self, key: str) -> int:
        return int(self.client.head_object(Bucket=self.bucket, Key=self._obj(key))["ContentLength"])

    def delete(self, key: str) -> None:
        super().delete(key)
        self.client.delete_object(Bucket=self.bucket, Key=self._obj(key))

    def delete_prefix(self, prefix: str) -> None:
        super().delete_prefix(prefix)
        for key in list(self.iter_keys(prefix)):
            self.client.delete_object(Bucket=self.bucket, Key=self._obj(key))

    def copy(self, src_key: str, dst_key: str) -> None:
        self.client.copy_object(Bucket=self.bucket, Key=self._obj(dst_key),
                                CopySource={"Bucket": self.bucket, "Key": self._obj(src_key)})

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        start = self._obj(prefix) if prefix else (self.prefix + "/" if self.prefix else "")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=start):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                yield key[len(self.prefix) + 1:] if self.prefix else key

    def health(self) -> dict:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return {"ok": True, "detail": f"{self.describe()} - reachable"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"{self.describe()} - {exc}"}


_store: Optional[LocalFileStore] = None


def store() -> LocalFileStore:
    global _store
    if _store is None:
        backend = os.environ.get("CONFLICTSCAN_FILES_BACKEND", "local").lower()
        root = os.environ.get("CONFLICTSCAN_FILES_ROOT") or DATA_DIR
        if backend == "s3":
            _store = S3FileStore(
                os.environ.get("CONFLICTSCAN_S3_ENDPOINT", ""), os.environ.get("CONFLICTSCAN_S3_BUCKET", "conflictscan"),
                os.environ.get("CONFLICTSCAN_S3_PREFIX", ""), os.environ.get("CONFLICTSCAN_S3_ACCESS_KEY", ""),
                os.environ.get("CONFLICTSCAN_S3_SECRET_KEY", ""), os.path.join(DATA_DIR, "file_cache"))
        else:
            _store = LocalFileStore(root)
    return _store


def reset(root: str = None) -> LocalFileStore:
    """Tests and restore: point the store at another folder."""
    global _store
    _store = LocalFileStore(root) if root else None
    return store()
