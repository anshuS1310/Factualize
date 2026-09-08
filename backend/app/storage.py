from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from .settings import Settings


@dataclass(frozen=True)
class StoredUpload:
    original_filename: str
    sha256: str
    byte_size: int
    path: Path


class LocalDocumentStorage:
    """Content-addressed PDF storage with streaming size and signature checks."""

    chunk_size = 1024 * 1024

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def save_pdf(self, upload: UploadFile) -> StoredUpload:
        filename = Path(upload.filename or "document.pdf").name
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Only PDF uploads are supported.",
            )

        max_bytes = self.settings.max_upload_mb * 1024 * 1024
        hasher = hashlib.sha256()
        bytes_written = 0
        first_chunk = b""
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".upload", dir=self.settings.data_dir, delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                while chunk := await upload.read(self.chunk_size):
                    if not first_chunk:
                        first_chunk = chunk
                    bytes_written += len(chunk)
                    if bytes_written > max_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"PDF exceeds the {self.settings.max_upload_mb} MB upload limit.",
                        )
                    hasher.update(chunk)
                    temporary_file.write(chunk)

            if not first_chunk.startswith(b"%PDF-"):
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail="The uploaded file is not a valid PDF signature.",
                )

            digest = hasher.hexdigest()
            destination = self.settings.documents_dir / f"{digest}.pdf"
            if destination.exists():
                temporary_path.unlink(missing_ok=True)
            else:
                os.replace(temporary_path, destination)
            return StoredUpload(filename, digest, bytes_written, destination)
        except Exception:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

    def page_cache_path(self, document_hash: str, page_number: int) -> Path:
        path = self.settings.cache_dir / document_hash / "pages"
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{page_number}.json"

    def page_render_path(self, document_hash: str, page_number: int) -> Path:
        path = self.settings.renders_dir / document_hash
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{page_number}.png"

    def delete_orphaned_document(self, document: dict[str, object]) -> None:
        """Delete only a source that no set references, plus its derived local cache."""
        source = Path(str(document["stored_path"])).resolve()
        documents_root = self.settings.documents_dir.resolve()
        if source.parent == documents_root:
            source.unlink(missing_ok=True)
        document_hash = str(document["sha256"])
        for root in (self.settings.cache_dir, self.settings.renders_dir):
            target = (root / document_hash).resolve()
            if target.parent == root.resolve() and target.is_dir():
                shutil.rmtree(target)
