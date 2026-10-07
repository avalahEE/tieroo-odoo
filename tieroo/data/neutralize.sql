-- A copy of the database (an Odoo.sh staging branch, odoo-bin neutralize) must not change real customers' cards:
-- without the sync key it cannot reach Tieroo. Connect the copy with Start with Tieroo on a test account if needed.
UPDATE res_company SET wallet_api_key = NULL, wallet_claim_token = NULL;
