from odoo.tools.sql import column_exists


def migrate(cr, version):
    if column_exists(cr, "res_company", "wallet_points_program_id"):
        cr.execute("""
            UPDATE loyalty_program p SET wallet_card = TRUE
              FROM res_company c
             WHERE c.wallet_points_program_id = p.id AND p.program_type = 'loyalty'
               AND NOT EXISTS (SELECT 1 FROM loyalty_program o
                                WHERE o.wallet_card AND o.program_type = 'loyalty'
                                  AND (o.company_id = c.id OR o.company_id IS NULL))
        """)
    if column_exists(cr, "res_company", "wallet_auto_send"):
        cr.execute("""
            UPDATE loyalty_program p SET wallet_auto_send = TRUE
              FROM res_company c
             WHERE c.wallet_auto_send AND p.wallet_card AND p.program_type = 'loyalty'
               AND (p.company_id = c.id OR p.company_id IS NULL)
        """)
