import re

import babel
import babel.dates

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request
from odoo.tools import email_normalize

from .join import ip_hash

TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _native(code):
    try:
        name = babel.Locale.parse(code).get_language_name(code) or code
    except (ValueError, babel.UnknownLocaleError):
        return code
    return name[:1].upper() + name[1:]


class WalletSignup(http.Controller):

    def _visitor_lang(self, active, chosen=None):
        if chosen in active:
            return chosen
        for value, _q in request.httprequest.accept_languages:
            code = value.replace("-", "_")
            match = next((c for c in active if c.lower() == code.lower()), None) or next(
                (c for c in active if c.split("_")[0] == code.split("_")[0].lower()), None)
            if match:
                return match
        return "en_US" if "en_US" in active else (active[0] if active else "en_US")

    def _page(self, token, chosen_lang=None):
        company = request.env["res.company"].sudo().search([("wallet_signup_token", "=", token)], limit=1) if TOKEN.match(token) else None
        if not company:
            raise request.not_found()
        active = request.env["res.lang"].sudo().search([("active", "=", True)]).mapped("code")
        lang = self._visitor_lang(active, chosen_lang)
        request.update_context(lang=lang)
        return company, {
            "company": company,
            "brand": company._wallet_brand(),
            "lang": lang,
            "langs": [(code, _native(code)) for code in active] if len(active) > 1 else [],
            "months": list(babel.dates.get_month_names("wide", locale=lang.split("_")[0] if lang else "en").items()),
            "post": {},
            "error": None,
            "card": None,
            "state": "form" if company._wallet_signup_open() else "closed",
        }

    @http.route("/wallet/join/<string:token>", type="http", auth="public", methods=["GET", "POST"], sitemap=False)
    def join(self, token, **post):
        company, values = self._page(token, post.get("lang"))
        values["post"] = post
        if request.httprequest.method == "POST" and values["state"] == "form":
            if post.get("website"):
                values["state"] = "sent"
            else:
                name, email = (post.get("name") or "").strip(), (post.get("email") or "").strip()
                env = request.env(context=dict(request.env.context, lang=values["lang"]))
                if not name or len(name) > 120:
                    values["error"] = env._("Enter your name.")
                elif not email_normalize(email):
                    values["error"] = env._("Enter a valid email address.")
                elif not post.get("consent"):
                    values["error"] = env._("Agree to the terms to join.")
                else:
                    day, month = post.get("day"), post.get("month")
                    try:
                        values["state"] = request.env["res.partner"].sudo()._wallet_signup_request(
                            company, name, email, lang=values["lang"], birthday=(day, month) if day and month else None,
                            ip_hash=ip_hash(request.env, request.httprequest.remote_addr or ""))
                    except UserError as e:
                        values["error"] = str(e)
        return request.render("tieroo.join_page", values)

    @http.route("/wallet/join/<string:token>/confirm/<string:rtoken>", type="http", auth="public", methods=["GET", "POST"], sitemap=False)
    def confirm(self, token, rtoken, **post):
        company, values = self._page(token)
        req = request.env["wallet.signup.request"].sudo()._find(company, rtoken) if TOKEN.match(rtoken) else None
        if req and (post.get("lang") or req.lang):
            company, values = self._page(token, post.get("lang") or req.lang)
        if not req:
            values["state"] = "expired"
        elif req.partner_id and req.partner_id.with_company(company)._wallet_card():
            values["state"] = "used"
        elif values["state"] == "form":
            values["state"] = "confirm"
            if request.httprequest.method == "POST":
                try:
                    card = req._confirm().sudo()
                    values.update(state="done", card={"apple": card.apple_url, "google": card.google_url, "page": card.url})
                except UserError:
                    values["state"] = "error"
        return request.render("tieroo.join_page", values)
