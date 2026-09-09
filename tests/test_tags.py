"""Tags have to be the thing you would actually search for."""

from __future__ import annotations

import unittest
from datetime import date

from scanvault.classify import derived_tags, from_response, rule_tags
from scanvault.config import load_config


class TestRuleTags(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_tax_authorities_are_tagged_taxes(self):
        for sender in (
            "HM Revenue & Customs",
            "Skatteverket",
            "Internal Revenue Service",
            "Finanzamt Berlin",
        ):
            self.assertIn("taxes", rule_tags(self.config, sender), sender)

    def test_government_senders_are_tagged_government(self):
        for text in ("Council Tax bill for 2024/25", "Companies House filing", "DVLA reminder"):
            self.assertIn("government", rule_tags(self.config, text), text)

    def test_a_mortgage_document_is_tagged_mortgage(self):
        tags = rule_tags(self.config, "Redemption statement for your mortgage account")
        self.assertIn("mortgage", tags)

    def test_unrelated_text_matches_nothing(self):
        self.assertEqual(rule_tags(self.config, "Thank you for visiting the coffee house"), [])

    def test_rules_can_be_replaced_from_config(self):
        self.config.tags.rules = {"boat": ["mooring", "marina"]}
        self.assertEqual(rule_tags(self.config, "Annual mooring fee"), ["boat"])
        self.assertEqual(rule_tags(self.config, "HMRC self assessment"), [])


class TestDerivedTags(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def meta(self, **kwargs):
        return from_response(
            {
                "title": kwargs.pop("title", "Mortgage statement"),
                "category": kwargs.pop("category", "Property"),
                "correspondent": kwargs.pop("correspondent", "Example Bank"),
                "document_date": kwargs.pop("document_date", "2024-05-02"),
                "tags": kwargs.pop("tags", ["statement"]),
                "subjects": kwargs.pop("subjects", ["12 Example Street"]),
            },
            self.config,
            "fallback",
            kwargs.pop("text", ""),
        )

    def test_the_year_is_a_tag(self):
        self.assertIn("year-2024", self.meta().tags)

    def test_an_undated_document_has_no_year_tag(self):
        tags = self.meta(document_date=None).tags
        self.assertFalse([tag for tag in tags if tag.startswith("year-")])

    def test_the_sender_is_a_tag(self):
        self.assertIn("example-bank", self.meta().tags)

    def test_what_the_document_is_about_is_a_tag(self):
        self.assertIn("12-example-street", self.meta().tags)

    def test_the_search_that_prompted_this(self):
        # "mortgage" plus the address should find the document.
        tags = self.meta(
            text="Your mortgage account. Redemption statement for 12 Example Street."
        ).tags
        self.assertIn("mortgage", tags)
        self.assertIn("12-example-street", tags)

    def test_a_tax_letter_is_findable_by_year(self):
        meta = self.meta(
            title="Self Assessment statement",
            category="Taxes",
            correspondent="HM Revenue & Customs",
            document_date="2023-01-31",
            tags=["self-assessment"],
            subjects=[],
        )
        self.assertIn("taxes", meta.tags)
        self.assertIn("year-2023", meta.tags)
        self.assertIn("hm-revenue-customs", meta.tags)

    def test_derived_tags_can_be_switched_off(self):
        self.config.tags.year_tag = False
        self.config.tags.correspondent_tag = False
        self.config.tags.subject_tags = False
        tags = self.meta().tags
        self.assertNotIn("year-2024", tags)
        self.assertNotIn("example-bank", tags)
        self.assertNotIn("12-example-street", tags)
        self.assertIn("property", tags, "the category is still tagged")

    def test_the_deterministic_tags_survive_the_cap(self):
        self.config.tags.max_tags = 3
        meta = self.meta(tags=[f"noise-{i}" for i in range(20)])
        self.assertIn("year-2024", meta.tags)
        self.assertIn("example-bank", meta.tags)
        self.assertNotIn("noise-0", meta.tags)

    def test_tags_are_unique_and_slugged(self):
        meta = self.meta(tags=["#Statement", "statement", "Example Bank"])
        self.assertEqual(len(meta.tags), len(set(meta.tags)))
        self.assertTrue(all(tag == tag.lower() and " " not in tag for tag in meta.tags))

    def test_derived_tags_helper_is_independent_of_the_model(self):
        meta = self.meta()
        derived = derived_tags(meta, self.config, "mortgage redemption statement")
        self.assertIn("property", derived)
        self.assertIn("mortgage", derived)


if __name__ == "__main__":
    unittest.main()


class TestVocabularyCoverage(unittest.TestCase):
    """The terminology someone's paperwork actually uses, in both languages."""

    def setUp(self):
        self.config = load_config()

    def check(self, text: str, expected: str):
        self.assertIn(expected, rule_tags(self.config, text), text)

    def test_student_finance(self):
        for text in (
            "CSN betalningsplan for 2024",
            "Centrala studiestödsnämnden - studiemedel",
            "Student Loans Company annual statement",
            "Your tuition fee loan balance",
            "Federal student aid summary",
        ):
            self.check(text, "student-loan")

    def test_pensions_and_investments(self):
        self.check("NEST workplace pension annual statement", "pension")
        self.check("Tjänstepension från Alecta", "pension")
        self.check("Portfolio statement with dividend summary", "investments")
        self.check("Ditt investeringssparkonto hos Nordnet", "investments")

    def test_identity_and_legal(self):
        self.check("Your new passport is enclosed", "identity")
        self.check("Uppehållstillstånd beviljat", "identity")
        self.check("Power of attorney signed before a solicitor", "legal")
        self.check("Bouppteckning efter dödsboet", "legal")

    def test_travel_subscriptions_and_warranty(self):
        self.check("Boarding pass - booking reference AB12CD", "travel")
        self.check("Annual membership renewal reminder", "subscription")
        self.check("Two year warranty and proof of purchase", "warranty")

    def test_home_medical_and_pets(self):
        self.check("Gas safety certificate after the installation", "home-improvement")
        self.check("Remiss till vårdcentralen", "medical")
        self.check("Veterinary bill including microchip", "pets")

    def test_loans_are_separate_from_mortgages(self):
        self.check("Personal loan agreement", "loan")
        self.check("Mortgage redemption statement", "mortgage")

    def test_word_boundaries_stop_false_positives(self):
        # "payee" is not PAYE, "risk" is not an ISK account, "taxi" is not tax.
        for text in ("Payee details enclosed", "There is a risk here", "Taxi receipt for the airport"):
            self.assertEqual(rule_tags(self.config, text), [], text)

    def test_every_rule_group_has_keywords_and_a_slug_safe_name(self):
        from scanvault.util import slugify

        for tag, keywords in self.config.tags.rules.items():
            self.assertTrue(keywords, tag)
            self.assertEqual(tag, slugify(tag), tag)


class TestCategories(unittest.TestCase):
    def test_the_common_kinds_of_paperwork_have_a_home(self):
        categories = load_config().categories
        for expected in ("Pensions", "Investments", "Loans", "Identity", "Legal", "Travel"):
            self.assertIn(expected, categories)

    def test_other_is_still_the_last_resort(self):
        self.assertEqual(load_config().categories[-1], "Other")
