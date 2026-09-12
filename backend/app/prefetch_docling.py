"""Optional one-time preparation command for Factualize's complex-page reader."""
from __future__ import annotations

from .complex_pdf import ComplexPageParser
from .settings import get_settings


def main() -> None:
    settings = get_settings()
    ComplexPageParser(settings.docling_models_dir)._prepare_models()
    print("Docling preparation complete.")


if __name__ == "__main__":
    main()
