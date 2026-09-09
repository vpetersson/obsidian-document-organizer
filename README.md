# obsidian-document-organizer

[![CI](https://github.com/vpetersson/obsidian-document-organizer/actions/workflows/ci.yml/badge.svg)](https://github.com/vpetersson/obsidian-document-organizer/actions/workflows/ci.yml)

Private document management for scanned paper. Point it at the folder your
scanner writes to and it gives you an organized Obsidian vault of searchable PDFs
and notes. **All of it can run on your own machine** — OCR is local software and
the classifier is [ollama](https://ollama.com), pointed at `localhost` by
default. No account, no cloud service, no telemetry, and your documents stay
files you own in a folder you chose. The package and CLI are called `scanvault`.

* **Phase 1 — OCR.** Every incoming PDF is checked for a text layer; image-only
  scans are run through OCR and re-saved as a searchable PDF. Photos —
  `.jpg`, `.png`, `.tiff`, `.heic` and friends — are converted to a searchable
  PDF the same way, so a picture of a receipt is a document like any other.
* **Phase 2 — Organize.** The text goes to a local ollama
  model (default `qwen3.5:9b`), which returns a title, category, date,
  correspondent, tags and a summary. scanvault writes an Obsidian note with that
  metadata as YAML frontmatter and files both the note and the PDF into a dated
  folder structure.
* **Organizer.** Both phases can be re-run over an *existing* vault: it OCRs
  attachments that have no text layer, adopts loose PDFs, and re-files notes
  whose metadata is missing or whose location no longer matches the configured
  layout.

The vault is laid out as a PARA ("Second Brain") structure, with the archive as
its cornerstone — scanned paper is reference material, so that is where it
lands. See [Vault layout](#vault-layout-para).

No Python dependencies — the standard library only. External tools (OCR engine,
ollama) are detected at runtime and reported by `scanvault doctor`.

## Privacy

The point of running this locally is that documents like these — invoices,
medical letters, bank statements, anything with your address on it — are exactly
what you do not want in someone else's system.

**All of it can run on your own machine, and by default it does.** OCR is local
software. The classifier is whatever ollama endpoint you configure, and the
default is `http://localhost:11434` — so out of the box, with a model pulled,
you can disconnect the machine from the network entirely and the whole pipeline
still works.

There is exactly one outbound request in the codebase, in `scanvault/llm.py`, and
it goes to that ollama host. Where you point it is your call: the machine you are
on, the box with the GPU in the next room, or something further away. Classifying
a document means sending its text to whatever you chose, and `scanvault doctor`
tells you which it is:

```
model host  : http://localhost:11434 (this machine)
```

Nothing else phones anywhere. No analytics, no update check, no crash reporting,
no account — not the PDFs, not the OCR text, not the metadata.

**What runs locally:** OCR through `ocrmypdf`/`tesseract`, text extraction
through poppler or pypdf, classification through ollama, and the filing logic
itself, which is standard-library Python.

**What is written, and where:** everything lives inside the vault you point at.

| Path | Contents |
| --- | --- |
| `<vault>/…` | Your notes and PDFs, as plain Markdown and PDF files |
| `<vault>/.scanvault/index.json` | SHA-256 of each filed document, so re-runs skip it |
| `<vault>/.scanvault/classifications.json` | The model's answers, cached so a preview is not paid for twice |
| `<vault>/.scanvault/work/` | Temporary OCR output |

Nothing is written outside the vault, and deleting `.scanvault/` costs you only
the dedupe index and the cache.

Two things worth being deliberate about, because they are your choice rather
than the tool's:

* the OCR text is embedded in each note so Obsidian can search it, which means
  the contents of a document are in plain text in your vault — set
  `include_text = false` under `[vault]` if you would rather they were not;
* if your vault sits in iCloud, Dropbox or a Git remote, your documents go
  wherever that syncs them. That is outside this tool, but it is the part most
  likely to matter.

## Install

The project is managed with [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:vpetersson/obsidian-document-organizer.git
cd obsidian-document-organizer
uv sync                     # creates .venv and installs the project
uv sync --extra fast        # optional: pypdf + fontTools + cryptography for
                            # faster, more accurate text extraction
```

`uv sync` picks up the pinned interpreter from `.python-version` and installs it
if it is missing. Run anything through `uv run`:

```bash
uv run scanvault --help
```

To get a `scanvault` command on your PATH without activating a venv:

```bash
uv tool install .           # or: uv tool install git+https://github.com/vpetersson/obsidian-document-organizer
```

System tools:

```bash
# Debian/Ubuntu — OCR engine (ocrmypdf is preferred, it wraps tesseract + ghostscript)
sudo apt install ocrmypdf tesseract-ocr poppler-utils
sudo apt install tesseract-ocr-swe        # one package per extra language

# Order matters: "swe+eng" was measured 3x more accurate than "eng+swe" on
# Swedish documents, and identical on English ones. Put your main language
# first. `scanvault doctor` lists any pack you have configured but not installed.

# macOS
brew install ocrmypdf poppler

# the model
ollama pull qwen3.5:9b
```

Then check everything is wired up:

```bash
scanvault doctor --vault ~/Obsidian/Archive --source ~/Scans/inbox
```

## Use

Prefix any of these with `uv run` if you did not `uv tool install` the CLI.

**Every command that writes previews by default.** Run it, read what it says it
will do, then repeat it with `--apply`. There is no command that changes your
vault without that flag, and `--dry-run` is accepted everywhere as an explicit
way to say "preview", which is useful in scripts.

```bash
# create the PARA folders (optional; ingest does it too)
scanvault init-vault --vault ~/Obsidian/Archive --apply

# see what would happen, change nothing
scanvault ingest --source ~/Scans/inbox --vault ~/Obsidian/Archive

# do it (originals are moved into the vault; --keep-source copies instead)
scanvault ingest --source ~/Scans/inbox --vault ~/Obsidian/Archive --apply

# keep watching the scanner folder
scanvault watch --source ~/Scans/inbox --vault ~/Obsidian/Archive --interval 30 --apply

# phase 1 only: OCR into searchable PDFs and dump the text
scanvault ocr ~/Scans/inbox --out ~/Scans/ocr --text-out ~/Scans/text --apply

# existing vault: preview, then apply (OCRs un-OCR'd PDFs, refiles notes)
scanvault organize --vault ~/Obsidian/Archive
scanvault organize --vault ~/Obsidian/Archive --apply
scanvault organize --vault ~/Obsidian/Archive --reclassify --apply
```

`ingest` is safe to re-run: every filed document is recorded by SHA-256 in
`<vault>/.scanvault/index.json`, so the same scan is never filed twice.

## What lands in the vault

```
Archive/
├── 1 Projects/
├── 2 Areas/
├── 3 Resources/
├── 4 Archive/
│   ├── Invoices/2024/2024-05-02 Acme Invoice INV-1234.md
│   └── _attachments/Invoices/2024/2024-05-02 Acme Invoice INV-1234.pdf
└── .scanvault/index.json
```

```markdown
---
title: "Acme Invoice INV-1234"
date: 2024-05-02
date_source: "document"
title_source: "model"
category: "Invoices"
correspondent: "Acme Ltd"
tags:
  - scan
  - invoices
  - year-2024
  - acme-ltd
  - acme
subjects:
  - "Account 4242"
reference: "INV-1234"
amount: "120.00"
currency: "EUR"
confidence: 0.95
para: "archive"
classifier: "llm"
source_file: "scan_001.pdf"
source_hash: "9f2c…"
ocr: "ocrmypdf"
pages: 2
attachment: "4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Invoice INV-1234.pdf"
---

# Acme Invoice INV-1234

Invoice INV-1234 from Acme Ltd for 120.00 EUR.

![[4 Archive/_attachments/Invoices/2024/2024-05-02 Acme Invoice INV-1234.pdf]]

> [!quote]- Extracted text
> ```text
> ACME LTD
> INVOICE 2024-05-02
> …
> ```
```

That last block is a callout, and the `-` after the type is what makes Obsidian
render it folded shut — so a page of OCR does not bury the summary and the
attachment while you are browsing, but the text is still in the file and search
still finds it. `[vault] extracted_text_style` takes `callout` (the default),
`details` for an HTML `<details>` block, or `plain` for the old heading, and
`include_text = false` leaves the text out altogether.

Notes written before this pick up the new shape on the next
`organize --apply`, which reports them as `extracted text is plain, not callout`
and rewrites them around the text they already hold.

## Organizing an existing vault

`scanvault organize` is phase 1 *and* phase 2 applied to documents that are
already in the vault. Like every other writing command it previews by default:
it prints one line per document and changes nothing until you add `--apply`.

```bash
scanvault organize --vault ~/Obsidian/Archive
```

```
[dry-run] ocr: 4 Archive/Invoices/2024/2024-05-02 Acme Invoice.md (attachment has no text layer)
[dry-run] relocate: 4 Archive/Unsorted/old.md -> 4 Archive/Contracts/2022/2022-02-02 Old Note.md (layout drift)
[dry-run] adopt: Work receipts/scan-2017-03-05.pdf (image-only PDF; will OCR, classify and file under "4 Archive")
[dry-run] duplicate: Work receipts/scan-2017-03-05 - 1.pdf (same content as 4 Archive/Receipts/2017/2017-03-05 Coffee House.md)

1 to OCR, 1 to relocate, 0 to rewrite, 1 to adopt (1 of them need OCR), 12 already filed, 1 duplicates, 3 left alone, 0 failed
3 notes were left alone because scanvault did not write them; pass --include-unmanaged to file those too, or -v to list them.
Nothing was changed. Re-run with --apply to execute.
```

Only the lines that mean something get printed: notes that are already filed,
left alone or part of the PARA scaffolding are counted in the summary but not
listed, because on a real vault those are hundreds of lines that bury the ones
that matter. `-v` lists everything, and works before or after the subcommand.

What each action means:

| Action | What `--apply` does |
| --- | --- |
| `ocr` | The note's PDF has no text layer. Runs OCR, **replaces the attachment with the searchable PDF**, refreshes the note's extracted text and records the backend in `ocr:`. If the note's metadata was thin, it is classified from the fresh text and refiled. |
| `relocate` | Moves the note and its PDF to where the templates say they belong. |
| `rewrite` | Keeps the location, refreshes frontmatter from a new classification. |
| `adopt` | A PDF in the vault that no note points at: OCR'd, classified and given a note. The plan says which of them have no text layer. |
| `duplicate` | Byte-identical to a document already filed. Reported, never filed twice and never deleted. |
| `already filed` | Nothing to do. |
| `left alone` | A note scanvault did not write — see below. |
| `index` | A PARA index note: scaffolding, never touched and never counted as a document. |

Folders are provenance, not clutter: a PDF adopted from `Work receipts/To
expense/` keeps `source_folder: "Work receipts/To expense"` in its
frontmatter and picks up `work-receipts` and `to-expense` as tags, so the
grouping your folders encoded survives the move into PARA. Set
`tag_source_folder = false` to keep the frontmatter but skip the tags.

A PDF only counts as loose when **no** note references it — `attachment:` in
frontmatter, an `![[embed]]`, a `[[wikilink]]` or a markdown link all keep it out
of the adopt list, so PDFs your own hand-written notes point at are never filed
a second time.

### The model is asked once

Planning classifies every note it is going to touch, and that is a model call
each. Those answers are cached in `<vault>/.scanvault/classifications.json`, so
the `--apply` you run after reading the preview reuses them instead of paying for
the same work again:

```
INFO classifications: 191 cached, 0 new
```

The key covers the document's text, the model, the category list and the language
hint, so switching model or editing a document misses the cache rather than
returning something stale. `--no-cache` forces fresh answers.

This is the one thing a dry run writes: the cache lives under `.scanvault/`
inside the vault, never in your documents.

Planning never runs OCR or moves anything, so a dry run stays cheap and safe;
the OCR work happens only under `--apply`. Pass `--no-ocr` to skip the OCR pass
entirely, `--reclassify` to re-run the model over every note, and `--no-adopt`
to ignore loose PDFs.

A document that was scanned before you had OCR set up therefore becomes
searchable — in Obsidian *and* in the PDF itself — with:

```bash
scanvault organize --vault ~/Obsidian/Archive --apply
```

## Dating a document

The date a document is filed under is the date printed *on* it — the invoice
date, the statement date, the date at the top of a letter — extracted from the
text by the model. Plenty of scans do not carry one, so scanvault falls back, in
this order:

| `date_source` | Where the date came from |
| --- | --- |
| `document` | Printed on the document itself. Always preferred. |
| `filename` | Parsed from the file name — `receipt Mar 5, 2017.pdf`, `statement_03_Jul_2025.pdf`, `2016-09-08 letter.pdf`. Only patterns with a four-digit year count, so an account number cannot pose as a date. |
| `pdf-metadata` | The `/CreationDate` the scanner wrote into the PDF. |
| `file-created` | The file's own creation date (modification time where the platform does not record one). |

Every note records which one was used, so a guessed date is never mistaken for a
real one. To review the guesses in Obsidian, search `date_source: "file-created"`.

```toml
[dates]
fallbacks = ["filename", "pdf-metadata", "file-created"]
day_first = true     # 03/04/2024 is the 3rd of April; set false for the US reading
```

Reorder that list to change precedence, or set it to `[]` to leave undated
documents in the `undated` folder rather than guessing.

## How good is the classification, and how would you know

`scanvault eval` scores the classifier against a labelled corpus of 39 documents
— English and Swedish, household and company paperwork — so a change to the
prompt or the model is measurable rather than a matter of opinion:

```bash
scanvault eval --no-llm          # rules and heuristics only
scanvault eval                   # the whole thing, with your model
scanvault eval --model qwen3.8-flash-next:125b-a6b-q4_K_M
```

```
documents      : 39
category        : 85%
  english       : 86%
  swedish       : 83%
  personal      : 81%
  business      : 92%
expected tags   : 100% found

misses:
  hard-en-loan-letter        category Employment != Loans
  ...
```

That 85% is the floor with **no model at all**. Six of the documents are written
specifically to defeat the keyword rules — a letter about "the money you borrowed
for your studies" that never says loan or CSN — and the deterministic layer gets
every one of them wrong. That is the honest split: rules and heuristics handle
the paperwork that announces itself, and the model earns its place on the rest.

Two caveats worth stating. The corpus is invented, so it contains no documents of
yours, and it was written by the same person who tuned the rules, which makes it
a regression test rather than proof of general quality. Point `--corpus` at your
own labelled JSON — same shape, `id`, `language`, `context`, `category`, `tags`,
`text` — and the numbers start being about your documents.

## What the classifier is doing, and why

Three layers, in order of how much they can be trusted:

1. **Keyword rules over the text** — 24 groups, English and Swedish, matched on
   folded text so a document OCR'd without the Swedish language pack
   (`Forfallodatum` rather than `Förfallodatum`) still matches. Swedish
   compounds the identifying word into a longer one, so a keyword ending in `*`
   matches inside a word: `faktur*` catches *faktura*, *fakturanummer* and
   *fakturadatum*; `forsakring*` catches *Försäkringsbrev*, which is the actual
   name of a Swedish insurance policy document.
2. **Facts** — category, year, sender, and what the document is about.
3. **The model** — for everything the first two cannot see.

A rule only overrules the model's category when the model reached for a generic
one, and a keyword found in the body never overrules a real category — an
invoice that quotes an IBAN is still an invoice. Where both fire, what a
document is *about* wins over what *form* it takes: an electricity bill is an
invoice, but `Utilities` is the shelf you would look on.

Measured on the bundled corpus with **no model at all**: 87% categories, 100% of
expected tags. `scanvault eval` reproduces that in a second, and `--model X`
tells you what the model adds on top.

## Tags

A document is only as findable as its tags, so they come from three places and
the first two do not depend on the model getting it right:

* **Rules.** Keyword rules run over the title, the sender, the summary and the
  start of the text, matching on word boundaries so `payee` is not PAYE and
  `risk` is not an ISK account. Twenty-four groups ship by default, in English
  and Swedish:

  | Tag | Some of what it matches |
  | --- | --- |
  | `taxes` | HMRC, Skatteverket, IRS, Finanzamt, self assessment, inkomstdeklaration, moms, P800 |
  | `government` | council tax, Companies House, DVLA, Bolagsverket, Kronofogden, folkbokföring |
  | `student-loan` | CSN, studiemedel, Student Loans Company, tuition fee loan, FAFSA |
  | `pension` | workplace pension, tjänstepension, Pensionsmyndigheten, SIPP, 401(k), annuity |
  | `investments` | portfolio statement, dividend, ISK, fondkonto, stocks and shares ISA |
  | `mortgage` / `loan` | redemption statement, bolån, amorteringskrav / loan agreement, avbetalning |
  | `property` | tenancy, leasehold, service charge, hyresavtal, bostadsrätt, stamp duty |
  | `insurance` | policy number, premium, hemförsäkring, trafikförsäkring, claim reference |
  | `utilities` / `telecoms` | meter reading, fjärrvärme, standing charge / mobilabonnemang, bredband |
  | `banking` | sort code, IBAN, kontoutdrag, autogiro, direct debit |
  | `vehicle` | MOT, V5C, besiktning, fordonsskatt, parkeringsanmärkning |
  | `medical` | NHS, prescription, 1177, vårdcentral, remiss, sjukintyg |
  | `employment` | payslip, P60, anställningsavtal, lönespecifikation, share options |
  | `education` | enrolment, antagningsbesked, examensbevis, transcript of records |
  | `identity` | passport, körkort, residence permit, uppehållstillstånd, personbevis |
  | `legal` | solicitor, fullmakt, testamente, claim form, bouppteckning, arvskifte |
  | `travel` | boarding pass, booking reference, itinerary, resebokning, car hire |
  | `subscription` / `warranty` | membership, medlemskap, abonnemang / guarantee, garanti |
  | `charity`, `pets`, `home-improvement` | gift aid, veterinär, microchip, offert, gas safety |

  The whole table lives in `[tags] rules` and is yours to edit — replace a group,
  add your own, or drop the lot.
* **Facts.** The category, the year (`year-2024`), the sender
  (`example-bank`), and whatever the document is *about* — a property address, a
  vehicle, an account holder — which the model returns as `subjects` and which
  become tags too.
* **The model.** Whatever else it thinks is worth tagging, filling the list up
  to `max_tags`.

So a mortgage statement ends up with something like:

```yaml
tags:
  - scan
  - property
  - year-2024
  - example-bank
  - 12-example-street
  - mortgage
  - statement
subjects:
  - "12 Example Street"
```

which means searching `mortgage` finds every mortgage document, searching
`12-example-street` finds everything about that property, and `taxes year-2023`
finds a year's tax paperwork regardless of who sent it.

```toml
[tags]
year_tag = true
correspondent_tag = true
subject_tags = true
max_tags = 12
review_below = 0.5   # below this confidence the note is tagged needs-review
# `extra_rules` adds to the built-in table; `rules` replaces it outright.
extra_rules = { boat = ["mooring", "marina", "hamnavgift"] }
```

When the model reaches for a generic category — `Correspondence`, `Personal`,
`Other` — but a keyword rule knows better, the rule wins: a bank's *Mortgage
Charges Tariff* is filed under `Loans`, not filed as correspondence. A keyword
found only in the body never overrules a real category, so an invoice that
quotes an IBAN is still an invoice. `[tags] rules_set_category = false` turns
that off, and `tag_categories` is the mapping.

Anything the model was unsure about, or that never reached the model at all, is
tagged `needs-review` — so the weak results are one search away rather than
something you find by accident months later.

Categories are a separate list — `Invoices`, `Receipts`, `Contracts`, `Banking`,
`Investments`, `Pensions`, `Loans`, `Taxes`, `Insurance`, `Medical`,
`Government`, `Identity`, `Legal`, `Employment`, `Education`, `Property`,
`Vehicle`, `Utilities`, `Travel`, `Subscriptions`, `Correspondence`, `Manuals`,
`Personal`, `Other` — and they decide the folder a document lands in, so keep
that list short enough to stay meaningful and set `categories` in the config if
these are not your filing cabinet.

Existing notes pick this up with `organize --reclassify --apply`.

## Photos and other image formats

A phone photo of a receipt, a `.png` from a scanning app, a `.tiff` from a flatbed:
`ingest` and `organize` treat all of them as documents. Each one is OCR'd into a
searchable PDF, and that PDF is what gets filed, classified and named — exactly
as if it had arrived as a PDF:

```
4 Archive/Banking/2021/2021-02-07 Example Bank - Annual statement.md
4 Archive/_attachments/Banking/2021/2021-02-07 Example Bank - Annual statement.pdf
4 Archive/_attachments/Banking/2021/2021-02-07 Example Bank - Annual statement.jpg
```

The photo it came from is kept next to the PDF and recorded as `original:` in the
note, because a conversion is not a replacement. Set `keep_original_image = false`
under `[vault]` if you would rather only keep the PDF.

Recognised: `.jpg`, `.jpeg`, `.png`, `.tif`, `.tiff`, `.bmp`, `.webp`, `.heic`,
`.heif`. The first five go straight through OCR; HEIC and WEBP are converted
first with ImageMagick (or `heif-convert`, or `sips` on macOS), and `scanvault
doctor` tells you whether you have one of those. An image carries no page size,
so `image_dpi` under `[ocr]` says what resolution to assume — 300 by default.

## Naming a document

A scan arrives called `SwiftScan Feb 7, 2021 11.45 AM.pdf` or `Scan 10.pdf`.
Neither is a name, so neither is kept: the note *and the PDF* are named from what
the document turned out to be.

```
4 Archive/Banking/2021/2021-02-07 Example Bank - Annual statement.md
4 Archive/_attachments/Banking/2021/2021-02-07 Example Bank - Annual statement.pdf
```

That is `{date} {name}`, where `{name}` is the correspondent and the title
together — `Example Bank - Annual statement` — collapsing to just the title when
there is no correspondent, and never repeating a correspondent that is already
part of the title.

The original filename is used only as a last resort, and only after the scanner
noise is stripped out of it: app names, timestamps, `- 1` duplicate markers and
`Scanned Documents`-style placeholders all go, and what remains has to contain
actual words. `Boiler service 2019-04-02 - 1.pdf` becomes `Boiler service`;
`Scan 10.pdf` becomes nothing, and the note is `Untitled document` until the
model or a re-run gives it a better one. Each note records where its title came
from in `title_source` (`model`, `text` or `filename`), so
`title_source: "filename"` finds the ones worth a second look.

Renaming an existing vault is `organize --apply`: it will show every document
whose filename does not match its metadata as a `relocate`.

## Vault layout (PARA)

The four folders are the PARA framework from *Building a Second Brain*:

| Folder | What belongs there |
| --- | --- |
| `1 Projects` | Short-term efforts with a goal and a finish line |
| `2 Areas` | Ongoing responsibilities you maintain over time |
| `3 Resources` | Topics and reference material you are not actively working |
| `4 Archive` | Everything inactive — **and the default home for every scan** |

`scanvault init-vault --vault ~/Obsidian/Archive --apply` creates the four folders
with a short index note in each; `ingest --apply` also creates them on first run.
Both are idempotent and never overwrite an existing note.

Documents move between buckets in two ways, and the organizer honours both:

* **Frontmatter.** Set `para: project` (or `area`, `resource`, `archive`) in a
  note and the next `organize --apply` moves the note *and its PDF* into that
  folder.
* **Location.** Drag a note into `1 Projects/` in Obsidian and leave the
  frontmatter alone — the organizer reads the bucket from where the note now
  lives instead of dragging it back to the archive.

Notes that scanvault did not write are left alone entirely: your own project and
area notes are never moved, even though they live in the same vault. Pass
`--include-unmanaged` to `organize` if you *do* want hand-made notes filed by the
same rules.

### Upgrading from 0.2

`ingest`, `watch`, `ocr`, `init-vault` and `init-config` used to act immediately;
`organize` was the only command that waited for `--apply`. They all wait now, so
add `--apply` to any script or service unit that expects work to happen. Nothing
else changed, and `--dry-run` still means what it always did.

### Migrating a vault from 0.1

0.1 filed everything under `Documents/` and `Attachments/`. To move an existing
vault into the PARA layout:

```bash
scanvault organize --vault ~/Obsidian/Archive          # preview, shows every move
scanvault organize --vault ~/Obsidian/Archive --apply
```

Notes and attachments move together, links are rewritten, and emptied folders are
pruned.

## Configuration

`scanvault init-config -o ~/Obsidian/Archive/.scanvault/scanvault.toml --apply`
writes a starting point. A config is picked up automatically from
`<vault>/.scanvault/scanvault.toml`, `<vault>/scanvault.toml` or `./scanvault.toml`;
CLI flags win over the file.

```toml
source_dir = "~/Scans/inbox"
vault_dir  = "~/Obsidian/Archive"
language_hint = "auto"             # "auto" keeps each document's own language
# categories = [...]               # the classifier may only choose from this list

[ocr]
backend = "auto"                   # auto | ocrmypdf | tesseract | none
languages = "swe+eng"
image_dpi = 300                    # assumed resolution for bare images
min_text_chars = 180               # a new scan with less text than this is OCR'd
searchable_min_chars = 10          # a vault PDF with less text than this has no text layer
force = false                      # re-OCR even when a text layer exists

[llm]
host = "http://localhost:11434"   # any ollama endpoint; the default keeps it all local
model = "qwen3.5:9b"
num_ctx = 8192
fallback_to_heuristics = true      # keep filing when ollama is down

[dates]
# Used only when the document's own text carries no date.
fallbacks = ["filename", "pdf-metadata", "file-created"]

[vault]
notes_dir = ""                     # the PARA folders live at the vault root
attachments_dir = ""
note_path_template = "{para}/{category}/{year}/{date} {name}"
attachment_path_template = "{para}/_attachments/{category}/{year}/{date} {name}"
source_action = "move"             # move | copy | leave
keep_original_image = true         # keep the photo an image document came from
include_text = true
tag_source_folder = true           # turn the folder a document came from into tags

[vault.para]
projects_dir = "1 Projects"
areas_dir = "2 Areas"
resources_dir = "3 Resources"
archive_dir = "4 Archive"
default_bucket = "archive"         # where a new scan goes
```

Template placeholders: `{para} {category} {year} {month} {date} {name} {title} {slug} {correspondent}`,
where `{para}` is the folder for the note's bucket. Drop `{para}` from the
templates for a flat, non-PARA vault.
Undated documents get `undated` for `{date}`/`{year}`, so nothing is silently
misfiled. Filenames are sanitised, and a name collision appends `-2`, `-3`, …

Change a template and `scanvault organize --apply` will move existing documents
to match it.

### Degraded modes

If ollama is unreachable (or you pass `--no-llm`), documents are still filed
using keyword heuristics and marked `classifier: "heuristic"`. `organize` treats
those notes as incomplete, so re-running it once ollama is back reclassifies them
properly. Set `fallback_to_heuristics = false` to fail loudly instead.

If no OCR backend is installed, PDFs that already have a text layer are still
processed; image-only ones are reported as failures and left in the source folder.
`organize` still lists the attachments that need OCR, with
`(no OCR backend installed)` in the reason, so you can see the backlog before
installing anything.

## Scanner integration

Point the printer's scan-to-folder (SMB/FTP) at `~/Scans/inbox` and run the
watcher as a user service:

```ini
# ~/.config/systemd/user/scanvault.service
[Unit]
Description=scanvault watcher
After=network-online.target

[Service]
ExecStart=%h/.local/bin/scanvault watch --apply --config %h/Obsidian/Archive/.scanvault/scanvault.toml
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now scanvault.service
```

The watcher only picks up files whose size has stopped changing, so half-written
scans are left alone until the printer finishes.

## Tests

```bash
uv run python -m unittest discover -s tests -t .
```

CI runs the same command on every push and pull request across Python 3.11-3.13,
plus one job with the `fast` extra, on a runner that has `ocrmypdf`, `tesseract`
and `poppler-utils` installed — so the OCR tests really execute there instead of
skipping.

The suite is stdlib-only. It builds real PDFs on the fly and stubs the ollama
client, so it needs neither a model nor an OCR engine; tests that need a PDF text
extractor skip themselves if neither `pypdf` nor `pdftotext` is present.

## Troubleshooting

**A wall of `WARNING Ignoring wrong pointing object` / `fontTools is required to
fully parse ...`.** That is pypdf commenting on the internals of scanned PDFs,
which are frequently malformed in ways that do not matter. Those library
warnings are silenced by default; `-vv` brings them back when you want them.
Installing the `fast` extra also pulls in fontTools, which removes the font
warnings at the source and improves text extraction from PDFs that use CFF Type 1
fonts.

**`WARNING pypdf failed on X.pdf (cryptography>=3.1 is required for AES
algorithm)`.** Some PDFs are encrypted with an empty password - banks do this
constantly - and pypdf needs `cryptography` to read them. The `fast` extra
installs it. Without it nothing is lost: poppler's `pdftotext` reads those files
and the text still comes through.

**A PDF that genuinely has a password.** `organize` reports it rather than
pretending it can be filed:

```
[dry-run] adopt: Scanned/Locked statement.pdf (password-protected PDF; neither text
extraction nor OCR can read it until the password is removed (qpdf --decrypt))
```

Remove the password and re-run:

```bash
qpdf --decrypt --password=yourpassword "Locked statement.pdf" decrypted.pdf
```

**`classification fell back to heuristics: model did not return JSON: ''`.** The
model answered with nothing. Usually one of two things: it is a reasoning build
that spent the whole response thinking, or the ollama version does not honour a
JSON-schema `format`. scanvault asks for thinking to be switched off, reads the
`thinking` field when the content is empty, and retries once in plain JSON mode
before giving up — and when it does give up it names the model instead of
silently degrading. `scanvault doctor` now asks the model for one JSON object,
so this shows up in a two-second check rather than halfway through a long run.

**`organize` looks like it is hanging on a big vault.** It should not any more:
planning logs `scanning N notes`, then `[i/N] classifying <note>` for every note
it sends to the model, then `scanned N PDFs...` every 50 files while it looks for
unfiled PDFs. Applying logs `[i/N] <action> <file>`. Add `-q` if you would rather
have silence.

## Notes and limits

* `organize` moves notes; wikilinks *from other notes* to a moved note are not
  rewritten. Run it before you start cross-linking, or keep links to the
  attachment path.
* The model tag defaults to `qwen3.5:9b`. Use `--model` (or `[llm] model`) for
  any other ollama tag or a local Modelfile build.
* Classification quality depends on OCR quality. `scanvault ocr --text-out --apply` is
  the quickest way to see what the model actually gets.
* Everything runs locally: no document text leaves the machine.
