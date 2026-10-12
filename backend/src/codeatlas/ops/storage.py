"""The bucket that holds the backups (specs/004-pilot-deployment, research R12).

`ObjectStore` is all that `ops backup` and `ops restore` need from a bucket, so tests replace it
with one in memory. `S3Store` reaches the S3-compatible bucket through `boto3`, with the endpoint,
region, key pair, and virtual-hosted addressing that the bucket's credentials give.
"""

from collections.abc import Collection
from pathlib import Path
from typing import Any, Protocol, Self

import boto3
from botocore.config import Config

from codeatlas.config import get_settings

# A DeleteObjects request takes at most 1,000 keys.
DELETE_BATCH_SIZE = 1000


class StorageError(Exception):
    """A missing bucket setting, or objects that the bucket did not delete."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ObjectStore(Protocol):
    """What the backup and the restore need from a bucket."""

    def put(self, key: str, path: Path) -> None:
        """Upload the file at `path` as `key`."""

    def get(self, key: str, path: Path) -> None:
        """Download `key` into the file at `path`."""

    def delete(self, keys: Collection[str]) -> None:
        """Delete every key in `keys`."""

    def list(self, prefix: str) -> list[str]:
        """Every key that starts with `prefix`."""


class S3Store:
    """The backups bucket through an S3 client."""

    def __init__(self, client: Any, bucket: str) -> None:
        # `boto3` ships no type information, so its client stays `Any` here only.
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_settings(cls) -> Self:
        """The bucket that the `BACKUP_*` settings name.

        Raises `StorageError` naming every missing setting by its environment variable, without
        any value.
        """
        settings = get_settings()
        values = {
            "BACKUP_ENDPOINT_URL": settings.backup_endpoint_url.strip(),
            "BACKUP_REGION": settings.backup_region.strip(),
            "BACKUP_BUCKET": settings.backup_bucket.strip(),
            "BACKUP_ACCESS_KEY_ID": settings.backup_access_key_id.strip(),
            "BACKUP_SECRET_ACCESS_KEY": settings.backup_secret_access_key.strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise StorageError(f"missing settings for the backups bucket: {', '.join(missing)}")
        client = boto3.client(
            "s3",
            endpoint_url=values["BACKUP_ENDPOINT_URL"],
            region_name=values["BACKUP_REGION"],
            aws_access_key_id=values["BACKUP_ACCESS_KEY_ID"],
            aws_secret_access_key=values["BACKUP_SECRET_ACCESS_KEY"],
            # Railway's current buckets use virtual-hosted addressing.
            config=Config(s3={"addressing_style": "virtual"}),
        )
        return cls(client, values["BACKUP_BUCKET"])

    def put(self, key: str, path: Path) -> None:
        self._client.upload_file(str(path), self._bucket, key)

    def get(self, key: str, path: Path) -> None:
        self._client.download_file(self._bucket, key, str(path))

    def delete(self, keys: Collection[str]) -> None:
        ordered = sorted(keys)
        for start in range(0, len(ordered), DELETE_BATCH_SIZE):
            batch = ordered[start : start + DELETE_BATCH_SIZE]
            response = self._client.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            errors = response.get("Errors", [])
            if errors:
                raise StorageError(
                    f"the bucket did not delete {len(errors)} objects, such as "
                    f"{errors[0].get('Key')} ({errors[0].get('Code')})"
                )

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        pages = self._client.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=prefix
        )
        for page in pages:
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys
