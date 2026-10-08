import logging
from datetime import datetime, time

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.addons.tieroo.models import _wallet_commit, _wallet_translations
from odoo.exceptions import AccessError, UserError
from odoo.tools import split_every

_logger = logging.getLogger(__name__)

PERIOD = relativedelta(months=12)


def _at(day):
    return datetime.combine(day, time.min)


def _pricelists_enabled(env):
    groups = env["res.groups"]
    if hasattr(groups, "_is_feature_enabled"):
        return groups._is_feature_enabled("product.group_product_pricelist")
    return env.ref("product.group_product_pricelist") in env.ref("base.group_user").implied_ids


class WalletLevel(models.Model):
    _name = "wallet.level"
    _description = "Customer level"
    _order = "customer_type desc, min_spend, id"

    name = fields.Char(required=True, translate=True)
    company_id = fields.Many2one("res.company", required=True, index=True, default=lambda self: self.env.company)
    customer_type = fields.Selection(
        [("b2c", "Private (B2C)"), ("b2b", "Company (B2B)")],
        required=True,
        default="b2c",
        help="B2C: private customers, by their own spend. B2B: companies, by the spend of the company and all its contacts.",
    )
    tag_id = fields.Many2one("res.partner.category", string="Tag", required=True, ondelete="restrict")
    pricelist_id = fields.Many2one(
        "product.pricelist", ondelete="restrict", help="Applied to the customer while they have this level.",
        domain="[('company_id', 'in', [company_id, False])]",
    )
    min_spend = fields.Monetary("Spend from", required=True, default=0.0, help="Spend in the customer's personal 12-month period, taxes included.")
    currency_id = fields.Many2one(related="company_id.currency_id")

    _CONSTRAINTS = [
        ("tag_unique", "UNIQUE(company_id, customer_type, tag_id)", "Each level of a customer type needs its own tag."),
        ("min_spend_unique", "UNIQUE(company_id, customer_type, min_spend)", "Two levels of the same customer type cannot start at the same spend."),
        ("min_spend_positive", "CHECK(min_spend >= 0)", "Spend threshold cannot be negative."),
    ]
    if hasattr(models, "Constraint"):
        _tag_unique, _min_spend_unique, _min_spend_positive = (models.Constraint(d, m) for _n, d, m in _CONSTRAINTS)
    else:
        _sql_constraints = _CONSTRAINTS

    @api.model_create_multi
    def create(self, vals_list):
        levels = super().create(vals_list)
        levels._wallet_allow_in_pos()
        return levels

    def write(self, vals):
        res = super().write(vals)
        if "pricelist_id" in vals:
            self._wallet_allow_in_pos()
        if "name" in vals:
            self.env["wallet.card"].sudo().search([("company_id", "in", self.company_id.ids)]).partner_id._wallet_mark()
        return res

    def _wallet_allow_in_pos(self):
        for company in self.company_id:
            self.with_company(company)._wallet_allow_in_pos_company()

    def _wallet_allow_in_pos_company(self):
        env = self.env(su=True)
        pricelists = env["wallet.level"].search([("company_id", "=", self.env.company.id)]).pricelist_id
        if not pricelists:
            return
        if not _pricelists_enabled(env):
            env["res.config.settings"].create({"group_product_pricelist": True}).execute()

        Pricelist = env["product.pricelist"]
        company = [("company_id", "in", [self.env.company.id, False]), ("id", "not in", pricelists.ids)]
        standard = Pricelist.search(company + [("item_ids", "=", False)], order="sequence, id", limit=1)
        if not standard:
            standard = Pricelist.create({"name": _("Standard"), "currency_id": self.env.company.currency_id.id})
        first_level = min(pricelists.mapped("sequence"))
        if standard.sequence >= first_level:
            standard.sequence = first_level - 1

        for config in env["pos.config"].search([("company_id", "=", self.env.company.id)]):
            default = config.pricelist_id or standard
            missing = (pricelists | default) - config.available_pricelist_ids
            if missing or not config.use_pricelist or not config.pricelist_id:
                config.write({
                    "use_pricelist": True,
                    "pricelist_id": default.id,
                    "available_pricelist_ids": [(4, p.id) for p in missing],
                })

    def action_recompute_all(self):
        if not self.env.user.has_group("point_of_sale.group_pos_manager"):
            raise AccessError(_("Only Point of Sale managers can recalculate the customer levels."))
        self.env["res.partner"].with_company(self.env.company)._wallet_levels_run_company()

    @api.model
    def _company_levels(self):
        return self.sudo().search([("company_id", "=", self.env.company.id)])


class ResPartner(models.Model):
    _inherit = "res.partner"

    wallet_level_id = fields.Many2one("wallet.level", string="Level", readonly=True, copy=False, company_dependent=True)
    wallet_excluded = fields.Boolean(
        "Excluded",
        copy=False,
        company_dependent=True,
        groups="point_of_sale.group_pos_manager",
        help="Excluded from the loyalty program. Hands off: level, tags and pricelist are never changed automatically (e.g. VIPs with a special deal), and the wallet card is closed. "
        "For a company this covers all its contacts.",
    )
    wallet_period_start = fields.Date(
        "Level period start",
        copy=False,
        company_dependent=True,
        groups="point_of_sale.group_pos_manager",
        help="The customer's personal 12-month period starts on the day they joined the programme. "
        "The level can only drop at the end of a period.",
    )
    wallet_period_end = fields.Date("Level period end", compute="_compute_wallet_period", compute_sudo=True)
    wallet_period_spend = fields.Monetary(
        "Period spend", compute="_compute_wallet_period", currency_field="wallet_currency_id", compute_sudo=True
    )
    wallet_currency_id = fields.Many2one("res.currency", compute="_compute_wallet_period", compute_sudo=True)

    @api.depends("wallet_period_start", "commercial_partner_id.wallet_period_start")
    @api.depends_context("company")
    def _compute_wallet_period(self):
        now = fields.Datetime.now()
        for p in self:
            entity = p.commercial_partner_id
            start = entity.wallet_period_start
            p.wallet_period_end = start and start + PERIOD
            p.wallet_period_spend = entity._wallet_spend_between(_at(start), now) if start and entity.id else 0.0
            p.wallet_currency_id = self.env.company.currency_id

    def _wallet_spend_between(self, since, until):
        self.ensure_one()
        members = self.env["res.partner"].sudo().with_context(active_test=False).search(
            [("commercial_partner_id", "=", self.id)]
        )
        pos = self.env["pos.order"].sudo()._read_group(
            [("partner_id", "in", members.ids), ("company_id", "=", self.env.company.id), ("state", "in", ("paid", "done")),
             ("date_order", ">=", since), ("date_order", "<", until)],
            [],
            ["amount_total:sum"],
        )[0][0]
        invoices = self.env["account.move"].sudo()._read_group(
            [("partner_id", "in", members.ids), ("company_id", "=", self.env.company.id), ("move_type", "in", ("out_invoice", "out_refund")),
             ("state", "=", "posted"), ("pos_order_ids", "=", False),
             ("invoice_date", ">=", since.date()), ("invoice_date", "<", until.date() if until.time() == time.min else until.date() + relativedelta(days=1))],
            [],
            ["amount_total_signed:sum"],
        )[0][0]
        return (pos or 0.0) + (invoices or 0.0)

    def _wallet_is_b2c(self):
        self.ensure_one()
        if self.commercial_partner_id != self or self.is_company or self.vat:
            return False
        ids = self.additional_identifiers if "additional_identifiers" in self._fields else None
        return not any(self._is_company_identifier(key) for key in (ids or {}))

    def _wallet_own_levels(self, levels):
        kind = "b2c" if self._wallet_is_b2c() else "b2b"
        return levels.filtered(lambda lvl: lvl.customer_type == kind).sorted("min_spend", reverse=True)

    @staticmethod
    def _wallet_level_for(own_levels, spend):
        return next((lvl for lvl in own_levels if spend >= lvl.min_spend), own_levels.browse())

    def _wallet_update_levels(self, now=None):
        levels = self.env["wallet.level"]._company_levels()
        if not levels or not self:
            return
        now = now or fields.Datetime.now()
        today = now.date()
        for contact in self.filtered(lambda p: p.commercial_partner_id != p and p.wallet_level_id and not p.wallet_excluded):
            contact._wallet_set_level(levels.browse(), levels, 0.0)

        joined = False
        for entity in self.commercial_partner_id.filtered(lambda p: not p.wallet_excluded):
            own = entity._wallet_own_levels(levels)
            start = entity.wallet_period_start
            if not start:
                spend = entity._wallet_spend_between(_at(today) - PERIOD, now)
                entity.wallet_period_start = today
                entity._wallet_apply(entity._wallet_level_for(own, spend), levels, spend)
                joined = True
                continue
            while start + PERIOD <= today:
                end = start + PERIOD
                spend = entity._wallet_spend_between(_at(start), _at(end))
                entity.wallet_period_start = start = end
                entity._wallet_apply(entity._wallet_level_for(own, spend), levels, spend)
            spend = entity._wallet_spend_between(_at(start), now)
            target, current = entity._wallet_level_for(own, spend), entity.wallet_level_id
            type_changed = current and current.customer_type != (own[:1].customer_type or current.customer_type)
            if target != current and (not current or type_changed or target.min_spend > current.min_spend):
                entity._wallet_set_level(target, levels, spend)
        if joined:
            self._wallet_wake()

    def _wallet_apply(self, level, all_levels, spend):
        if level != self.wallet_level_id:
            self._wallet_set_level(level, all_levels, spend)
        else:
            self._wallet_mark()

    def _wallet_set_level(self, level, all_levels, spend):
        self.ensure_one()
        old = self.wallet_level_id
        tags = [(3, t.id) for t in all_levels.tag_id - level.tag_id]
        if level:
            tags.append((4, level.tag_id.id))
        vals = {"wallet_level_id": level.id, "category_id": tags}
        if level.pricelist_id or old.pricelist_id:
            vals["property_product_pricelist"] = level.pricelist_id.id
        self.write(vals)
        self._wallet_mark()
        me = self.with_context(lang=self.env.company.partner_id.lang or self.env.lang)
        me.message_post(
            body=me.env._(
                "Level changed: %(old)s → %(new)s (spend: %(spend)s, %(company)s)",
                old=old.name or me.env._("none"),
                new=level.name or me.env._("none"),
                spend=self.env.company.currency_id.format(spend),
                company=self.env.company.name,
            )
        )

    def _wallet_progress(self, now=None):
        entity = self.commercial_partner_id
        start = entity.wallet_period_start
        if not start or entity.wallet_excluded:
            return {"next": None, "keep": None}
        now = now or fields.Datetime.now()
        spend = entity._wallet_spend_between(_at(start), now)
        own = entity._wallet_own_levels(self.env["wallet.level"]._company_levels()).sorted("min_spend")
        current = entity.wallet_level_id
        floor = current.min_spend if current else -1
        nxt = next((lvl for lvl in own if lvl.min_spend > floor and lvl.min_spend > spend), None)
        last_day = start + PERIOD - relativedelta(days=1)
        keep = None
        if current:
            if spend >= current.min_spend:
                keep = {"missing": 0.0, "until": (last_day + PERIOD).isoformat()}
            else:
                keep = {"missing": current.min_spend - spend, "until": last_day.isoformat()}
        return {
            "next": {"id": str(nxt.id), "name": nxt.name, "missing": nxt.min_spend - spend} if nxt else None,
            "keep": keep,
        }

    @api.model
    def _cron_wallet_levels(self, now=None):
        for company in self.env["wallet.level"].sudo().search([]).company_id:
            self.with_company(company)._wallet_levels_run_company(now)

    @api.model
    def _wallet_levels_run_company(self, now=None):
        now = now or fields.Datetime.now()
        since = now - PERIOD
        company = self.env.company
        ids = set(self.sudo().search(["|", ("wallet_level_id", "!=", False), ("wallet_period_start", "!=", False)]).ids)
        for partner, in self.env["pos.order"].sudo()._read_group(
            [("company_id", "=", company.id), ("partner_id", "!=", False), ("state", "in", ("paid", "done")), ("date_order", ">=", since)],
            ["partner_id"],
        ):
            ids.add(partner.id)
        for partner, in self.env["account.move"].sudo()._read_group(
            [("company_id", "=", company.id), ("partner_id", "!=", False), ("move_type", "=", "out_invoice"),
             ("state", "=", "posted"), ("invoice_date", ">=", since.date())],
            ["partner_id"],
        ):
            ids.add(partner.id)
        for chunk in split_every(500, sorted(ids), self.sudo().browse):
            chunk._wallet_update_levels(now=now)
            chunk._wallet_mark()
            _wallet_commit(self.env, len(chunk))


    _WALLET_LEVEL_FIELDS = {"vat", "additional_identifiers", "wallet_period_start", "wallet_excluded"}

    @api.depends("wallet_excluded", "commercial_partner_id.wallet_excluded")
    @api.depends_context("company")
    def _compute_wallet_card_state(self):
        return super()._compute_wallet_card_state()

    def _wallet_card_open(self):
        return super()._wallet_card_open() and not self.commercial_partner_id.wallet_excluded

    @api.model_create_multi
    def create(self, vals_list):
        partners = super().create(vals_list)
        if any(p.email and p.parent_id for p in partners):
            self._wallet_wake()
        return partners

    def write(self, vals):
        res = super().write(vals)
        if self._WALLET_LEVEL_FIELDS & vals.keys():
            self._wallet_mark()
        if "wallet_excluded" in vals:
            self._wallet_note_exclusion(vals["wallet_excluded"])
        return res

    def _wallet_note_exclusion(self, excluded):
        company = self.env.company.name
        _ = self.with_context(lang=self.env.company.partner_id.lang or self.env.lang).env._
        for entity in self:
            has_cards = self.env["wallet.card"].sudo().search_count([
                ("company_id", "=", self.env.company.id), ("partner_id.commercial_partner_id", "=", entity.id),
            ])
            if excluded:
                body = _("Excluded from the loyalty program (%s).", company)
                if has_cards:
                    body += " " + _("Wallet card closed.")
            else:
                body = _("Back in the loyalty program (%s).", company)
                if has_cards:
                    body += " " + _("Wallet card active again.")
            entity.message_post(body=body)

    def _wallet_payload(self):
        payload = super()._wallet_payload()
        entity = self.commercial_partner_id
        if not entity.wallet_period_start and not entity.wallet_level_id:
            return payload
        level = entity.wallet_level_id
        progress = self._wallet_progress()
        payload["texts"] = {**payload["texts"], **self.env.company._wallet_level_texts()}
        payload.update({
            "company": entity.name if entity != self else None,
            "level": {"id": str(level.id), "name": level.name} if level else None,
            "next": progress["next"] and {"kind": "level", **progress["next"]},
            "keep": progress["keep"],
            "currency": self.env.company.currency_id.symbol,
        })
        return payload

    @api.model
    def _wallet_issue_candidates(self, limit):
        candidates = super()._wallet_issue_candidates(limit)
        if not self.env.company.wallet_levels_auto_send:
            return candidates
        people = self.sudo().search([
            ("commercial_partner_id.wallet_period_start", "!=", False), ("commercial_partner_id.wallet_excluded", "!=", True),
            ("type", "=", "contact"), ("is_company", "=", False), ("email", "!=", False),
            ("wallet_card_ids", "not any", [("company_id", "=", self.env.company.id)]),
        ], limit=limit, order="id")
        people = people.filtered(lambda p: not (p.commercial_partner_id == p and not p._wallet_is_b2c()))
        return (candidates | people)[:limit]

    @api.model
    def _wallet_issue_domain(self):
        return super()._wallet_issue_domain() + [("commercial_partner_id.wallet_excluded", "!=", True)]

    def _wallet_is_member(self):
        return super()._wallet_is_member() or bool(self.commercial_partner_id.sudo().wallet_period_start)

    def _wallet_prepare_card(self):
        self.ensure_one()
        entity = self.commercial_partner_id.sudo()
        if entity.wallet_excluded:
            raise UserError(_("This customer is excluded from the loyalty program, so the wallet card is closed."))
        if not entity.wallet_period_start:
            self._wallet_ensure_barcode()
            self.sudo()._wallet_update_levels()
        return super()._wallet_prepare_card()


def _upgrade_safely(partners, company):
    if not partners:
        return
    partners = partners.sudo().with_company(company)
    try:
        with partners.env.cr.savepoint():
            partners._wallet_update_levels()
            partners.commercial_partner_id._wallet_mark()
    except Exception:
        _logger.exception("tieroo_levels: level update failed for partners %s", partners.ids)


class PosOrder(models.Model):
    _inherit = "pos.order"

    def write(self, vals):
        res = super().write(vals)
        if vals.get("state") == "paid":
            for order in self:
                _upgrade_safely(order.partner_id, order.company_id)
        return res


class AccountMove(models.Model):
    _inherit = "account.move"

    def _post(self, soft=True):
        posted = super()._post(soft)
        for move in posted.filtered(lambda m: m.move_type == "out_invoice" and not m.pos_order_ids):
            _upgrade_safely(move.partner_id, move.company_id)
        return posted


class ResCompany(models.Model):
    _inherit = "res.company"

    wallet_levels_auto_send = fields.Boolean(
        "Send wallet cards to level customers automatically",
        help="Create a card for every customer in the levels programme who has an email (for B2B: each contact "
        "person) and email it. Switching this on emails everyone who has no card yet.",
    )

    def _wallet_level_texts(self):
        levels = self.env["wallet.level"].sudo().search([("company_id", "=", self.id)])
        return {f"level:{l.id}": _wallet_translations(l, "name") for l in levels}

    def _wallet_design_context(self):
        ctx = super()._wallet_design_context()
        ctx["texts"] = {**ctx["texts"], **self._wallet_level_texts()}
        levels = self.env["wallet.level"].sudo().with_context(lang="en_US").search([("company_id", "=", self.id)])
        ctx["levels"] = [
            {"id": str(l.id), "name": l.name, "type": l.customer_type}
            for l in levels.sorted(lambda l: (l.customer_type != "b2c", l.min_spend))
        ]
        return ctx


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    wallet_levels_auto_send = fields.Boolean(related="company_id.wallet_levels_auto_send", readonly=False)
