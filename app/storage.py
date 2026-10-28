"""All object-storage (S3) code lives in this one module.

On AWS no keys are configured: aioboto3 picks up the IAM role of the EC2 instance / ECS task.
For the local S3 emulator (moto) dummy keys come from the (git-ignored) .env file.
"""

from typing import Any

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import get_settings

_session = aioboto3.Session()


def _client(public: bool = False) -> Any:
    s = get_settings()
    endpoint = (s.s3_public_endpoint_url or s.s3_endpoint_url) if public else s.s3_endpoint_url
    return _session.client(
        "s3",
        region_name=s.s3_region,
        endpoint_url=endpoint,
        aws_access_key_id=s.aws_access_key_id,
        aws_secret_access_key=s.aws_secret_access_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


async def presigned_upload(key: str, content_type: str, max_bytes: int, expires_in: int) -> dict[str, Any]:
    """Pre-signed POST (not PUT): only POST policies can enforce a max size (content-length-range)."""
    s = get_settings()
    async with _client(public=True) as s3:
        return await s3.generate_presigned_post(  # type: ignore[no-any-return]
            Bucket=s.s3_bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, max_bytes],
            ],
            ExpiresIn=expires_in,
        )


async def presigned_download(key: str, expires_in: int) -> str:
    s = get_settings()
    async with _client(public=True) as s3:
        return await s3.generate_presigned_url(  # type: ignore[no-any-return]
            "get_object", Params={"Bucket": s.s3_bucket, "Key": key}, ExpiresIn=expires_in
        )


async def object_size(key: str) -> int | None:
    """Size in bytes, or None if the object does not exist."""
    async with _client() as s3:
        try:
            head = await s3.head_object(Bucket=get_settings().s3_bucket, Key=key)
        except ClientError as exc:
            # Without s3:ListBucket (least privilege) S3 answers 403 instead of 404 for a missing key.
            if exc.response.get("Error", {}).get("Code") in ("403", "404", "NoSuchKey", "NotFound"):
                return None
            raise
        return int(head["ContentLength"])


async def read_first_bytes(key: str, n: int = 16) -> bytes:
    """Ranged GET: we only download the first n bytes to check the file signature."""
    async with _client() as s3:
        obj = await s3.get_object(Bucket=get_settings().s3_bucket, Key=key, Range=f"bytes=0-{n - 1}")
        async with obj["Body"] as body:
            return await body.read()  # type: ignore[no-any-return]


async def delete_object(key: str) -> None:
    async with _client() as s3:
        await s3.delete_object(Bucket=get_settings().s3_bucket, Key=key)


async def ensure_bucket() -> None:
    """Dev/test helper: create the private bucket if it is missing."""
    s = get_settings()
    async with _client() as s3:
        try:
            await s3.head_bucket(Bucket=s.s3_bucket)
        except ClientError:
            await s3.create_bucket(Bucket=s.s3_bucket)
