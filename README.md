# obsidian-document-organizer

[![CI](https://github.com/vpetersson/obsidian-document-organizer/actions/workflows/ci.yml/badge.svg)](https://github.com/vpetersson/obsidian-document-organizer/actions/workflows/ci.yml)

Turn a scanner's output folder into an organized Obsidian vault. The package and
CLI are called `scanvault`.

* **Phase 1 — OCR.** Every incoming PDF is checked for a text layer; image-only
  scans are run through OCR and re-saved as a searchable PDF.
* **Phase 2 — Organize.** The text is sent to a local [ollama](https://ollama.com)
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
category: "Invoices"
correspondent: "Acme Ltd"
tags:
  - scan
  - invoices
  - acme
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

## Extracted text

```text
ACME LTD
INVOICE 2024-05-02
…
```
```

The extracted text is embedded so Obsidian's own search finds documents by their
contents; turn it off with `include_text = false`.

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
```

Reorder that list to change precedence, or set it to `[]` to leave undated
documents in the `undated` folder rather than guessing.

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
language_hint = "English"          # language for generated titles/summaries
# categories = [...]               # the classifier may only choose from this list

[ocr]
backend = "auto"                   # auto | ocrmypdf | tesseract | none
languages = "eng+swe"
min_text_chars = 180               # a new scan with less text than this is OCR'd
searchable_min_chars = 10          # a vault PDF with less text than this has no text layer
force = false                      # re-OCR even when a text layer exists

[llm]
host = "http://localhost:11434"
model = "qwen3.5:9b"
num_ctx = 8192
fallback_to_heuristics = true      # keep filing when ollama is down

[dates]
# Used only when the document's own text carries no date.
fallbacks = ["filename", "pdf-metadata", "file-created"]

[vault]
notes_dir = ""                     # the PARA folders live at the vault root
attachments_dir = ""
note_path_template = "{para}/{category}/{year}/{date} {title}"
attachment_path_template = "{para}/_attachments/{category}/{year}/{date} {title}"
source_action = "move"             # move | copy | leave
include_text = true
tag_source_folder = true           # turn the folder a document came from into tags

[vault.para]
projects_dir = "1 Projects"
areas_dir = "2 Areas"
resources_dir = "3 Resources"
archive_dir = "4 Archive"
default_bucket = "archive"         # where a new scan goes
```

Template placeholders: `{para} {category} {year} {month} {date} {title} {slug} {correspondent}`,
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

**`organize` looks like it is hanging on a big vault.** Planning hashes and
probes every PDF that no note points at. It logs `scanned N PDFs...` every 50
files, so you can tell it is working.

## Notes and limits

* `organize` moves notes; wikilinks *from other notes* to a moved note are not
  rewritten. Run it before you start cross-linking, or keep links to the
  attachment path.
* The model tag defaults to `qwen3.5:9b`. Use `--model` (or `[llm] model`) for
  any other ollama tag or a local Modelfile build.
* Classification quality depends on OCR quality. `scanvault ocr --text-out --apply` is
  the quickest way to see what the model actually gets.
* Everything runs locally: no document text leaves the machine.
