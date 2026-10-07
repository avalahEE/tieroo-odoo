from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

WAIT_TRIES = 20


class TierooReturn(http.Controller):

    @http.route("/tieroo/return", type="http", auth="user", methods=["GET"], sitemap=False)
    def tieroo_return(self, company=None, tieroo=None, attempt="0", **kw):
        env = request.env
        if not env.user.has_group("base.group_system"):
            raise Forbidden()
        company = env["res.company"].browse(int(company)) if company and company.isdigit() else env.company
        if company not in env.user.company_ids:
            raise Forbidden()
        if tieroo == "cancel":
            company.sudo().wallet_signup_notice = "cancelled"
            return request.redirect(company._wallet_settings_url(), local=False)
        values = {"company": company, "error": None, "refresh": None, "retry": None}
        try:
            if company._wallet_claim() == "done":
                return request.redirect(company._wallet_settings_url(), local=False)
            attempt = int(attempt) if attempt.isdigit() else 0
            url = f"/tieroo/return?company={company.id}&tieroo=done&attempt=%s"
            if attempt < WAIT_TRIES:
                values["refresh"] = url % (attempt + 1)
            else:
                values["retry"] = url % 0
        except UserError as e:
            values["error"] = str(e)
        values["settings"] = company._wallet_settings_url()
        return request.render("tieroo.tieroo_return_page", values)
