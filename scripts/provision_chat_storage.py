"""One-time provisioning for the in-app project chat attachment store.

Creates (or updates, idempotently) the private Supabase Storage bucket that
holds chat images / PDFs / files. Safe to re-run.

Usage (from the repo root, venv active):

    python scripts/provision_chat_storage.py

Reads SUPABASE_PROJECT_URL, SUPABASE_SERVICE_ROLE_KEY, CHAT_STORAGE_BUCKET and
CHAT_MAX_ATTACHMENT_MB from the environment / .env (same as the app). The
service-role key is required; get it from the Supabase dashboard ->
Project Settings -> API -> "service_role" secret.

This is the code equivalent of spec section 2 "Supabase Storage setup":
private bucket, 25 MB per-file cap, MIME allowlist from section 4.
"""
import sys

import httpx

from core.config import settings
from services.storage_service import ALLOWED_MIME_TYPES

TIMEOUT_SECONDS = 15.0


def _fail(message: str) -> None:
    print(f"ERROR: {message}")
    sys.exit(1)


def main() -> None:
    base = settings.supabase_project_url.strip().rstrip("/")
    key = settings.supabase_service_role_key.strip()
    bucket = settings.chat_storage_bucket.strip() or "chat-attachments"
    size_limit = max(settings.chat_max_attachment_mb, 1) * 1024 * 1024

    if not base:
        _fail("SUPABASE_PROJECT_URL is not set (e.g. https://<ref>.supabase.co).")
    if not key:
        _fail(
            "SUPABASE_SERVICE_ROLE_KEY is not set. Supabase dashboard -> "
            "Project Settings -> API -> 'service_role' secret."
        )

    api = f"{base}/storage/v1/bucket"
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    body = {
        "id": bucket,
        "name": bucket,
        "public": False,
        "file_size_limit": size_limit,
        "allowed_mime_types": sorted(ALLOWED_MIME_TYPES),
    }

    with httpx.Client(timeout=TIMEOUT_SECONDS, headers=headers) as client:
        existing = client.get(f"{api}/{bucket}")
        body_text = existing.text or ""
        not_found = existing.status_code == 404 or (
            existing.status_code == 400
            and ("NoSuchBucket" in body_text or "Bucket not found" in body_text)
        )
        if existing.status_code == 200:
            resp = client.put(f"{api}/{bucket}", json=body)
            action = "updated"
        elif not_found:
            resp = client.post(api, json=body)
            action = "created"
        elif existing.status_code in (401, 403):
            _fail("service-role key rejected by Supabase Storage (401/403).")
            return
        else:
            _fail(f"unexpected response probing bucket: HTTP {existing.status_code} {body_text[:300]}")
            return

        if resp.status_code >= 400:
            _fail(f"bucket {action} failed: HTTP {resp.status_code} {resp.text[:400]}")

        verify = client.get(f"{api}/{bucket}")
        info = verify.json() if verify.status_code == 200 else {}
        print(f"OK: bucket '{bucket}' {action}.")
        print(f"    public        : {info.get('public')}")
        print(f"    file_size_limit: {info.get('file_size_limit')} bytes")
        print(f"    allowed_mime   : {len(info.get('allowed_mime_types') or [])} types")
        print("No RLS policies are needed - the backend uses the service-role key and")
        print("hands the app only short-lived signed URLs.")


if __name__ == "__main__":
    main()
