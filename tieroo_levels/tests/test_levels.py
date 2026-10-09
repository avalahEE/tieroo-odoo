from datetime import datetime
from unittest.mock import patch

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.tieroo_levels.models import _pricelists_enabled


def groups(env):
    return "group_ids" if "group_ids" in env["res.users"]._fields else "groups_id"

@tagged("post_install", "-at_install")
class TestWalletLevels(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo()[groups(cls.env)] += cls.env.ref("point_of_sale.group_pos_manager") | cls.env.ref("tieroo_levels.group_levels_admin")
        admin = cls.env(su=True)
        Tag = admin["res.partner.category"]
        Pricelist = admin["product.pricelist"]
        cls.silver_pl = Pricelist.create({"name": "Silver -5%"})
        cls.gold_pl = Pricelist.create({"name": "Gold -10%"})
        cls.b2b_pl = Pricelist.create({"name": "B2B -7%"})
        bronze, silver, gold = (Tag.create({"name": n}) for n in ("Bronze", "Silver", "Gold"))
        cls.bronze, cls.silver, cls.gold, cls.b2b_silver, cls.b2b_gold = admin["wallet.level"].create([
            {"name": "Bronze", "tag_id": bronze.id, "min_spend": 0},
            {"name": "Silver", "tag_id": silver.id, "min_spend": 300, "pricelist_id": cls.silver_pl.id},
            {"name": "Gold", "tag_id": gold.id, "min_spend": 1000, "pricelist_id": cls.gold_pl.id},
            {"name": "Silver", "customer_type": "b2b", "tag_id": silver.id, "min_spend": 5000, "pricelist_id": cls.b2b_pl.id},
            {"name": "Gold", "customer_type": "b2b", "tag_id": gold.id, "min_spend": 20000, "pricelist_id": cls.gold_pl.id},
        ])
        cls.customer = cls.env["res.partner"].create({"name": "Mari Maasikas"})
        cls.customer.wallet_excluded = False
        cls.today = fields.Date.today()

    def invoice(self, amount, days_ago=0, partner=None, refund=False):
        return self.init_invoice(
            "out_refund" if refund else "out_invoice",
            partner=partner or self.customer,
            invoice_date=self.today - relativedelta(days=days_ago),
            amounts=[amount],
            taxes=[],
            post=True,
        )

    def level_tags(self, partner):
        return (partner.category_id & (self.bronze | self.silver | self.gold).tag_id).mapped("name")

    def run_nightly(self, **delta):
        self.env["res.partner"]._cron_wallet_levels(now=datetime.now() + relativedelta(**delta))


    def test_joining_starts_today_at_the_lowest_level_and_earlier_purchases_do_not_count(self):
        anna = self.env["res.partner"].create({"name": "Anna"})
        self.invoice(1200, days_ago=200, partner=anna)
        self.assertFalse(anna.wallet_level_id)
        self.assertTrue(anna.wallet_excluded)
        anna.wallet_excluded = False
        self.assertEqual(anna.wallet_period_start, self.today)
        self.assertEqual(anna.wallet_period_end, self.today + relativedelta(months=12, days=-1))
        self.assertEqual(anna.wallet_level_id, self.bronze)
        self.assertEqual(anna.wallet_period_spend, 0)
        self.assertIn("Joined Customer Levels", anna.message_ids[0].body + anna.message_ids[1].body)

    def test_nobody_joins_by_buying_or_by_the_nightly_job(self):
        lonely = self.env["res.partner"].create({"name": "Never joined"})
        self.invoice(5000, partner=lonely)
        self.run_nightly()
        self.assertFalse(lonely.wallet_level_id)
        self.assertFalse(lonely.wallet_period_start)

    def test_upgrade_immediately_during_the_period(self):
        self.invoice(350)
        self.assertEqual(self.customer.wallet_level_id, self.silver)
        self.assertEqual(self.level_tags(self.customer), ["Silver"])
        self.assertEqual(self.customer.property_product_pricelist, self.silver_pl)
        self.invoice(700)
        self.assertEqual(self.customer.wallet_level_id, self.gold)
        self.assertEqual(self.level_tags(self.customer), ["Gold"])
        self.assertEqual(self.customer.property_product_pricelist, self.gold_pl)
        self.assertEqual(self.customer.wallet_period_spend, 1050)

    def test_never_drops_during_the_period(self):
        self.invoice(1200)
        self.invoice(1000, refund=True)
        self.run_nightly(months=6)
        self.run_nightly(months=11, days=27)
        self.assertEqual(self.customer.wallet_level_id, self.gold)

    def test_period_end_sets_the_level_from_that_periods_spend(self):
        self.invoice(1200)
        self.run_nightly(months=12, days=1)
        self.assertEqual(self.customer.wallet_level_id, self.gold)
        self.assertEqual(self.customer.wallet_period_start, self.today + relativedelta(months=12))
        self.run_nightly(months=24, days=1)
        self.assertEqual(self.customer.wallet_level_id, self.bronze)
        self.assertIn("Gold → Bronze", self.customer.message_ids[0].body)

    def test_missed_nights_close_every_ended_period(self):
        self.invoice(1200)
        self.run_nightly(months=36, days=1)
        self.assertEqual(self.customer.wallet_level_id, self.bronze)
        self.assertEqual(self.customer.wallet_period_start, self.today + relativedelta(months=36))

    def test_progress_next_level_and_keeping_the_level(self):
        self.invoice(350)
        last_day = self.customer.wallet_period_start + relativedelta(months=12, days=-1)
        progress = self.customer._wallet_progress()
        self.assertEqual(progress["next"], {"id": str(self.gold.id), "name": "Gold", "missing": 650})
        self.assertEqual(progress["keep"], {"missing": 0.0, "until": (last_day + relativedelta(months=12)).isoformat()})
        self.run_nightly(months=12, days=1)
        last_day = self.customer.wallet_period_start + relativedelta(months=12, days=-1)
        self.assertEqual(self.customer._wallet_progress()["keep"], {"missing": 300, "until": last_day.isoformat()})


    def test_a_customer_outside_the_levels_is_left_alone(self):
        vip_pl = self.env(su=True)["product.pricelist"].create({"name": "VIP Mari -20%"})
        vip = self.env["res.partner"].create({"name": "VIP", "property_product_pricelist": vip_pl.id})
        self.invoice(5000, partner=vip)
        self.run_nightly(months=13)
        self.assertFalse(vip.wallet_level_id)
        self.assertEqual(vip.property_product_pricelist, vip_pl)

    def test_leaving_removes_the_level_its_tag_and_pricelist_and_coming_back_starts_again(self):
        anna = self.env["res.partner"].create({"name": "Anna", "wallet_excluded": False,
                                               "wallet_period_start": self.today - relativedelta(days=30)})
        self.invoice(1200, days_ago=10, partner=anna)
        self.assertEqual(anna.property_product_pricelist, self.gold_pl)
        anna.wallet_excluded = True
        self.assertFalse(anna.wallet_level_id)
        self.assertEqual(self.level_tags(anna), [])
        self.assertNotIn(anna.property_product_pricelist, self.silver_pl | self.gold_pl)
        self.assertFalse(anna.wallet_period_start)
        anna.wallet_excluded = False
        self.assertEqual(anna.wallet_level_id, self.bronze)
        self.assertEqual(anna.wallet_period_start, self.today)


    def test_import_with_opening_spend_and_level_holds_the_level_to_the_period_end(self):
        start = self.today - relativedelta(months=2)
        kai = self.env["res.partner"].create({"name": "Kai", "wallet_excluded": False, "wallet_period_start": start,
                                              "wallet_opening_spend": 150, "wallet_level_id": self.silver.id})
        self.assertEqual(kai.wallet_level_id, self.silver)
        self.assertEqual(kai.wallet_period_spend, 150)
        self.assertEqual(kai.property_product_pricelist, self.silver_pl)
        self.invoice(900, partner=kai)
        self.assertEqual(kai.wallet_level_id, self.gold)
        self.run_nightly(months=10, days=1)
        self.assertEqual(kai.wallet_level_id, self.gold)
        self.assertEqual(kai.wallet_opening_spend, 0)
        self.assertIn("opening spend", kai.message_ids[0].body + kai.message_ids[1].body)

    def test_an_import_by_level_name_takes_the_customer_s_kind(self):
        kai = self.env["res.partner"].create({"name": "Kai", "wallet_excluded": False, "wallet_level_id": self.b2b_silver.id})
        self.assertEqual(kai.wallet_level_id, self.silver)

    def test_imported_period_start_must_be_within_the_last_12_months(self):
        for day in (self.today + relativedelta(days=1), self.today - relativedelta(months=12)):
            with self.assertRaises(UserError):
                self.env["res.partner"].create({"name": "X", "wallet_excluded": False, "wallet_period_start": day})

    def test_a_level_needs_a_member_of_its_kind(self):
        outside = self.env["res.partner"].create({"name": "Outside"})
        with self.assertRaises(UserError):
            outside.wallet_level_id = self.gold
        platinum = self.env(su=True)["wallet.level"].create({"name": "Platinum", "customer_type": "b2b", "min_spend": 50000,
                                                             "tag_id": self.env(su=True)["res.partner.category"].create({"name": "Platinum"}).id})
        with self.assertRaises(UserError):
            self.customer.wallet_level_id = platinum

    def test_level_change_keeps_other_tags(self):
        newsletter = self.env(su=True)["res.partner.category"].create({"name": "Newsletter"})
        self.customer.category_id = [(4, newsletter.id)]
        self.invoice(350)
        self.invoice(700)
        self.assertEqual(sorted(self.customer.category_id.mapped("name")), ["Gold", "Newsletter"])


    def test_b2b_level_counts_the_whole_company(self):
        Partner = self.env["res.partner"]
        company = Partner.create({"name": "Mööbel OÜ", "vat": "EE100931558"})
        jaan = Partner.create({"name": "Jaan", "parent_id": company.id})
        kati = Partner.create({"name": "Kati", "parent_id": company.id})
        company.wallet_excluded = False

        self.invoice(3000, partner=jaan)
        self.assertEqual(company.wallet_period_start, self.today)
        self.assertTrue(jaan.wallet_excluded)
        self.assertFalse(company.wallet_level_id)
        self.assertFalse(jaan.wallet_level_id)

        self.invoice(2500, partner=kati)
        self.assertEqual(company.wallet_level_id, self.b2b_silver)
        self.assertEqual(self.level_tags(company), ["Silver"])
        self.assertEqual(company.property_product_pricelist, self.b2b_pl)
        self.assertFalse(jaan.wallet_level_id | kati.wallet_level_id)
        self.assertEqual(jaan.property_product_pricelist, self.b2b_pl)
        self.assertEqual(kati.wallet_period_spend, 5500)
        self.assertEqual(kati._wallet_progress()["next"], {"id": str(self.b2b_gold.id), "name": "Gold", "missing": 14500})

    def test_company_with_only_a_registry_code_is_b2b(self):
        if "additional_identifiers" not in self.env["res.partner"]._fields:
            self.skipTest("this Odoo has no additional identifiers")
        firm = self.env["res.partner"].create({"name": "Väike OÜ", "additional_identifiers": {"EE_EN": "11234563"}, "wallet_excluded": False})
        self.assertFalse(firm._wallet_is_b2c())
        self.invoice(1500, partner=firm)
        self.assertFalse(firm.wallet_level_id)
        self.invoice(4000, partner=firm)
        self.assertEqual(firm.wallet_level_id, self.b2b_silver)
        self.assertTrue(self.customer._wallet_is_b2c())

    def test_level_moves_to_b2b_when_customer_turns_out_to_be_a_company(self):
        self.invoice(1200)
        self.assertEqual(self.customer.wallet_level_id, self.gold)
        self.customer.vat = "12345678"
        self.run_nightly(days=1)
        self.assertFalse(self.customer.wallet_level_id)
        self.assertEqual(self.level_tags(self.customer), [])
        self.assertFalse(self.customer.specific_property_product_pricelist)


    def test_level_pricelists_are_enabled_in_every_pos(self):
        admin = self.env(su=True)
        admin["pos.config"].create({"name": "A shop opened after the levels"})
        configs = admin["pos.config"].search([("company_id", "=", self.env.company.id)])
        self.assertTrue(all(c.use_pricelist for c in configs))
        for c in configs:
            self.assertLessEqual(self.silver_pl | self.gold_pl, c.available_pricelist_ids)
        vip = admin["product.pricelist"].create({"name": "VIP -15%"})
        self.gold.sudo().pricelist_id = vip
        for c in configs:
            self.assertIn(vip, c.available_pricelist_ids)

    def test_an_open_pos_session_gets_the_new_pricelist_at_once(self):
        admin = self.env(su=True)
        self.bronze.sudo().pricelist_id = admin["product.pricelist"].create({"name": "Bronze -5%"})
        config = admin["pos.config"].create({"name": "Kassa 2"})
        session = admin["pos.session"].create({"config_id": config.id, "user_id": self.env.uid})
        PosConfig = type(admin["pos.config"])
        with patch.object(PosConfig, "notify_synchronisation", autospec=True) as notify:
            anna = self.env["res.partner"].create({"name": "Anna"})
            anna.wallet_excluded = False
            anna.wallet_excluded = True
        sent = [c.args for c in notify.call_args_list if c.args[0] == config]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][1:], (session.id, 0, {"res.partner": [anna.id]}))

    def test_level_pricelist_really_applies_and_nobody_else_gets_it(self):
        discount = "discount" in dict(self.env["product.pricelist.item"]._fields["compute_price"].selection)
        rule = {"compute_price": "discount", "price_discount": 10} if discount else {"compute_price": "percentage", "percent_price": 10}
        self.gold_pl.sudo().item_ids = [(0, 0, {"applied_on": "3_global", **rule})]
        product = self.env["product.product"].sudo().create({"name": "Kohv", "list_price": 100})
        self.invoice(1200)
        bystander = self.env["res.partner"].create({"name": "Ei ostnud midagi"})
        self.assertTrue(_pricelists_enabled(self.env))
        self.assertAlmostEqual(self.customer.property_product_pricelist._get_product_price(product, 1.0), 90)
        fallback = bystander.property_product_pricelist
        self.assertNotIn(fallback, self.silver_pl | self.gold_pl)
        self.assertAlmostEqual(fallback._get_product_price(product, 1.0) if fallback else 100, 100)

    def test_only_levels_administrators_switch_customers_or_recalculate(self):
        clerk = self.env["res.users"].sudo().create({
            "name": "Müüja", "login": "myyja@example.ee",
            groups(self.env): [(6, 0, [self.env.ref("base.group_user").id, self.env.ref("base.group_partner_manager").id,
                                       self.env.ref("point_of_sale.group_pos_manager").id])],
        })
        customer = self.customer.with_user(clerk)
        customer.name = "Mari Mets"
        with self.assertRaises(AccessError):
            customer.wallet_excluded = True
        with self.assertRaises(AccessError):
            customer.wallet_level_id = self.gold
        with self.assertRaises(AccessError):
            customer.wallet_period_start = self.today - relativedelta(days=1)
        self.assertTrue(customer.wallet_period_end)
        self.env["res.partner"].with_user(clerk).get_views([(False, "form")])
        with self.assertRaises(AccessError):
            self.bronze.with_user(clerk).action_recompute_all()
