import logging
import secrets
import threading

import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules import module as odoo_module
from odoo.tools import SQL, split_every

_logger = logging.getLogger(__name__)
PLATFORM_URL = "https://app.tieroo.com"
TIMEOUT = 10
BATCH = 200
BATCH_TIMEOUT = 120
CRON_RESERVE = 120
ACCOUNT_TIMEOUT = 4
CLAIM_TIMEOUT = 30
SOON = 20


def _wallet_api(env, method, url, company=None, timeout=TIMEOUT, **kw):
    try:
        r = getattr(requests, method)(url, headers=company._wallet_headers() if company else None, timeout=timeout, **kw)
    except requests.RequestException as e:
        raise UserError(env._("Tieroo could not be reached: %s", e)) from e
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data if isinstance(data, dict) else {}


def _wallet_api_error(env, status, data):
    code = data.get("error")
    if code == "invalid_vat":
        return env._("The VAT number was not accepted. Check it in the company's settings: the EU VAT number with its country prefix, e.g. EE123456780.")
    if code == "invalid_company":
        return env._("Tieroo did not accept the company's data: check the name, email and address.")
    if code == "billing_not_configured":
        return env._("Signing up with Tieroo is not available at the moment. Try again later.")
    if code == "key_in_use":
        return env._("This company's Tieroo key is in use in another Odoo database or company. Contact Tieroo.")
    if code == "suspended":
        return env._("This company's Tieroo account is suspended. Contact Tieroo.")
    if code == "already_claimed":
        return env._("This sign-up has already been used. If the company is still not connected, start again.")
    return env._("Tieroo answered with an error (%(status)s %(error)s).", status=status, error=code or "")


def _wallet_rewards(program):
    rewards = program.sudo().reward_ids.sorted("required_points")[:20] if program else []
    return [{"id": str(r.id), "name": (r.description or r.display_name)[:60], "points": r.required_points} for r in rewards]


def _wallet_translations(record, field):
    source = record.with_context(lang="en_US")[field]
    if not source:
        return {}
    langs = [code for code, _name in record.env["res.lang"].get_installed() if code != "en_US"]
    texts = {code: v[:80] for code in langs if (v := record.with_context(lang=code)[field]) and v != source}
    return {"en_US": source[:80], **texts}


def _wallet_texts(program):
    if not program:
        return {}
    program = program.sudo()
    texts = {"program": _wallet_translations(program, "name"), "points": _wallet_translations(program, "portal_point_name")}
    for r in program.reward_ids.sorted("required_points")[:20]:
        texts[f"reward:{r.id}"] = _wallet_translations(r, "description")
    return {k: v for k, v in texts.items() if v}


def _wallet_commit(env, processed=1):
    if env.context.get("ir_cron_progress_id"):
        return env["ir.cron"]._commit_progress(processed)
    return float("inf")


def _wallet_push_soon(registry, uid, ids):
    try:
        with registry.cursor() as cr:
            env = api.Environment(cr, uid, {})
            env["wallet.card"].sudo().browse(sorted(ids)).exists().filtered("sync_needed")._push(raise_errors=False)
    except Exception:
        _logger.warning("tieroo: sending a change straight away failed, the job will", exc_info=True)


def _wallet_expiring(card):
    history = card.env["loyalty.history"].sudo() if card else None
    if not card or not hasattr(history, "_get_points_left_per_award"):
        return None
    awards, left = history._get_points_left_per_award(card)
    by_date = {}
    for a in awards:
        if a.expiration_date and left[a] > 0:
            by_date[a.expiration_date] = by_date.get(a.expiration_date, 0) + left[a]
    return [{"points": p, "date": d.isoformat()} for d, p in sorted(by_date.items())[:10]] or None


def _wallet_programs_changed(programs):
    programs = programs.exists()
    if programs:
        programs.env["loyalty.card"].sudo().search([("program_id", "in", programs.ids), ("partner_id", "!=", False)]).partner_id._wallet_mark()


class ResCompany(models.Model):
    _inherit = "res.company"

    wallet_api_url = fields.Char("Tieroo platform URL")
    wallet_api_key = fields.Char("Wallet sync key", groups="base.group_system")
    wallet_claim_token = fields.Char(groups="base.group_system", copy=False)
    wallet_signup_notice = fields.Selection([("connected", "Connected"), ("cancelled", "Cancelled")], groups="base.group_system", copy=False)
    wallet_consent_date = fields.Datetime("Agreed to send data to Tieroo", groups="base.group_system", copy=False)
    wallet_shops_on = fields.Boolean("Show the card near our shops")

    def _wallet_shop_points(self, shops):
        return [{"lat": s.partner_id.partner_latitude, "lon": s.partner_id.partner_longitude} for s in shops]

    def _wallet_shops(self):
        self.ensure_one()
        if not self.wallet_shops_on:
            return self.env["wallet.shop"]
        return self.env["wallet.shop"].sudo().search([("company_id", "=", self.id), ("on_card", "=", True)]).filtered("has_point")

    def _wallet_mark_all(self, always=False):
        companies = self if always else self.filtered("wallet_shops_on")
        cards = self.env["wallet.card"].sudo().search([("company_id", "in", companies.ids), ("sync_needed", "=", False)])
        if cards:
            cards.write({"sync_needed": True})
            self.env["res.partner"]._wallet_wake()

    def write(self, vals):
        res = super().write(vals)
        if "wallet_shops_on" in vals:
            self._wallet_mark_all(always=True)
        return res

    def _wallet_base(self):
        return (self.sudo().wallet_api_url or PLATFORM_URL).rstrip("/")

    def _wallet_headers(self):
        params = self.env["ir.config_parameter"].sudo()
        uuid = (params.get_str if hasattr(params, "get_str") else params.get_param)("database.uuid")
        return {"Authorization": f"Bearer {self.sudo().wallet_api_key}", "X-Tieroo-Instance": f"{uuid}:{self.id}"}

    def _wallet_loyalty_program(self):
        self.ensure_one()
        return self.env["loyalty.program"].sudo().search([
            ("wallet_card", "=", True), ("program_type", "=", "loyalty"), ("company_id", "in", [self.id, False]),
        ], limit=1)

    def _wallet_open_designer(self, start=None):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only administrators can design the wallet card."))
        company = self.sudo()
        base, key = company._wallet_base(), company.wallet_api_key
        if not key:
            raise UserError(_("Connect %s to Tieroo first: Settings → Tieroo.", company.name))
        company.wallet_signup_notice = False
        try:
            r = requests.post(f"{base}/sync/v1/design-session", json={**company._wallet_design_context(), **({"start": start} if start else {})},
                              headers=company._wallet_headers(), timeout=TIMEOUT)
            r.raise_for_status()
            url = r.json()["url"]
        except (requests.RequestException, ValueError, KeyError) as e:
            raise UserError(_("Tieroo could not be reached: %s", e)) from e
        return {"type": "ir.actions.act_url", "url": url, "target": "new"}

    def _wallet_design_context(self):
        self.ensure_one()
        program = self._wallet_loyalty_program().with_context(lang="en_US")
        return {
            "company": self.name,
            "texts": _wallet_texts(program),
            "rewards": _wallet_rewards(program),
            "languages": self.env["res.lang"].sudo().search([("active", "=", True)]).mapped("code"),
            "uiLang": "et" if (self.env.user.lang or "").startswith("et") else "en",
            "levels": [],
        }


    def _wallet_check_admin(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only administrators can manage the Tieroo account."))

    def _wallet_settings_url(self):
        return f"{self.get_base_url()}/odoo/settings?cids={self.id}#tieroo"

    def _wallet_levels_on(self):
        return bool(self.env["ir.module.module"].sudo().search_count([("name", "=", "tieroo_levels"), ("state", "=", "installed")]))

    def _wallet_signup_data(self):
        self.ensure_one()
        if not self.vat or not self.country_id:
            raise UserError(_("Fill in the VAT number and the country of %(company)s first: Settings → Companies → %(company)s.", company=self.name))
        vat = self.vat.replace(" ", "").upper()
        prefix = "EL" if self.country_id.code == "GR" else self.country_id.code
        return {
            "name": self.name, "vat": vat if vat[:2].isalpha() else prefix + vat, "country": self.country_id.code,
            "email": self.env.user.email or self.email, "street": self.street, "city": self.city, "zip": self.zip,
        }

    def _wallet_start(self):
        self.ensure_one()
        self._wallet_check_admin()
        company = self.sudo()
        if company.wallet_api_key:
            raise UserError(_("%s is already connected to Tieroo.", company.name))
        if not company.wallet_consent_date:
            raise UserError(_("Tick the box that you agree to send data to Tieroo first."))
        if company.wallet_claim_token:
            state = company._wallet_claim(gone_ok=True)
            if state == "done":
                return {"type": "ir.actions.act_url", "url": company._wallet_settings_url(), "target": "self"}
            if state == "waiting":
                raise UserError(_("Your sign-up is being confirmed (the VAT number check). Try again in a minute."))
        body = {
            "company": {k: v.strip() for k, v in company._wallet_signup_data().items() if isinstance(v, str) and v.strip()},
            "returnUrl": f"{company.get_base_url()}/tieroo/return?company={company.id}",
            "levelsModule": company._wallet_levels_on(),
        }
        status, answer = _wallet_api(self.env, "post", f"{company._wallet_base()}/onboard/v1/start", json=body)
        if status != 200 or not answer.get("url") or not answer.get("claimToken"):
            raise UserError(_wallet_api_error(self.env, status, answer))
        company.write({"wallet_claim_token": answer["claimToken"], "wallet_signup_notice": False})
        return {"type": "ir.actions.act_url", "url": answer["url"], "target": "self"}

    def _wallet_claim(self, gone_ok=False):
        self.ensure_one()
        self._wallet_check_admin()
        company = self.sudo()
        if not company.wallet_claim_token:
            if company.wallet_api_key:
                return "done"
            raise UserError(_("No sign-up is waiting for %s. Start again in Settings → Tieroo.", company.name))
        status, data = _wallet_api(self.env, "post", f"{company._wallet_base()}/onboard/v1/claim",
                                   json={"claimToken": company.wallet_claim_token}, timeout=CLAIM_TIMEOUT)
        if status == 409:
            return "unpaid" if data.get("error") == "not_completed" else "waiting"
        if status == 404 and gone_ok:
            return "gone"
        if status != 200 or not data.get("key"):
            raise UserError(_wallet_api_error(self.env, status, data))
        company.write({"wallet_api_key": data["key"], "wallet_claim_token": False, "wallet_signup_notice": "connected"})
        return "done"

    def _wallet_account(self):
        company = self.sudo()
        try:
            status, data = _wallet_api(self.env, "get", f"{company._wallet_base()}/sync/v1/account", company=company, timeout=ACCOUNT_TIMEOUT)
        except UserError as e:
            _logger.warning("tieroo: could not read the account of %s: %s", company.name, e)
            return None
        if status == 403 and data.get("error") in ("suspended", "closed"):
            return {"status": data["error"]}
        if status == 409 and data.get("error") == "key_in_use":
            return {"status": "elsewhere"}
        return data if status == 200 else None

    def _wallet_billing(self):
        self.ensure_one()
        self._wallet_check_admin()
        company = self.sudo()
        if not company.wallet_api_key:
            raise UserError(_("Connect %s to Tieroo first: Settings → Tieroo.", company.name))
        status, data = _wallet_api(self.env, "post", f"{company._wallet_base()}/sync/v1/billing", company=company,
                                   json={"returnUrl": company._wallet_settings_url(), "levelsModule": company._wallet_levels_on()})
        if status != 200 or not data.get("url"):
            raise UserError(_wallet_api_error(self.env, status, data))
        return {"type": "ir.actions.act_url", "url": data["url"], "target": "self"}


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    @api.depends("company_id")
    def _compute_wallet_shops(self):
        for s in self:
            shops = self.env["wallet.shop"].sudo().search([("company_id", "=", s.company_id.id)])
            s.wallet_shops_count = len(shops)
            s.wallet_shops_missing = len(shops.filtered(lambda x: not x.has_point))

    def action_wallet_email_templates(self):
        return {
            "type": "ir.actions.act_window", "name": _("Tieroo email templates"), "res_model": "mail.template",
            "view_mode": "list,form", "domain": [("id", "in", self.env["mail.template"]._wallet_template_ids())],
        }

    def action_wallet_shops(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "name": _("Shops on the card"), "res_model": "wallet.shop", "view_mode": "list",
            "domain": [("company_id", "=", self.company_id.id)], "context": {"default_company_id": self.company_id.id},
        }

    wallet_api_url = fields.Char(related="company_id.wallet_api_url", readonly=False)
    wallet_api_key = fields.Char(related="company_id.wallet_api_key", readonly=False)
    wallet_shops_on = fields.Boolean(related="company_id.wallet_shops_on", readonly=False)
    wallet_shops_count = fields.Integer(compute="_compute_wallet_shops")
    wallet_shops_missing = fields.Integer(compute="_compute_wallet_shops")

    wallet_signup_notice = fields.Selection(related="company_id.wallet_signup_notice")
    wallet_data_consent = fields.Boolean("I agree that Odoo sends data to Tieroo", compute="_compute_wallet_data_consent",
                                         inverse="_inverse_wallet_data_consent")
    wallet_connected = fields.Boolean(compute="_compute_wallet_account")
    wallet_account_error = fields.Boolean(compute="_compute_wallet_account")
    wallet_plan_name = fields.Char("Plan", compute="_compute_wallet_account")
    wallet_plan_extra = fields.Char(compute="_compute_wallet_account")
    wallet_cards_used = fields.Integer("Cards in use", compute="_compute_wallet_account")
    wallet_cards_limit = fields.Integer("Card limit", compute="_compute_wallet_account")
    wallet_status = fields.Selection([("active", "Active"), ("past_due", "Payment failed"), ("suspended", "Suspended"), ("closed", "Closed"),
                                      ("elsewhere", "Key in use elsewhere")],
                                     "Account status", compute="_compute_wallet_account")
    wallet_over_limit = fields.Boolean(compute="_compute_wallet_account")
    wallet_grace_until = fields.Date(compute="_compute_wallet_account")
    wallet_can_create = fields.Boolean(compute="_compute_wallet_account")
    wallet_levels_missing = fields.Selection([("add", "Add it to the plan"), ("upgrade", "Needs a paid plan")], compute="_compute_wallet_account")

    @api.depends("company_id")
    def _compute_wallet_data_consent(self):
        for s in self:
            s.wallet_data_consent = bool(s.company_id.sudo().wallet_consent_date)

    def _inverse_wallet_data_consent(self):
        for s in self:
            company = s.company_id.sudo()
            if s.wallet_data_consent != bool(company.wallet_consent_date):
                s.company_id._wallet_check_admin()
                company.wallet_consent_date = fields.Datetime.now() if s.wallet_data_consent else False

    @api.depends("company_id")
    def _compute_wallet_account(self):
        for s in self:
            company = s.company_id.sudo()
            a = company._wallet_account() if company.wallet_api_key else {}
            s.wallet_connected = bool(company.wallet_api_key)
            s.wallet_account_error = a is None
            a = a or {}
            plan = a.get("plan") or {}
            s.wallet_plan_name = plan.get("name")
            extra = [_("Yearly") if a.get("interval") == "year" else _("Monthly")] if a.get("subscribed") else []
            s.wallet_plan_extra = " · ".join(extra + ([_("Customer Levels")] if a.get("levels") else []))
            s.wallet_cards_used = a.get("billableCards") or 0
            s.wallet_cards_limit = a.get("limit") or plan.get("cards") or 0
            s.wallet_status = a.get("status") if a.get("status") in ("active", "past_due", "suspended", "closed", "elsewhere") else False
            s.wallet_over_limit = bool(a.get("overLimitSince"))
            s.wallet_grace_until = (a.get("graceUntil") or "")[:10] or False
            s.wallet_can_create = a.get("canCreate", True)
            missing = s.wallet_connected and not s.wallet_account_error and not a.get("levels") and company._wallet_levels_on()
            s.wallet_levels_missing = ("upgrade" if plan.get("id") == "free" else "add") if missing else False

    def action_wallet_design(self):
        return self.company_id._wallet_open_designer()

    def action_wallet_signup(self):
        return self.company_id._wallet_start()

    def action_wallet_billing(self):
        return self.company_id._wallet_billing()


class WalletCard(models.Model):
    _name = "wallet.card"
    _description = "Wallet card"
    _rec_name = "partner_id"

    partner_id = fields.Many2one("res.partner", required=True, index=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", required=True, index=True, default=lambda self: self.env.company)
    url = fields.Char(readonly=True, groups="base.group_system")
    apple_url = fields.Char(readonly=True, groups="base.group_system")
    google_url = fields.Char(readonly=True, groups="base.group_system")
    platform_url = fields.Char(compute="_compute_platform_url")
    sync_needed = fields.Boolean()
    email_pending = fields.Boolean(help="Issued in bulk; the email with the card is still to be sent.")
    state = fields.Selection([("active", "Active"), ("closed", "Closed")], compute="_compute_state", compute_sudo=True)

    if hasattr(models, "Constraint"):
        _partner_company_unique = models.Constraint("UNIQUE(partner_id, company_id)", "A customer has one card per company.")
    else:
        _sql_constraints = [("partner_company_unique", "UNIQUE(partner_id, company_id)", "A customer has one card per company.")]

    @api.depends("partner_id.active")
    def _compute_platform_url(self):
        for card in self:
            card.platform_url = card.company_id._wallet_base()

    def _compute_state(self):
        for card in self:
            card.state = "active" if card._open() else "closed"

    def _scoped(self):
        self.ensure_one()
        lang = self.partner_id.lang or self.company_id.partner_id.lang or self.env.lang
        return self.partner_id.sudo().with_company(self.company_id).with_context(lang=lang)

    def _open(self):
        return self._scoped()._wallet_card_open()

    def _push(self, raise_errors=True):
        done = 0
        for company, cards in self.sudo().grouped("company_id").items():
            if not company.wallet_api_key:
                cards.sync_needed = True
                if raise_errors:
                    raise UserError(_("Connect %s to Tieroo first: Settings → Tieroo.", company.name))
                continue
            if len(cards) == 1:
                done += cards._push_one(raise_errors)
                continue
            for chunk in split_every(BATCH, cards.ids, cards.browse):
                try:
                    done += chunk._push_batch()
                except (requests.RequestException, ValueError, KeyError) as e:
                    chunk.sync_needed = True
                    if raise_errors:
                        raise UserError(_("Tieroo could not be reached: %s", e)) from e
                    _logger.warning("tieroo: batch sync failed for %s cards of %s: %s", len(chunk), company.name, e)
        return done

    def _push_batch(self):
        company = self.company_id
        customers, close, texts, shops = [], [], {}, company._wallet_shop_points(company._wallet_shops()[:10])
        for card in self:
            ref = str(card.partner_id.id)
            if card._open():
                payload = card._scoped()._wallet_payload()
                texts.update(payload.pop("texts"))
                payload.pop("shops")
                customers.append({"ref": ref, **payload})
            else:
                close.append(ref)
        r = requests.put(f"{company._wallet_base()}/sync/v1/customers", json={"texts": texts, "shops": shops, "customers": customers, "close": close},
                         headers=company._wallet_headers(), timeout=BATCH_TIMEOUT)
        r.raise_for_status()
        results = {x["ref"]: x for x in r.json()["results"]}
        done = 0
        for card in self:
            res = results.get(str(card.partner_id.id))
            if res and res.get("error") in ("retry", "plan_limit"):
                card.sync_needed = True
                continue
            if res and "error" not in res:
                card.write({"sync_needed": False, "url": res["url"], "apple_url": res.get("apple"), "google_url": res.get("google")})
            else:
                card.sync_needed = False
                if res:
                    _logger.warning("tieroo: the platform refused card %s: %s", card.id, res["error"])
                    continue
            done += 1
        return done

    def _push_one(self, raise_errors):
        done = 0
        for card in self:
            company = card.company_id
            url = f"{company._wallet_base()}/sync/v1/customers/{card.partner_id.id}"
            headers = company._wallet_headers()
            try:
                if card._open():
                    r = requests.put(url, json=card._scoped()._wallet_payload(), headers=headers, timeout=TIMEOUT)
                    r.raise_for_status()
                    links = r.json()
                    card.write({"sync_needed": False, "url": links["url"], "apple_url": links.get("apple"), "google_url": links.get("google")})
                else:
                    requests.delete(url, headers=headers, timeout=TIMEOUT).raise_for_status()
                    card.sync_needed = False
                done += 1
            except (requests.RequestException, ValueError, KeyError) as e:
                card.sync_needed = True
                if raise_errors and getattr(getattr(e, "response", None), "status_code", None) == 402:
                    raise UserError(_("The Tieroo plan's card limit is reached: choose a bigger plan in Settings → Tieroo.")) from e
                if raise_errors:
                    raise UserError(_("Tieroo could not be reached: %s", e)) from e
                _logger.warning("tieroo: sync failed for card %s: %s", card.id, e)
        return done

    def _send_email(self, again=False):
        self.ensure_one()
        card = self.sudo()
        card.email_pending = False
        self.env["mail.mail"].sudo().browse(
            self.env.ref("tieroo.mail_template_wallet_card").sudo().with_company(card.company_id).send_mail(
                card.id,
                email_values={"model": False, "res_id": False, "auto_delete": True},
                email_layout_xmlid="mail.mail_notification_light",
            )
        )
        partner = card.partner_id.with_context(lang=card.company_id.partner_id.lang or card.env.lang)
        args = {"company": card.company_id.name, "email": partner.email}
        partner.message_post(body=partner.env._("Wallet card (%(company)s) emailed again to %(email)s.", **args) if again
                             else partner.env._("Wallet card (%(company)s) emailed to %(email)s.", **args))

    @api.model
    def _cron_sync(self):
        for company in self.env["res.company"].sudo().search([("wallet_api_key", "!=", False)]):
            Partner = self.env["res.partner"].sudo().with_company(company)
            if not Partner._wallet_issue_new_cards():
                return
            while True:
                todo = self.sudo().search([("company_id", "=", company.id), ("sync_needed", "=", True)], limit=BATCH)
                if not todo:
                    break
                pushed = todo._push(raise_errors=False)
                left = _wallet_commit(self.env, len(todo))
                if not pushed:
                    break
                if left < CRON_RESERVE:
                    Partner._wallet_wake()
                    return


class ResPartner(models.Model):
    _inherit = "res.partner"

    wallet_card_ids = fields.One2many("wallet.card", "partner_id", "Wallet cards")
    wallet_card_state = fields.Selection(
        [("none", "Not created"), ("active", "Active"), ("closed", "Closed")],
        "Wallet card",
        compute="_compute_wallet_card_state",
        compute_sudo=True,
    )

    _WALLET_FIELDS = {"name", "barcode", "active", "parent_id", "lang"}

    @api.depends("wallet_card_ids.state", "active", "commercial_partner_id")
    @api.depends_context("company")
    def _compute_wallet_card_state(self):
        for p in self:
            p.wallet_card_state = ("active" if p._wallet_card_open() else "closed") if p._wallet_card() else "none"

    def _wallet_card(self):
        self.ensure_one()
        return self.env["wallet.card"].sudo().search([("partner_id", "=", self._origin.id), ("company_id", "=", self.env.company.id)], limit=1)

    def _wallet_card_open(self):
        self.ensure_one()
        return self.active and self._wallet_is_member()

    def _wallet_is_member(self):
        self.ensure_one()
        return bool(self._wallet_loyalty_card())


    @api.model
    def _wallet_points_program(self):
        return self.env.company._wallet_loyalty_program()

    def _wallet_loyalty_card(self):
        self.ensure_one()
        program = self._wallet_points_program()
        if not program:
            return self.env["loyalty.card"]
        return self.env["loyalty.card"].sudo().search([("partner_id", "=", self.id), ("program_id", "=", program.id)], limit=1)

    def _wallet_payload(self):
        self.ensure_one()
        card = self._wallet_loyalty_card()
        program = card.program_id
        next_reward = None
        if card:
            rewards = card.program_id.reward_ids.filtered(lambda r: r.required_points > card.points).sorted("required_points")
            if rewards:
                r = rewards[0]
                next_reward = {"kind": "reward", "id": str(r.id), "name": (r.description or r.display_name)[:40], "missing": r.required_points - card.points}
        return {
            "barcode": self.barcode,
            "name": self.name,
            "company": self.parent_id.name if self.parent_id else None,
            "level": None,
            "points": card.points if card else None,
            "next": next_reward,
            "keep": None,
            "currency": None,
            "expires": card.expiration_date.isoformat() if card and card.expiration_date else None,
            "expiring": _wallet_expiring(card),
            "rewards": _wallet_rewards(program),
            "lang": self.lang or None,
            "texts": _wallet_texts(program or self._wallet_points_program()),
            "places": self._wallet_places(),
            "shops": self.env.company._wallet_shop_points(self.env.company._wallet_shops()[:10]),
        }

    def _wallet_places(self):
        self.ensure_one()
        company = self.env.company
        shops = company._wallet_shops()
        if not shops:
            return []
        bought = {}
        if "warehouse_id" in self.env["pos.config"]._fields:
            for config, count in self.env["pos.order"].sudo()._read_group(
                    [("partner_id", "=", self.id), ("company_id", "=", company.id), ("state", "not in", ("draft", "cancel"))],
                    ["config_id"], ["__count"]):
                address = config.warehouse_id.partner_id
                if address:
                    bought[address.id] = bought.get(address.id, 0) + count
        city = (self.city or "").strip().lower()
        same_city = lambda s: bool(city) and (s.partner_id.city or "").strip().lower() == city
        ranked = sorted(shops, key=lambda s: (-bought.get(s.partner_id.id, 0), not same_city(s), s.sequence, s.id))
        return company._wallet_shop_points(sorted(ranked[:10], key=lambda s: (s.sequence, s.id)))


    @api.model
    def _wallet_issue_candidates(self, limit):
        program = self._wallet_points_program()
        if not program or not program.wallet_auto_send:
            return self.browse()
        self.env.flush_all()
        self.env.cr.execute(SQL("""
            SELECT DISTINCT lc.partner_id FROM loyalty_card lc
            WHERE lc.program_id = %s AND lc.partner_id IS NOT NULL AND lc.active
              AND NOT EXISTS (SELECT 1 FROM wallet_card w WHERE w.partner_id = lc.partner_id AND w.company_id = %s)""",
            program.id, self.env.company.id))
        ids = [row[0] for row in self.env.cr.fetchall()]
        return self.sudo().search([("id", "in", ids), ("email", "!=", False)] + self._wallet_issue_domain(), limit=limit, order="id")

    @api.model
    def _wallet_issue_domain(self):
        return []

    @api.model
    def _wallet_without_card(self, partners, limit):
        return self.sudo().search([
            ("id", "in", partners.ids), ("email", "!=", False),
            ("wallet_card_ids", "not any", [("company_id", "=", self.env.company.id)]),
        ], limit=limit)

    @api.model
    def _wallet_issue_new_cards(self, limit=1000):
        Card = self.env["wallet.card"].sudo()
        candidates = self._wallet_issue_candidates(limit)
        cards = Card.search([("company_id", "=", self.env.company.id), ("email_pending", "=", True), ("sync_needed", "=", True)], limit=limit)
        for partner in candidates:
            try:
                with self.env.cr.savepoint():
                    cards |= partner._wallet_prepare_card()
            except UserError as e:
                _logger.warning("tieroo: could not issue a card to partner %s: %s", partner.id, e)
        cards.email_pending = True
        pushed = cards._push(raise_errors=False)
        pending = Card.search([("company_id", "=", self.env.company.id), ("email_pending", "=", True), ("sync_needed", "=", False), ("url", "!=", False)])
        for card in pending:
            try:
                with self.env.cr.savepoint():
                    card._send_email()
                    card.email_pending = False
            except Exception as e:
                _logger.warning("tieroo: could not email card %s: %s", card.id, e)
            if _wallet_commit(self.env) < CRON_RESERVE:
                self._wallet_wake()
                return False
        if len(candidates) == limit and pushed:
            self._wallet_wake()
        return True

    def _wallet_prepare_card(self):
        self.ensure_one()
        self._wallet_ensure_barcode()
        return self._wallet_card() or self.env["wallet.card"].sudo().create({"partner_id": self.id, "company_id": self.env.company.id})

    def _wallet_create_card(self):
        card = self._wallet_prepare_card()
        card._push()
        return card


    def write(self, vals):
        res = super().write(vals)
        if self._WALLET_FIELDS & vals.keys():
            self._wallet_mark()
        if {"email", "parent_id", "active"} & vals.keys():
            self._wallet_wake()
        if "city" in vals:
            self._wallet_mark()
        if {"partner_latitude", "partner_longitude", "city", "active"} & vals.keys():
            self.env["wallet.shop"].sudo().search([("partner_id", "in", self.ids)]).company_id._wallet_mark_all()
        return res

    def _wallet_wake(self):
        self.env.ref("tieroo.cron_wallet_sync").sudo()._trigger()

    def _wallet_mark(self):
        entities = self.commercial_partner_id | self
        cards = self.env["wallet.card"].sudo().search([
            "|", ("partner_id", "in", self.ids), ("partner_id.commercial_partner_id", "in", entities.ids),
        ])
        if cards:
            cards.write({"sync_needed": True})
            self._wallet_wake()
            self._wallet_push_after_commit(cards)

    def _wallet_push_after_commit(self, cards):
        data = self.env.cr.postcommit.data
        if "tieroo.push" not in data:
            ids = data["tieroo.push"] = set()
            registry, uid = self.env.registry, self.env.uid

            def start():
                if len(ids) <= SOON and not odoo_module.current_test:
                    threading.Thread(target=_wallet_push_soon, args=(registry, uid, ids), daemon=True).start()
            self.env.cr.postcommit.add(start)
        data["tieroo.push"].update(cards.ids)

    def _wallet_ensure_barcode(self):
        for partner in self.filtered(lambda p: not p.barcode):
            partner.barcode = "042" + "".join(secrets.choice("0123456789") for _ in range(13))


    def action_wallet_resend(self):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only administrators can send the wallet card again."))
        card = self._wallet_card()
        if not card or card.state != "active":
            raise UserError(_("This customer has no active wallet card in %s.", self.env.company.name))
        if not self.email:
            raise UserError(_("Add the customer's email address first."))
        card._push()
        card._send_email(again=True)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "success", "message": _("Wallet card sent to %s.", self.email)},
        }


class LoyaltyHistory(models.Model):
    _inherit = "loyalty.history"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.card_id.partner_id._wallet_mark()
        return records


class LoyaltyProgram(models.Model):
    _inherit = "loyalty.program"

    wallet_card = fields.Boolean(
        "Issue Tieroo card", copy=False, groups="base.group_system",
        help="Customers in this programme get a card in Apple Wallet and Google Wallet showing their points and the "
        "next reward. One loyalty programme per company.",
    )

    wallet_auto_send = fields.Boolean(
        "Send wallet cards automatically", copy=False, groups="base.group_system",
        help="Create a card for every member with an email and email it. Switching this on emails every member "
        "who has no card yet.",
    )

    @api.constrains("wallet_card", "program_type", "company_id", "active")
    def _check_wallet_card(self):
        for program in self.sudo().filtered(lambda p: p.wallet_card and p.active):
            company = [program.company_id.id, False] if program.company_id else program.env["res.company"].search([]).ids + [False]
            if program.search_count([("id", "!=", program.id), ("wallet_card", "=", True),
                                  ("program_type", "=", program.program_type), ("company_id", "in", company)]):
                raise ValidationError(_("Only one programme of this type per company can issue Tieroo cards."))

    @api.model_create_multi
    def create(self, vals_list):
        programs = super().create(vals_list)
        if programs.sudo().filtered("wallet_card"):
            self.env["res.partner"]._wallet_wake()
        return programs

    def write(self, vals):
        res = super().write(vals)
        if {"wallet_card", "active", "company_id", "program_type", "name", "portal_point_name", "currency_id"} & vals.keys():
            _wallet_programs_changed(self)
            self.env["res.partner"]._wallet_wake()
        elif "wallet_auto_send" in vals:
            self.env["res.partner"]._wallet_wake()
        return res

    def action_wallet_design(self):
        self.ensure_one()
        return (self.company_id or self.env.company)._wallet_open_designer()


class LoyaltyReward(models.Model):
    _inherit = "loyalty.reward"

    _WALLET_FIELDS = {"required_points", "active", "program_id", "description", "reward_type", "discount", "discount_mode",
                      "discount_applicability", "reward_product_id"}

    @api.model_create_multi
    def create(self, vals_list):
        rewards = super().create(vals_list)
        _wallet_programs_changed(rewards.program_id)
        return rewards

    def write(self, vals):
        before = self.program_id
        res = super().write(vals)
        if self._WALLET_FIELDS & vals.keys():
            _wallet_programs_changed(before | self.program_id)
        return res

    def unlink(self):
        programs = self.program_id
        res = super().unlink()
        _wallet_programs_changed(programs)
        return res


class LoyaltyCard(models.Model):
    _inherit = "loyalty.card"

    @api.model_create_multi
    def create(self, vals_list):
        cards = super().create(vals_list)
        if cards.partner_id:
            cards.partner_id._wallet_wake()
        return cards

    def write(self, vals):
        before = self.partner_id
        res = super().write(vals)
        if {"partner_id", "active", "program_id"} & vals.keys():
            (before | self.partner_id)._wallet_mark()
            self.partner_id._wallet_wake()
        return res

    def unlink(self):
        partners = self.partner_id
        res = super().unlink()
        partners._wallet_mark()
        return res


class WalletShop(models.Model):
    _name = "wallet.shop"
    _description = "Shop on the Tieroo card"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    on_card = fields.Boolean("On the card", default=True, help="Switch off while the shop is closed, e.g. for renovation: it stays on the list.")
    company_id = fields.Many2one("res.company", required=True, index=True, ondelete="cascade", default=lambda self: self.env.company)
    partner_id = fields.Many2one("res.partner", "Address", required=True, ondelete="cascade")
    city = fields.Char(related="partner_id.city")
    partner_latitude = fields.Float(related="partner_id.partner_latitude", readonly=False)
    partner_longitude = fields.Float(related="partner_id.partner_longitude", readonly=False)
    has_point = fields.Boolean(compute="_compute_has_point")

    @api.depends("partner_id.partner_latitude", "partner_id.partner_longitude", "partner_id.active")
    def _compute_has_point(self):
        for s in self:
            lat, lon = s.partner_id.partner_latitude, s.partner_id.partner_longitude
            s.has_point = bool(s.partner_id.active and lat and lon) and -90 <= lat <= 90 and -180 <= lon <= 180

    _CONSTRAINTS = [("partner_unique", "UNIQUE(company_id, partner_id)", "This address is already on the list.")]
    if hasattr(models, "Constraint"):
        _partner_unique = models.Constraint(_CONSTRAINTS[0][1], _CONSTRAINTS[0][2])
    else:
        _sql_constraints = _CONSTRAINTS

    @api.model_create_multi
    def create(self, vals_list):
        shops = super().create(vals_list)
        shops.company_id._wallet_mark_all()
        return shops

    def write(self, vals):
        before = self.company_id
        res = super().write(vals)
        (before | self.company_id)._wallet_mark_all()
        return res

    def unlink(self):
        companies = self.company_id
        res = super().unlink()
        companies._wallet_mark_all()
        return res

    def action_add_warehouses(self):
        company = self.env.company
        if "stock.warehouse" not in self.env:
            raise UserError(_("Inventory is not installed: add the shops' addresses one by one."))
        have = self.search([("company_id", "=", company.id)]).partner_id
        addresses = self.env["stock.warehouse"].search([("company_id", "=", company.id)]).partner_id - have
        self.create([{"company_id": company.id, "partner_id": a.id} for a in addresses])
        return {"type": "ir.actions.client", "tag": "soft_reload"}


class MailTemplate(models.Model):
    _inherit = "mail.template"

    _WALLET_TEMPLATES = ("tieroo.mail_template_wallet_card", "tieroo.mail_template_join_confirm")

    @api.model
    def _wallet_template_ids(self):
        return [t.id for t in (self.env.ref(x, raise_if_not_found=False) for x in self._WALLET_TEMPLATES) if t]

    @api.ondelete(at_uninstall=False)
    def _unlink_except_wallet(self):
        if set(self.ids) & set(self._wallet_template_ids()):
            raise UserError(_("Tieroo sends this email, so the template cannot be deleted. Change its text, or bring "
                              "back the original with Reset Template."))
