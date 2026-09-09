# obsidian-document-organizer

Turn a scanner's output folder into an organized Obsidian vault. The package and
CLI are called `scanvault`.

* **Phase 1 — OCR.** Every incoming PDF is checked for a text layer; image-only
  scans are run through OCR and re-saved as a searchable PDF.
* **Phase 2 — Organize.** The text is sent to a local [ollama](https://ollama.com)
  model (default `qwen3.5:9b`), which returns a title, category, date,
  correspondent, tags and a summary. scanvault writes an Obsidian note with that
  metadata as YAML frontmatter and files both the note and the PDF into a dated
  folder structure.
* **Organizer.** The same classification can be re-run over an *existing* vault:
  it adopts loose PDFs and re-files notes whose metadata is missing or whose
  location no longer matches the configured layout.

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
uv sync --extra fast        # optional: adds pypdf for faster text extraction
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

```bash
# create the PARA folders (optional; ingest does it too)
scanvault init-vault --vault ~/Obsidian/Archive

# see what would happen, change nothing
scanvault ingest --source ~/Scans/inbox --vault ~/Obsidian/Archive --dry-run

# do it (originals are moved into the vault; --keep-source copies instead)
scanvault ingest --source ~/Scans/inbox --vault ~/Obsidian/Archive

# keep watching the scanner folder
scanvault watch --source ~/Scans/inbox --vault ~/Obsidian/Archive --interval 30

# phase 1 only: OCR into searchable PDFs and dump the text
scanvault ocr ~/Scans/inbox --out ~/Scans/ocr --text-out ~/Scans/text

# existing vault: dry run, then apply
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

## Vault layout (PARA)

The four folders are the PARA framework from *Building a Second Brain*:

| Folder | What belongs there |
| --- | --- |
| `1 Projects` | Short-term efforts with a goal and a finish line |
| `2 Areas` | Ongoing responsibilities you maintain over time |
| `3 Resources` | Topics and reference material you are not actively working |
| `4 Archive` | Everything inactive — **and the default home for every scan** |

`scanvault init-vault --vault ~/Obsidian/Archive` creates the four folders with a
short index note in each; `ingest` also creates them on first run. Both are
idempotent and never overwrite an existing note.

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

### Migrating a vault from 0.1

0.1 filed everything under `Documents/` and `Attachments/`. To move an existing
vault into the PARA layout:

```bash
scanvault organize --vault ~/Obsidian/Archive          # dry run, shows every move
scanvault organize --vault ~/Obsidian/Archive --apply
```

Notes and attachments move together, links are rewritten, and emptied folders are
pruned.

## Configuration

`scanvault init-config -o ~/Obsidian/Archive/.scanvault/scanvault.toml` writes a
starting point. A config is picked up automatically from
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
min_text_chars = 180               # fewer characters than this ⇒ treat as image-only
force = false                      # re-OCR even when a text layer exists

[llm]
host = "http://localhost:11434"
model = "qwen3.5:9b"
num_ctx = 8192
fallback_to_heuristics = true      # keep filing when ollama is down

[vault]
notes_dir = ""                     # the PARA folders live at the vault root
attachments_dir = ""
note_path_template = "{para}/{category}/{year}/{date} {title}"
attachment_path_template = "{para}/_attachments/{category}/{year}/{date} {title}"
source_action = "move"             # move | copy | leave
include_text = true

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

## Scanner integration

Point the printer's scan-to-folder (SMB/FTP) at `~/Scans/inbox` and run the
watcher as a user service:

```ini
# ~/.config/systemd/user/scanvault.service
[Unit]
Description=scanvault watcher
After=network-online.target

[Service]
ExecStart=%h/.local/bin/scanvault watch --config %h/Obsidian/Archive/.scanvault/scanvault.toml
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

The suite is stdlib-only. It builds real PDFs on the fly and stubs the ollama
client, so it needs neither a model nor an OCR engine; tests that need a PDF text
extractor skip themselves if neither `pypdf` nor `pdftotext` is present.

## Notes and limits

* `organize` moves notes; wikilinks *from other notes* to a moved note are not
  rewritten. Run it before you start cross-linking, or keep links to the
  attachment path.
* The model tag defaults to `qwen3.5:9b`. Use `--model` (or `[llm] model`) for
  any other ollama tag or a local Modelfile build.
* Classification quality depends on OCR quality. `scanvault ocr --text-out` is
  the quickest way to see what the model actually gets.
* Everything runs locally: no document text leaves the machine.
