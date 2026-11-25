# Security policy

Quillbox handles mailbox credentials and mail content, so security reports are very welcome.

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's private reporting instead:
**Security** tab → **Report a vulnerability** on this repository.

Include what you found, how to reproduce it, and the version or commit you tested.
You should get a first response within a week.

## Supported versions

Quillbox is pre-release (v0.x). Fixes go into the latest release only.

## Scope notes

- The server listens on `127.0.0.1` by default. Exposing it (`server.host`) relies on the access token
  in `data/.auth_token`; issues that bypass that token are in scope.
- Passwords are kept in the OS credential store (or DPAPI on Windows), never in `config.yaml`.
- With `llm.provider: api`, mail content is sent to the endpoint you configure; that is by design.
