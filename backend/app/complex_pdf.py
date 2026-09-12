"""Lazy Docling adapter; failures remain visible, never substituted with plain text."""
from __future__ import annotations

import json
import threading
from pathlib import Path


class ComplexPageParser:
    """Read a one-page complex PDF with Docling, OCR and table structure enabled.

    Docling normally downloads models into Hugging Face's symlink-based cache.
    That can fail on Windows devices without Developer Mode.  We prefetch just
    the models this pipeline needs into a normal local directory and point
    Docling at it, so its initial setup is copy-based and repeatable.
    """

    _prepare_lock = threading.Lock()

    def __init__(self, artifacts_dir: Path):
        self._converter = None
        self.artifacts_dir = artifacts_dir
        self._prepared = False

    def _prepare_models(self) -> None:
        if self._prepared:
            return
        with self._prepare_lock:
            if self._prepared:
                return
            marker = self.artifacts_dir / ".factualize-docling-ready.json"
            if not marker.is_file():
                from docling.utils.model_downloader import download_models

                self.artifacts_dir.mkdir(parents=True, exist_ok=True)
                # ``local_dir`` downloads are normal files instead of symlinked
                # cache pointers.  Keep the selection lean: layout, table and
                # English OCR are the only stages enabled below.
                download_models(
                    output_dir=self.artifacts_dir,
                    with_layout=True,
                    with_tableformer=True,
                    with_code_formula=False,
                    with_picture_classifier=False,
                    with_rapidocr=True,
                    rapidocr_models=["onnxruntime:english"],
                )
                marker.write_text(
                    json.dumps({"pipeline": "layout-table-rapidocr-en", "version": 1}),
                    encoding="utf-8",
                )
            self._prepared = True

    def convert(self, page):
        from io import BytesIO

        import fitz
        from docling.datamodel.base_models import DocumentStream, InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        if self._converter is None:
            self._prepare_models()
            options = PdfPipelineOptions(do_ocr=True, do_table_structure=True)
            options.artifacts_path = self.artifacts_dir
            options.ocr_options = RapidOcrOptions(backend="onnxruntime", lang=["english"])
            options.table_structure_options.do_cell_matching = True
            self._converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
        # One page is the resumable unit. Docling sees page 1; remap to the source below.
        with fitz.open() as single:
            single.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
            stream = DocumentStream(name="page.pdf", stream=BytesIO(single.tobytes()))
        result = self._converter.convert(stream)
        if getattr(result.status, "value", result.status) != "success":
            raise RuntimeError("Docling did not complete this page; retry is available.")
        document = result.document
        blocks = []
        tables = []
        for item, _ in document.iterate_items():
            text = getattr(item, "text", "")
            is_table = hasattr(item, "data") and hasattr(item.data, "table_cells")
            if is_table:
                tables.append(item.data.model_dump(mode="json"))
                text = item.export_to_markdown(doc=document)
            for prov in getattr(item, "prov", []):
                box = prov.bbox.to_top_left_origin(page_height=page.rect.height)
                if text:
                    blocks.append(
                        {
                            "kind": "table" if is_table else "text",
                            "text": text,
                            "bbox": [box.l, box.t, box.r, box.b],
                        }
                    )
        text = document.export_to_markdown()
        if not text.strip():
            raise RuntimeError("No readable content was recovered by Docling; this page needs review.")
        return text, blocks, tables
