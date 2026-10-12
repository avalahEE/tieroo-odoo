from . import controllers, models, join, join_controllers


def uninstall_hook(env):
    import logging
    import requests
    for company in env["res.company"].sudo().search([("wallet_api_key", "!=", False)]):
        try:
            requests.delete(f"{company._wallet_base()}/sync/v1/customers", headers=company._wallet_headers(), timeout=30).raise_for_status()
        except requests.RequestException as e:
            logging.getLogger(__name__).warning("tieroo: could not close the cards of %s: %s", company.name, e)
    env.cr.execute("DROP TABLE IF EXISTS wallet_card_change")
