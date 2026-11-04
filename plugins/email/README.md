# email

Read a mailbox over IMAP, built for Gmail with an app password: list mailboxes, list and search messages, read one. The plugin only
reads. Mailboxes are opened read-only and bodies fetched without marking anything as read, and it has no write or admin tools.

- Set-up and troubleshooting: [docs/GUIDE_EMAIL.md](../../docs/GUIDE_EMAIL.md)
- Design and limits: [docs/email.md](../../docs/email.md)
- Settings are the `Config` class in `__init__.py`; the password is stored encrypted by the host.

All message content is returned as untrusted data. Anyone can email the mailbox, so treat what Claude reads there as text from a
stranger, and do not give the same Claude conversation tools that can send mail or act on your accounts.
