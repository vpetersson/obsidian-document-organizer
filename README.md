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

Documents are filed by category and year under one folder. If your vault is a
PARA ("Second Brain") vault, `layout = "para"` puts them in `4 Archive` inside
it instead. See [Vault layout](#vault-layout).

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
| `<vault>/.scanvault/tags.json` | The tags in use, so a new document reuses them instead of inventing near-copies |
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

### What `--source` looks at

Every subfolder, unless you pass `--no-recursive` — **including folders that are
symlinks**, because a scanner folder is very often a link to where the documents
actually live: an alias into iCloud Drive, a network share, an external disk. A
link pointing back at its own parent is walked once rather than forever.

Two things are skipped, and both say so rather than looking like an empty
folder:

* a folder scanvault is not allowed to read — "nothing to do" and "not allowed
  to look" should not print the same thing;
* a file that is in iCloud but has not been downloaded to this machine. macOS
  leaves a hidden `.name.pdf.icloud` placeholder in its place and there are no
  bytes to read, so scanvault names them and moves on.

Hidden folders (anything starting with `.`) are left alone, as is scanvault's
own `.ocr.pdf` working output.

## Commands

| Command | What it does |
| --- | --- |
| `ingest` | file new scans from a source folder into the vault |
| `watch` | the same, polling the source folder |
| `organize` | OCR, reclassify and re-file documents already in the vault |
| `ocr` | phase 1 only: turn scans into searchable PDFs and dump their text |
| `init-vault` | create the folder documents are filed into, and the CSS snippet |
| `init-config` | write a starting `scanvault.toml` |
| `doctor` | check the OCR toolchain, the model, and whether requests really run in parallel |
| `tags` | the vault's tag vocabulary, and the near-duplicates in it |
| `eval` | score the classifier against a labelled corpus |

Every command that writes takes `--apply` and `--dry-run`; `-v`/`--verbose` and
`-q`/`--quiet` work before or after the subcommand. Beyond that:

| Flag | On | Meaning |
| --- | --- | --- |
| `--source`, `--vault` | ingest, watch | folders to read from and write to |
| `--workers N` | ingest, watch, organize, eval | documents in flight at once |
| `--model`, `--ollama-host` | all of the above | which model, and where it runs |
| `--no-llm` | all of the above | skip the model, use rules and heuristics |
| `--lang eng+swe` | ingest, watch, ocr | OCR languages, main one first |
| `--force-ocr` | ingest, watch, ocr | OCR even when a text layer exists |
| `--keep-source` | ingest, watch | copy the original instead of moving it |
| `--no-recursive` | ingest | do not descend into subfolders |
| `--no-state` | ingest | ignore the dedupe index |
| `--no-cache` | ingest, organize | ask the model again instead of reusing answers |
| `--reclassify` | organize | re-run the model over every note |
| `--no-adopt` | organize | ignore PDFs no note points at |
| `--no-ocr` | organize | skip the OCR pass |
| `--include-unmanaged` | organize | also file notes scanvault did not write |
| `--out`, `--text-out` | ocr | where to put searchable PDFs and extracted text |
| `--interval`, `--iterations` | watch | how often to poll, and how many times |
| `--duplicates`, `--min-count` | tags | only tags spelled more than one way, and how rare to show |
| `--corpus` | eval | a labelled corpus of your own |
| `--output`, `--force` | init-config | where to write, and overwrite if it exists |

## What lands in the vault

```
your-vault/
├── Archive/
│   ├── Archive.md                                                    <- what this folder is
│   ├── Invoices/2024/2024-05-02 Acme Ltd - Invoice INV-1234.md
│   └── _attachments/
│       └── Invoices/2024/2024-05-02 Acme Ltd - Invoice INV-1234.pdf
├── .obsidian/
│   └── snippets/scanvault.css   <- folds the properties panel away
└── .scanvault/
    ├── index.json              <- SHA-256 of everything filed, so re-runs skip it
    ├── classifications.json    <- the model's answers, so a preview is not paid for twice
    ├── tags.json               <- the tag vocabulary, so it stays one vocabulary
    └── work/                   <- temporary OCR output
```

```markdown
---
title: "Invoice INV-1234"
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
  - invoice
document_type: "commercial invoice"
context: "business"
subjects:
  - Account 4242
reference: "INV-1234"
amount: "120.00"
currency: "EUR"
language: "English"
confidence: 0.95
para: "archive"
classifier: "llm"
scanvault_version: "0.22.0"
processed: "2026-09-10T09:02:23Z"
source_file: "scan_001.pdf"
source_hash: "9f2c…"
ocr: "ocrmypdf"
pages: 2
attachment: "Archive/_attachments/Invoices/2024/2024-05-02 Acme Ltd - Invoice INV-1234.pdf"
cssclasses:
  - scanvault
---

# Invoice INV-1234

Invoice INV-1234 from Acme Ltd for 120.00 EUR, due within 30 days.

![[Archive/_attachments/Invoices/2024/2024-05-02 Acme Ltd - Invoice INV-1234.pdf]]

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

### Folding the properties away

The frontmatter above is for machines: which classifier ran, where the date came
from, the hash that stops the document being filed twice. Obsidian renders all
of it as a Properties panel above every note, so a scanned letter opens on a
screen of bookkeeping before the letter.

Obsidian's own setting for this — Settings → Editor → *Properties in document* →
`Hidden` — is global, and hides them in your hand-written notes too. So each
note scanvault writes carries a `cssclasses` value instead, and `init-vault`
writes a snippet that folds the panel shut on those notes only:

```
<vault>/.obsidian/snippets/scanvault.css
```

Turn it on once, in Settings → Appearance → *CSS snippets*. The "Properties"
header stays where it is; hovering or focusing it brings the rest back. The file
is yours from the moment it exists — scanvault never overwrites it — and it
carries a commented alternative that hides the panel outright rather than
folding it. The selectors describe Obsidian's own markup, which Obsidian is free
to change, so treat it as a starting point rather than something scanvault keeps
in step.

`scanvault doctor` reports which of the three states you are in, because a
snippet that was written but never switched on looks exactly like CSS that does
not work:

```
properties  : folded away by scanvault.css
properties  : scanvault.css written but NOT enabled - Settings -> Appearance -> CSS snippets
properties  : shown in full (run init-vault --apply to write the CSS snippet)
```

Set `cssclasses = []` under `[vault]` to write no class and no snippet, or name
your own classes there if you would rather style the notes yourself. Existing
notes pick the class up on the next `organize --apply`, reported as
`rewrite: … (missing cssclasses)` so you see the count before it happens; a
class you added by hand is kept alongside it.

## Organizing an existing vault

`scanvault organize` is phase 1 *and* phase 2 applied to documents that are
already in the vault. Like every other writing command it previews by default:
it prints one line per document and changes nothing until you add `--apply`.

```bash
scanvault organize --vault ~/Obsidian/Archive
```

```
[dry-run] ocr: Archive/Invoices/2024/2024-05-02 Acme Invoice.md (attachment has no text layer)
[dry-run] relocate: Archive/Unsorted/old.md -> Archive/Contracts/2022/2022-02-02 Old Note.md (layout drift)
[dry-run] adopt: Work receipts/scan-2017-03-05.pdf (image-only PDF; will OCR, classify and file under "Archive")
[dry-run] duplicate: Work receipts/scan-2017-03-05 - 1.pdf (same content as Archive/Receipts/2017/2017-03-05 Coffee House.md)

1 to OCR, 1 to relocate, 0 to rewrite, 1 to adopt (1 of them need OCR), 12 already filed, 1 duplicates, 3 left alone, 0 failed
3 notes were left alone because they were not written by scanvault; pass --include-unmanaged to file those too, or -v to list them.
Nothing was changed. Re-run with --apply to execute.
```

Only the lines that mean something get printed: notes that are already filed,
left alone or part of the PARA scaffolding are counted in the summary but not
listed, because on a real vault those are hundreds of lines that bury the ones
that matter. `-v` lists everything, and works before or after the subcommand.

What each action means:

| Action | What `--apply` does |
| --- | --- |
| `ocr` | Nothing the note points at can be read. Runs OCR, **replaces a PDF attachment with the searchable version**, refreshes the note's extracted text and records the backend in `ocr:`. If the note's metadata was thin, it is classified from the fresh text and refiled. |
| `relocate` | Moves the note and its PDF to where the templates say they belong. |
| `rewrite` | Keeps the location, refreshes frontmatter from a new classification, folds a tag onto the vault's spelling of it, or adds a `cssclasses` value the note predates. Anything you wrote in the note — prose, embeds — is carried across. |
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

### Notes that embed their scans

An Obsidian note written by hand holds its pages as embeds rather than an
`attachment:` key:

```markdown
# Bank letter

![[Scan Page 196.jpg]]
![[Scan Page 197.jpg]]
```

Those embeds are treated as the note's documents. They are OCR'd — every page,
not just the first — and the text goes into the note, so a note that was two
pictures becomes searchable. `![[...]]` is markup naming a file, never a word of
the document: it is not read as text, never becomes a title or a filename, and
is never quoted back into the extracted-text block, where it would neither
render nor link.

The images themselves are **left where they are**. The note links to them by
name and replacing a `.jpg` with a PDF would break the link you wrote. When the
note moves, its embeds are rewritten as vault-relative links so they keep
working from the new location. Bare names are resolved the way Obsidian resolves
them — beside the note first, then anywhere in the vault — and a name that
matches two files is left alone rather than guessed at.

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
date, the statement date, the date at the top of a letter. The model reports it,
and scanvault reads the text itself as well, because a date the model missed is
still printed on the page. Only if the document truly carries no date does it
fall back to the file:

| `date_source` | Where the date came from |
| --- | --- |
| `document` | The model read it off the document. Always preferred. |
| `text` | scanvault found it in the OCR text — see below. |
| `filename` | Parsed from the file name — `receipt Mar 5, 2017.pdf`, `statement_03_Jul_2025.pdf`, `2016-09-08 letter.pdf`. Only patterns with a four-digit year count, so an account number cannot pose as a date. |
| `pdf-metadata` | The `/CreationDate` the scanner wrote into the PDF. |
| `file-created` | The file's oldest timestamp. Copying a document into a vault resets its creation time to now while carrying its modification time across, so the older of the two is used — otherwise every adopted document is dated the day you ran the organizer. |

Every note records which one was used, so a guessed date is never mistaken for a
real one. To review the guesses in Obsidian, search `date_source: "file-created"`.

### Which date on the page

A page has several dates on it and only one of them dates the document, so the
reader does not take the first thing that looks like a date. It finds all of
them, then reads the words in front of each:

* **A labelled date wins.** `Invoice date`, `Fakturadatum`, `Statement date`,
  `Date of issue`, `Utfärdat`, `Kvittodatum` and their neighbours mean "this is
  the document's date", and beat an unlabelled number elsewhere on the page.
* **A due date is never it.** `Due`, `Payment due`, `Förfallodatum`, `Sista
  betalningsdag`, `Valid until`, `Gäller från`, `Date of birth`, `Period` —
  a date introduced by any of those is discarded rather than ranked lower. An
  invoice is dated the day it was written, not the day it must be paid.
* **A range is not a date.** `1 Jan 2024 - 31 Dec 2024` is a coverage period,
  and both ends are dropped.
* **Otherwise, position decides**, because a letterhead is at the top and the
  copyright line is at the bottom.

Both languages are read together — `2 maj 2024`, `2 May 2024`, `May 2, 2024`,
`2024-05-02`, `02/05/2024`, `2nd May 2024` and `March 2024` all parse — and
Swedish is matched with the diacritics folded away, so a page OCR'd without the
`swe` language pack still matches `Forfallodatum`.

```toml
[dates]
fallbacks = ["text", "filename", "pdf-metadata", "file-created"]
day_first = true     # 03/04/2024 is the 3rd of April; set false for the US reading
```

Reorder that list to change precedence, drop `"text"` to leave dating to the
model alone, or set it to `[]` to leave undated documents in the `undated`
folder rather than guessing.

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
category        : 87%
  english       : 86%
  swedish       : 89%
  personal      : 85%
  business      : 92%
expected tags   : 100% found
date            : 100%

misses:
  hard-en-loan-letter        category Employment != Loans
  ...
```

That 87% is the floor with **no model at all**. Six of the documents are written
specifically to defeat the keyword rules — a letter about "the money you borrowed
for your studies" that never says loan or CSN — and the deterministic layer gets
every one of them wrong. That is the honest split: rules and heuristics handle
the paperwork that announces itself, and the model earns its place on the rest.

Two caveats worth stating. The corpus is invented, so it contains no documents of
yours, and it was written by the same person who tuned the rules, which makes it
a regression test rather than proof of general quality. Point `--corpus` at your
own labelled JSON — same shape, `id`, `language`, `context`, `category`, `tags`,
an optional `date` (`null` where the document carries none), `text` — and the
numbers start being about your documents.

The `date` line scores the date reader on the same corpus, and counts reading
*no* date off a document that carries none as correct — inventing one from a
due date is the failure this is here to catch.

## Speed

Classification dominates a big run: OCR is seconds, the model is seconds *per
document*, and a vault has hundreds. Each document goes through the whole
pipeline on its own — read, OCR, classify, written — without waiting for the
others, so the first note appears seconds after you start rather than after the
last document has been classified, and a run that dies half way has half its
work on disk.

You can see it happening, and check it: each document is reported as it is
filed rather than in a batch at the end, each line carries how long that
document took, and the run ends with what actually overlapped.

```
INFO [5/22] classifying BRW…_000135.pdf
INFO [2/22] done BRW…_000130.pdf in 6.4s
filed: BRW…_000130.pdf -> Archive/Government/2026/2026-09-10 Electoral registration.md

INFO 22 documents in 41s (7.1s each, 3.8 at a time with 4 workers)
```

"3.8 at a time with 4 workers" is the number that matters. If it says 1.0, the
requests are being queued rather than run — almost always ollama, which
serialises anything past `OLLAMA_NUM_PARALLEL`. `scanvault doctor` measures that
directly: it times one request against several and tells you which it is.

The write step stays on one thread. Unique filenames, the dedupe index and the
cache file are shared state, and writing a note is milliseconds against seconds
of model time, so serialising it costs nothing measurable and removes a whole
category of race.

```bash
scanvault organize --vault ~/Obsidian/Archive --workers 8 --apply
```

Four at a time by default. The ceiling is not this tool but the model server:
ollama serialises requests beyond `OLLAMA_NUM_PARALLEL`, so raising `--workers`
past that just queues.

```bash
OLLAMA_NUM_PARALLEL=8 ollama serve
```

Worth knowing before you turn it up: the workers also run OCR, and `ocrmypdf` is
itself multi-threaded, so on a laptop 4 workers each running OCR can be slower
than 2. `[ocr] jobs` caps what each OCR pass uses. `--workers 1` restores the
old strictly-sequential behaviour, and `scanvault doctor` prints what you are
set to.

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

### The tag ledger

That only works if there is *one* spelling of each tag. Left to itself, a
classifier tags every document from scratch and the vocabulary drifts —
`mortgage` on forty documents and `mortgages` on one, `council-tax` and
`counciltax`, `skatteverket` and `skatteverkets`. Each is a reasonable answer on
its own, and the set of them is useless: searching `mortgage` finds four fifths
of your mortgage paperwork.

So scanvault keeps a ledger of the tags the vault actually uses, and matches
against it before minting a new one. Seeded from your notes at the start of
every run — including notes scanvault did not write, because fighting your own
tags with near-copies of them is the problem rather than the solution — and
cached in `<vault>/.scanvault/tags.json`.

Matching is two steps, cheap first:

* a **fingerprint** — diacritics folded, words singularised and sorted — so
  `invoices` and `invoice`, `Council Tax` and `council-tax`, `tax-council` and
  `council-tax` are the same tag before any similarity is computed;
* failing that, **string similarity** against the tags already in use, which
  catches the rest: `counciltax` → `council-tax`, `skatteverkets` →
  `skatteverket`.

The spelling that wins is the one most documents already use, so the ledger
follows your vault rather than the other way round — except for the tags in
`[tags] rules` and your category list, which keep their spelling however rare
they are. That vocabulary is the fixed part.

**What is never merged** matters more than what is:

* two tags whose digits differ, so `year-2023` and `year-2024` — 89% similar and
  completely unrelated — stay apart, as do `invoice-2024-0188` and its siblings;
* anything below `merge_cutoff` (0.88), which keeps `mortgage` apart from
  `mortgage-statement`, `medical` from `medicine`, `hsbc` from `hsbc-bank-plc`
  and `banking` from `bank`. Set it to `1.0` to turn similarity matching off
  and keep only the fingerprint.

`scanvault tags` prints the vocabulary, and `--duplicates` prints just the mess:

```console
$ scanvault tags --vault ~/Obsidian/Archive
   40  mortgage       <- mortgages
   11  council-tax    <- counciltax, council-taxes
    9  year-2024
    ...

$ scanvault tags --vault ~/Obsidian/Archive --duplicates
2 tag(s) spelled more than one way:
  mortgage                       <- mortgages
  council-tax                    <- counciltax, council-taxes

organize --apply rewrites the notes that use the other spellings.
```

An existing vault is consolidated by `organize`, which reports each note whose
tags are off the ledger as `rewrite: … (tags not in the ledger: mortgages ->
mortgage)` and leaves the rest alone.

```toml
[tags]
year_tag = true
correspondent_tag = true
subject_tags = true
max_tags = 12
merge_cutoff = 0.88                # how alike two tags must be to be one tag
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
Archive/Banking/2021/2021-02-07 Example Bank - Annual statement.md
Archive/_attachments/Banking/2021/2021-02-07 Example Bank - Annual statement.pdf
Archive/_attachments/Banking/2021/2021-02-07 Example Bank - Annual statement.jpg
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
Archive/Banking/2021/2021-02-07 Example Bank - Annual statement.md
Archive/_attachments/Banking/2021/2021-02-07 Example Bank - Annual statement.pdf
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

## Vault layout

Everything lands in one folder, by category and year:

```
Archive/
├── Invoices/2024/2024-05-02 Acme Ltd - Invoice INV-1234.md
└── _attachments/Invoices/2024/2024-05-02 Acme Ltd - Invoice INV-1234.pdf
```

`[vault] documents_dir` names that folder — `Archive` by default, `""` to file
straight into the vault root — and the path templates are yours to change.

### If your vault is a Second Brain

Scanned paper is reference material: it is *all* archive, which is why PARA's
other three folders sat empty. Set `layout = "para"` if your vault is organised
that way and you want documents to live inside it:

```toml
[vault]
layout = "para"      # 1 Projects, 2 Areas, 3 Resources, 4 Archive
```

Then documents still land in `4 Archive`, and move between buckets in two ways
the organizer honours: set `para: project` in a note's frontmatter, or drag the
note into `1 Projects/` and leave the frontmatter alone — the bucket is read from
where the note now lives rather than dragged back.

Either way, notes scanvault did not write are left alone: your own project and
area notes are never moved. `--include-unmanaged` opts them in.

### Moving an existing vault between layouts

`organize --apply`. Switching from PARA to flat reports every document as
`relocate: Archive/… -> Archive/…`; notes and PDFs move together and links are
rewritten.

### Upgrading from 0.2

`ingest`, `watch`, `ocr`, `init-vault` and `init-config` used to act immediately;
`organize` was the only command that waited for `--apply`. They all wait now, so
add `--apply` to any script or service unit that expects work to happen. Nothing
else changed, and `--dry-run` still means what it always did.

### Migrating a vault from 0.1

0.1 filed everything under `Documents/` and `Attachments/`. To move an existing
vault into the current layout:

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
# "text" reads the date off the document; the rest are guesses about the file.
fallbacks = ["text", "filename", "pdf-metadata", "file-created"]

[vault]
layout = "flat"                    # flat | para
documents_dir = "Archive"          # the folder documents live in ("" = vault root)
note_path_template = "{root}/{category}/{year}/{date} {name}"
attachment_path_template = "{root}/_attachments/{category}/{year}/{date} {name}"
source_action = "move"             # move | copy | leave
keep_original_image = true         # keep the photo an image document came from
include_text = true
extracted_text_style = "callout"   # callout | details | plain
cssclasses = ["scanvault"]         # the class the properties snippet targets ([] = none)
tag_source_folder = true           # turn the folder a document came from into tags

[vault.para]
projects_dir = "1 Projects"
areas_dir = "2 Areas"
resources_dir = "3 Resources"
archive_dir = "4 Archive"
default_bucket = "archive"         # where a new scan goes
```

Template placeholders: `{root} {category} {year} {month} {date} {name} {title} {slug} {correspondent}`,
where `{root}` is the documents folder — or the note's PARA folder when
`layout = "para"`. `{para}` still works as an alias.
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
