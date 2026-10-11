"""File storage: Supabase Storage when configured, otherwise a local folder (development only).

Buckets: ``proposals`` (public, proposal PDFs linked from emails) and ``evidence`` (private, ROI photos).
"""

from __future__ import annotations

from pathlib import Path

import httpx

PUBLIC_BUCKETS = {"proposals"}


class Storage:
    def __init__(self, settings, local_dir: str | Path = ".local_storage") -> None:
        self.url = settings.supabase_url.rstrip("/")
        self.key = settings.supabase_service_role_key
        self.api_url = settings.public_api_url.rstrip("/")
        self.local_dir = Path(local_dir)

    @property
    def remote(self) -> bool:
        return bool(self.url and self.key)

    def _headers(self, content_type: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.key}", "apikey": self.key}
        if content_type:
            headers.update({"Content-Type": content_type, "x-upsert": "true"})
        return headers

    def public_url(self, bucket: str, path: str) -> str:
        if self.remote:
            return f"{self.url}/storage/v1/object/public/{bucket}/{path}"
        return f"{self.api_url}/files/{bucket}/{path}"

    async def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> str:
        """Store the bytes and return a URL (public buckets) or the storage path (private buckets)."""
        if self.remote:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{self.url}/storage/v1/object/{bucket}/{path}",
                                         content=data, headers=self._headers(content_type))
                resp.raise_for_status()
        else:
            target = self.local_dir / bucket / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return self.public_url(bucket, path) if bucket in PUBLIC_BUCKETS else path

    async def download(self, bucket: str, path: str) -> bytes:
        if self.remote:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{self.url}/storage/v1/object/authenticated/{bucket}/{path}",
                                        headers=self._headers())
                resp.raise_for_status()
                return resp.content
        return (self.local_dir / bucket / path).read_bytes()

    def local_file(self, bucket: str, path: str) -> Path | None:
        """Only used to serve public local files in development."""
        if self.remote or bucket not in PUBLIC_BUCKETS:
            return None
        target = (self.local_dir / bucket / path).resolve()
        root = (self.local_dir / bucket).resolve()
        return target if root in target.parents and target.is_file() else None
