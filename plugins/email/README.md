# email

Read a mailbox over any IMAP server (Gmail, AgentMail, Fastmail and others): list folders, list and search messages, read one. The plugin only
reads. Folders are opened read-only and bodies fetched without marking anything as read, and it has no write or admin tools.

- Optional, off by default (`notify_url`): the plugin checks the visible folders for new mail and calls a Claude Code scheduled task's
  API trigger, sending folder names and message ids only (never a sender, subject or text). Set-up: [docs/GUIDE_EMAIL.md](../../docs/GUIDE_EMAIL.md) section 4.
- Set-up and troubleshooting: [docs/GUIDE_EMAIL.md](../../docs/GUIDE_EMAIL.md)
- Design and limits: [docs/email.md](../../docs/email.md)
- Settings are the `Config` class in `__init__.py`; the password is stored encrypted by the host.

All message content is returned as untrusted data. Anyone can email the mailbox, so treat what Claude reads there as text from a
stranger, and do not give the same Claude conversation tools that can send mail or act on your accounts.
