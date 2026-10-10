import re
from unittest.mock import MagicMock, patch

from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged

from .test_card import add_points

PUT = "odoo.addons.tieroo.models.requests.put"
BRAND = "odoo.addons.tieroo.join.requests.get"
GET = "odoo.addons.tieroo.models.requests.get"


def ok():
    r = MagicMock()
    r.json.return_value = {"url": "https://wallet.test/p/abc?s=sig", "serial": "abc", "apple": None, "google": None}
    return r


class SignupSetup:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.write({"wallet_api_url": "https://wallet.test/", "wallet_api_key": "wk_testkey"})
        cls.program = cls.env["loyalty.program"].create({
            "name": "Kohviklubi", "program_type": "loyalty", "trigger": "auto", "applies_on": "both", "wallet_card": True,
            "rule_ids": [(0, 0, {"reward_point_mode": "money", "reward_point_amount": 1})],
        })
        cls.company._wallet_signup_set_default_tag()
        cls.Partner = cls.env["res.partner"]

    def join(self, email="mari@example.ee", name="Mari Maasikas", birthday=None, ip="ip1"):
        token = f"tok-{email}-{self.env['wallet.signup.request'].search_count([])}"
        with patch("odoo.addons.tieroo.join.secrets.token_urlsafe", return_value=token):
            result = self.Partner._wallet_signup_request(self.company, name, email, lang="en_US", birthday=birthday, ip_hash=ip)
        return result, token

    def confirm(self, token):
        req = self.env["wallet.signup.request"]._find(self.company, token)
        with patch(PUT, return_value=ok()):
            return req._confirm()

    def card_mails(self, partner):
        return self.env["mail.mail"].sudo().search([("recipient_ids", "in", partner.ids), ("subject", "like", "Your loyalty card")])


@tagged("post_install", "-at_install")
class TestSignup(SignupSetup, TransactionCase):

    def test_nothing_happens_until_the_email_is_confirmed(self):
        result, _token = self.join(birthday=("29", "2"))
        self.assertEqual(result, "sent")
        self.assertFalse(self.Partner.search([("email", "=", "mari@example.ee")]))
        mail = self.env["mail.mail"].sudo().search([("email_to", "=", "mari@example.ee")])
        self.assertEqual(len(mail), 1)
        self.assertIn("/confirm/tok-mari@example.ee", mail.body_html)

    def test_the_confirmation_email_goes_out_at_once(self):
        scheduler = self.env.ref("mail.ir_cron_mail_scheduler_action")
        Trigger = self.env["ir.cron.trigger"].sudo()
        before = Trigger.search_count([("cron_id", "=", scheduler.id)])
        self.join()
        self.assertGreater(Trigger.search_count([("cron_id", "=", scheduler.id)]), before)

    def test_confirmed_customer_gets_contact_tag_birthday_loyalty_card_and_card(self):
        _result, token = self.join(birthday=("29", "2"))
        card = self.confirm(token)
        mari = self.Partner.search([("email", "=", "mari@example.ee")])
        self.assertEqual(card.partner_id, mari)
        self.assertIn(self.env.ref("tieroo.tag_joined_via_qr"), mari.category_id)
        self.assertEqual((mari.wallet_birth_day, mari.wallet_birth_month), (29, "2"))
        self.assertTrue(self.env["loyalty.card"].search([("partner_id", "=", mari.id), ("program_id", "=", self.program.id)]))
        self.assertEqual(len(self.card_mails(mari)), 1)
        self.assertTrue(any("confirmed the email" in m.body for m in mari.message_ids))
        req = self.env["wallet.signup.request"]._find(self.company, token)
        with patch(PUT) as put:
            self.assertEqual(req._confirm(), card)
        put.assert_not_called()
        self.assertEqual(len(self.card_mails(mari)), 1)
        self.assertEqual(len([m for m in mari.message_ids if "confirmed the email" in m.body]), 1)

    def test_signup_matches_the_oldest_contact_not_an_address(self):
        self.Partner.create({"name": "Tarneaadress", "email": "mari@example.ee", "type": "delivery"})
        mari = self.Partner.create({"name": "Mari", "email": "mari@example.ee"})
        self.Partner.create({"name": "Mari teine", "email": "mari@example.ee"})
        card = self.confirm(self.join()[1])
        self.assertEqual(card.partner_id, mari)

    def test_the_confirmation_email_is_an_odoo_template_with_the_link(self):
        _result, token = self.join()
        mail = self.env["mail.mail"].sudo().search([("email_to", "=", "mari@example.ee")], order="id desc", limit=1)
        self.assertIn(f"/confirm/{token}", mail.body_html)
        self.assertIn("Confirm and get the card", mail.body_html)
        self.assertEqual(mail.subject, f"Confirm joining {self.company.name}")
        self.assertFalse(mail.model)
        templates = self.env["res.config.settings"].create({}).action_wallet_email_templates()
        found = self.env["mail.template"].search(templates["domain"])
        self.assertEqual(set(found.mapped("name")), {"Tieroo: wallet card", "Tieroo: confirm joining"})
        with self.assertRaises(UserError):
            found.unlink()

    def test_an_archived_customer_joining_again_comes_back_with_their_points(self):
        mari = self.Partner.create({"name": "Mari", "email": "mari@example.ee"})
        old = self.env["loyalty.card"].create({"program_id": self.program.id, "partner_id": mari.id})
        add_points(old, 40)
        old.active = False
        mari.active = False
        card = self.confirm(self.join()[1])
        self.assertEqual(card.partner_id, mari)
        self.assertTrue(mari.active)
        self.assertTrue(old.active)
        self.assertEqual(old.points, 40)
        self.assertEqual(self.env["loyalty.card"].search_count([("partner_id", "=", mari.id), ("program_id", "=", self.program.id)]), 1)
        self.assertEqual(len(self.card_mails(mari)), 1)

    def test_known_email_is_not_duplicated_and_birthday_cannot_be_changed(self):
        self.confirm(self.join(name="Mari", birthday=("3", "5"))[1])
        self.env.cr.execute("UPDATE wallet_signup_request SET create_date = create_date - interval '3 minutes'")
        self.confirm(self.join(name="Someone else", birthday=("4", "6"), email="MARI@example.ee")[1])
        mari = self.Partner.search([("email_normalized", "=", "mari@example.ee")])
        self.assertEqual(len(mari), 1)
        self.assertEqual((mari.name, mari.wallet_birth_day, mari.wallet_birth_month), ("Mari", 3, "5"))

    def test_one_confirmation_email_per_address_per_10_minutes(self):
        self.join()
        self.assertEqual(self.join()[0], "sent")
        self.assertEqual(self.env["mail.mail"].sudo().search_count([("email_to", "=", "mari@example.ee")]), 1)

    def test_merchant_chooses_the_tag_or_none(self):
        vip = self.env["res.partner.category"].create({"name": "QR VIP"})
        self.company.wallet_signup_tag_id = vip
        self.confirm(self.join(email="a@example.ee")[1])
        self.assertEqual(self.Partner.search([("email", "=", "a@example.ee")]).category_id, vip)
        self.company.wallet_signup_tag_id = False
        self.confirm(self.join(email="b@example.ee")[1])
        self.assertFalse(self.Partner.search([("email", "=", "b@example.ee")]).category_id)

    def test_companies_are_never_matched(self):
        company_rec = self.Partner.create({"name": "Firma", "email": "firma@example.ee", "vat": "EE100931558"})
        self.confirm(self.join(email="firma@example.ee")[1])
        person = self.Partner.search([("email", "=", "firma@example.ee"), ("is_company", "=", False)])
        self.assertTrue(person)
        self.assertFalse(company_rec.category_id)

    def test_a_company_contact_person_is_not_the_private_customer(self):
        firma = self.Partner.create({"name": "Avalah OÜ", "is_company": True})
        at_work = self.Partner.create({"name": "Mari", "email": "mari@example.ee", "parent_id": firma.id})
        card = self.confirm(self.join(email="mari@example.ee")[1])
        self.assertNotEqual(card.partner_id, at_work)
        self.assertFalse(card.partner_id.parent_id)
        self.assertEqual(card.partner_id.email, "mari@example.ee")

    def test_terms_and_privacy_links_must_be_web_addresses(self):
        with self.assertRaises(ValidationError):
            self.company.wallet_terms_url = "javascript:alert(1)"
        self.company.wallet_terms_url = "https://kohvik.ee/kliendiprogramm"

    def test_invalid_birthday_is_ignored(self):
        self.confirm(self.join(birthday=("31", "2"))[1])
        self.assertFalse(self.Partner.search([("email", "=", "mari@example.ee")]).wallet_birth_month)
        self.confirm(self.join(email="k@example.ee", birthday=("05", " 07"))[1])
        self.assertEqual(self.Partner.search([("email", "=", "k@example.ee")]).wallet_birth_month, "7")

    def test_too_many_signups_from_one_address(self):
        for i in range(30):
            self.join(email=f"u{i}@example.ee")
        self.assertEqual(self.join(email="u99@example.ee")[0], "limited")

    def test_closed_without_a_wallet_programme(self):
        self.program.wallet_card = False
        self.assertEqual(self.join()[0], "closed")

    def test_designer_gets_the_join_link_for_the_posters(self):
        url = self.company._wallet_signup_url()
        self.assertRegex(url, r"/wallet/join/[A-Za-z0-9_-]{16,}$")
        self.assertEqual(self.company._wallet_signup_url(), url)
        ctx = self.company._wallet_design_context()
        self.assertEqual(ctx["signupUrl"], url)
        self.assertEqual(ctx["address"]["company"], self.company.name)
        r = MagicMock()
        r.json.return_value = {"url": "https://wallet.test/design/open?t=abc"}
        with patch("odoo.addons.tieroo.models.requests.post", return_value=r) as post:
            self.env["res.config.settings"].create({}).action_wallet_signup_posters()
        self.assertEqual(post.call_args.kwargs["json"]["start"], "poster")

@tagged("post_install", "-at_install")
class TestJoinPage(SignupSetup, HttpCase):

    def setUp(self):
        super().setUp()
        brand = MagicMock()
        brand.json.return_value = {"logoUrl": "https://wallet.test/i/p?n=logo%403x&v=1", "background": "#0b3d2e", "foreground": "#ffffff"}
        patcher = patch(BRAND, return_value=brand)
        patcher.start()
        self.addCleanup(patcher.stop)
        from odoo.addons.tieroo import join as signup_models
        signup_models._BRAND.clear()

    def test_page_form_and_thank_you(self):
        path = self.company._wallet_signup_url().split("/wallet/join/")[1]
        page = self.url_open(f"/wallet/join/{path}")
        self.assertIn("https://wallet.test/i/p?n=logo%403x", page.text)
        self.assertIn("#0b3d2e", page.text)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Join our loyalty programme", page.text)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        with patch("odoo.addons.tieroo.join.secrets.token_urlsafe", return_value="kati-link-token-1234567890"):
            sent = self.url_open(f"/wallet/join/{path}", data={
                "csrf_token": token, "name": "Kati", "email": "kati@example.ee", "consent": "1", "day": "", "month": ""})
        self.assertIn("Check your email", sent.text)
        self.assertFalse(self.Partner.search([("email", "=", "kati@example.ee")]))
        link = f"/wallet/join/{path}/confirm/kati-link-token-1234567890"
        page = self.url_open(link)
        self.assertIn("Confirm joining", page.text)
        self.assertFalse(self.Partner.search([("email", "=", "kati@example.ee")]))
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        with patch(PUT, return_value=ok()):
            done = self.url_open(link, data={"csrf_token": token})
        self.assertIn("Welcome", done.text)
        self.assertIn("https://wallet.test/p/abc", done.text)
        self.assertTrue(self.Partner.search([("email", "=", "kati@example.ee")]))
        again = self.url_open(link)
        self.assertIn("You have already joined", again.text)
        self.assertNotIn("https://wallet.test/p/abc", again.text)
        self.assertIn("This link has expired", self.url_open(f"/wallet/join/{path}/confirm/not-a-valid-link-token-00").text)

    def test_honeypot_and_bad_link(self):
        path = self.company._wallet_signup_url().split("/wallet/join/")[1]
        token = re.search(r'name="csrf_token" value="([^"]+)"', self.url_open(f"/wallet/join/{path}").text).group(1)
        bot = self.url_open(f"/wallet/join/{path}", data={
            "csrf_token": token, "name": "Bot", "email": "bot@example.ee", "consent": "1", "website": "http://spam"})
        self.assertIn("Check your email", bot.text)
        self.assertFalse(self.env["wallet.signup.request"].search([("email", "=", "bot@example.ee")]))
        self.assertEqual(self.url_open("/wallet/join/not-a-real-token-123").status_code, 404)
