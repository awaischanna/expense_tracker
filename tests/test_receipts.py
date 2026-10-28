"""S3 tests run against a local moto server (never real AWS).

The client upload goes through the real pre-signed POST, so the URL/fields/policy are
exercised end to end.
"""

from typing import Any

import httpx
import pytest

from app import storage
from app.services.receipts import sniff_type
from app.worker.main import process_s3_deletions
from tests.conftest import auth_headers, execute, make_category, make_expense, scalar

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PDF = b"%PDF-1.7\n" + b"\x00" * 100
EXE = b"MZ\x90\x00" + b"\x00" * 100  # Windows executable header


async def setup_expense(client: httpx.AsyncClient) -> tuple[dict[str, str], int]:
    h = await auth_headers(client)
    cat = await make_category(client, h)
    exp = await make_expense(client, h, cat)
    return h, int(exp["id"])


async def presign_and_upload(
    client: httpx.AsyncClient, h: dict[str, str], expense_id: int, content: bytes, content_type: str = "image/png"
) -> dict[str, Any]:
    r = await client.post(f"/expenses/{expense_id}/receipt/upload-url", json={"content_type": content_type}, headers=h)
    assert r.status_code == 200, r.text
    post = r.json()
    async with httpx.AsyncClient() as s3:  # the "mobile app" talking straight to S3
        up = await s3.post(post["url"], data=post["fields"], files={"file": ("receipt", content, content_type)})
    assert up.status_code in (200, 204), up.text
    return post  # type: ignore[no-any-return]


async def test_upload_url_shape(client: httpx.AsyncClient) -> None:
    h, eid = await setup_expense(client)
    r = await client.post(f"/expenses/{eid}/receipt/upload-url", json={"content_type": "application/pdf"}, headers=h)
    body = r.json()
    user_id = await scalar("SELECT id FROM users")
    assert body["key"].startswith(f"receipts/{user_id}/{eid}/") and body["key"].endswith(".pdf")
    assert body["max_bytes"] == 5 * 1024 * 1024
    assert body["fields"]["key"] == body["key"]
    assert "policy" in body["fields"] or "Policy" in body["fields"]
    r = await client.post(f"/expenses/{eid}/receipt/upload-url", json={"content_type": "image/gif"}, headers=h)
    assert r.status_code == 422


@pytest.mark.parametrize(("content", "ctype"), [(PNG, "image/png"), (JPG, "image/jpeg"), (PDF, "application/pdf")])
async def test_confirm_and_download_valid_file(client: httpx.AsyncClient, content: bytes, ctype: str) -> None:
    h, eid = await setup_expense(client)
    post = await presign_and_upload(client, h, eid, content, ctype)
    r = await client.post(f"/expenses/{eid}/receipt/confirm", json={"key": post["key"]}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["receipt_key"] == post["key"]
    assert r.json()["version"] == 2  # attaching a receipt changes the expense -> new ETag
    r = await client.get(f"/expenses/{eid}/receipt", headers=h)
    assert r.status_code == 200
    assert r.json()["expires_in"] == 900
    async with httpx.AsyncClient() as s3:
        assert (await s3.get(r.json()["url"])).content == content


async def test_exe_renamed_to_png_is_rejected_and_deleted(client: httpx.AsyncClient) -> None:
    h, eid = await setup_expense(client)
    post = await presign_and_upload(client, h, eid, EXE, "image/png")
    r = await client.post(f"/expenses/{eid}/receipt/confirm", json={"key": post["key"]}, headers=h)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_file_type"
    assert await storage.object_size(post["key"]) is None  # removed from the bucket
    assert (await client.get(f"/expenses/{eid}", headers=h)).json()["receipt_key"] is None


async def test_pdf_uploaded_under_png_key_is_rejected(client: httpx.AsyncClient) -> None:
    h, eid = await setup_expense(client)
    post = await presign_and_upload(client, h, eid, PDF, "image/png")
    r = await client.post(f"/expenses/{eid}/receipt/confirm", json={"key": post["key"]}, headers=h)
    assert r.status_code == 422


async def test_confirm_rejects_foreign_or_missing_keys(client: httpx.AsyncClient) -> None:
    h, eid = await setup_expense(client)
    r = await client.post(
        f"/expenses/{eid}/receipt/confirm",
        json={"key": "receipts/999/1/00000000-0000-0000-0000-000000000000.png"},
        headers=h,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_receipt_key"
    user_id = await scalar("SELECT id FROM users")
    missing = f"receipts/{user_id}/{eid}/00000000-0000-0000-0000-000000000000.png"
    r = await client.post(f"/expenses/{eid}/receipt/confirm", json={"key": missing}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "receipt_not_found"
    assert (await client.get(f"/expenses/{eid}/receipt", headers=h)).status_code == 404


async def test_other_user_cannot_touch_receipts(client: httpx.AsyncClient) -> None:
    _, eid = await setup_expense(client)
    bob = await auth_headers(client, "bob@example.com")
    r = await client.post(f"/expenses/{eid}/receipt/upload-url", json={"content_type": "image/png"}, headers=bob)
    assert r.status_code == 404
    assert (await client.get(f"/expenses/{eid}/receipt", headers=bob)).status_code == 404


async def test_replacing_receipt_queues_old_file_for_deletion(client: httpx.AsyncClient) -> None:
    h, eid = await setup_expense(client)
    first = await presign_and_upload(client, h, eid, PNG)
    await client.post(f"/expenses/{eid}/receipt/confirm", json={"key": first["key"]}, headers=h)
    second = await presign_and_upload(client, h, eid, JPG, "image/jpeg")
    await client.post(f"/expenses/{eid}/receipt/confirm", json={"key": second["key"]}, headers=h)
    assert await process_s3_deletions() == 1
    assert await storage.object_size(first["key"]) is None
    assert await storage.object_size(second["key"]) is not None


async def test_deleting_expense_deletes_its_file(client: httpx.AsyncClient) -> None:
    h, eid = await setup_expense(client)
    post = await presign_and_upload(client, h, eid, PNG)
    await client.post(f"/expenses/{eid}/receipt/confirm", json={"key": post["key"]}, headers=h)
    assert (await client.delete(f"/expenses/{eid}", headers=h)).status_code == 204
    assert await storage.object_size(post["key"]) is None
    assert await scalar("SELECT count(*) FROM s3_deletions") == 0


async def test_failed_s3_delete_is_retried_later(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    h, eid = await setup_expense(client)
    post = await presign_and_upload(client, h, eid, PNG)
    await client.post(f"/expenses/{eid}/receipt/confirm", json={"key": post["key"]}, headers=h)

    real_delete = storage.delete_object

    async def broken_delete(key: str) -> None:
        raise ConnectionError("S3 is down")

    monkeypatch.setattr(storage, "delete_object", broken_delete)
    assert (await client.delete(f"/expenses/{eid}", headers=h)).status_code == 204  # user is not blocked
    assert await scalar("SELECT count(*) FROM s3_deletions") == 1
    await process_s3_deletions()  # still failing -> backoff
    assert await scalar("SELECT attempts FROM s3_deletions") == 1
    assert await storage.object_size(post["key"]) is not None

    monkeypatch.setattr(storage, "delete_object", real_delete)
    await execute("UPDATE s3_deletions SET next_attempt_at = now()")  # skip the backoff wait
    assert await process_s3_deletions() == 1
    assert await scalar("SELECT count(*) FROM s3_deletions") == 0
    assert await storage.object_size(post["key"]) is None


def test_sniff_type() -> None:
    assert sniff_type(PNG) == "png"
    assert sniff_type(JPG) == "jpg"
    assert sniff_type(PDF) == "pdf"
    assert sniff_type(EXE) is None
    assert sniff_type(b"") is None
