# Mintt Cash Dashboard — App Status

This page exists to satisfy QuickBooks Online's app configuration requirements
(Launch URL, Disconnect URL, and Connect/Reconnect URL). The Mintt Cash
Dashboard has no user-facing web interface — there is nothing to log into or
click through here.

### What this app is

A scheduled internal process that reads financial data from Mintt's
QuickBooks Online company on a recurring basis and writes a summarized cash
report to an internal spreadsheet for Mintt's own review. It runs
automatically and unattended; no one signs in to it directly.

### Connecting / reconnecting

The connection between this app and QuickBooks Online is configured directly
by Mintt's technical administrator using OAuth credentials stored securely
outside of QuickBooks. There is no separate connect or reconnect flow for end
users to initiate from a web page.

### Disconnecting

To disconnect this app from your QuickBooks Online company, use QuickBooks
Online's own connected-apps management: **Settings → Apps → My Apps**, and
revoke access from there. That action is authoritative regardless of what
this page says.

### Questions

Contact Mintt at [insert contact email].
