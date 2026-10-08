======================
Tieroo – Loyalty cards
======================

Your Odoo Loyalty cards in Apple Wallet and Google Wallet, updated after every purchase.

Before you start
================

* Odoo 18, 19 or 20, Community or Enterprise, on Odoo.sh or your own server.
* A loyalty programme in Odoo Loyalty (Point of Sale > Products > Discount & Loyalty).
* Your company's VAT number and country (Settings > Companies).
* Odoo can send email that reaches your customers. If your company email is on your own domain, connect
  Odoo to that domain's mail server (Settings > General Settings > Emails > Custom Email Servers, e.g. Google
  Workspace or Microsoft 365). Sent from Odoo.sh's servers under your address, the cards often land in spam
  or are refused when your domain has a strict DMARC policy, and so do your invoices.

Set up
======

1. Install **Tieroo – Loyalty cards**.
2. Open **Settings > Tieroo**.
3. Read what Odoo sends to Tieroo, tick the box to agree, and click **Start with Tieroo**.
4. The plan page opens with your company's details filled in. Choose a plan.
   The free plan is up to 100 cards.
5. Confirm with Stripe. A payment card is asked for the free plan too, but nothing is charged.
6. You are back in Odoo and connected. Nothing to copy or paste.

Design the card
===============

1. Open your loyalty programme (Point of Sale > Products > Discount & Loyalty) and tick **Wallet card**.
   One loyalty programme per company.
2. Click **Design the wallet card** (or Settings > Tieroo > Design the card).
   The designer opens with your programme already there.
3. **Look**: logo, background colour and banner.
4. **iPhone** and **Android**: drag the fields (points, next reward, name...) onto the card,
   or click a field and then a place.
5. **Translations**: names and labels in your customers' languages.
6. **Save**. Every card on every phone updates within minutes.

Send the cards
==============

* **By email**: tick *Send wallet cards automatically* on the loyalty programme.
  Every member with an email gets the card, and so does every new member.
* **Again**: a customer did not get the email? Ask them to look in spam, then open the customer, check
  the email address and click the *Wallet card* button to send it again. Emails that keep landing in spam
  mean Odoo's outgoing mail needs your domain's server (see *Before you start*).
* **With a QR poster**: install *Tieroo – Join via QR* and print the poster from the designer.

The email has *Add to Apple Wallet* and *Add to Google Wallet* buttons.
At the checkout, the cashier scans the QR code on the card in the Odoo POS as usual.

What customers see
==================

* The card shows the new points a moment after each sale.
* A notice when a reward is ready, and 14 days before points expire.
* At most one *points changed* notice in 20 hours.

Plan and billing
================

**Settings > Tieroo > Plan and billing**: change the plan, pay monthly or yearly,
add Customer Levels, see invoices or cancel.

Questions
=========

Write to support@tieroo.com.
