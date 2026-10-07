from odoo.tools.sql import column_exists


def migrate(cr, version):
    if column_exists(cr, "res_company", "wallet_auto_send"):
        cr.execute("UPDATE res_company SET wallet_levels_auto_send = TRUE WHERE wallet_auto_send")
