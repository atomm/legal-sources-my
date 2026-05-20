#!/usr/bin/env python3
"""
IT/CassazioneCivile -- Italian Supreme Court (Corte di Cassazione) Data Fetcher

Fetches Italian Supreme Court case law from the SentenzeWeb Solr API.

API Details:
  - Base URL: https://www.italgiure.giustizia.it/sncass/
  - Solr endpoint: /isapi/hc.dll/sn.solr/sn-collection/select
  - Authentication: None required (public access)
  - Coverage: 1.87M+ documents total
    - Civil cases (snciv): 186,000+
    - Criminal cases (snpen): 237,000+
    - Civil/Penal registry (sic): 1.4M+

Data Fields:
  - id: Unique document ID
  - ocr: Full text (OCR extracted from PDF)
  - kind: Document type (snciv=civil, snpen=criminal)
  - numdec: Decision number
  - anno: Year
  - datdep: Date of deposit (YYYYMMDD)
  - datdec: Date of decision (YYYYMMDD)
  - tipoprov: Type (Sentenza, Ordinanza)
  - szdec: Section number
  - presidente: President judge
  - relatore: Reporting judge
  - materia: Subject matter

License: Open Government Data (Italian IODL)

Usage:
  python bootstrap.py bootstrap                          # Full initial pull
  python bootstrap.py bootstrap --sample                 # Fetch 10+ sample records
  python bootstrap.py bootstrap --sample --save-files    # Also save PDF + TXT locally
  python bootstrap.py bootstrap --save-files             # Full pull + save files
  python bootstrap.py bootstrap --save-files --save-dir /path/to/dir
  python bootstrap.py update                             # Incremental update
  python bootstrap.py test                               # Quick connectivity test
"""

import os
import re
import sys
import html
import json
import logging
import urllib3
from io import BytesIO
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Generator, Optional, Dict, Any, List
from urllib.parse import urlencode

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


try:
    # Insert source dir so cassazione_pdf_extractor.py is found regardless
    # of the working directory the process is launched from
    _SRC_DIR = str(Path(__file__).parent)
    if _SRC_DIR not in sys.path:
        sys.path.insert(0, _SRC_DIR)
    from cassazione_pdf_extractor import extract_decision, DecisionLayers
    HAS_PDF_EXTRACTOR = True
except ImportError as _e:
    HAS_PDF_EXTRACTOR = False
    import warnings
    warnings.warn(f'cassazione_pdf_extractor not available ({_e}); PDF extraction disabled')


# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from common.base_scraper import BaseScraper
from common.http_client import HttpClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("legal-data-hunter.IT.cassazione")

# API Configuration
BASE_URL = "https://www.italgiure.giustizia.it"
SOLR_ENDPOINT = "/sncass/isapi/hc.dll/sn.solr/sn-collection/select"

# Solr fields to retrieve
SOLR_FIELDS = [
    "id", "ocr", "kind", "numdec", "anno", "datdep", "datdec",
    "tipoprov", "szdec", "presidente", "relatore", "materia",
    "filename", "ocrdis", "ssz", "pd", "sicId", "rnc-sp", "rnc-art",
    "rnc-gen", "sic-autorita", "sic-localita", "sic-ricorrente",
    "sic-contro", "sic-intimato", "sic-consigliere", "sic-nrg",
    "sic-anno_nrg", "sic-anno_prov", "sic-num_provv", "sic-data_ud",
    "sic-datdep",
]

# Document types to fetch
DOC_TYPES = ["snciv", "snpen"]  # Civil and Criminal decisions

# Pagination
PAGE_SIZE = 50  # Conservative to avoid timeouts

# ---------------------------------------------------------------------------
# DEV: hardcode sample filters here for testing, set to None to disable
# ---------------------------------------------------------------------------
SAMPLE_FILTER = {
    "kind":     None,       # e.g. "snciv" or "snpen"; None = both
    "anno":     None,       # e.g. "2023"; None = all years
    "numdec":   None,       # e.g. "8500" for a specific judgment number; None = all
    "tipoprov": None,       # e.g. "Sentenza" or "Ordinanza"; None = all types
}

# ---------------------------------------------------------------------------
# DEV: file saving — works regardless of how the scraper is invoked
#   (python3 bootstrap.py, python3 runner.py, or any other entry point)
#   Set SAVE_FILES = True to persist PDF and TXT for every normalized record.
#   SAVE_DIR = None defaults to a "files/" folder next to bootstrap.py.
# ---------------------------------------------------------------------------
SAVE_FILES = True
SAVE_DIR: Optional[Path] = None   # e.g. Path("/tmp/cassazione")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def first(v):
    """Return the first element of a list, or the value itself."""
    return v[0] if isinstance(v, list) and v else v


def jointext(v) -> Optional[str]:
    """Join a list of strings into a single string."""
    if not v:
        return None
    if isinstance(v, list):
        return " ".join(item for item in v if item)
    return v


def parse_date(v) -> Optional[datetime]:
    """Parse a date string in YYYYMMDD or DD/MM/YYYY format."""
    v = first(v)
    if not v:
        return None
    for fmt in ("%Y%m%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def build_query(doc_type: str) -> str:
    """Build a Solr query string for the given doc_type, applying any SAMPLE_FILTER."""
    clauses = [f"kind:{doc_type}"]
    if SAMPLE_FILTER.get("anno"):
        clauses.append(f'anno:{SAMPLE_FILTER["anno"]}')
    if SAMPLE_FILTER.get("numdec"):
        clauses.append(f'numdec:{SAMPLE_FILTER["numdec"]}')
    if SAMPLE_FILTER.get("tipoprov"):
        clauses.append(f'tipoprov:"{SAMPLE_FILTER["tipoprov"]}"')
    return " AND ".join(clauses)


def ecli_to_filename(ecli: str) -> str:
    """Convert ECLI:IT:CASS:2023:1234CIV to ECLI-IT-CASS-2023-1234CIV."""
    return ecli.replace(":", "-")


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    """Represents a single court decision from italgiure.giustizia.it."""

    # --- common ---
    id: str
    kind: str
    materia: list
    presidente: Optional[str]
    relatore: Optional[str]
    datdep: Optional[datetime]

    # --- snciv / snpen only ---
    anno: Optional[str] = None
    numdec: Optional[str] = None
    tipoprov: Optional[str] = None
    szdec: Optional[str] = None
    ssz: Optional[str] = None
    datdec: Optional[datetime] = None
    pd: Optional[str] = None
    ocr: Optional[str] = None
    ocrdis: Optional[str] = None
    filename: Optional[str] = None
    sic_id: Optional[str] = None
    rnc_sp: Optional[str] = None
    rnc_art: Optional[str] = None
    rnc_gen: Optional[str] = None

    # --- sic only ---
    autorita: list = field(default_factory=list)
    localita: list = field(default_factory=list)
    ricorrente: list = field(default_factory=list)
    contro: list = field(default_factory=list)
    intimato: list = field(default_factory=list)
    consigliere: list = field(default_factory=list)
    nrg: Optional[str] = None
    anno_nrg: Optional[str] = None
    anno_prov: Optional[str] = None
    num_provv: Optional[str] = None
    data_ud: Optional[datetime] = None

    @classmethod
    def from_doc(cls, doc: dict) -> "Decision":
        return cls(
            # common
            id=doc.get("id"),
            kind=doc.get("kind"),
            materia=doc.get("materia", []),
            presidente=first(doc.get("presidente")),
            relatore=first(doc.get("relatore")),
            datdep=parse_date(doc.get("datdep") or doc.get("sic-datdep")),
            # snciv/snpen
            anno=doc.get("anno"),
            numdec=doc.get("numdec"),
            tipoprov=doc.get("tipoprov"),
            szdec=doc.get("szdec"),
            ssz=doc.get("ssz"),
            datdec=parse_date(doc.get("datdec")),
            pd=doc.get("pd"),
            ocr=jointext(doc.get("ocr")),
            ocrdis=jointext(doc.get("ocrdis")),
            filename=first(doc.get("filename")),
            sic_id=first(doc.get("sicId")),
            rnc_sp=first(doc.get("rnc-sp")),
            rnc_art=first(doc.get("rnc-art")),
            rnc_gen=first(doc.get("rnc-gen")),
            # sic
            autorita=doc.get("sic-autorita", []),
            localita=doc.get("sic-localita", []),
            ricorrente=doc.get("sic-ricorrente", []),
            contro=doc.get("sic-contro", []),
            intimato=doc.get("sic-intimato", []),
            consigliere=doc.get("sic-consigliere", []),
            nrg=first(doc.get("sic-nrg")),
            anno_nrg=first(doc.get("sic-anno_nrg")),
            anno_prov=first(doc.get("sic-anno_prov")),
            num_provv=first(doc.get("sic-num_provv")),
            data_ud=parse_date(doc.get("sic-data_ud")),
        )

    def pdf_url(self) -> Optional[str]:
        if not self.filename or not self.kind:
            return None
        clean_filename = self.filename.replace(".pdf", ".clean.pdf")
        return (
            f"https://www.italgiure.giustizia.it/xway/application/nif/clean/hc.dll"
            f"?verbo=attach&db={self.kind}&id={clean_filename}"
        )

    def ecli(self) -> Optional[str]:
        if not self.anno or not self.numdec:
            return None
        suffix = "CIV" if self.kind == "snciv" else "PEN" if self.kind == "snpen" else None
        if not suffix:
            return None
        number = str(int(self.numdec))
        return f"ECLI:IT:CASS:{self.anno}:{number}{suffix}"

    def has_text(self) -> bool:
        return bool(self.ocr and len(self.ocr) >= 100)

    def fetch_pdf_bytes(self, timeout: int = 60) -> Optional[bytes]:
        """Download the PDF and return raw bytes, or None on failure."""
        url = self.pdf_url()
        if not url:
            return None
        try:
            r = requests.get(url, verify=False, timeout=timeout)
            r.raise_for_status()
            return r.content
        except Exception as e:
            print(f"[{self.id}] PDF download failed: {e}")
            return None

    def fetch_pdf_text(self, timeout: int = 60) -> Optional[str]:
        """
        Download the PDF and extract body text via cassazione_pdf_extractor.
        Falls back to the Solr ocr field if download or extraction fails.
        """
        if not HAS_PDF_EXTRACTOR:
            raise ImportError(
                "cassazione_pdf_extractor is required (and pdfminer.six): "
                "pip install pdfminer.six"
            )

        pdf_bytes = self.fetch_pdf_bytes(timeout=timeout)
        if not pdf_bytes:
            return self.ocr or None

        try:
            layers = extract_decision(pdf_bytes)
            return layers.body if layers.body else self.ocr
        except Exception as e:
            print(f"[{self.id}] PDF text extraction failed: {e}")
            return self.ocr or None

    def fetch_pdf_layers(self, timeout: int = 60) -> Optional["DecisionLayers"]:
        """
        Download the PDF and return the full DecisionLayers object
        (body, sections, header metadata, oggetto).
        Returns None if download or extraction fails.
        """
        if not HAS_PDF_EXTRACTOR:
            return None

        pdf_bytes = self.fetch_pdf_bytes(timeout=timeout)
        if not pdf_bytes:
            return None

        try:
            return extract_decision(pdf_bytes)
        except Exception as e:
            print(f"[{self.id}] PDF layer extraction failed: {e}")
            return None

    def save_pdf(self, directory: str = ".", timeout: int = 60) -> Optional[str]:
        """Download and save the PDF to disk. Returns saved filepath or None."""
        pdf_bytes = self.fetch_pdf_bytes(timeout=timeout)
        if not pdf_bytes:
            return None
        filepath = os.path.join(directory, f"{self.id}.pdf")
        try:
            with open(filepath, "wb") as f:
                f.write(pdf_bytes)
            return filepath
        except Exception as e:
            print(f"[{self.id}] PDF save failed: {e}")
            return None

    def save_pdf_text(self, directory: str = ".", timeout: int = 60) -> Optional[str]:
        """Extract text from PDF and save as .txt. Returns saved filepath or None."""
        text = self.fetch_pdf_text(timeout=timeout)
        if not text:
            return None
        filepath = os.path.join(directory, f"{self.id}.txt")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
            return filepath
        except Exception as e:
            print(f"[{self.id}] Text save failed: {e}")
            return None


# ---------------------------------------------------------------------------
# Low-level client (search / paginate / facets)
# ---------------------------------------------------------------------------

class ItalgiureClient:
    """
    Thin wrapper around the italgiure Solr API.
    Useful for ad-hoc searches independent of the BaseScraper pipeline.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False

    def _query(self, params: dict) -> dict:
        params.setdefault("wt", "json")
        r = self.session.get(BASE_URL + SOLR_ENDPOINT, params=params)
        r.raise_for_status()
        return r.json()

    def search(
        self,
        query: str = "*:*",
        kind: Optional[str] = None,
        materia: Optional[str] = None,
        autorita: Optional[str] = None,
        localita: Optional[str] = None,
        anno_prov: Optional[int] = None,
        anno_nrg: Optional[int] = None,
        ricorrente: Optional[str] = None,
        contro: Optional[str] = None,
        relatore: Optional[str] = None,
        rows: int = 10,
        start: int = 0,
    ) -> tuple[list[Decision], int]:
        filters = []
        if kind:
            filters.append(f'kind:"{kind}"')
        if materia:
            filters.append(f'sic-materia:"{materia}"')
        if autorita:
            filters.append(f'sic-autorita:"{autorita}"')
        if localita:
            filters.append(f'sic-localita:"{localita}"')
        if anno_prov:
            filters.append(f'sic-anno_prov:"{anno_prov}"')
        if anno_nrg:
            filters.append(f'sic-anno_nrg:"{anno_nrg}"')
        if ricorrente:
            filters.append(f'sic-ricorrente:"{ricorrente}"')
        if contro:
            filters.append(f'sic-contro:"{contro}"')
        if relatore:
            filters.append(f'sic-relatore:"{relatore}"')

        params = {
            "q": query,
            "rows": rows,
            "start": start,
            "fl": "*",
        }
        if filters:
            params["fq"] = filters

        data = self._query(params)
        total = data["response"]["numFound"]
        docs = [Decision.from_doc(d) for d in data["response"]["docs"]]
        return docs, total

    def paginate(self, page_size: int = 50, **kwargs) -> Generator[Decision, None, None]:
        """Generator that yields all matching decisions page by page."""
        start = 0
        while True:
            docs, total = self.search(rows=page_size, start=start, **kwargs)
            if not docs:
                break
            yield from docs
            start += page_size
            if start >= total:
                break

    def facets(self, field: str, query: str = "*:*", limit: int = 20) -> dict:
        data = self._query({
            "q": query,
            "rows": 0,
            "facet": "true",
            "facet.field": field,
            "facet.limit": limit,
            "facet.mincount": 1,
        })
        counts = data["facet_counts"]["facet_fields"][field]
        return dict(zip(counts[::2], counts[1::2]))


# ---------------------------------------------------------------------------
# Full scraper (BaseScraper pipeline)
# ---------------------------------------------------------------------------

class CassazioneCivileScraper(BaseScraper):
    """
    Scraper for IT/CassazioneCivile -- Italian Supreme Court Case Law.
    Country: IT
    URL: https://www.cortedicassazione.it

    Data types: case_law
    Auth: none (Open access via SentenzeWeb)
    """

    def __init__(self):
        source_dir = Path(__file__).parent
        super().__init__(source_dir)

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        self._session.verify = False

        self.client = HttpClient(
            base_url=BASE_URL,
            headers={
                "User-Agent": "Mozilla/5.0 LegalDataHunter/1.0",
                "Accept": "application/json",
            },
            timeout=60,
        )

    def _solr_query(self, query: str, start: int = 0, rows: int = PAGE_SIZE,
                    sort: str = "pd desc") -> Dict[str, Any]:
        """Execute a Solr query and return the response dict."""
        params = {
            "q": query,
            "start": start,
            "rows": rows,
            "wt": "json",
            "fl": ",".join(SOLR_FIELDS),
            "sort": sort,
        }

        url = f"{BASE_URL}{SOLR_ENDPOINT}?{urlencode(params)}"

        try:
            self.rate_limiter.wait()
            resp = self._session.get(url, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", {"numFound": 0, "start": 0, "docs": []})

        except requests.exceptions.Timeout:
            logger.warning(f"Solr query timeout: start={start}")
            return {"numFound": 0, "start": 0, "docs": []}
        except Exception as e:
            logger.error(f"Solr query failed: {e}")
            return {"numFound": 0, "start": 0, "docs": []}

    def _format_date(self, date_str: str) -> Optional[str]:
        """Convert date from YYYYMMDD to ISO 8601 format."""
        if not date_str or len(date_str) < 8:
            return None
        try:
            if '-' in date_str:
                dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            else:
                dt = datetime.strptime(date_str[:8], "%Y%m%d")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    def _build_ecli(self, doc: Dict[str, Any]) -> str:
        """Build ECLI identifier. Format: ECLI:IT:CASS:{YEAR}:{ID}"""
        decision = Decision.from_doc(doc)
        ecli = decision.ecli()
        if ecli:
            return ecli
        year = doc.get("anno", "")
        doc_id = doc.get("id", "")
        if year and doc_id:
            return f"ECLI:IT:CASS:{year}:{doc_id}"
        return ""

    def fetch_sample(self, n: int = 12) -> Generator[dict, None, None]:
        """
        Yield a balanced sample: up to n//2 records from snciv and n//2 from snpen.
        Skips 'sic' entirely. If SAMPLE_FILTER.kind is set to one type, all n
        records come from that type.
        """
        kinds = [SAMPLE_FILTER["kind"]] if SAMPLE_FILTER.get("kind") else ["snciv", "snpen"]
        quota = max(1, n // len(kinds))

        for doc_type in kinds:
            type_name = "civil" if doc_type == "snciv" else "criminal"
            query = build_query(doc_type)
            logger.info(f"Sampling {quota} {type_name} decisions — query: {query}")

            fetched = 0
            start = 0
            while fetched < quota:
                result = self._solr_query(query, start=start, rows=min(PAGE_SIZE, quota - fetched))
                docs = result.get("docs", [])
                if not docs:
                    break
                for doc in docs:
                    ocr = jointext(doc.get("ocr", "")) or ""
                    if len(ocr) < 100:
                        continue
                    yield doc
                    fetched += 1
                    if fetched >= quota:
                        break
                start += len(docs)

            logger.info(f"  → yielded {fetched} {type_name} sample records")

    def run_sample(self, n: int = 12) -> dict:
        """Override BaseScraper.run_sample to use fetch_sample() for balanced results."""
        records_saved = 0
        sample_dir = Path(__file__).parent / "sample"
        sample_dir.mkdir(parents=True, exist_ok=True)

        for raw in self.fetch_sample(n=n):
            try:
                normalized = self.normalize(raw)
                if not normalized.get("text") and not normalized.get("pdf_text"):
                    continue
                out_path = sample_dir / f"record_{records_saved:04d}.json"
                out_path.write_text(
                    json.dumps(normalized, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
                records_saved += 1
                logger.info(f"Sample [{records_saved}/{n}] {normalized['_id']} — {normalized['title'][:60]}")
                if records_saved >= n:
                    break
            except Exception as e:
                logger.warning(f"Sample record failed: {e}")

        # Write all_samples.json aggregating every saved record
        all_records = []
        for i in range(records_saved):
            p = sample_dir / f"record_{i:04d}.json"
            try:
                all_records.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        all_samples_path = sample_dir / "all_samples.json"
        all_samples_path.write_text(
            json.dumps(all_records, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        logger.info(f"Written {len(all_records)} records to {all_samples_path}")

        return {"sample_records_saved": records_saved, "sample_dir": str(sample_dir)}

    def fetch_all(self) -> Generator[dict, None, None]:
        """Yield all case law documents from the Court of Cassation."""
        kinds = [SAMPLE_FILTER["kind"]] if SAMPLE_FILTER.get("kind") else DOC_TYPES
        for doc_type in kinds:
            type_name = "civil" if doc_type == "snciv" else "criminal"
            query = build_query(doc_type)
            logger.info(f"Fetching {type_name} decisions — query: {query}")

            result = self._solr_query(query, start=0, rows=1)
            total = result.get("numFound", 0)
            logger.info(f"Total {type_name} documents: {total:,}")

            start = 0
            while start < total:
                result = self._solr_query(query, start=start, rows=PAGE_SIZE)
                docs = result.get("docs", [])

                if not docs:
                    logger.warning(f"No documents returned at offset {start}")
                    break

                for doc in docs:
                    ocr = jointext(doc.get("ocr", "")) or ""
                    if len(ocr) < 100:
                        continue
                    yield doc

                start += len(docs)

                if start % 500 == 0:
                    logger.info(f"Progress: {start:,}/{total:,} ({100*start/total:.1f}%)")

    def fetch_updates(self, since: datetime) -> Generator[dict, None, None]:
        """Yield documents deposited since the given date."""
        since_str = since.strftime("%Y%m%d")
        now_str = datetime.now().strftime("%Y%m%d")

        kinds = [SAMPLE_FILTER["kind"]] if SAMPLE_FILTER.get("kind") else DOC_TYPES
        for doc_type in kinds:
            type_name = "civil" if doc_type == "snciv" else "criminal"
            base_query = build_query(doc_type)
            query = f"{base_query} AND pd:[{since_str} TO {now_str}]"
            logger.info(f"Fetching {type_name} updates — query: {query}")

            result = self._solr_query(query, start=0, rows=1)
            total = result.get("numFound", 0)
            logger.info(f"Found {total:,} {type_name} updates")

            start = 0
            while start < total:
                result = self._solr_query(query, start=start, rows=PAGE_SIZE)
                docs = result.get("docs", [])
                if not docs:
                    break
                for doc in docs:
                    ocr = jointext(doc.get("ocr", "")) or ""
                    if len(ocr) < 100:
                        continue
                    yield doc
                start += len(docs)

    def _extract_pdf_layers(self, pdf_url: str, doc_id: str = "") -> Optional["DecisionLayers"]:
        """
        Download the PDF from pdf_url and extract layered content.
        Returns a (DecisionLayers, bytes) tuple or (None, None) if unavailable.
        Logs a warning but never raises — normalize() always succeeds.
        """
        if not HAS_PDF_EXTRACTOR:
            logger.warning(f"[{doc_id}] HAS_PDF_EXTRACTOR=False — cassazione_pdf_extractor not importable")
            return None, None
        if not pdf_url:
            logger.warning(f"[{doc_id}] pdf_url is empty — decision.filename or kind missing")
            return None, None
        try:
            logger.info(f"[{doc_id}] Fetching PDF from: {pdf_url}")
            resp = self._session.get(pdf_url, timeout=60, verify=False)
            logger.info(f"[{doc_id}] HTTP {resp.status_code}, content-length={len(resp.content):,} bytes")
            resp.raise_for_status()
            pdf_bytes = resp.content
            layers = extract_decision(pdf_bytes)
            logger.info(f"[{doc_id}] Extracted: body={len(layers.body):,} chars, sections={list(layers.sections.keys())}, oggetto={layers.oggetto!r}")
            return layers, pdf_bytes
        except Exception as e:
            logger.warning(f"[{doc_id}] PDF layer extraction failed: {type(e).__name__}: {e}")
            return None, None

    def _save_files(self, ecli: str, pdf_bytes: Optional[bytes],
                    pdf_text: Optional[str], save_dir: Path) -> None:
        """
        Save PDF and extracted text to save_dir, named by ECLI (: replaced with -).
        Silently skips if the respective content is None.
        """
        if not ecli:
            return
        stem = ecli_to_filename(ecli)
        save_dir.mkdir(parents=True, exist_ok=True)

        if pdf_bytes:
            pdf_path = save_dir / f"{stem}.pdf"
            try:
                pdf_path.write_bytes(pdf_bytes)
                logger.info(f"Saved PDF:  {pdf_path}")
            except Exception as e:
                logger.warning(f"Could not save PDF {pdf_path}: {e}")

        if pdf_text:
            txt_path = save_dir / f"{stem}.txt"
            try:
                txt_path.write_text(pdf_text, encoding="utf-8")
                logger.info(f"Saved TXT:  {txt_path}")
            except Exception as e:
                logger.warning(f"Could not save TXT {txt_path}: {e}")

    def normalize(self, raw: dict, save_files: bool = False,
                  save_dir: Optional[Path] = None) -> dict:
        """
        Transform raw Solr document into standard schema.

        Args:
            raw:        Raw Solr document dict.
            save_files: If True, save the PDF and extracted TXT to save_dir.
            save_dir:   Directory for saved files (default: ./files next to bootstrap.py).
        """
        decision = Decision.from_doc(raw)

        doc_id = raw.get("id", "")
        kind = raw.get("kind", "")

        numdec = decision.numdec or ""
        anno = decision.anno or ""
        tipoprov = decision.tipoprov or ""
        szdec = decision.szdec or ""

        date_deposit = self._format_date(
            raw.get("datdep") if not isinstance(raw.get("datdep"), list)
            else first(raw.get("datdep"))
        )
        date_decision = self._format_date(raw.get("datdec", ""))

        case_type = "Civile" if kind == "snciv" else "Penale"
        title = f"Cassazione {case_type}, {tipoprov} n. {numdec}/{anno}"
        materia = first(raw.get("materia", "")) or ""
        if materia:
            title += f" - {materia}"

        # Build ECLI early — needed for file naming
        ecli = self._build_ecli(raw)

        # ── PDF extraction (best-effort, never blocks normalization) ──────────
        pdf_url = decision.pdf_url() or ""
        logger.info(f"[{doc_id}] decision.filename={decision.filename!r}, kind={decision.kind!r}, pdf_url={pdf_url!r}")
        layers, pdf_bytes = self._extract_pdf_layers(pdf_url, doc_id=doc_id)
        pdf_text     = layers.body if layers and layers.body else None
        pdf_sections = layers.sections if layers and layers.sections else None
        pdf_oggetto  = layers.oggetto if layers and layers.oggetto else None

        # ── Optionally persist PDF + TXT to disk ──────────────────────────────
        # Honour both the explicit argument and the module-level SAVE_FILES flag
        # so the feature works whether called from main(), runner.py, or any
        # other entry point.
        _do_save = save_files or SAVE_FILES
        if _do_save:
            _save_dir = save_dir or SAVE_DIR or Path(__file__).parent / "files"
            self._save_files(ecli, pdf_bytes, pdf_text, _save_dir)

        return {
            "_id": doc_id,
            "_source": "IT/CassazioneCivile",
            "_type": "case_law",
            "_fetched_at": datetime.now(timezone.utc).isoformat(),
            "title": title,
            "date": date_deposit or date_decision or "",
            "url": pdf_url,
            "ecli": ecli,
            "court": "Corte Suprema di Cassazione",
            "jurisdiction": "IT",
            "case_type": case_type.lower(),
            "decision_type": tipoprov,
            "decision_number": numdec,
            "section": szdec,
            "year": anno,
            "date_deposit": date_deposit,
            "date_decision": date_decision,
            "president": decision.presidente or "",
            "reporter": decision.relatore or "",
            "subject_matter": materia,
            "sic_id": decision.sic_id or "",
            "rnc_sp": decision.rnc_sp or "",
            "rnc_art": decision.rnc_art or "",
            "rnc_gen": decision.rnc_gen or "",
            "language": "it",
            # text: Solr OCR field (fast, always present when document has text)
            "text": decision.ocr if decision.has_text() else None,
            "text_dis": decision.ocrdis if decision.ocrdis else None,
            # pdf_text: clean body extracted directly from PDF layout
            #           (no watermarks, footers, or header metadata)
            "pdf_text": pdf_text,
            # pdf_sections: {"Fatti di causa": "...", "Ragioni della decisione": "..."}
            "pdf_sections": pdf_sections,
            # pdf_oggetto: subject from the right-column block (snciv only)
            "pdf_oggetto": pdf_oggetto,
        }

    def test_connection(self):
        """Quick connectivity test."""
        print("Testing Court of Cassation SentenzeWeb API...")

        print("\n1. Testing Solr endpoint...")
        try:
            result = self._solr_query("*:*", start=0, rows=1)
            total = result.get("numFound", 0)
            print(f"   Total documents: {total:,}")
        except Exception as e:
            print(f"   ERROR: {e}")
            return

        print("\n2. Testing civil cases (snciv)...")
        try:
            result = self._solr_query("kind:snciv", start=0, rows=2)
            total = result.get("numFound", 0)
            print(f"   Civil cases: {total:,}")

            docs = result.get("docs", [])
            if docs:
                decision = Decision.from_doc(docs[0])
                print(f"   Sample ID: {decision.id}")
                print(f"   Type: {decision.tipoprov}")
                print(f"   ECLI: {decision.ecli()}")
                print(f"   PDF URL: {decision.pdf_url()}")
                print(f"   Has text: {decision.has_text()} ({len(decision.ocr or ''):,} chars)")
                if decision.ocr:
                    print(f"   Text preview: {decision.ocr[:200]}...")
        except Exception as e:
            print(f"   ERROR: {e}")

        print("\n3. Testing criminal cases (snpen)...")
        try:
            result = self._solr_query("kind:snpen", start=0, rows=1)
            total = result.get("numFound", 0)
            print(f"   Criminal cases: {total:,}")
        except Exception as e:
            print(f"   ERROR: {e}")

        print("\n4. Testing full document normalize...")
        try:
            result = self._solr_query("kind:snciv", start=0, rows=1)
            docs = result.get("docs", [])
            if docs:
                normalized = self.normalize(docs[0])
                print(f"   Title: {normalized['title']}")
                print(f"   Date: {normalized['date']}")
                print(f"   Text length (ocr):     {len(normalized['text'] or ''):,} chars")
                print(f"   Text length (pdf):     {len(normalized['pdf_text'] or ''):,} chars")
                print(f"   PDF sections:          {list((normalized['pdf_sections'] or {}).keys())}")
                print(f"   PDF oggetto:           {normalized['pdf_oggetto']}")
                print(f"   ECLI: {normalized['ecli']}")
                print(f"   PDF URL: {normalized['url']}")
        except Exception as e:
            print(f"   ERROR: {e}")

        print("\nTest complete!")


def main():
    scraper = CassazioneCivileScraper()

    if len(sys.argv) < 2:
        print(
            "Usage: python bootstrap.py [bootstrap|update|test] "
            "[--sample] [--sample-size N] [--save-files] [--save-dir PATH]"
        )
        sys.exit(1)

    command = sys.argv[1]
    sample_mode  = "--sample" in sys.argv
    save_files   = "--save-files" in sys.argv
    sample_size  = 12

    save_dir = None
    if "--save-dir" in sys.argv:
        idx = sys.argv.index("--save-dir")
        save_dir = Path(sys.argv[idx + 1])
    elif save_files:
        save_dir = Path(__file__).parent / "files"

    if "--sample-size" in sys.argv:
        idx = sys.argv.index("--sample-size")
        sample_size = int(sys.argv[idx + 1])

    if save_files:
        logger.info(f"File saving enabled → {save_dir}")

    # Patch normalize to forward save_files / save_dir transparently so
    # BaseScraper.bootstrap() / run_sample() — which call self.normalize(raw) —
    # also respect the flags without needing changes to the base class.
    if save_files:
        _orig_normalize = scraper.normalize
        scraper.normalize = lambda raw: _orig_normalize(
            raw, save_files=True, save_dir=save_dir
        )

    if command == "test":
        scraper.test_connection()

    elif command == "bootstrap":
        if sample_mode:
            stats = scraper.run_sample(n=sample_size)
            print(
                f"\nSample complete: "
                f"{stats.get('sample_records_saved', 0)} records saved to sample/"
            )
        else:
            stats = scraper.bootstrap()
            print(
                f"\nBootstrap complete: {stats['records_new']} new, "
                f"{stats['records_updated']} updated, "
                f"{stats['records_skipped']} skipped"
            )
        print(json.dumps(stats, indent=2))

    elif command == "update":
        stats = scraper.update()
        print(
            f"\nUpdate complete: {stats['records_new']} new, "
            f"{stats['records_updated']} updated"
        )
        print(json.dumps(stats, indent=2))

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()