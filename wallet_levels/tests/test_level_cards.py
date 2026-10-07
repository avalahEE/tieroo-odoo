from unittest.mock import MagicMock, patch

from dateutil.relativedelta import relativedelta

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

PUT = "odoo.addons.wallet_card.models.requests.put"
DELETE = "odoo.addons.wallet_card.models.requests.delete"

def ok(url="https://wallet.test/p/abc?s=sig"):
    r = MagicMock()
    r.json.return_value = {"url": url, "serial": "abc", "apple": None, "google": None}
    r.raise_for_status.return_value = None
    return r

@tagged("post_install", "-at_install")
class TestLevelCards(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write({"wallet_api_url": "https://wallet.test/", "wallet_api_key": "wk_testkey"})
        Tag, Level = cls.env["res.partner.category"], cls.env["wallet.level"]
        silver, gold = Tag.create({"name": "Silver"}), Tag.create({"name": "Gold"})
        cls.silver = Level.create({"name": "Silver", "tag_id": silver.id, "min_spend": 0})
        cls.gold = Level.create({"name": "Gold", "tag_id": gold.id, "min_spend": 1000})
        cls.b2b_gold = Level.create({"name": "Gold", "customer_type": "b2b", "tag_id": gold.id, "min_spend": 0})
        cls.mari = cls.env["res.partner"].create({"name": "Mari Maasikas", "email": "mari@example.ee"})

    def auto_send(self):
        self.env.company.wallet_levels_auto_send = True

    def test_creating_a_card_joins_and_shows_the_level(self):
        with patch(PUT, return_value=ok()) as put:
            self.mari._wallet_create_card()
        self.assertTrue(self.mari.wallet_period_start)
        payload = put.call_args.kwargs["json"]
        self.assertEqual(payload.pop("texts")[f"level:{self.silver.id}"]["en_US"], "Silver")
        self.assertEqual(payload, {
            "barcode": self.mari.barcode,
            "name": "Mari Maasikas",
            "company": None,
            "level": {"id": str(self.silver.id), "name": "Silver"},
            "points": None,
            "next": {"kind": "level", "id": str(self.gold.id), "name": "Gold", "missing": 1000.0},
            "keep": {"missing": 0.0, "until": (self.mari.wallet_period_end + relativedelta(months=12, days=-1)).isoformat()},
            "currency": self.env.company.currency_id.symbol,
            "expires": None,
            "expiring": None,
            "rewards": [],
            "lang": self.mari.lang,
        })

    def test_b2b_contact_gets_own_card_with_company_level(self):
        company = self.env["res.partner"].create({"name": "Mööbel OÜ", "vat": "EE100931558"})
        jaan = self.env["res.partner"].create({"name": "Jaan", "parent_id": company.id, "email": "jaan@moobel.ee"})
        kati = self.env["res.partner"].create({"name": "Kati", "parent_id": company.id, "email": "kati@moobel.ee"})
        with patch(PUT, return_value=ok()) as put:
            jaan._wallet_create_card()
        payload = put.call_args.kwargs["json"]
        self.assertEqual((payload["name"], payload["company"]), ("Jaan", "Mööbel OÜ"))
        self.assertEqual(payload["level"], {"id": str(self.b2b_gold.id), "name": "Gold"})
        self.assertEqual(payload["barcode"], jaan.barcode)
        self.assertTrue(company.wallet_period_start)
        self.assertFalse(jaan.wallet_period_start)
        with patch(PUT, return_value=ok("https://wallet.test/p/kati?s=x")):
            kati._wallet_create_card()
        self.assertNotEqual(jaan.barcode, kati.barcode)
        (jaan | kati).wallet_card_ids.sync_needed = False
        company._wallet_mark()
        self.assertTrue(jaan._wallet_card().sync_needed and kati._wallet_card().sync_needed)

    def test_excluding_closes_the_card_and_including_reopens_it(self):
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        self.mari.wallet_excluded = True
        self.assertEqual(self.mari.wallet_card_state, "closed")
        self.assertIn("Wallet card closed", self.mari.message_ids[0].body)
        with patch(DELETE, return_value=ok()) as delete, patch(PUT) as put:
            self.env["wallet.card"]._cron_sync()
        delete.assert_called_once()
        put.assert_not_called()
        with self.assertRaises(UserError):
            self.mari._wallet_create_card()
        with self.assertRaises(UserError):
            self.mari.action_wallet_resend()

        self.mari.wallet_excluded = False
        self.assertEqual(self.mari.wallet_card_state, "active")
        self.assertIn("Wallet card active again", self.mari.message_ids[0].body)
        with patch(PUT, return_value=ok()) as put, patch(DELETE) as delete:
            self.env["wallet.card"]._cron_sync()
        put.assert_called_once()
        delete.assert_not_called()

    def test_excluding_a_company_closes_all_its_contacts_cards(self):
        company = self.env["res.partner"].create({"name": "Erand OÜ", "vat": "EE100931558"})
        jaan = self.env["res.partner"].create({"name": "Jaan", "parent_id": company.id, "email": "jaan@erand.ee"})
        with patch(PUT, return_value=ok()):
            jaan._wallet_create_card()
        company.wallet_excluded = True
        self.assertEqual(jaan.wallet_card_state, "closed")
        with patch(DELETE, return_value=ok()) as delete:
            self.env["wallet.card"]._cron_sync()
        self.assertIn(f"/customers/{jaan.id}", delete.call_args.args[0])

    def test_joining_customer_gets_card_without_odoo_loyalty(self):
        self.auto_send()
        self.mari._wallet_update_levels()
        with patch(PUT, return_value=ok()):
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(self.mari.wallet_card_state, "active")

    def test_b2b_each_contact_person_gets_a_card_the_company_record_does_not(self):
        self.auto_send()
        company = self.env["res.partner"].create({"name": "Mööbel OÜ", "vat": "EE100931558", "email": "info@moobel.ee"})
        jaan = self.env["res.partner"].create({"name": "Jaan", "parent_id": company.id, "email": "jaan@moobel.ee"})
        no_mail = self.env["res.partner"].create({"name": "Peeter", "parent_id": company.id})
        company._wallet_update_levels()
        with patch(PUT, return_value=ok()):
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(jaan.wallet_card_state, "active")
        self.assertEqual(company.wallet_card_state, "none")
        self.assertEqual(no_mail.wallet_card_state, "none")
        kati = self.env["res.partner"].create({"name": "Kati", "parent_id": company.id, "email": "kati@moobel.ee"})
        with patch(PUT, return_value=ok("https://wallet.test/p/kati?s=x")):
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(kati.wallet_card_state, "active")
