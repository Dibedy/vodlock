import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath


def storage_key(key):
    value = PurePosixPath(key)
    if value.is_absolute() or ".." in value.parts or "\\" in key or ":" in key:
        raise ValueError("Invalid storage key")
    return str(value)


class LocalStorage:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def put(self, key, data, content_type="application/json"):
        path = self.root / storage_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return key

    def get(self, key):
        return (self.root / storage_key(key)).read_bytes()


class S3Storage:
    def __init__(self, bucket, endpoint=None):
        import boto3

        self.client = boto3.client("s3", endpoint_url=endpoint)
        self.bucket = bucket

    def put(self, key, data, content_type="application/json"):
        key = storage_key(key)
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata={"sha256": hashlib.sha256(data).hexdigest()},
        )
        return key

    def get(self, key):
        return self.client.get_object(Bucket=self.bucket, Key=storage_key(key))["Body"].read()


def configured_storage(config):
    bucket = os.environ.get("SPOILLESS_STORAGE_BUCKET")
    return (
        S3Storage(bucket, os.environ.get("SPOILLESS_STORAGE_ENDPOINT")) if bucket else LocalStorage(config.storage_path)
    )
