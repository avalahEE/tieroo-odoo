from unittest.mock import MagicMock, patch

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.addons.tieroo.tests.test_card import changed, platform
from odoo.tests import Form, TransactionCase, tagged

PUT = "odoo.addons.tieroo.models.requests.put"
DELETE = "odoo.addons.tieroo.models.requests.delete"

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

    def start(self):
        self.env.company.sudo().wallet_levels_started = fields.Datetime.now()

    def test_a_member_s_card_shows_the_level(self):
        self.start()
        self.mari.wallet_excluded = False
        with patch(PUT, return_value=ok()) as put:
            self.mari._wallet_create_card()
        payload = put.call_args.kwargs["json"]
        self.assertEqual(payload.pop("texts")[f"level:{self.silver.id}"]["en_US"], "Silver")
        self.assertEqual(payload, {
            "barcode": self.mari.barcode,
            "name": "Mari Maasikas",
            "company": None,
            "level": {"id": str(self.silver.id), "name": "Silver"},
            "points": None,
            "next": {"kind": "level", "id": str(self.gold.id), "name": "Gold", "missing": 1000.0},
            "keep": None,
            "currency": self.env.company.currency_id.symbol,
            "expires": None,
            "expiring": None,
            "lastSale": None,
            "rewards": [],
            "lang": self.mari.lang,
            "places": [], "shops": [],
        })

    def test_installing_changes_nothing_until_start(self):
        loyalty = self.env["loyalty.program"].create({"name": "Klubi", "program_type": "loyalty", "trigger": "auto", "applies_on": "both"})
        loyalty.wallet_card = True
        self.env["loyalty.card"].create({"program_id": loyalty.id, "partner_id": self.mari.id})
        self.assertEqual(self.mari._wallet_is_member(), True)
        self.assertTrue(self.mari.wallet_excluded)
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        settings = self.env["res.config.settings"].create({})
        self.assertIn("Card holders who join at Silver: 1", settings.wallet_levels_preview)
        with patch(PUT, return_value=ok()):
            settings.action_wallet_levels_start()
        self.assertFalse(self.mari.wallet_excluded)
        self.assertEqual(self.mari.wallet_level_id, self.silver)
        self.assertEqual(self.mari.wallet_card_state, "active")
        anna = self.env["res.partner"].create({"name": "Anna", "email": "anna@example.ee"})
        self.env["loyalty.card"].create({"program_id": loyalty.id, "partner_id": anna.id})
        self.assertFalse(anna._wallet_is_member())

    def test_b2b_contact_gets_own_card_with_company_level(self):
        self.start()
        company = self.env["res.partner"].create({"name": "Mööbel OÜ", "vat": "EE100931558"})
        jaan = self.env["res.partner"].create({"name": "Jaan", "parent_id": company.id, "email": "jaan@moobel.ee"})
        kati = self.env["res.partner"].create({"name": "Kati", "parent_id": company.id, "email": "kati@moobel.ee"})
        with self.assertRaises(UserError):
            jaan.wallet_excluded = False
        b2b = self.b2b_gold.sudo().pricelist_id = self.env["product.pricelist"].sudo().create({"name": "B2B -7%"})
        company.wallet_excluded = False
        self.assertTrue(jaan.wallet_excluded)
        (jaan | kati).write({"wallet_excluded": False})
        with patch(PUT, return_value=ok()) as put:
            jaan._wallet_create_card()
        payload = put.call_args.kwargs["json"]
        self.assertEqual((payload["name"], payload["company"]), ("Jaan", "Mööbel OÜ"))
        self.assertEqual(payload["level"], {"id": str(self.b2b_gold.id), "name": "Gold"})
        self.assertEqual(payload["barcode"], jaan.barcode)
        with patch(PUT, return_value=ok("https://wallet.test/p/kati?s=x")):
            kati._wallet_create_card()
        self.assertNotEqual(jaan.barcode, kati.barcode)
        self.env["wallet.card"]._wallet_take()
        (jaan | kati).wallet_card_ids.sync_needed = False
        company._wallet_mark()
        self.assertTrue(changed(jaan._wallet_card()) and kati._wallet_card().sync_needed)
        kati.wallet_excluded = True
        self.assertEqual(kati.wallet_card_state, "closed")
        self.assertEqual(jaan.wallet_card_state, "active")
        self.assertTrue(company.wallet_level_id)
        self.assertEqual(kati.property_product_pricelist, b2b)

    def test_leaving_closes_the_card_and_joining_again_reopens_it(self):
        self.start()
        self.mari.wallet_excluded = False
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        self.mari.wallet_excluded = True
        self.assertEqual(self.mari.wallet_card_state, "closed")
        self.assertIn("Left Customer Levels", self.mari.message_ids[0].body)
        with patch(DELETE, return_value=ok()) as delete, patch(PUT) as put:
            self.env["wallet.card"]._cron_sync()
        delete.assert_called_once()
        put.assert_not_called()
        with self.assertRaises(UserError):
            self.mari.action_wallet_resend()

        self.mari.wallet_excluded = False
        self.assertEqual(self.mari.wallet_card_state, "active")
        with patch(PUT, return_value=ok()) as put, patch(DELETE) as delete:
            self.env["wallet.card"]._cron_sync()
        put.assert_called()
        delete.assert_not_called()

    def test_a_company_leaving_takes_its_contacts(self):
        self.start()
        company = self.env["res.partner"].create({"name": "Erand OÜ", "vat": "EE100931558", "wallet_excluded": False})
        jaan = self.env["res.partner"].create({"name": "Jaan", "parent_id": company.id, "email": "jaan@erand.ee", "wallet_excluded": False})
        with patch(PUT, return_value=ok()):
            jaan._wallet_create_card()
        company.wallet_excluded = True
        self.assertTrue(jaan.wallet_excluded)
        self.assertEqual(jaan.wallet_card_state, "closed")
        with patch(DELETE, return_value=ok()) as delete:
            self.env["wallet.card"]._cron_sync()
        self.assertIn(f"/customers/{jaan.id}", delete.call_args.args[0])

    def test_members_get_their_card_by_email_without_odoo_loyalty(self):
        self.start()
        company = self.env["res.partner"].create({"name": "Mööbel OÜ", "vat": "EE100931558", "email": "info@moobel.ee", "wallet_excluded": False})
        jaan = self.env["res.partner"].create({"name": "Jaan", "parent_id": company.id, "email": "jaan@moobel.ee", "wallet_excluded": False})
        no_mail = self.env["res.partner"].create({"name": "Peeter", "parent_id": company.id, "wallet_excluded": False})
        self.mari.wallet_excluded = False
        with patch(PUT, side_effect=platform()):
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(self.mari.wallet_card_state, "active")
        self.assertEqual(jaan.wallet_card_state, "active")
        self.assertEqual(company.wallet_card_state, "none")
        self.assertEqual(no_mail.wallet_card_state, "none")
        self.assertTrue(self.env["mail.mail"].sudo().search([("recipient_ids", "in", self.mari.ids)]))

    def test_joining_on_the_join_page_with_levels_alone(self):
        company = self.env.company
        self.assertFalse(company._wallet_signup_open())
        self.start()
        self.assertTrue(company._wallet_signup_open())
        with patch("odoo.addons.tieroo.join.secrets.token_urlsafe", return_value="tok-liis"):
            self.env["res.partner"]._wallet_signup_request(company, "Liis Tamm", "liis@example.ee", lang="en_US", birthday=None, ip_hash="ip9")
        req = self.env["wallet.signup.request"]._find(company, "tok-liis")
        with patch(PUT, return_value=ok()):
            card = req._confirm()
        liis = card.partner_id
        self.assertFalse(liis.wallet_excluded)
        self.assertEqual(liis.wallet_level_id, self.silver)
        self.assertFalse(self.env["loyalty.card"].search([("partner_id", "=", liis.id)]))
        self.assertEqual(liis.wallet_card_state, "active")

    def join_page(self, name, email, token):
        with patch("odoo.addons.tieroo.join.secrets.token_urlsafe", return_value=token):
            self.env["res.partner"]._wallet_signup_request(self.env.company, name, email, lang="en_US", birthday=None, ip_hash=token)
        with patch(PUT, return_value=ok()):
            return self.env["wallet.signup.request"]._find(self.env.company, token)._confirm().partner_id

    def test_a_leaver_and_an_archived_contact_come_back_on_the_join_page(self):
        self.start()
        liis = self.join_page("Liis Tamm", "liis@example.ee", "tok-1")
        liis.with_context(wallet_levels_internal=True).wallet_joined = fields.Date.today() - relativedelta(days=100)
        with patch(PUT, return_value=ok()):
            liis.wallet_excluded = True
        self.env["wallet.signup.request"].sudo().search([]).unlink()
        self.assertEqual(self.join_page("Liis Tamm", "liis@example.ee", "tok-2"), liis)
        self.assertEqual((liis.wallet_joined, liis.wallet_level_id), (fields.Date.today(), self.silver))
        self.assertEqual(liis.wallet_card_state, "active")

        old = self.env["res.partner"].create({"name": "Peeter Puu", "email": "peeter@example.ee", "active": False})
        self.assertEqual(self.join_page("Peeter Puu", "peeter@example.ee", "tok-3"), old)
        self.assertTrue(old.active)
        self.assertEqual((old.wallet_joined, old.wallet_level_id), (fields.Date.today(), self.silver))

    def test_a_new_contact_from_the_form_by_anyone(self):
        user = self.env["res.users"].create({"name": "Müüja", "login": "myyja@example.ee",
                                             "group_ids" if "group_ids" in self.env["res.users"]._fields else "groups_id":
                                             [(6, 0, [self.env.ref("base.group_user").id, self.env.ref("base.group_partner_manager").id])]})
        with Form(self.env["res.partner"].with_user(user)) as f:
            f.name = "Uus klient"
        self.assertTrue(f.record.wallet_excluded)
        with Form(self.mari.with_user(user)) as f:
            f.phone = "5555"

    def loyalty(self):
        program = self.env["loyalty.program"].create({"name": "Klubi", "program_type": "loyalty", "trigger": "auto", "applies_on": "both"})
        program.wallet_card = True
        return program

    def test_start_twice_and_nobody_emailed_and_their_own_pricelist_gives_way(self):
        program = self.loyalty()
        other = self.env["product.pricelist"].create({"name": "Eri"})
        self.gold.pricelist_id = self.silver.pricelist_id = self.env["product.pricelist"].create({"name": "Silver -5%"})
        self.mari.property_product_pricelist = other
        self.env["loyalty.card"].create({"program_id": program.id, "partner_id": self.mari.id})
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        settings = self.env["res.config.settings"].create({})
        self.assertIn("Card holders who join at Silver: 1", settings.wallet_levels_preview)
        mails = self.env["mail.mail"].sudo().search_count([])
        with patch(PUT, return_value=ok()):
            settings.action_wallet_levels_start()
            self.env.company._wallet_levels_start()
        self.assertEqual(self.mari.wallet_level_id, self.silver)
        self.assertEqual(self.mari.wallet_joined, fields.Date.today())
        self.assertEqual(self.mari.property_product_pricelist, self.silver.pricelist_id)
        self.assertEqual(self.env["mail.mail"].sudo().search_count([]), mails)

    def test_joining_on_the_join_page_with_loyalty_and_levels(self):
        program = self.loyalty()
        self.start()
        with patch("odoo.addons.tieroo.join.secrets.token_urlsafe", return_value="tok-both"):
            self.env["res.partner"]._wallet_signup_request(self.env.company, "Kai", "kai@example.ee", lang="en_US", birthday=None, ip_hash="ip8")
        with patch(PUT, return_value=ok()) as put:
            card = self.env["wallet.signup.request"]._find(self.env.company, "tok-both")._confirm()
        kai = card.partner_id
        self.assertTrue(self.env["loyalty.card"].search([("partner_id", "=", kai.id), ("program_id", "=", program.id)]))
        self.assertEqual(kai.wallet_level_id, self.silver)
        payload = put.call_args.kwargs["json"]
        self.assertEqual((payload["level"]["name"], payload["points"]), ("Silver", 0))

    def test_one_person_with_a_private_card_and_a_company_card(self):
        self.start()
        company = self.env["res.partner"].create({"name": "Mööbel OÜ", "vat": "EE100931558", "wallet_excluded": False})
        work = self.env["res.partner"].create({"name": "Mari Maasikas", "parent_id": company.id, "email": "mari@example.ee", "wallet_excluded": False})
        self.mari.wallet_excluded = False
        with patch(PUT, return_value=ok()) as put:
            self.mari._wallet_create_card()
            work._wallet_create_card()
        self.assertEqual(len(put.call_args_list), 2)
        self.assertNotEqual(self.mari.barcode, work.barcode)
        self.assertEqual(self.mari.wallet_level_id, self.silver)
        self.assertEqual(company.wallet_level_id, self.b2b_gold)
        work.wallet_excluded = True
        self.assertEqual(self.mari.wallet_card_state, "active")
        with self.assertRaises(UserError):
            work.write({"wallet_excluded": False, "wallet_level_id": self.b2b_gold.id})
