from unittest.mock import MagicMock, patch

import requests

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

PUT = "odoo.addons.tieroo.models.requests.put"
GET = "odoo.addons.tieroo.models.requests.get"
POST = "odoo.addons.tieroo.models.requests.post"
TOKEN = "t" * 43


def resp(status=200, data=None):
    r = MagicMock(status_code=status)
    r.json.return_value = data or {}
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"{status}", response=r)
    return r


class TierooSetup:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.write({
            "wallet_api_url": "https://wallet.test/", "wallet_api_key": False, "vat": "EE123456780",
            "country_id": cls.env.ref("base.ee").id, "street": "Rüütli 1", "city": "Tartu", "zip": "51007",
        })
        cls.env.user.email = "admin@kohvik.ee"


@tagged("post_install", "-at_install")
class TestTieroo(TierooSetup, TransactionCase):

    def test_start_sends_the_company_and_opens_the_billing_page(self):
        with patch(POST, return_value=resp(200, {"url": "https://wallet.test/billing/open?t=x", "claimToken": TOKEN})) as post:
            action = self.env["res.config.settings"].create({"wallet_data_consent": True}).action_wallet_signup()
        self.assertEqual(action, {"type": "ir.actions.act_url", "url": "https://wallet.test/billing/open?t=x", "target": "self"})
        self.assertEqual(post.call_args.args[0], "https://wallet.test/onboard/v1/start")
        self.assertEqual(post.call_args.kwargs["json"], {
            "company": {"name": self.company.name, "vat": "EE123456780", "country": "EE", "email": "admin@kohvik.ee",
                        "street": "Rüütli 1", "city": "Tartu", "zip": "51007"},
            "returnUrl": f"{self.company.get_base_url()}/tieroo/return?company={self.company.id}",
            "levelsModule": self.company._wallet_levels_on(),
        })
        self.assertEqual(self.company.wallet_claim_token, TOKEN)
        self.assertFalse(self.company.wallet_api_key)

    def test_nothing_is_sent_without_consent(self):
        with patch(POST) as post, self.assertRaisesRegex(UserError, "agree to send data"):
            self.env["res.config.settings"].create({}).action_wallet_signup()
        post.assert_not_called()
        self.assertFalse(self.company.wallet_consent_date)
        with patch(POST, return_value=resp(200, {"url": "https://wallet.test/billing/open?t=x", "claimToken": TOKEN})):
            self.env["res.config.settings"].create({"wallet_data_consent": True}).action_wallet_signup()
        self.assertTrue(self.company.wallet_consent_date)

    def test_missing_vat_and_refused_vat(self):
        self.company.vat = False
        with self.assertRaisesRegex(UserError, "VAT number"):
            self.env["res.config.settings"].create({"wallet_data_consent": True}).action_wallet_signup()
        self.company.vat = "EE123456780"
        with patch(POST, return_value=resp(400, {"error": "invalid_vat"})), self.assertRaisesRegex(UserError, "VAT number was not accepted"):
            self.env["res.config.settings"].create({"wallet_data_consent": True}).action_wallet_signup()
        self.assertFalse(self.company.wallet_claim_token)

    def test_claim_saves_the_key_once_and_waits_for_stripe(self):
        self.company.wallet_claim_token = TOKEN
        with patch(POST, return_value=resp(409, {"error": "not_completed"})):
            self.assertEqual(self.company._wallet_claim(), "unpaid")
        with patch(POST, return_value=resp(409, {"error": "vat_pending"})):
            self.assertEqual(self.company._wallet_claim(), "waiting")
        self.assertEqual(self.company.wallet_claim_token, TOKEN)
        with patch(POST, return_value=resp(409, {"error": "vat_pending"})) as post, self.assertRaisesRegex(UserError, "being confirmed"):
            self.env["res.config.settings"].create({"wallet_data_consent": True}).action_wallet_signup()
        self.assertEqual(post.call_count, 1)
        with patch(POST, side_effect=[resp(404, {"error": "not_found"}), resp(200, {"url": "https://wallet.test/billing/open?t=z", "claimToken": "u" * 43})]):
            self.assertEqual(self.env["res.config.settings"].create({"wallet_data_consent": True}).action_wallet_signup()["url"], "https://wallet.test/billing/open?t=z")
        self.company.wallet_claim_token = TOKEN
        with patch(POST, return_value=resp(200, {"key": "wk_new", "plan": "free"})) as post:
            self.assertEqual(self.company._wallet_claim(), "done")
        self.assertEqual(post.call_args.args[0], "https://wallet.test/onboard/v1/claim")
        self.assertEqual(post.call_args.kwargs["json"], {"claimToken": TOKEN})
        self.assertEqual((self.company.wallet_api_key, self.company.wallet_claim_token, self.company.wallet_signup_notice), ("wk_new", False, "connected"))
        with patch(POST) as post:
            self.assertEqual(self.company._wallet_claim(), "done")
        post.assert_not_called()

    def test_only_administrators(self):
        clerk = new_test_user(self.env, login="clerk_tieroo", groups="base.group_user")
        with self.assertRaises(AccessError):
            self.company.with_user(clerk)._wallet_start()
        with self.assertRaises(AccessError):
            self.company.with_user(clerk)._wallet_claim()
        with self.assertRaises(AccessError):
            self.company.with_user(clerk)._wallet_billing()

    def connected(self, account):
        self.company.wallet_api_key = "wk_testkey"
        with patch(GET, return_value=account) as get:
            settings = self.env["res.config.settings"].create({})
            settings.wallet_plan_name
        return settings, get

    def test_account_panel(self):
        settings, get = self.connected(resp(200, {
            "plan": {"id": "free", "name": "Loyalty Cards 100", "cards": 100, "eurPerMonth": 0}, "status": "past_due",
            "billableCards": 112, "limit": 100, "overLimitSince": "2026-10-01T08:00:00.000Z", "graceUntil": "2026-10-15T08:00:00.000Z",
            "canCreate": True, "subscribed": False}))
        self.assertEqual(get.call_args.args[0], "https://wallet.test/sync/v1/account")
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer wk_testkey"})
        self.assertEqual(
            (settings.wallet_connected, settings.wallet_account_error, settings.wallet_plan_name, settings.wallet_cards_used,
             settings.wallet_cards_limit, settings.wallet_status, settings.wallet_over_limit, str(settings.wallet_grace_until),
             settings.wallet_can_create, settings.wallet_plan_extra),
            (True, False, "Loyalty Cards 100", 112, 100, "past_due", True, "2026-10-15", True, ""))
        settings, _get = self.connected(resp(403, {"error": "suspended"}))
        self.assertEqual((settings.wallet_status, settings.wallet_account_error), ("suspended", False))
        with patch(GET, side_effect=requests.Timeout("slow")):
            settings = self.env["res.config.settings"].create({})
            self.assertTrue(settings.wallet_account_error)

    def test_plan_and_billing_opens_the_page(self):
        settings, _get = self.connected(resp(200, {"plan": {"id": "cards_300", "name": "Loyalty Cards 300", "cards": 300}, "status": "active",
                                                   "billableCards": 10, "limit": 300, "canCreate": True, "subscribed": True,
                                                   "interval": "year", "levels": True}))
        self.assertEqual(settings.wallet_plan_extra, "Yearly · Customer Levels")
        with patch(POST, return_value=resp(200, {"url": "https://wallet.test/billing/open?t=y"})) as post:
            action = settings.action_wallet_billing()
        self.assertEqual(action, {"type": "ir.actions.act_url", "url": "https://wallet.test/billing/open?t=y", "target": "self"})
        self.assertEqual(post.call_args.args[0], "https://wallet.test/sync/v1/billing")
        self.assertEqual(post.call_args.kwargs["json"], {"returnUrl": f"{self.company.get_base_url()}/odoo/settings?cids={self.company.id}#tieroo",
                                                         "levelsModule": self.company._wallet_levels_on()})
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer wk_testkey"})


@tagged("post_install", "-at_install")
class TestTierooReturn(TierooSetup, HttpCase):

    def test_way_back_from_stripe(self):
        self.authenticate("admin", "admin")
        url = f"/tieroo/return?company={self.company.id}&tieroo="
        self.company.wallet_claim_token = TOKEN
        with patch(POST, return_value=resp(409, {"error": "not_completed"})):
            page = self.url_open(url + "done")
        self.assertIn("Waiting for the payment confirmation", page.text)
        self.assertIn(f'url=/tieroo/return?company={self.company.id}&amp;tieroo=done&amp;attempt=1', page.text)
        with patch(POST, return_value=resp(409, {"error": "not_completed"})):
            page = self.url_open(url + "done&attempt=20")
        self.assertNotIn('http-equiv="refresh"', page.text)
        self.assertIn("Check again", page.text)
        with patch(POST, return_value=resp(200, {"key": "wk_new", "plan": "free"})):
            page = self.url_open(url + "done", allow_redirects=False)
        self.assertEqual(page.status_code, 303)
        self.assertTrue(page.headers["Location"].endswith(f"/odoo/settings?cids={self.company.id}#tieroo"))
        self.env.invalidate_all()
        self.assertEqual((self.company.wallet_api_key, self.company.wallet_claim_token), ("wk_new", False))

    def test_cancel_and_errors(self):
        self.authenticate("admin", "admin")
        url = f"/tieroo/return?company={self.company.id}&tieroo="
        page = self.url_open(url + "cancel", allow_redirects=False)
        self.assertEqual(page.status_code, 303)
        self.env.invalidate_all()
        self.assertEqual(self.company.wallet_signup_notice, "cancelled")
        self.company.wallet_claim_token = TOKEN
        with patch(POST, return_value=resp(410, {"error": "already_claimed"})):
            page = self.url_open(url + "done")
        self.assertIn("already been used", page.text)

    def test_non_admin_refused(self):
        new_test_user(self.env, login="clerk_tieroo", groups="base.group_user")
        self.authenticate("clerk_tieroo", "clerk_tieroo")
        self.company.wallet_claim_token = TOKEN
        with patch(POST) as post:
            page = self.url_open(f"/tieroo/return?company={self.company.id}&tieroo=done")
        self.assertEqual(page.status_code, 403)
        post.assert_not_called()


@tagged("post_install", "-at_install")
class TestPlanLimit(TransactionCase):

    def test_plan_limit_keeps_the_change_and_the_job_does_not_loop(self):
        self.env.company.write({"wallet_api_url": "https://wallet.test/", "wallet_api_key": "wk_testkey"})
        people = self.env["res.partner"].create([{"name": f"Klient {i}", "email": f"k{i}@example.ee"} for i in range(3)])
        cards = self.env["wallet.card"].create([{"partner_id": p.id, "sync_needed": True} for p in people])
        cron = self.env.ref("tieroo.cron_wallet_sync")
        triggers = lambda: self.env["ir.cron.trigger"].search_count([("cron_id", "=", cron.id)])
        before = triggers()
        limited = resp(200, {"closed": [], "results": [{"ref": str(p.id), "error": "plan_limit"} for p in people]})
        with patch(PUT, return_value=limited) as put:
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(put.call_count, 1)
        self.assertEqual(triggers(), before)
        self.assertTrue(all(cards.mapped("sync_needed")))
        with patch(PUT, return_value=resp(402, {"error": "plan_limit"})), self.assertRaisesRegex(UserError, "card limit"):
            cards[0]._push()
        self.assertTrue(cards[0].sync_needed)
