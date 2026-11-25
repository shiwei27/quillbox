# Quillbox

Local-first AI mail workspace: syncs your mailboxes (IMAP / POP3 / Exchange EWS; Outlook on Windows is experimental)
into a local SQLite store, optionally builds a local vector index, and uses an LLM of your choice (local Ollama
by default, or any OpenAI-compatible API) for summaries, action items, thread insight, reply drafts and rule
generation. Ships a web UI and an MCP server.

本地优先的 AI 邮件工作台：把邮箱同步到本机 SQLite（可选本地向量库），用你自选的大模型（默认本地 Ollama，
也可接任何 OpenAI 兼容接口）做摘要、待办提取、会话洞察、回复起草与规则生成；提供 Web 界面与 MCP server。

> Status: pre-release (v0.1 in preparation). Interfaces and setup steps will change.

## Requirements

- Python 3.10+
- macOS, Linux or Windows

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .                 # minimal: IMAP/POP3/SMTP, keyword search
pip install -e ".[all]"          # everything: vector search, Exchange, attachment text, Windows extras
```

| extra | adds |
|---|---|
| `vector` | semantic search (chromadb, sentence-transformers, torch). Without it, search is keyword-only. |
| `ews` | Exchange (EWS) accounts |
| `attachments` | reading text from xlsx / pdf / docx attachments |
| `windows` | DPAPI credential storage and the experimental Outlook COM backend (Windows only) |

## Configure

```bash
cp config.example.yaml config.yaml          # fill in your accounts
python pop3_setup.py set you@example.com    # store the mail password in the OS credential store
```

- Passwords never go into `config.yaml`. They are stored in the macOS Keychain / Linux Secret Service
  (Windows: DPAPI-encrypted file by default; set `QUILLBOX_CRED_BACKEND=keyring` to use Credential Manager).
- LLM: `llm.provider: ollama` (default) or `api`. For `api`, set `QUILLBOX_API_KEY` in the environment.
  **With `api`, mail content is sent to the endpoint you configure.**

### Outlook backend (experimental)

On Windows with desktop Outlook installed, `email.outlook_enabled: true` reads mail through Outlook COM
(needs the `windows` extra). It is off by default, not covered by CI, and may change or be removed;
IMAP / POP3 / EWS are the supported paths.

## Run

```bash
python server.py     # web UI on http://127.0.0.1:8765 (loopback only by default)
python daemon.py     # background sync + AI worker
```

The server listens on `127.0.0.1` unless `server.host` is set. If you expose it, remote requests require the
access token in `data/.auth_token`; prefer SSH port forwarding or an authenticating reverse proxy.

## Tests

```bash
pip install -e ".[test]"
python run_tests.py                 # unit tier: offline, no server / browser / mailbox / model needed
python run_tests.py --list          # every test and its tier (see test_tiers.txt)
```

Tests are standalone scripts (exit code 0 = pass). The `unit` tier runs in CI; `vector` needs the
`[vector]` extra and `windows` needs Windows. End-to-end tests (running instance, browser, real mailbox,
real model) are not included in this release yet.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
