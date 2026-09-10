"""The README has to describe this program, not a previous one.

Documentation drifts silently: nothing fails when a default changes, a flag is
renamed or the vault layout moves. These tests fail instead.
"""

from __future__ import annotations

import re
import tempfile
import tomllib
import unittest
from dataclasses import fields, is_dataclass
from datetime import date
from pathlib import Path

from scanvault.classify import DocumentMeta
from scanvault.cli import build_parser
from scanvault.config import Config, load_config
from scanvault.vault import Vault

README = Path(__file__).resolve().parent.parent / "README.md"

# Flags belonging to other people's programs, quoted in passing.
FOREIGN_FLAGS = {"--decrypt", "--password", "--extra", "--now", "--user", "--jobs"}
# Example values rather than real settings.
EXAMPLE_KEYS = {"source_dir", "vault_dir"}


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def config_keys(obj: object, prefix: str = "") -> dict[str, object]:
    found: dict[str, object] = {}
    for field in fields(obj):  # type: ignore[arg-type]
        value = getattr(obj, field.name)
        if is_dataclass(value):
            found.update(config_keys(value, f"{prefix}{field.name}."))
        else:
            found[f"{prefix}{field.name}"] = value
    return found


def documented_settings() -> dict[str, object]:
    settings: dict[str, object] = {}
    for block in re.findall(r"```toml\n(.*?)```", readme_text(), re.DOTALL):
        stripped = "\n".join(
            line for line in block.splitlines() if not line.strip().startswith("# ")
        )
        data = tomllib.loads(stripped)

        def walk(node: dict, prefix: str = "") -> None:
            for key, value in node.items():
                if isinstance(value, dict) and key not in ("rules", "extra_rules", "tag_categories"):
                    walk(value, f"{prefix}{key}.")
                else:
                    settings[f"{prefix}{key}"] = value

        walk(data)
    return settings


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.real = config_keys(Config())
        self.documented = documented_settings()

    def test_every_documented_setting_exists(self):
        for key in self.documented:
            self.assertIn(key, self.real, f"README documents a setting that is gone: {key}")

    def test_documented_defaults_are_the_real_defaults(self):
        for key, value in self.documented.items():
            if key in EXAMPLE_KEYS or isinstance(self.real[key], dict):
                continue
            if key in ("ocr.languages",):  # documented as a recommendation
                continue
            self.assertEqual(self.real[key], value, f"{key} in the README is not the default")

    def test_the_toml_in_the_readme_parses(self):
        self.assertTrue(self.documented, "no configuration is documented at all")


class TestCommandSurface(unittest.TestCase):
    def setUp(self):
        self.readme = readme_text()
        parser = build_parser()
        self.subcommands = next(
            action for action in parser._actions if isinstance(action.choices, dict)
        ).choices

    def test_every_command_is_documented(self):
        for name in self.subcommands:
            self.assertIn(f"`{name}`", self.readme, f"undocumented command: {name}")

    def test_every_flag_is_documented(self):
        for name, sub in self.subcommands.items():
            for action in sub._actions:
                for option in action.option_strings:
                    if not option.startswith("--") or option == "--help":
                        continue
                    self.assertIn(option, self.readme, f"undocumented flag: {name} {option}")

    def test_the_readme_invents_no_flags(self):
        real = {
            option
            for sub in self.subcommands.values()
            for action in sub._actions
            for option in action.option_strings
        }
        real |= {"--help", "--version", "--config", "--verbose", "--quiet"}
        for flag in set(re.findall(r"--[a-z][a-z-]+", self.readme)):
            if flag in FOREIGN_FLAGS:
                continue
            self.assertIn(flag, real, f"the README documents a flag that does not exist: {flag}")

    def test_the_readme_invents_no_commands(self):
        # Only inside code, so prose like "scanvault did not write it" is not
        # mistaken for an invocation.
        # Shell blocks and code spans only, so neither prose ("scanvault did
        # not write it") nor a systemd unit is read as an invocation.
        shell = [
            block
            for label, block in re.findall(
                r"^```(\w*)$(.*?)^```$", self.readme, re.MULTILINE | re.DOTALL
            )
            if label in ("bash", "sh", "console", "")
        ]
        code = re.findall(r"`([^`\n]+)`", self.readme) + shell
        for snippet in code:
            for command in re.findall(r"scanvault ([a-z][a-z-]+)", snippet):
                self.assertIn(
                    command,
                    set(self.subcommands),
                    f"the README shows a command that does not exist: scanvault {command}",
                )


class TestLayout(unittest.TestCase):
    """The paths in the README are the paths the code produces."""

    def setUp(self):
        self.readme = readme_text()
        self.tmp = tempfile.TemporaryDirectory()
        self.config = load_config(overrides={"vault_dir": self.tmp.name})
        self.vault = Vault(self.config)
        self.meta = DocumentMeta(
            title="Invoice INV-1234",
            category="Invoices",
            document_date=date(2024, 5, 2),
            correspondent="Acme Ltd",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_documented_note_path_is_what_we_write(self):
        note = self.vault.note_path(self.meta).relative_to(self.tmp.name).as_posix()
        self.assertEqual(note, "Archive/Invoices/2024/2024-05-02 Acme Ltd - Invoice INV-1234.md")
        # The trees draw the leading folder with box characters, so match the
        # part below it.
        self.assertIn(note.split("/", 1)[1], self.readme)
        self.assertIn(self.config.vault.documents_dir + "/", self.readme)

    def test_the_documented_attachment_path_is_what_we_write(self):
        attachment = self.vault.attachment_path(self.meta).relative_to(self.tmp.name).as_posix()
        self.assertIn(attachment, self.readme)

    def test_the_state_folder_is_where_the_readme_says(self):
        self.assertEqual(self.config.state_root.name, ".scanvault")
        for name in ("index.json", "classifications.json"):
            self.assertIn(name, self.readme)


class TestNoteSample(unittest.TestCase):
    def test_the_sample_note_is_what_the_code_renders(self):
        readme = readme_text()
        match = re.search(r"```markdown\n(---\ntitle:.*?\n)```", readme, re.DOTALL)
        self.assertIsNotNone(match, "the README no longer shows a note")
        documented = match.group(1)

        # Everything the sample claims about the frontmatter has to be a key we
        # actually write, in the order we write it.
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(overrides={"vault_dir": tmp})
            vault = Vault(config)
            # Every optional field populated, so a field the README shows is
            # missing here only because we stopped writing it.
            meta = DocumentMeta(
                title="Invoice INV-1234",
                category="Invoices",
                document_date=date(2024, 5, 2),
                correspondent="Acme Ltd",
                summary="A summary.",
                tags=["scan"],
                subjects=["Account 4242"],
                language="English",
                reference="INV-1234",
                amount="120.00",
                currency="EUR",
                confidence=0.95,
                date_source="document",
                context="business",
                document_type="commercial invoice",
            )
            rendered = vault.render_note(
                meta,
                "text",
                vault.attachment_path(meta),
                extra={
                    "source_file": "scan_001.pdf",
                    "source_hash": "9f2c",
                    "ocr": "ocrmypdf",
                    "pages": 2,
                },
            )

        rendered_keys = [
            line.split(":", 1)[0]
            for line in rendered.splitlines()
            if re.match(r"^[a-z_]+:", line)
        ]
        documented_keys = [
            line.split(":", 1)[0]
            for line in documented.splitlines()
            if re.match(r"^[a-z_]+:", line)
        ]
        for key in documented_keys:
            self.assertIn(key, rendered_keys, f"the sample note shows a field we do not write: {key}")
        self.assertEqual(
            documented_keys,
            [key for key in rendered_keys if key in documented_keys],
            "the sample note lists the frontmatter in a different order than we write it",
        )


if __name__ == "__main__":
    unittest.main()
