from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.addons.tieroo.models import _wallet_commit


def add_points(card, points, description="test"):
    before = card.points
    card.env["loyalty.history"].create({"card_id": card.id, "description": description, "issued": points, "used": 0})
    if card.points == before:
        card.points = before + points


def changed(card):
    card.env["wallet.card"]._wallet_take()
    return card.sync_needed


def groups(env):
    return "group_ids" if "group_ids" in env["res.users"]._fields else "groups_id"

PUT = "odoo.addons.tieroo.models.requests.put"
DELETE = "odoo.addons.tieroo.models.requests.delete"
GET = "odoo.addons.tieroo.models.requests.get"
POST = "odoo.addons.tieroo.models.requests.post"


def ok(url="https://wallet.test/p/abc?s=sig", apple=None, google=None):
    r = MagicMock()
    r.json.return_value = {"url": url, "serial": "abc", "apple": apple, "google": google}
    r.raise_for_status.return_value = None
    return r


def platform(fail=()):
    def put(url, json=None, headers=None, timeout=None):
        if json is None or "customers" not in json:
            return ok()
        r = MagicMock()
        r.raise_for_status.return_value = None
        r.json.return_value = {"closed": json["close"], "results": [
            {"ref": c["ref"], "error": "invalid"} if c["ref"] in fail else
            {"ref": c["ref"], "serial": c["ref"], "url": f"https://wallet.test/p/{c['ref']}?s=sig", "apple": None, "google": None, "changed": True}
            for c in json["customers"]]}
        return r
    return put


@tagged("post_install", "-at_install")
class TestWalletCard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.write({"wallet_api_url": "https://wallet.test/", "wallet_api_key": "wk_testkey"})
        cls.program = cls.env["loyalty.program"].create({
            "name": "Kohviklubi", "program_type": "loyalty", "trigger": "auto", "applies_on": "both",
            "rule_ids": [(0, 0, {"reward_point_mode": "money", "reward_point_amount": 1})],
            "reward_ids": [
                (0, 0, {"reward_type": "discount", "discount": 10, "required_points": 100, "description": "Tasuta kohv"}),
                (0, 0, {"reward_type": "discount", "discount": 20, "required_points": 300, "description": "Kook"}),
            ],
        })
        cls.program.wallet_card = True
        cls.mari = cls.env["res.partner"].create({"name": "Mari Maasikas", "email": "mari@example.ee"})
        cls.mari_card = cls.join(cls.mari, 60)

    @classmethod
    def join(cls, partner, points):
        card = cls.env["loyalty.card"].create({"program_id": cls.program.id, "partner_id": partner.id})
        add_points(card, points)
        return card

    def mails_to(self, partner):
        return self.env["mail.mail"].sudo().search([("recipient_ids", "in", partner.ids), ("subject", "like", "Your loyalty card")])

    def auto_send(self, on=True):
        self.program.wallet_auto_send = on
        self.program.wallet_send_after = 0


    def test_the_scheduled_job_commits_on_every_odoo_version(self):
        cron = self.env.ref("tieroo.cron_wallet_sync")
        progress = self.env["ir.cron.progress"].sudo().create({"cron_id": cron.id, "remaining": 5, "done": 0})
        env = self.env(context=dict(self.env.context, ir_cron_progress_id=progress.id, cron_id=cron.id))
        with patch.object(self.env.cr, "commit") as commit:
            self.assertGreater(_wallet_commit(env, 2), 0)
        commit.assert_called()

    def test_card_shows_points_and_next_reward(self):
        with patch(PUT, return_value=ok()) as put:
            self.mari._wallet_create_card()
        self.assertRegex(self.mari.barcode, r"^042\d{13}$")
        url, = put.call_args.args
        self.assertEqual(url, f"https://wallet.test/sync/v1/customers/{self.mari.id}")
        self.assertEqual(put.call_args.kwargs["headers"]["Authorization"], "Bearer wk_testkey")
        self.assertRegex(put.call_args.kwargs["headers"]["X-Tieroo-Instance"], rf"^[0-9a-f-]{{36}}:{self.env.company.id}$")
        coffee, cake = self.program.reward_ids.sorted("required_points")
        en = lambda text: {"en_US": text}
        self.assertEqual(put.call_args.kwargs["json"], {
            "barcode": self.mari.barcode,
            "name": "Mari Maasikas",
            "company": None,
            "level": None,
            "points": 60.0,
            "next": {"kind": "reward", "id": str(coffee.id), "name": "Tasuta kohv", "missing": 40.0},
            "keep": None,
            "currency": None,
            "expires": None,
            "expiring": None,
            "lastSale": None,
            "rewards": [{"id": str(coffee.id), "name": "Tasuta kohv", "points": 100.0}, {"id": str(cake.id), "name": "Kook", "points": 300.0}],
            "lang": self.mari.lang,
            "texts": {
                "program": en("Kohviklubi"), "points": en(self.program.portal_point_name),
                f"reward:{coffee.id}": en("Tasuta kohv"), f"reward:{cake.id}": en("Kook"),
            },
            "places": [], "shops": [],
        })
        self.assertEqual(self.mari._wallet_card().url, "https://wallet.test/p/abc?s=sig")

    def test_card_lists_the_points_to_expire_by_date(self):
        if not hasattr(self.env["loyalty.history"], "_get_points_left_per_award"):
            self.skipTest("this Odoo keeps no expiry per award")
        soon = fields.Date.today() + timedelta(days=10)
        self.env["loyalty.history"].create({"card_id": self.mari_card.id, "description": "kampaania", "issued": 40, "used": 0, "expiration_date": soon})
        self.env["loyalty.history"].create({"card_id": self.mari_card.id, "description": "hiljem", "issued": 5, "used": 0,
                                            "expiration_date": soon + timedelta(days=30)})
        self.env["loyalty.history"].create({"card_id": self.mari_card.id, "description": "sama päev", "issued": 2, "used": 0, "expiration_date": soon})
        self.assertEqual(self.mari._wallet_payload()["expiring"], [
            {"points": 42.0, "date": soon.isoformat()}, {"points": 5.0, "date": (soon + timedelta(days=30)).isoformat()}])

    def test_empty_platform_url_means_ours(self):
        self.env.company.wallet_api_url = False
        with patch(PUT, return_value=ok()) as put:
            self.mari._wallet_create_card()
        self.assertTrue(put.call_args.args[0].startswith("https://app.tieroo.com/sync/v1/"))

    def test_points_change_updates_the_card(self):
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        add_points(self.mari_card, 50, "ost")
        self.assertTrue(changed(self.mari._wallet_card()))
        with patch(PUT, return_value=ok()) as put:
            self.env["wallet.card"]._cron_sync()
        payload = put.call_args.kwargs["json"]
        self.assertEqual(payload["points"], 110)
        self.assertEqual(payload["next"], {"kind": "reward", "id": str(self.program.reward_ids.sorted("required_points")[1].id), "name": "Kook", "missing": 190.0})
        self.assertFalse(self.mari._wallet_card().sync_needed)

    def test_a_points_change_goes_out_straight_after_the_save(self):
        from contextlib import nullcontext
        from odoo.addons.tieroo.models import _wallet_push_soon
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        add_points(self.mari_card, 50, "ost")
        card = self.mari._wallet_card()
        self.assertIn(card.id, self.env.cr.postcommit.data["tieroo.push"])
        registry = MagicMock()
        with patch("odoo.addons.tieroo.models.threading.Thread") as thread, patch("odoo.modules.module.current_test", None):
            self.env.cr.postcommit.run()
        _target, (reg, uid, ids) = thread.call_args.kwargs["target"], thread.call_args.kwargs["args"]
        self.assertEqual(ids, {card.id})
        registry.cursor = lambda: nullcontext(self.env.cr)
        with patch(PUT, return_value=ok()) as put:
            _wallet_push_soon(registry, uid, ids)
        self.assertEqual(put.call_args.kwargs["json"]["points"], 110)
        self.assertFalse(card.sync_needed)

    def test_only_the_chosen_programme_is_shown(self):
        other = self.env["loyalty.program"].create({
            "name": "Muu", "program_type": "loyalty", "trigger": "auto", "applies_on": "both",
            "rule_ids": [(0, 0, {"reward_point_mode": "money", "reward_point_amount": 1})],
        })
        card = self.env["loyalty.card"].create({"program_id": other.id, "partner_id": self.mari.id})
        add_points(card, 999, "t")
        self.assertEqual(self.mari._wallet_payload()["points"], 60)

    def test_only_a_ticked_programme_is_on_the_card(self):
        self.program.wallet_card = False
        self.assertIsNone(self.mari._wallet_payload()["points"])
        self.assertFalse(self.env["res.partner"]._wallet_issue_candidates(10))

    def test_one_wallet_programme_per_type_and_company(self):
        def loyalty(**vals):
            return self.env["loyalty.program"].create({"name": "Teine", "program_type": "loyalty", "trigger": "auto", "applies_on": "both", **vals})
        with self.assertRaises(ValidationError):
            loyalty(wallet_card=True)
        other_company = self.env["res.company"].create({"name": "Teine OÜ"})
        loyalty(wallet_card=True, company_id=other_company.id)

    def test_only_administrators_can_open_the_designer(self):
        cashier = self.env["res.users"].create({"name": "Kassa", "login": "kassa", groups(self.env): [(6, 0, [self.env.ref("point_of_sale.group_pos_user").id])]})
        with self.assertRaises(AccessError):
            self.program.with_user(cashier).action_wallet_design()

    def test_archived_programme_cannot_come_back_as_a_second_one(self):
        self.program.action_archive()
        self.env["loyalty.program"].create({"name": "Uus", "program_type": "loyalty", "trigger": "auto", "applies_on": "both", "wallet_card": True})
        with self.assertRaises(ValidationError):
            self.program.action_unarchive()

    def test_programme_form_opens_the_designer(self):
        r = MagicMock(status_code=200)
        r.json.return_value = {"url": "https://wallet.test/design/open?t=abc"}
        with patch(POST, return_value=r) as post:
            action = self.program.action_wallet_design()
        self.assertEqual(action["url"], "https://wallet.test/design/open?t=abc")
        self.assertEqual(post.call_args.kwargs["json"]["texts"]["program"]["en_US"], "Kohviklubi")


    def test_nothing_is_sent_while_automatic_sending_is_off(self):
        with patch(PUT, return_value=ok()) as put:
            self.env["wallet.card"]._cron_sync()
        put.assert_not_called()
        self.assertEqual(self.mari.wallet_card_state, "none")
        self.assertFalse(self.mails_to(self.mari))

    def test_loyalty_members_with_email_get_the_card_by_email_once(self):
        self.auto_send()
        no_mail = self.env["res.partner"].create({"name": "Ilma meilita"})
        self.join(no_mail, 10)
        outsider = self.env["res.partner"].create({"name": "Pole liige", "email": "x@example.ee"})
        with patch(PUT, return_value=ok()):
            self.env["wallet.card"]._cron_sync()
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(self.mari.wallet_card_state, "active")
        self.assertEqual(no_mail.wallet_card_state, "none")
        self.assertEqual(outsider.wallet_card_state, "none")
        mail = self.mails_to(self.mari)
        self.assertEqual(len(mail), 1)
        self.assertIn("https://wallet.test/p/abc?s=sig", mail.body_html)
        self.assertIn("emailed to mari@example.ee", self.mari.message_ids[0].body)
        self.assertNotIn("wallet.test/p/", self.mari.message_ids[0].body)

    def test_blacklisted_member_gets_no_card_by_itself_but_a_resend_still_goes(self):
        self.auto_send()
        self.env["mail.blacklist"].sudo()._add("mari@example.ee")
        with patch(PUT, return_value=ok()):
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(self.mari.wallet_card_state, "none")
        self.assertFalse(self.mails_to(self.mari))
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
            self.mari.action_wallet_resend()
        self.assertEqual(len(self.mails_to(self.mari)), 1)

    def test_switching_on_sends_only_to_new_members_until_the_button(self):
        self.program.wallet_auto_send = True
        newcomer = self.env["res.partner"].create({"name": "Uus", "email": "uus@example.ee"})
        self.join(newcomer, 5)
        with patch(PUT, return_value=ok()):
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(self.mari.wallet_card_state, "none")
        self.assertEqual(newcomer.wallet_card_state, "active")
        self.program.invalidate_recordset(["wallet_existing_count"])
        self.assertEqual(self.program.wallet_existing_count, 1)
        account = MagicMock(status_code=200, json=lambda: {"limit": 100, "billableCards": 1})
        with patch(GET, return_value=account), patch(PUT, return_value=ok()):
            self.program.action_wallet_send_existing()
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(self.mari.wallet_card_state, "active")
        self.assertEqual(len(self.mails_to(self.mari)), 1)

    def test_sending_to_existing_members_must_fit_the_plan(self):
        self.program.wallet_auto_send = True
        full = MagicMock(status_code=200, json=lambda: {"limit": 100, "billableCards": 100})
        with patch(GET, return_value=full), self.assertRaises(UserError):
            self.program.action_wallet_send_existing()
        self.assertTrue(self.program.wallet_send_after)

    def test_new_member_gets_a_card(self):
        self.auto_send()
        jaan = self.env["res.partner"].create({"name": "Jaan", "email": "jaan@example.ee"})
        with patch(PUT, return_value=ok()):
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(jaan.wallet_card_state, "none")
        self.join(jaan, 0)
        with patch(PUT, return_value=ok("https://wallet.test/p/jaan?s=x")):
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(jaan.wallet_card_state, "active")


    def test_unreachable_platform_keeps_change_for_retry(self):
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        self.mari.name = "Mari Mets"
        with patch(PUT, side_effect=requests.ConnectionError("down")):
            self.env["wallet.card"]._cron_sync()
        self.assertTrue(self.mari._wallet_card().sync_needed)

    def test_job_re_triggers_only_when_cards_get_through(self):
        self.auto_send()
        cron = self.env.ref("tieroo.cron_wallet_sync")
        triggers = lambda: self.env["ir.cron.trigger"].search_count([("cron_id", "=", cron.id)])
        before = triggers()
        with patch(PUT, side_effect=requests.ConnectionError("down")):
            self.env["res.partner"]._wallet_issue_new_cards(limit=1)
        self.assertEqual(triggers(), before)
        self.assertFalse(self.mails_to(self.mari))
        jaan = self.env["res.partner"].create({"name": "Jaan Jõgi", "email": "jaan@example.ee"})
        self.join(jaan, 10)
        with patch(PUT, side_effect=platform()):
            self.env["res.partner"]._wallet_issue_new_cards(limit=1)
        self.assertGreater(triggers(), before)
        self.assertEqual(len(self.mails_to(self.mari)), 1)
        self.assertEqual(len(self.mails_to(jaan)), 1)
        self.mari.name = "Mari Mets"
        self.env.company.wallet_api_key = False
        self.assertEqual(self.mari._wallet_card()._push(raise_errors=False), 0)

    def test_many_cards_go_in_batches(self):
        people = self.env["res.partner"].create([{"name": f"Klient {i}", "email": f"k{i}@example.ee"} for i in range(450)])
        for p in people:
            self.join(p, 5)
        self.auto_send()
        with patch(PUT, side_effect=platform(fail={str(people[3].id)})) as put:
            self.env["wallet.card"]._cron_sync()
        batches = [c for c in put.call_args_list if c.args[0] == "https://wallet.test/sync/v1/customers"]
        self.assertEqual([len(c.kwargs["json"]["customers"]) for c in batches], [200, 200, 51])
        self.assertEqual(batches[0].kwargs["json"]["texts"]["program"]["en_US"], "Kohviklubi")
        self.assertNotIn("texts", batches[0].kwargs["json"]["customers"][0])
        cards = self.env["wallet.card"].search([("partner_id", "in", people.ids)])
        self.assertEqual(len(cards), 450)
        self.assertFalse(cards.filtered("sync_needed"))
        self.assertEqual(cards.filtered(lambda c: c.partner_id == people[0]).sudo().url, f"https://wallet.test/p/{people[0].id}?s=sig")
        self.assertEqual(len(self.mails_to(people[0])), 1)
        self.assertFalse(self.mails_to(people[3]))
        for card in self.env["loyalty.card"].search([("partner_id", "in", people[:250].ids)]):
            add_points(card, 1, "ost")
        with patch(PUT, side_effect=platform()) as put:
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(put.call_args_list[0].args[0], f"https://wallet.test/sync/v1/customers/{people[3].id}")
        self.assertEqual([len(c.kwargs["json"]["customers"]) for c in put.call_args_list[1:]], [200, 49])
        self.assertEqual(len(self.mails_to(people[3])), 1)

    def test_archived_customer_card_is_closed(self):
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        self.mari.active = False
        self.assertEqual(self.mari.wallet_card_state, "closed")
        with patch(DELETE, return_value=ok()) as delete:
            self.env["wallet.card"]._cron_sync()
        delete.assert_called_once()

    def test_deleted_contact_erases_the_card_on_the_platform(self):
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        self.mari_card.unlink()
        ref = str(self.mari.id)
        self.mari.unlink()
        Erasure = self.env["wallet.erasure"].sudo()
        with patch(DELETE, side_effect=requests.ConnectionError("down")):
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(Erasure.search([]).ref, ref)
        with patch(DELETE, return_value=ok()) as delete:
            self.env["wallet.card"]._cron_sync()
        self.assertEqual(delete.call_args.kwargs["params"], {"erase": 1})
        self.assertTrue(delete.call_args.args[0].endswith(f"/sync/v1/customers/{ref}"))
        self.assertFalse(Erasure.search([]))

    def test_erasure_on_a_closed_account_is_done(self):
        Erasure = self.env["wallet.erasure"].sudo()
        Erasure.create({"company_id": self.env.company.id, "ref": "999"})
        closed = MagicMock(status_code=401)
        with patch(DELETE, return_value=closed):
            self.env["wallet.card"]._cron_sync()
        self.assertFalse(Erasure.search([]))

    def test_uninstalling_closes_every_card(self):
        from odoo.addons.tieroo import uninstall_hook
        with patch(DELETE, return_value=ok()) as delete:
            uninstall_hook(self.env)
        self.assertTrue(delete.call_args.args[0].endswith("/sync/v1/customers"))


    def test_resend_on_request(self):
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
            self.mari.email = "mari.uus@example.ee"
            action = self.mari.action_wallet_resend()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(len(self.mails_to(self.mari)), 1)
        self.assertIn("emailed again to mari.uus@example.ee", self.mari.message_ids[0].body)

    def test_resend_needs_an_active_card(self):
        with self.assertRaises(UserError):
            self.mari.action_wallet_resend()
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        self.mari.active = False
        with self.assertRaises(UserError):
            self.mari.action_wallet_resend()

    def test_clerk_can_neither_resend_nor_see_the_link(self):
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        clerk = self.env["res.users"].create({
            "name": "Kassapidaja", "login": "kassa@example.ee",
            groups(self.env): [(6, 0, [self.env.ref("base.group_partner_manager").id, self.env.ref("point_of_sale.group_pos_manager").id])],
        })
        as_clerk = self.mari.with_user(clerk)
        self.assertEqual(as_clerk.wallet_card_state, "active")
        with self.assertRaises(AccessError):
            as_clerk.wallet_card_ids.read(["url"])
        as_clerk.email = "kassa@example.ee"
        with patch(PUT, return_value=ok()) as put, self.assertRaises(AccessError):
            as_clerk.action_wallet_resend()
        put.assert_not_called()
        self.assertFalse(self.mails_to(self.mari))

    def test_employees_cannot_read_the_card_email(self):
        links = ok(apple="https://wallet.test/p/abc/apple.pkpass?s=sig", google="https://wallet.test/p/abc/google?s=sig")
        with patch(PUT, return_value=links):
            self.mari._wallet_create_card()._send_email()
        self.assertEqual(len(self.mails_to(self.mari)), 1)
        employee = self.env["res.users"].create({
            "name": "Töötaja", "login": "tootaja@example.ee", groups(self.env): [(6, 0, [self.env.ref("base.group_user").id])],
        })
        env = self.env(user=employee)
        self.assertFalse(env["mail.message"].search([("body", "ilike", "wallet.test/p/")]))
        self.assertFalse(env["mail.message"].search([("model", "=", "wallet.card")]))
        self.assertFalse(env["ir.attachment"].search([("name", "=", "loyalty-card.pkpass")]))
        with self.assertRaises(AccessError):
            env["mail.mail"].search([])

    def test_pos_manager_creates_programmes(self):
        manager = self.env["res.users"].create({
            "name": "Juhataja", "login": "juhataja@example.ee",
            groups(self.env): [(6, 0, [self.env.ref("base.group_user").id, self.env.ref("point_of_sale.group_pos_manager").id])],
        })
        promo = self.env["loyalty.program"].with_user(manager).create({"name": "Sügiskampaania", "program_type": "promotion"})
        promo.name = "Talvekampaania"
        self.assertFalse(promo.sudo().wallet_card)


    def test_email_has_wallet_buttons_and_no_pass_file(self):
        links = ok(apple="https://wallet.test/p/abc/apple.pkpass?s=sig", google="https://wallet.test/p/abc/google?s=sig")
        with patch(PUT, return_value=links), patch(GET) as get:
            self.mari._wallet_create_card()
            self.mari.action_wallet_resend()
        get.assert_not_called()
        mail = self.mails_to(self.mari)
        self.assertIn("Add to Apple Wallet", mail.body_html)
        self.assertIn("/badge/apple.png?lang=", mail.body_html)
        self.assertIn("https://wallet.test/p/abc/google?s=sig", mail.body_html)
        self.assertIn("48 hours", mail.body_html)
        self.assertNotIn("Open loyalty card", mail.body_html)
        self.assertFalse(mail.attachment_ids)
        self.assertFalse(mail.model)
        try:
            mail._postprocess_sent_message(success_pids=self.mari, success_emails=[])
        except TypeError:
            mail._postprocess_sent_message(success_pids=self.mari)
        self.assertFalse(mail.exists())

    def test_email_falls_back_to_the_card_page_without_wallets(self):
        with patch(PUT, return_value=ok()), patch(GET) as get:
            self.mari._wallet_create_card()
            self.mari.action_wallet_resend()
        get.assert_not_called()
        mail = self.mails_to(self.mari)
        self.assertIn("Open loyalty card", mail.body_html)
        self.assertFalse(mail.attachment_ids)


    def test_design_button_opens_the_designer_with_a_one_time_link(self):
        r = MagicMock(status_code=200)
        r.json.return_value = {"url": "https://wallet.test/design/open?t=abc"}
        with patch(POST, return_value=r) as post:
            action = self.env["res.config.settings"].create({}).action_wallet_design()
        self.assertEqual(action["url"], "https://wallet.test/design/open?t=abc")
        self.assertEqual(post.call_args.args[0], "https://wallet.test/sync/v1/design-session")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer wk_testkey")
        sent = post.call_args.kwargs["json"]
        self.assertEqual((sent["company"], sent["texts"]["program"]["en_US"], sent["levels"]), (self.env.company.name, "Kohviklubi", []))

    def test_designer_refused_says_why(self):
        for status, data, text in ((403, {"error": "suspended"}, "suspended"), (401, {"error": "unauthorized"}, "closed")):
            r = MagicMock(status_code=status)
            r.json.return_value = data
            with patch(POST, return_value=r), self.assertRaises(UserError) as e:
                self.env["res.config.settings"].create({}).action_wallet_design()
            self.assertIn(text, str(e.exception))
            self.assertNotIn("could not be reached", str(e.exception))

    def test_language_change_and_new_reward_update_the_card(self):
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        self.mari.lang = "en_US"
        self.assertTrue(changed(self.mari._wallet_card()))
        self.mari._wallet_card().sync_needed = False
        self.program.reward_ids = [(0, 0, {"reward_type": "discount", "discount": 5, "required_points": 50, "description": "Kringel"})]
        self.assertTrue(changed(self.mari._wallet_card()))

    def test_renaming_in_odoo_updates_the_card(self):
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        card = self.mari._wallet_card()
        for record, vals in ((self.program, {"name": "Püsikliendid"}), (self.program, {"portal_point_name": "Templid"}),
                             (self.program.reward_ids[:1], {"description": "Tasuta kohv"}),
                             (self.program.reward_ids.filtered(lambda r: r.reward_type == "discount")[:1], {"discount": 15})):
            changed(card)
            card.sync_needed = False
            record.write(vals)
            self.assertTrue(changed(card), vals)


    def test_shops_on_the_card_city_first_then_the_merchants_order(self):
        Partner, Shop = self.env["res.partner"], self.env["wallet.shop"]
        big = Partner.create({"name": "Tallinn Kristiine", "city": "Tallinn", "partner_latitude": 59.42, "partner_longitude": 24.70})
        tartu = Partner.create({"name": "Tartu Lõunakeskus", "city": "Tartu", "partner_latitude": 58.36, "partner_longitude": 26.68})
        nowhere = Partner.create({"name": "Pärnu", "city": "Pärnu"})
        Shop.create([{"partner_id": big.id, "sequence": 1}, {"partner_id": tartu.id, "sequence": 2}, {"partner_id": nowhere.id, "sequence": 3}])
        self.mari.city = "Tartu"
        self.assertEqual(self.mari._wallet_payload()["places"], [])
        self.env.company.wallet_shops_on = True
        payload = self.mari._wallet_payload()
        self.assertEqual(payload["places"], [{"lat": 59.42, "lon": 24.70}, {"lat": 58.36, "lon": 26.68}])
        self.assertEqual(payload["shops"], payload["places"])
        more = Partner.create([{"name": f"Tallinn {i}", "city": "Tallinn", "partner_latitude": 59.4 + i / 100, "partner_longitude": 24.7} for i in range(10)])
        Shop.create([{"partner_id": p.id, "sequence": 5 + i} for i, p in enumerate(more)])
        Shop.search([("partner_id", "=", tartu.id)]).sequence = 30
        places = self.mari._wallet_payload()["places"]
        self.assertEqual(len(places), 10)
        self.assertEqual(places[0], {"lat": 59.42, "lon": 24.70})
        self.assertEqual(places[-1], {"lat": 58.36, "lon": 26.68})
        self.assertNotIn({"lat": 58.36, "lon": 26.68}, self.mari._wallet_payload()["shops"])
        Shop.search([("partner_id", "=", tartu.id)]).on_card = False
        self.assertNotIn({"lat": 58.36, "lon": 26.68}, self.mari._wallet_payload()["places"])
        Shop.search([("partner_id", "=", tartu.id)]).on_card = True
        nowhere.partner_latitude = 594.37
        self.assertNotIn(594.37, [p["lat"] for p in self.mari._wallet_payload()["places"]])
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        card = self.mari._wallet_card()
        changed(card)
        card.sync_needed = False
        tartu.partner_latitude = 58.37
        self.assertTrue(changed(card))
        card.sync_needed = False
        self.mari.city = "Tallinn"
        self.assertTrue(changed(card))
        settings = self.env["res.config.settings"].create({})
        self.assertEqual((settings.wallet_shops_count, settings.wallet_shops_missing), (13, 1))
        if "stock.warehouse" in self.env:
            Shop.browse().action_add_warehouses()
            addresses = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)]).partner_id
            self.assertEqual(Shop.search([("partner_id", "in", addresses.ids)]).partner_id, addresses)


    def test_archived_or_deleted_loyalty_card_closes_the_card(self):
        with patch(PUT, return_value=ok()):
            self.mari._wallet_create_card()
        card = self.mari._wallet_card()
        if "wallet_period_start" in self.mari._fields:
            self.mari.wallet_period_start = False
        self.mari_card.active = False
        card.invalidate_recordset(["state"])
        self.assertEqual(card.state, "closed")
        with patch(DELETE, return_value=ok()) as delete:
            self.env["wallet.card"]._cron_sync()
        delete.assert_called_once()
        self.mari_card.active = True
        self.assertTrue(changed(card))
        with patch(PUT, return_value=ok()) as put:
            self.env["wallet.card"]._cron_sync()
        put.assert_called_once()
        card.invalidate_recordset(["state"])
        self.assertEqual(card.state, "active")
        self.assertFalse(self.mails_to(self.mari))
        card.sync_needed = False
        self.mari_card.unlink()
        self.assertTrue(changed(card))
        card.invalidate_recordset(["state"])
        self.assertEqual(card.state, "closed")

    def test_no_card_for_an_archived_loyalty_card(self):
        self.mari_card.active = False
        self.program.wallet_auto_send = True
        self.assertFalse(self.env["res.partner"]._wallet_issue_candidates(10))
