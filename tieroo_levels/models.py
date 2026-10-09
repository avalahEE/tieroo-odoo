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

    @api.model
    def name_search(self, name="", domain=None, operator="ilike", limit=100):
        found = super().name_search(name, domain, operator, limit)
        return found[:1] if operator == "=" else found

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
        if not self.env.user.has_group("tieroo_levels.group_levels_admin"):
            raise AccessError(_("Only Customer levels administrators can recalculate the customer levels."))
        self.env["res.partner"].with_company(self.env.company)._wallet_levels_run_company()

    @api.model
    def _company_levels(self):
        return self.sudo().search([("company_id", "=", self.env.company.id)])


class ResPartner(models.Model):
    _inherit = "res.partner"

    wallet_level_id = fields.Many2one("wallet.level", string="Level", copy=False, company_dependent=True,
                                      help="Set from the period's spend. An imported level holds until the end of its period.")
    wallet_joined = fields.Date("Joined Customer Levels", copy=False, company_dependent=True, readonly=True)
    wallet_entity_level_id = fields.Many2one("wallet.level", string="Company level", related="commercial_partner_id.wallet_level_id")
    wallet_excluded = fields.Boolean(
        "Excluded from Levels", compute="_compute_wallet_excluded", inverse="_inverse_wallet_excluded",
        default=True,
        help="On for everyone until they join: a private person on the join page (QR), a company and each of its contact "
        "persons by a levels administrator, by hand or by import. Switching it on again removes the level, its tag and "
        "pricelist, and closes the card.",
    )
    wallet_period_start = fields.Date(
        "Level period start",
        copy=False,
        company_dependent=True,
        groups="tieroo_levels.group_levels_admin",
        help="The customer's personal 12-month period. The level can only drop at the end of a period. For customers of "
        "an earlier system, import it with the opening spend.",
    )
    wallet_opening_spend = fields.Float(
        "Opening spend", copy=False, company_dependent=True, digits="Product Price",
        groups="tieroo_levels.group_levels_admin",
        help="Spend in this period before Odoo (an earlier system), taxes included. It counts until the period ends, "
        "then goes to 0.",
    )
    wallet_period_end = fields.Date("Level period end", compute="_compute_wallet_period", compute_sudo=True)
    wallet_period_spend = fields.Monetary(
        "Period spend", compute="_compute_wallet_period", currency_field="wallet_currency_id", compute_sudo=True
    )
    wallet_currency_id = fields.Many2one("res.currency", compute="_compute_wallet_period", compute_sudo=True)

    @api.depends("wallet_joined")
    @api.depends_context("company")
    def _compute_wallet_excluded(self):
        for p in self:
            p.wallet_excluded = not p.sudo().wallet_joined

    def _inverse_wallet_excluded(self):
        pass

    @api.depends("wallet_period_start", "commercial_partner_id.wallet_period_start")
    @api.depends_context("company")
    def _compute_wallet_period(self):
        now = fields.Datetime.now()
        for p in self:
            entity = p.commercial_partner_id.sudo()
            start = entity.wallet_joined and entity.wallet_period_start
            p.wallet_period_end = start and start + PERIOD - relativedelta(days=1)
            p.wallet_period_spend = entity._wallet_spend_between(_at(start), now) + entity.wallet_opening_spend if start and entity.id else 0.0
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
        for contact in self.filtered(lambda p: p.commercial_partner_id != p and p.wallet_level_id):
            contact._wallet_set_level(levels.browse(), levels, 0.0)

        for entity in self.commercial_partner_id.sudo().filtered(lambda p: p.wallet_joined and p.wallet_period_start):
            own = entity._wallet_own_levels(levels)
            start, opening = entity.wallet_period_start, entity.wallet_opening_spend
            while start + PERIOD <= today:
                end = start + PERIOD
                odoo = entity._wallet_spend_between(_at(start), _at(end))
                spend = odoo + opening
                old = entity.wallet_level_id
                entity.with_context(wallet_levels_internal=True).write({"wallet_period_start": end, "wallet_opening_spend": 0.0})
                entity._wallet_apply(entity._wallet_level_for(own, spend), levels, spend)
                if opening:
                    currency = self.env.company.currency_id
                    entity._wallet_note(entity.env._(
                        "Period %(start)s – %(end)s ended: %(spend)s (opening spend %(opening)s + Odoo %(odoo)s), level %(old)s → %(new)s.",
                        start=start, end=end - relativedelta(days=1), spend=currency.format(spend), opening=currency.format(opening),
                        odoo=currency.format(odoo), old=old.name or entity.env._("none"), new=entity.wallet_level_id.name or entity.env._("none")))
                start, opening = end, 0.0
            spend = entity._wallet_spend_between(_at(start), now) + opening
            target, current = entity._wallet_level_for(own, spend), entity.wallet_level_id
            type_changed = current and current.customer_type != (own[:1].customer_type or current.customer_type)
            if target != current and (not current or type_changed or target.min_spend > current.min_spend):
                entity._wallet_set_level(target, levels, spend)

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
        self.with_context(wallet_levels_internal=True).write(vals)
        if "property_product_pricelist" in vals:
            for session in self.env["pos.session"].sudo().search([("company_id", "=", self.env.company.id), ("state", "!=", "closed")]):
                session.config_id.notify_synchronisation(session.id, 0, {"res.partner": (self | self.child_ids).ids})
        self._wallet_mark()
        self._wallet_note(self.env._(
            "Level changed: %(old)s → %(new)s (spend: %(spend)s, %(company)s)",
            old=old.name or self.env._("none"), new=level.name or self.env._("none"),
            spend=self.env.company.currency_id.format(spend), company=self.env.company.name))

    def _wallet_note(self, body):
        self.with_context(lang=self.env.company.partner_id.lang or self.env.lang).message_post(body=body)


    def _wallet_levels_join(self):
        today = fields.Date.context_today(self)
        for p in self.sudo().filtered(lambda x: not x.wallet_joined):
            internal = p.with_context(wallet_levels_internal=True)
            if p.commercial_partner_id != p:
                if not p.commercial_partner_id.wallet_joined:
                    raise UserError(p.env._("%s: switch the company on in Customer Levels first.", p.display_name))
                internal.wallet_joined = today
            else:
                internal.write({"wallet_joined": today, "wallet_period_start": p.wallet_period_start or today})
                p._wallet_update_levels()
            p._wallet_note(p.env._("Joined Customer Levels (%s).", p.env.company.name))
            p._wallet_mark()
        self._wallet_wake()

    def _wallet_levels_leave(self):
        levels = self.env["wallet.level"]._company_levels()
        for p in self.sudo().filtered("wallet_joined"):
            internal = p.with_context(wallet_levels_internal=True)
            if p.commercial_partner_id == p:
                if p.wallet_level_id:
                    p._wallet_set_level(levels.browse(), levels, p.wallet_period_spend)
                internal.write({"wallet_joined": False, "wallet_period_start": False, "wallet_opening_spend": 0.0})
                p.search([("commercial_partner_id", "=", p.id), ("id", "!=", p.id), ("wallet_joined", "!=", False)])._wallet_levels_leave()
            else:
                internal.wallet_joined = False
            p._wallet_note(p.env._("Left Customer Levels (%s).", p.env.company.name))
            p._wallet_mark()

    @api.model
    def _wallet_check_levels_admin(self):
        if not self.env.su and not self.env.user.has_group("tieroo_levels.group_levels_admin"):
            raise AccessError(_("Only Customer levels administrators can switch customers on or off, or change their "
                                "level, period or opening spend."))

    def _wallet_levels_admin_edit(self, vals, excluded, level):
        today = fields.Date.context_today(self)
        for p in self.sudo():
            start = p.wallet_period_start
            if "wallet_period_start" in vals and start and not (today - PERIOD < start <= today):
                raise UserError(p.env._("%(name)s: the level period must start within the last 12 months, not on %(day)s.",
                                        name=p.display_name, day=start))
            if excluded is True:
                p._wallet_levels_leave()
            elif excluded is False:
                p._wallet_levels_join()
            if level is not None:
                if p.commercial_partner_id != p:
                    raise UserError(p.env._("%s: a company's contact person has no level of their own; set it on the company.", p.display_name))
                if not p.wallet_joined:
                    raise UserError(p.env._("%s: switch the customer on in Customer Levels first.", p.display_name))
                own = p._wallet_own_levels(self.env["wallet.level"]._company_levels())
                if level and level not in own and len(own.filtered(lambda lvl: lvl.name == level.name)) == 1:
                    level = own.filtered(lambda lvl: lvl.name == level.name)
                if level and level not in own:
                    raise UserError(p.env._("%(name)s: %(level)s is not a level for this kind of customer.", name=p.display_name, level=level.name))
                if level != p.wallet_level_id:
                    p._wallet_set_level(level, self.env["wallet.level"]._company_levels(), 0.0)
            if p.wallet_joined and {"wallet_period_start", "wallet_opening_spend"} & vals.keys():
                currency = self.env.company.currency_id
                p._wallet_note(p.env._("Level period from %(start)s, opening spend %(opening)s.",
                                       start=p.wallet_period_start, opening=currency.format(p.wallet_opening_spend)))
                p._wallet_update_levels()


    def _wallet_progress(self, now=None):
        entity = self.commercial_partner_id.sudo()
        start = entity.wallet_period_start
        if not start or not entity.wallet_joined:
            return {"next": None, "keep": None}
        now = now or fields.Datetime.now()
        spend = entity._wallet_spend_between(_at(start), now) + entity.wallet_opening_spend
        own = entity._wallet_own_levels(self.env["wallet.level"]._company_levels()).sorted("min_spend")
        current = entity.wallet_level_id
        floor = current.min_spend if current else -1
        nxt = next((lvl for lvl in own if lvl.min_spend > floor and lvl.min_spend > spend), None)
        last_day = start + PERIOD - relativedelta(days=1)
        keep = None
        if current.min_spend > 0:
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
        company, since = self.env.company, now - relativedelta(days=2)
        Partner = self.sudo()
        ended = Partner.search([("wallet_joined", "!=", False), ("wallet_period_start", "<=", now.date() - PERIOD)])
        buyers = self.env["pos.order"].sudo().search([("company_id", "=", company.id), ("state", "in", ("paid", "done")),
                                                      ("date_order", ">=", since)]).partner_id
        buyers |= self.env["account.move"].sudo().search([("company_id", "=", company.id), ("state", "=", "posted"),
                                                          ("move_type", "in", ("out_invoice", "out_refund")),
                                                          ("invoice_date", ">=", since.date())]).partner_id
        ids = (ended | buyers.commercial_partner_id.filtered("wallet_joined")).ids
        for chunk in split_every(500, sorted(ids), Partner.browse):
            chunk._wallet_update_levels(now=now)
            _wallet_commit(self.env, len(chunk))


    _WALLET_LEVEL_FIELDS = {"vat", "additional_identifiers", "wallet_period_start", "wallet_joined", "wallet_opening_spend"}
    _WALLET_ADMIN_FIELDS = {"wallet_excluded", "wallet_level_id", "wallet_period_start", "wallet_opening_spend"}

    @api.depends("wallet_joined", "commercial_partner_id.wallet_joined")
    @api.depends_context("company")
    def _compute_wallet_card_state(self):
        return super()._compute_wallet_card_state()

    def _wallet_levels_started(self):
        return bool(self.env.company.sudo().wallet_levels_started)

    def _wallet_is_member(self):
        if not self._wallet_levels_started():
            return super()._wallet_is_member()
        me = self.sudo()
        return bool(me.wallet_joined and me.commercial_partner_id.wallet_joined)

    @staticmethod
    def _wallet_levels_noop(vals):
        return {k: v for k, v in vals.items() if not (k == "wallet_excluded" and v is True or k in ("wallet_level_id", "wallet_period_start", "wallet_opening_spend") and not v)}

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [dict(v) for v in vals_list]
        for v in vals_list:
            for k in self._WALLET_ADMIN_FIELDS & v.keys() - self._wallet_levels_noop(v).keys():
                del v[k]
        if self.env.context.get("wallet_levels_internal") or not any(self._WALLET_ADMIN_FIELDS & v.keys() for v in vals_list):
            return super().create(vals_list)
        self._wallet_check_levels_admin()
        Level = self.env["wallet.level"]
        edits = [(v.pop("wallet_excluded", None), Level.browse(v.pop("wallet_level_id") or []) if "wallet_level_id" in v else None, set(v)) for v in vals_list]
        partners = super().create(vals_list)
        for partner, (excluded, level, keys) in zip(partners, edits):
            partner._wallet_levels_admin_edit(dict.fromkeys(keys, True), excluded, level)
        return partners

    def write(self, vals):
        if self._WALLET_ADMIN_FIELDS & vals.keys() and not self.env.context.get("wallet_levels_internal"):
            vals = {k: v for k, v in vals.items() if k not in self._WALLET_ADMIN_FIELDS or any(
                (p.sudo()[k].id if k == "wallet_level_id" else p.sudo()[k]) != (v or False if k != "wallet_opening_spend" else v or 0.0) for p in self)}
        if self.env.context.get("wallet_levels_internal") or not (self._WALLET_ADMIN_FIELDS & vals.keys()):
            res = super().write(vals)
            if self._WALLET_LEVEL_FIELDS & vals.keys():
                self._wallet_mark()
            return res
        self._wallet_check_levels_admin()
        vals = dict(vals)
        excluded = vals.pop("wallet_excluded", None)
        level = self.env["wallet.level"].browse(vals.pop("wallet_level_id") or []) if "wallet_level_id" in vals else None
        res = super().write(vals)
        self._wallet_levels_admin_edit(vals, excluded, level)
        if self._WALLET_LEVEL_FIELDS & vals.keys():
            self._wallet_mark()
        return res

    def _wallet_signup_joined(self):
        super()._wallet_signup_joined()
        if self._wallet_levels_started() and self.env["wallet.level"]._company_levels():
            self.sudo()._wallet_levels_join()

    def _wallet_payload(self):
        payload = super()._wallet_payload()
        entity = self.commercial_partner_id.sudo()
        if not entity.wallet_joined:
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
    def _wallet_issue_candidates(self, limit, everyone=False):
        if not self._wallet_levels_started():
            return super()._wallet_issue_candidates(limit, everyone)
        people = self.sudo().search([
            ("wallet_joined", "!=", False), ("commercial_partner_id.wallet_joined", "!=", False),
            ("type", "=", "contact"), ("is_company", "=", False), ("email", "!=", False),
            ("wallet_card_ids", "not any", [("company_id", "=", self.env.company.id)]),
        ] + self._wallet_issue_domain(), limit=limit, order="id")
        return people.filtered(lambda p: not (p.commercial_partner_id == p and not p._wallet_is_b2c()))


def _upgrade_safely(partners, company):
    if not partners:
        return
    partners = partners.sudo().with_company(company)
    try:
        with partners.env.cr.savepoint():
            partners._wallet_update_levels(now=fields.Datetime.now() + relativedelta(seconds=1))
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


class PosConfig(models.Model):
    _inherit = "pos.config"

    @api.model_create_multi
    def create(self, vals_list):
        configs = super().create(vals_list)
        self.env["wallet.level"].sudo().search([("company_id", "in", configs.company_id.ids)])._wallet_allow_in_pos()
        return configs


class AccountMove(models.Model):
    _inherit = "account.move"

    def _post(self, soft=True):
        posted = super()._post(soft)
        for move in posted.filtered(lambda m: m.move_type == "out_invoice" and not m.pos_order_ids):
            _upgrade_safely(move.partner_id, move.company_id)
        return posted


class ResCompany(models.Model):
    _inherit = "res.company"

    wallet_levels_started = fields.Datetime("Customer Levels started", copy=False, readonly=True)

    def _wallet_join_target(self):
        return super()._wallet_join_target() or bool(self.sudo().wallet_levels_started and self.env["wallet.level"].sudo().search_count([("company_id", "=", self.id)]))

    def _wallet_levels_holders(self):
        self.ensure_one()
        cards = self.env["wallet.card"].sudo().search([("company_id", "=", self.id)])
        Partner = self.env["res.partner"].sudo().with_company(self)
        return Partner.browse(cards.partner_id.ids).filtered(
            lambda p: p.active and p.commercial_partner_id == p and p._wallet_is_b2c() and not p.wallet_joined and p._wallet_card_open())

    def _wallet_levels_preview(self):
        self.ensure_one()
        lowest = self.env["wallet.level"].sudo().search([("company_id", "=", self.id), ("customer_type", "=", "b2c")], order="min_spend", limit=1)
        return {"holders": len(self._wallet_levels_holders()), "level": lowest.name or ""}

    def _wallet_levels_start(self):
        self.ensure_one()
        Partner = self.env["res.partner"].sudo().with_company(self)
        for chunk in split_every(200, self._wallet_levels_holders().ids, Partner.browse):
            chunk._wallet_levels_join()
            _wallet_commit(self.env, len(chunk))
        self.sudo().wallet_levels_started = fields.Datetime.now()
        Partner._wallet_wake()

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
        ctx["currency"] = self.currency_id.symbol
        return ctx


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    wallet_levels_started = fields.Datetime(related="company_id.wallet_levels_started")
    wallet_levels_preview = fields.Char(compute="_compute_wallet_levels_preview")

    @api.depends("company_id")
    def _compute_wallet_levels_preview(self):
        for s in self:
            company = s.company_id
            if company.wallet_levels_started or not self.env["wallet.level"].sudo().search_count([("company_id", "=", company.id)]):
                s.wallet_levels_preview = False
                continue
            p = company._wallet_levels_preview()
            s.wallet_levels_preview = self.env._("Card holders who join at %(level)s: %(holders)s. Nobody else changes.", **p)

    def action_wallet_levels_start(self):
        self.env["res.partner"]._wallet_check_levels_admin()
        self.company_id._wallet_levels_start()
        return {"type": "ir.actions.client", "tag": "reload"}
