import calendar
import hashlib
import logging
import re
import secrets
import time
from datetime import timedelta

import requests


from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import email_normalize

_logger = logging.getLogger(__name__)
SIGNUPS_PER_HOUR = 30
RESEND_AFTER = timedelta(minutes=2)
_BRAND = {}
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ResCompany(models.Model):
    _inherit = "res.company"

    wallet_signup_token = fields.Char(copy=False, readonly=True, groups="base.group_system")
    wallet_terms_url = fields.Char("Loyalty programme terms URL", help="Linked from the join page's consent checkbox: \"agree to its terms\".")
    wallet_privacy_url = fields.Char("Privacy policy URL", help="Linked from the join page's consent checkbox.")
    wallet_signup_tag_id = fields.Many2one(
        "res.partner.category", "Tag for new members",
        default=lambda self: self.env.ref("tieroo.tag_joined_via_qr", raise_if_not_found=False),
        help="Added to every customer who joins via the QR. Use it in Odoo Automation Rules or Marketing "
        "Automation, e.g. a welcome email or a newsletter. Empty = no tag.",
    )

    if hasattr(models, "Constraint"):
        _wallet_signup_token_unique = models.Constraint("UNIQUE(wallet_signup_token)", "The join link must be unique.")
    else:
        _sql_constraints = [("wallet_signup_token_unique", "UNIQUE(wallet_signup_token)", "The join link must be unique.")]

    @api.constrains("wallet_terms_url", "wallet_privacy_url")
    def _check_wallet_signup_urls(self):
        for c in self:
            if any(u and not u.startswith(("https://", "http://")) for u in (c.wallet_terms_url, c.wallet_privacy_url)):
                raise ValidationError(_("Links must start with https://"))

    @api.model
    def _wallet_signup_set_default_tag(self):
        tag = self.env.ref("tieroo.tag_joined_via_qr")
        self.sudo().search([("wallet_signup_tag_id", "=", False)]).wallet_signup_tag_id = tag

    def _wallet_signup_url(self):
        self.ensure_one()
        company = self.sudo()
        if not company.wallet_signup_token:
            company.wallet_signup_token = secrets.token_urlsafe(16)
        return f"{company.get_base_url()}/wallet/join/{company.wallet_signup_token}"

    def _wallet_signup_open(self):
        self.ensure_one()
        company = self.sudo()
        return bool(company._wallet_join_target() and company.wallet_api_key)

    def _wallet_join_target(self):
        return bool(self._wallet_loyalty_program())

    def _wallet_brand(self):
        self.ensure_one()
        company = self.sudo()
        hit = _BRAND.get((self.env.cr.dbname, company.id))
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        brand = hit[1] if hit else {}
        base = company._wallet_base()
        if company.wallet_api_key:
            try:
                r = requests.get(f"{base}/sync/v1/brand", headers=company._wallet_headers(), timeout=3)
                r.raise_for_status()
                data = r.json()
                brand = {
                    "logo": data.get("logoUrl") if str(data.get("logoUrl") or "").startswith(("https://", "http://")) else None,
                    "background": data.get("background") if COLOR.match(str(data.get("background"))) else None,
                    "foreground": data.get("foreground") if COLOR.match(str(data.get("foreground"))) else None,
                }
            except (requests.RequestException, ValueError, AttributeError) as e:
                _logger.info("tieroo: join page brand not available for company %s: %s", company.id, e)
        _BRAND[(self.env.cr.dbname, company.id)] = (time.monotonic(), brand)
        return brand

    def _wallet_design_context(self):
        ctx = super()._wallet_design_context()
        ctx["signupUrl"] = self._wallet_signup_url()
        p = self.partner_id
        address = {
            "company": self.name, "contact": self.env.user.name, "street": p.street, "street2": p.street2, "zip": p.zip,
            "city": p.city, "country": p.country_id.code, "email": p.email or self.env.user.email, "phone": p.phone,
        }
        ctx["address"] = {k: str(v)[:120] for k, v in address.items() if v}
        return ctx


class ResPartner(models.Model):
    _inherit = "res.partner"

    wallet_birth_day = fields.Integer("Birthday (day)")
    wallet_birth_month = fields.Selection([(str(m), calendar.month_name[m]) for m in range(1, 13)], "Birthday (month)")

    @api.constrains("wallet_birth_day", "wallet_birth_month")
    def _check_wallet_birthday(self):
        for p in self:
            if bool(p.wallet_birth_day) != bool(p.wallet_birth_month) or (p.wallet_birth_day and not _valid_day(p.wallet_birth_day, p.wallet_birth_month)):
                raise ValidationError(_("Enter the birthday as a day and a month that exist (29 February is fine)."))

    @api.model
    def _wallet_signup_request(self, company, name, email, lang=None, birthday=None, ip_hash=""):
        company = company.sudo()
        Request = self.env["wallet.signup.request"].sudo()
        now = fields.Datetime.now()
        Request.search([("create_date", "<", now - timedelta(days=7))]).unlink()
        if not company._wallet_signup_open():
            return "closed"
        if ip_hash and Request.search_count([("ip_hash", "=", ip_hash), ("create_date", ">", now - timedelta(hours=1))]) >= SIGNUPS_PER_HOUR:
            return "limited"
        email = email_normalize(email)
        if not email:
            raise UserError(_("Enter a valid email address."))
        if Request.search_count([("company_id", "=", company.id), ("email", "=", email), ("create_date", ">", now - RESEND_AFTER)]):
            return "sent"
        day, month = _birthday(birthday)
        token = secrets.token_urlsafe(24)
        Request.create({
            "company_id": company.id, "email": email, "name": name.strip()[:120], "lang": lang or False,
            "birth_day": day, "birth_month": month, "ip_hash": ip_hash, "token_hash": _hash(token),
        })._send_confirmation(token)
        return "sent"


def _valid_day(day, month):
    try:
        day, month = int(day), int(month)
    except (TypeError, ValueError):
        return False
    return 1 <= month <= 12 and 1 <= day <= calendar.monthrange(2000, month)[1]


def _birthday(birthday):
    if birthday and _valid_day(*birthday):
        return int(birthday[0]), str(int(birthday[1]))
    return 0, False


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class WalletSignupRequest(models.Model):
    _name = "wallet.signup.request"
    _description = "loyalty programme sign-up"

    company_id = fields.Many2one("res.company", required=True, ondelete="cascade")
    email = fields.Char(required=True, index=True)
    name = fields.Char()
    lang = fields.Char()
    birth_day = fields.Integer()
    birth_month = fields.Char()
    ip_hash = fields.Char(index=True)
    token_hash = fields.Char(index=True)
    partner_id = fields.Many2one("res.partner", ondelete="cascade")

    def _send_confirmation(self, token):
        self.ensure_one()
        url = f"{self.company_id.sudo()._wallet_signup_url()}/confirm/{token}"
        self.env.ref("tieroo.mail_template_join_confirm").sudo().with_context(confirm_url=url).send_mail(
            self.id,
            email_values={"model": False, "res_id": False, "auto_delete": True},
            email_layout_xmlid="mail.mail_notification_light",
        )
        self.env.ref("mail.ir_cron_mail_scheduler_action").sudo()._trigger()

    @api.model
    def _find(self, company, token):
        return self.sudo().search([
            ("company_id", "=", company.id), ("token_hash", "=", _hash(token)),
            ("create_date", ">", fields.Datetime.now() - timedelta(hours=48)),
        ], limit=1)

    def _confirm(self):
        self.ensure_one()
        company = self.company_id.sudo()
        env = self.sudo().with_company(company).env
        if not company._wallet_signup_open():
            raise UserError(_("Joining is not open at the moment"))
        program = company._wallet_loyalty_program()
        if self.partner_id and (card := self.partner_id.with_company(company)._wallet_card()):
            return card
        if not self.partner_id:
            partner = env["res.partner"].with_context(active_test=False).search([
                ("email_normalized", "=", self.email), ("company_id", "in", [company.id, False]), ("is_company", "=", False), ("parent_id", "=", False), ("vat", "=", False),
                ("type", "=", "contact"), "|", ("user_ids", "=", False), ("user_ids.share", "=", True),
            ], order="active desc, id", limit=1) or env["res.partner"].create({"name": self.name or self.email, "email": self.email, **({"lang": self.lang} if self.lang else {})})
            if not partner.active:
                partner.active = True
            if company.wallet_signup_tag_id:
                partner.category_id = [(4, company.wallet_signup_tag_id.id)]
            if self.birth_month and not partner.wallet_birth_month:
                partner.write({"wallet_birth_day": self.birth_day, "wallet_birth_month": self.birth_month})
            if program:
                loyalty = env["loyalty.card"].with_context(active_test=False).search(
                    [("partner_id", "=", partner.id), ("program_id", "=", program.id)], order="active desc, id", limit=1)
                if not loyalty:
                    env["loyalty.card"].create({"program_id": program.id, "partner_id": partner.id})
                elif not loyalty.active:
                    loyalty.active = True
            note_env = partner.with_context(lang=company.partner_id.lang or env.lang).env
            partner.message_post(body=note_env._("Joined %(program)s via the QR code: confirmed the email address and agreed to the terms (%(company)s).",
                                                 program=program.name or company.name, company=company.name))
            self.sudo().partner_id = partner
        partner = self.partner_id.with_company(company)
        partner._wallet_signup_joined()
        with env.cr.savepoint():
            had_card = bool(partner._wallet_card())
            card = partner._wallet_create_card()
            card._send_email(again=had_card)
        return card


def ip_hash(env, address):
    params = env["ir.config_parameter"].sudo()
    secret = (params.get_str if hasattr(params, "get_str") else params.get_param)("database.secret") or ""
    return hashlib.sha256(f"{secret}:{address}".encode()).hexdigest()


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    wallet_signup_url = fields.Char("Join link", compute="_compute_wallet_signup_url")
    wallet_signup_tag_id = fields.Many2one(related="company_id.wallet_signup_tag_id", readonly=False)
    wallet_terms_url = fields.Char(related="company_id.wallet_terms_url", readonly=False)
    wallet_privacy_url = fields.Char(related="company_id.wallet_privacy_url", readonly=False)

    @api.depends("company_id")
    def _compute_wallet_signup_url(self):
        for s in self:
            s.wallet_signup_url = s.company_id._wallet_signup_url() if self.env.user.has_group("base.group_system") else False

    def action_wallet_signup_posters(self):
        return self.company_id._wallet_open_designer(start="poster")
