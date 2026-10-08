from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.tests import tagged


def groups(env):
    return "group_ids" if "group_ids" in env["res.users"]._fields else "groups_id"

PUT = "odoo.addons.tieroo.models.requests.put"


def ok(url):
    r = MagicMock()
    r.json.return_value = {"url": url, "serial": "s", "apple": None, "google": None}
    r.raise_for_status.return_value = None
    return r


@tagged("post_install", "-at_install")
class TestMultiCompany(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo()[groups(cls.env)] += cls.env.ref("point_of_sale.group_pos_manager")
        cls.company_a = cls.env.company
        cls.company_b = cls.setup_other_company(name="Pood B")["company"]
        admin = cls.env(su=True)
        cls.company_a.write({"wallet_api_url": "https://a.wallet.test", "wallet_api_key": "wk_a"})
        cls.company_b.write({"wallet_api_url": "https://b.wallet.test", "wallet_api_key": "wk_b"})
        Tag, Level = admin["res.partner.category"], admin["wallet.level"]
        cls.gold_a = Level.create({"name": "Gold", "company_id": cls.company_a.id, "tag_id": Tag.create({"name": "A Gold"}).id, "min_spend": 1000})
        cls.gold_b = Level.with_company(cls.company_b).create({"name": "Gold", "company_id": cls.company_b.id, "tag_id": Tag.create({"name": "B Gold"}).id, "min_spend": 100})
        cls.mari = cls.env["res.partner"].create({"name": "Mari Maasikas", "email": "mari@example.ee"})

    def invoice(self, amount, company):
        return self.init_invoice(
            "out_invoice", partner=self.mari, invoice_date=fields.Date.today(), amounts=[amount], taxes=[],
            company=company, post=True,
        )

    def in_company(self, company):
        return self.mari.with_company(company)

    def test_spend_and_level_are_per_company(self):
        self.invoice(500, self.company_b)
        a, b = self.in_company(self.company_a), self.in_company(self.company_b)
        self.assertEqual(b.wallet_level_id, self.gold_b)
        self.assertEqual(b.wallet_period_spend, 500)
        self.assertEqual(a.wallet_period_spend, 0)
        self.assertFalse(a.wallet_level_id)
        self.assertFalse(a.wallet_period_start)
        self.assertIn("B Gold", self.mari.category_id.mapped("name"))
        self.assertNotIn("A Gold", self.mari.category_id.mapped("name"))

    def test_a_level_change_in_one_company_keeps_the_other_companys_tag(self):
        self.invoice(500, self.company_b)
        self.invoice(1500, self.company_a)
        self.assertEqual(sorted(self.mari.category_id.mapped("name")), ["A Gold", "B Gold"])

    def test_each_company_has_its_own_card_and_platform(self):
        self.invoice(500, self.company_b)
        self.invoice(1500, self.company_a)
        with patch(PUT, return_value=ok("https://a.wallet.test/p/1?s=x")) as put:
            self.in_company(self.company_a)._wallet_create_card()
        self.assertEqual(put.call_args.args[0], f"https://a.wallet.test/sync/v1/customers/{self.mari.id}")
        self.assertEqual(put.call_args.kwargs["headers"]["Authorization"], "Bearer wk_a")
        self.assertEqual(put.call_args.kwargs["json"]["level"]["name"], "Gold")
        with patch(PUT, return_value=ok("https://b.wallet.test/p/2?s=y")) as put:
            self.in_company(self.company_b)._wallet_create_card()
        self.assertEqual(put.call_args.kwargs["headers"]["Authorization"], "Bearer wk_b")
        cards = self.mari.wallet_card_ids
        self.assertEqual(sorted(cards.company_id.mapped("name")), sorted([self.company_a.name, "Pood B"]))
        self.assertEqual(self.in_company(self.company_a).wallet_card_state, "active")
        self.assertEqual(self.in_company(self.company_b).wallet_card_state, "active")

    def test_exclusion_is_per_company(self):
        self.invoice(500, self.company_b)
        self.invoice(1500, self.company_a)
        for company, url in ((self.company_a, "https://a.wallet.test/p/1?s=x"), (self.company_b, "https://b.wallet.test/p/2?s=y")):
            with patch(PUT, return_value=ok(url)):
                self.in_company(company)._wallet_create_card()
        self.in_company(self.company_a).wallet_excluded = True
        self.assertEqual(self.in_company(self.company_a).wallet_card_state, "closed")
        self.assertEqual(self.in_company(self.company_b).wallet_card_state, "active")
        self.assertFalse(self.in_company(self.company_b).wallet_excluded)
        self.assertIn(self.company_a.name, self.mari.message_ids[0].body)

    def test_settings_are_per_company(self):
        settings_a = self.env["res.config.settings"].with_company(self.company_a).create({})
        settings_b = self.env["res.config.settings"].with_company(self.company_b).create({})
        self.assertEqual(settings_a.wallet_api_url, "https://a.wallet.test")
        self.assertEqual(settings_b.wallet_api_url, "https://b.wallet.test")
        settings_b.wallet_levels_auto_send = True
        settings_b.execute()
        self.assertTrue(self.company_b.wallet_levels_auto_send)
        self.assertFalse(self.company_a.wallet_levels_auto_send)

    def test_designer_gets_only_this_companys_levels(self):
        self.assertEqual(self.company_b._wallet_design_context()["levels"], [{"id": str(self.gold_b.id), "name": "Gold", "type": "b2c"}])
        self.assertEqual(len(self.company_a._wallet_design_context()["levels"]), 1)
