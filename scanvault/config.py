"""Configuration: defaults, TOML file, and CLI overrides."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

DEFAULT_CATEGORIES = [
    "Invoices",
    "Receipts",
    "Contracts",
    "Banking",
    "Accounting",
    "Investments",
    "Pensions",
    "Loans",
    "Taxes",
    "Insurance",
    "Medical",
    "Government",
    "Identity",
    "Legal",
    "Employment",
    "Education",
    "Property",
    "Vehicle",
    "Utilities",
    "Travel",
    "Subscriptions",
    "Correspondence",
    "Manuals",
    "Personal",
    "Other",
]

CONFIG_FILENAME = "scanvault.toml"


@dataclass
class OcrConfig:
    # "auto" picks ocrmypdf, then tesseract, then gives up.
    backend: str = "auto"
    languages: str = "eng"
    # Below this many extracted characters a PDF counts as image-only, i.e.
    # worth OCR'ing when it first arrives.
    min_text_chars: int = 180
    # A PDF that already carries this much text has been OCR'd (or was born
    # digital). Kept low on purpose: a receipt's whole text layer is 50
    # characters, and re-OCR'ing it on every organize run is pure waste.
    searchable_min_chars: int = 10
    # Re-OCR even when a text layer is present.
    force: bool = False
    # Assumed resolution when a bare image carries none.
    image_dpi: int = 300
    rotate_pages: bool = True
    deskew: bool = True
    optimize: int = 1
    timeout: int = 900
    jobs: int = 0  # 0 -> let the backend decide


DATE_SOURCES = ("filename", "pdf-metadata", "file-created")


@dataclass
class DateConfig:
    """Where a document's date comes from when its text does not carry one.

    The date inside the document always wins; these are tried in order after it.
    Set to an empty list to leave undated documents undated.
    """

    fallbacks: list[str] = field(default_factory=lambda: list(DATE_SOURCES))
    # 03/04/2024 is the 3rd of April here and the 4th of March in the US.
    day_first: bool = True


# Keyword -> tag rules applied on top of whatever the model returns, so a
# document from a tax authority is tagged `taxes` whether or not the model
# thought to. Matched case-insensitively, on word boundaries, against the title,
# the correspondent, the summary and the start of the text. Keywords are English
# and Swedish because that is what this paperwork is in; add your own.
DEFAULT_TAG_RULES: dict[str, list[str]] = {
    "taxes": [
        "hmrc", "hm revenue", "revenue & customs", "revenue and customs", "self assessment",
        "irs.gov", "internal revenue service", "skatteverket", "inkomstdeklaration",
        "finanzamt", "agenzia delle entrate", "canada revenue agency", "australian taxation",
        "tax return", "corporation tax", "vat return", "capital gains",
        "slutskattebesked", "skatteuträkning", "deklaration", "tax credit",
        "tax code notice", "p800", "national insurance", "personnummer",
    ],
    "government": [
        "council tax", "borough of", "kommun", "county council", "ministry of", "home office",
        "companies house", "bolagsverket", "land registry", "dvla", "passport office",
        "migrationsverket", "electoral register", "register of electors", "försäkringskassan",
        "department for", "gov.uk", "lantmäteriet", "kronofogden", "polismyndigheten",
        "folkbokföring", "socialstyrelsen", "transportstyrelsen", "arbetsförmedlingen",
        "planning permission", "building control",
    ],
    "student-loan": [
        "csn", "centrala studiestödsnämnden", "studielån", "studiemedel", "återbetalning av lån",
        "student loans company", "student finance", "tuition fee loan", "maintenance loan",
        "federal student aid", "fafsa", "nelnet", "navient", "sallie mae", "betalningsplan",
    ],
    "pension": [
        "pension", "tjänstepension", "premiepension", "pensionsmyndigheten", "workplace pension",
        "auto enrolment", "annuity", "sipp", "401(k)", "ira contribution", "nest pension",
        "alecta", "amf", "avtalspension", "retirement statement",
    ],
    "investments": [
        "brokerage", "portfolio statement", "dividend", "isk", "investeringssparkonto",
        "avanza", "nordnet", "fondkonto", "share certificate", "stocks and shares isa",
        "capital account statement", "vanguard", "index fund", "aktieutdelning",
    ],
    "mortgage": [
        "mortgage", "remortgage", "redemption statement", "loan to value", "bolån",
        "amorteringskrav", "räntebesked", "fixed rate expiry",
    ],
    "loan": [
        "loan agreement", "credit agreement", "instalment plan", "avbetalning", "blancolån",
        "personal loan", "hire purchase", "overdraft",
    ],
    "property": [
        "tenancy", "leasehold", "freehold", "landlord", "estate agent", "service charge",
        "ground rent", "stamp duty", "conveyanc", "hyresavtal", "bostadsrätt",
        "föreningsstämma", "brf", "energy performance certificate", "survey report",
    ],
    "insurance": [
        "policy number", "insurance certificate", "insurance premium", "premium due",
        "no claims", "försäkring",
        "hemförsäkring", "trafikförsäkring", "claim reference", "excess payable", "renewal notice",
    ],
    "utilities": [
        "electricity", "gas supply", "water and wastewater", "broadband", "meter reading",
        "energy bill", "energy tariff", "unit rate", "elräkning", "fjärrvärme", "avfallshantering", "standing charge",
    ],
    "telecoms": [
        "mobile bill", "sim only", "line rental", "mobilabonnemang", "bredband",
        "data allowance", "roaming charges",
    ],
    "banking": [
        "sort code", "iban", "account statement", "bankgiro", "swift/bic", "kontoutdrag",
        "credit card statement", "direct debit", "autogiro", "standing order",
    ],
    "vehicle": [
        "mot test", "v5c", "vehicle registration", "logbook", "besiktning", "bilprovningen",
        "road tax", "fordonsskatt", "service history", "congestion charge", "parkeringsanmärkning",
    ],
    "medical": [
        "nhs", "patient", "prescription", "vaccination", "vårdcentral", "1177", "remiss",
        "journalutdrag", "dental", "optician", "referral letter", "sjukintyg",
    ],
    "employment": [
        "payslip", "p60", "p45", "employment contract", "anställningsavtal", "lönespecifikation",
        "notice period", "probation period", "arbetsgivarintyg", "bonus letter", "share options",
    ],
    "education": [
        "tuition", "enrolment", "transcript of records", "antagningsbesked", "kursintyg",
        "examensbevis", "diploma", "course certificate", "school report", "terminsbetyg",
    ],
    "identity": [
        "passport", "driving licence", "driver's license", "körkort", "id-kort", "national id",
        "residence permit", "uppehållstillstånd", "birth certificate", "personbevis",
        "marriage certificate", "vigselbevis",
    ],
    "legal": [
        "solicitor", "advokat", "power of attorney", "fullmakt", "deed of", "last will",
        "testamente", "court claim", "claim form", "tingsrätt", "settlement agreement",
        "non-disclosure", "confidentiality agreement", "sekretessavtal", "governed by the laws",
        "bouppteckning", "arvskifte",
    ],
    "travel": [
        "boarding pass", "booking reference", "itinerary", "flight number", "hotel confirmation",
        "biljett", "resebokning", "car hire", "travel insurance", "visa application",
    ],
    "subscription": [
        "subscription", "membership", "renewal reminder", "medlemskap", "abonnemang",
        "annual membership", "gym membership", "licence fee",
    ],
    "warranty": [
        "warranty", "guarantee certificate", "guarantee period", "garanti",
        "proof of purchase", "extended cover",
        "return policy", "kvitto sparas",
    ],
    "business": [
        "organisationsnummer", "org.nr", "vat registration", "vat number", "company number",
        "purchase order", "bill to", "faktura till", "bankgiro", "plusgiro", "leverantörsfaktura",
        "kundfaktura", "payment terms", "betalningsvillkor", "our reference", "vår referens",
        "styrelsen", "aktiebolag", "ltd", "plc", "gmbh", "oy", "limited company",
        "non-disclosure", "mutual nda", "governed by the laws", "notice period",
        "contract of employment", "anställningsavtal", "employer", "arbetsgivare",
    ],
    "accounting": [
        "årsredovisning", "bokslut", "revisionsberättelse", "balansräkning", "resultaträkning",
        "annual accounts", "profit and loss", "balance sheet", "ledger", "bookkeeping",
        "nettoomsättning", "auditor",
    ],
    "payroll": [
        "arbetsgivardeklaration", "arbetsgivaravgifter", "avdragen skatt", "payroll",
        "employer national insurance", "pension contributions summary",
    ],
    "vat": ["momsdeklaration", "utgående moms", "ingående moms", "vat return", "vat due"],
    "charity": ["gift aid", "donation receipt", "gåvobevis", "charity number", "sponsorship"],
    "pets": ["veterinary", "veterinär", "microchip", "pet insurance", "vaccination card"],
    "home-improvement": [
        "quotation for", "offert", "builder", "renovation", "installation certificate",
        "gas safety", "electrical certificate", "byggnadsarbete", "hantverkare",
    ],
}


# When the model puts a document in a generic bucket but a keyword rule knows
# what it is, the rule wins. Only these buckets are overridable.
GENERIC_CATEGORIES = ("Other", "Correspondence", "Personal")

# These say something about a document without saying what it is, so they never
# overrule a category that came from somewhere better.
WEAK_TAGS = ("banking", "government", "property", "business", "legal")

# Which category a rule tag implies, for exactly that case.
# Tags that describe a document precisely enough to file it. Order is priority.
DEFAULT_TAG_CATEGORIES: dict[str, str] = {
    "vat": "Taxes",
    "payroll": "Taxes",
    "accounting": "Accounting",
    "mortgage": "Loans",
    "loan": "Loans",
    "student-loan": "Loans",
    "pension": "Pensions",
    "investments": "Investments",
    "taxes": "Taxes",
    "government": "Government",
    "property": "Property",
    "insurance": "Insurance",
    "utilities": "Utilities",
    "telecoms": "Utilities",
    "banking": "Banking",  # from here down: too broad to overrule a real guess
    "vehicle": "Vehicle",
    "medical": "Medical",
    "employment": "Employment",
    "education": "Education",
    "identity": "Identity",
    "legal": "Legal",
    "travel": "Travel",
    "subscription": "Subscriptions",
    "warranty": "Receipts",
    "home-improvement": "Property",
}


@dataclass
class TagConfig:
    # Extra tags every note gets.
    base: list[str] = field(default_factory=lambda: ["scan"])
    # Add a `year-2024` tag, so a year's paperwork is one search away.
    year_tag: bool = True
    # Tag the sender, so everything from one organisation is one search away.
    correspondent_tag: bool = True
    # Tag what the document is about - a property, a vehicle, an account.
    subject_tags: bool = True
    max_tags: int = 12
    # Below this confidence the note is tagged `needs-review`.
    review_below: float = 0.5
    # Let a keyword rule pick the category when the model reached for a generic
    # one. A mortgage tariff is not "Correspondence".
    rules_set_category: bool = True
    tag_categories: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_TAG_CATEGORIES)
    )
    rules: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_TAG_RULES.items()}
    )
    # Merged on top of `rules`, so you can add a group - or more keywords to an
    # existing one - without restating the built-in table.
    extra_rules: dict[str, list[str]] = field(default_factory=dict)

    def all_rules(self) -> dict[str, list[str]]:
        merged = {tag: list(keywords) for tag, keywords in self.rules.items()}
        for tag, keywords in self.extra_rules.items():
            merged.setdefault(tag, [])
            merged[tag].extend(k for k in keywords if k not in merged[tag])
        return merged


@dataclass
class LlmConfig:
    host: str = "http://localhost:11434"
    model: str = "qwen3.5:9b"
    # Fall back to heuristics instead of failing when ollama is unreachable.
    fallback_to_heuristics: bool = True
    temperature: float = 0.0
    num_ctx: int = 8192
    timeout: int = 300
    # How much document text the classifier gets to see.
    max_chars: int = 12000
    keep_alive: str = "5m"


@dataclass
class ParaConfig:
    """PARA ("Second Brain") top-level folders.

    Scanned documents are reference material, so they land in the archive
    unless a note says otherwise via its `para:` frontmatter key.
    """

    projects_dir: str = "1 Projects"
    areas_dir: str = "2 Areas"
    resources_dir: str = "3 Resources"
    archive_dir: str = "4 Archive"
    default_bucket: str = "archive"

    def folder(self, bucket: str) -> str:
        return {
            "project": self.projects_dir,
            "area": self.areas_dir,
            "resource": self.resources_dir,
            "archive": self.archive_dir,
        }.get(bucket, self.archive_dir)

    def buckets(self) -> dict[str, str]:
        """bucket name -> folder, in PARA order."""
        return {
            "project": self.projects_dir,
            "area": self.areas_dir,
            "resource": self.resources_dir,
            "archive": self.archive_dir,
        }


BUCKETS = ("project", "area", "resource", "archive")


@dataclass
class VaultConfig:
    # The PARA folders sit at the vault root, so the templates carry the
    # full path from there.
    notes_dir: str = ""
    attachments_dir: str = ""
    para: ParaConfig = field(default_factory=ParaConfig)
    # Available placeholders: para, category, year, month, date, name, title,
    # slug, correspondent. `name` is correspondent + title, which is what makes
    # a filename readable on its own.
    note_path_template: str = "{para}/{category}/{year}/{date} {name}"
    attachment_path_template: str = "{para}/_attachments/{category}/{year}/{date} {name}"
    # move | copy | leave
    source_action: str = "move"
    # Turn the folder a document came from into tags, so an existing folder
    # tree ("Work receipts/To expense") survives the move into PARA.
    tag_source_folder: bool = True
    # An image is filed as a searchable PDF; keep the picture it came from too.
    keep_original_image: bool = True
    # Embed the OCR text in the note so Obsidian search can reach it.
    include_text: bool = True
    # How that text is presented: a callout folded shut by default, an HTML
    # <details> block, or the plain heading it used to be.
    extracted_text_style: str = "callout"
    max_text_chars: int = 20000
    # Written under the vault; holds the dedupe index.
    state_dir: str = ".scanvault"


@dataclass
class Config:
    source_dir: Path | None = None
    vault_dir: Path | None = None
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    # "auto" keeps each document's own language for its title and summary.
    language_hint: str = "auto"
    ocr: OcrConfig = field(default_factory=OcrConfig)
    dates: DateConfig = field(default_factory=DateConfig)
    tags: TagConfig = field(default_factory=TagConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    vault: VaultConfig = field(default_factory=VaultConfig)

    @property
    def notes_root(self) -> Path:
        return self._vault_subdir(self.vault.notes_dir)

    @property
    def attachments_root(self) -> Path:
        return self._vault_subdir(self.vault.attachments_dir)

    @property
    def state_root(self) -> Path:
        return self._vault_subdir(self.vault.state_dir)

    def _vault_subdir(self, name: str) -> Path:
        if self.vault_dir is None:
            raise ValueError("vault_dir is not set")
        return self.vault_dir / name if name else self.vault_dir


def _apply(target: Any, values: dict[str, Any], path: str) -> None:
    known = {f.name: f for f in fields(target)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"unknown config key: {path}{key}")
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value, f"{path}{key}.")
            continue
        annotation = known[key].type
        if annotation in ("Path | None", "Path") and isinstance(value, str):
            value = Path(value).expanduser()
        setattr(target, key, value)


def load_config(path: Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    """Load defaults, then a TOML file (if any), then explicit overrides."""
    config = Config()
    if path is not None:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        _apply(config, data, "")
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if "." in key:
            section_name, _, leaf = key.partition(".")
            _apply(getattr(config, section_name), {leaf: value}, f"{section_name}.")
        else:
            _apply(config, {key: value}, "")
    return config


def find_config(explicit: Path | None, vault_dir: Path | None) -> Path | None:
    """Explicit path wins, then <vault>/.scanvault/scanvault.toml, then ./scanvault.toml."""
    if explicit is not None:
        if not explicit.exists():
            raise FileNotFoundError(f"config file not found: {explicit}")
        return explicit
    candidates = []
    if vault_dir is not None:
        candidates.append(vault_dir / ".scanvault" / CONFIG_FILENAME)
        candidates.append(vault_dir / CONFIG_FILENAME)
    candidates.append(Path.cwd() / CONFIG_FILENAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
