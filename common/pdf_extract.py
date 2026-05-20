"""
common/pdf_extract.py

Shared PDF text extraction utility for all scrapers.

Provides:
  extract_pdf_markdown(source, source_id, pdf_bytes, table) -> str
  preload_existing_ids(source, table) -> set[str]

extract_pdf_markdown uses pdfplumber for text extraction.
preload_existing_ids is a stub — returns empty set unless your storage
layer implements it (the signature matches what scrapers expect).
"""

import logging
from typing import Optional

logger = logging.getLogger("legal-data-hunter.pdf_extract")


def extract_pdf_markdown(
    source: str,
    source_id: str,
    pdf_bytes: bytes,
    table: str = "case_law",
) -> Optional[str]:
    """
    Extract text from PDF bytes using pdfplumber.

    Args:
        source:     Source ID string (e.g. "SE/SupremeCourt") — used for logging only.
        source_id:  Document ID — used for logging only.
        pdf_bytes:  Raw PDF bytes.
        table:      Target table name — unused, kept for interface compatibility.

    Returns:
        Extracted text as a string, or None if extraction fails or yields nothing.
    """
    try:
        import pdfplumber
        import io

        text_parts = []

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text.strip())

        if not text_parts:
            logger.warning(f"[{source}] {source_id}: pdfplumber returned no text")
            return None

        full_text = "\n\n".join(text_parts)

        # Basic cleanup
        import re
        full_text = re.sub(r"\n{3,}", "\n\n", full_text)
        full_text = re.sub(r"[ \t]+", " ", full_text)
        full_text = full_text.strip()

        return full_text if full_text else None

    except ImportError:
        logger.error("pdfplumber is not installed. Run: pip install pdfplumber")
        return None
    except Exception as e:
        logger.error(f"[{source}] {source_id}: PDF extraction failed: {e}")
        return None


def preload_existing_ids(source: str, table: str = "case_law") -> set:
    """
    Return the set of document IDs already stored for this source.

    This is a stub implementation that returns an empty set.
    Override or extend if your storage layer (storage.py) supports
    querying existing IDs — plug in the real implementation there.

    Args:
        source:  Source ID string (e.g. "SE/SupremeCourt").
        table:   Target table name.

    Returns:
        Set of existing document ID strings (empty by default).
    """
    return set()