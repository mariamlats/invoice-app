# Security

For configuration and deployment requirements, see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

Please do not put credentials, customer data, invoice PDFs or signatures in public issues. Use GitHub private vulnerability reporting if it is enabled, or contact the repository owner privately using the contact details on their GitHub profile.

Keep application dependencies up to date and run `python -m pip_audit -r requirements.txt` when upgrading. A passing scan is a point-in-time check against known dependency advisories, not a guarantee that an application has no security issues.
