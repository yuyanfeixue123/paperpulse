<div align="center">

# PaperPulse

**Papers that find you, not the other way around.**

[English](README.md) · [简体中文](README.zh-CN.md)

[![CI](https://github.com/yuyanfeixue123/paperpulse/actions/workflows/ci.yml/badge.svg)](https://github.com/yuyanfeixue123/paperpulse/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Sources](https://img.shields.io/badge/sources-45%20built--in-informational.svg)](config/sources.yaml)
[![No API key for users](https://img.shields.io/badge/users-no%20API%20key%20needed-brightgreen.svg)](docs/llm-providers.md)

</div>

---

## What it does

You describe your research interest in one sentence. PaperPulse collects new papers from 45 sources every 30 minutes, filters them against your profile with an LLM, and emails you the ones worth reading at the time you chose. The ratings you leave in each email reshape the profile for next time.

```
"urban planning master's — I care about assessing the built environment with
 street-view imagery and deep learning, and about 15-minute cities and transit
 accessibility. I don't want pure algorithm theory or medical image segmentation."
                          │
                          ▼
              structured interest profile
   keywords (bilingual) · exclusions · arXiv categories · queries · thresholds
                          │
                          ▼
   arXiv  OpenAlex  DOAJ  Europe PMC  bioRxiv  OSF  Nature  Science  PLOS  NBER …
                          │  every 30 min · 3-day overlap · dedup by DOI/arXiv/title
                          ▼
              hard filter → FTS5 BM25 recall → LLM batch scoring
                          │                          (20 papers per call)
                          ▼
   final = 0.7·LLM + 0.2·taste_sim + 0.1·freshness − 0.5·already_sent
                          │
                          ▼
                 daily email ──★1–5 / not interested──▶ profile evolves
```

## Why

Existing tools make you build boolean queries, pick venues, or sit in front of a recommendation dashboard. The bottleneck isn't finding papers — it's **filtering** them. PaperPulse spends the LLM budget on that one job, keeps the whole system small enough to run on a 1 vCPU / 1 GB box, and has the deployer configure credentials **once** so end users never apply for an API key.

## Design constraints

| | |
|---|---|
| **Runtime** | Single process — uvicorn + APScheduler + a 4-thread pool. No Redis, no Celery, no Postgres |
| **Database** | SQLite in WAL mode. ~300 MB RSS with the default sources |
| **Task queue** | A `task_runs` table, not Redis. Unfinished work survives a crash and resumes on restart |
| **Install** | systemd + `uv`, or Docker for evaluation. Bare metal is the production target |
| **User onboarding** | Natural language only. Credentials belong to the deployer |
| **Structured output** | Three-tier degradation — `json_schema` → `json_object` → text + server-side repair — so almost any model works |
| **Mail** | Brevo's 300/day free tier carries ~300 active users. Quota is a hard gate: over-limit mail is deferred to the next day, never dropped silently |

## Quick start

Minimum: **1 vCPU / 1 GB RAM / 20 GB SSD / a public IP + a domain**.

### A · One-line installer (production)

```bash
git clone https://github.com/yuyanfeixue123/paperpulse.git && cd paperpulse
sudo ./scripts/install.sh --repo https://github.com/yuyanfeixue123/paperpulse.git --domain papers.example.com
```

Installs dependencies and `uv`, creates the `paperpulse` user, generates `SECRET_KEY` / `ENCRYPTION_KEY` (mode 600), initializes the database and the built-in sources, writes the systemd unit and Caddyfile, enables the service on boot, schedules a nightly 03:30 backup, then drops into the configuration wizard. Safe to re-run — it never overwrites an existing `.env`, `config/config.yaml`, or `data/`.

### B · No browser? Use the terminal wizard

Over SSH the domain and HTTPS usually aren't up yet, so the web admin is unreachable. The terminal wizard covers all six steps with an ANSI progress bar, numbered menus, and secrets kept off-screen:

```bash
python -m app.cli setup            # interactive; Ctrl-C anytime, progress is saved
python -m app.cli setup --check    # system-check report only
```

The LLM and email steps **verify connectivity before letting you continue**. Non-TTY environments (pipes, CI) automatically degrade to plain input.

### C · Manual, five steps

```bash
useradd -r -m -d /opt/paperpulse paperpulse
cd /opt/paperpulse && git clone https://github.com/yuyanfeixue123/paperpulse.git . && uv sync --frozen
cp config/config.example.yaml config/config.yaml
python -m app.cli init-db && python -m app.cli create-admin --email you@example.com
sudo cp deploy/paperpulse.service /etc/systemd/system/ && systemctl enable --now paperpulse
```

Then finish configuration with `python -m app.cli setup`, or visit `https://your.domain/admin/setup` once DNS and TLS are live.

### D · Docker (evaluation only)

```bash
cp .env.example .env   # fill in the two secrets
docker compose up -d
```

Docker is **not** the production target — it costs an extra 50–80 MB of resident memory.

## Built-in sources

Links verified on **2026-10-03**. 🟢 verified · 🟡 derived from official templates, verify on your own deployment · ⚪ unverified.

| Category | Sources |
|---|---|
| **APIs, broad** | arXiv (9 discipline feeds) 🟢 · OpenAlex 🟢 · Crossref 🟢 · DOAJ 🟢 · Europe PMC 🟢 · OSF Preprints (5 providers) 🟢 |
| **APIs, domain** | bioRxiv 🟢 · medRxiv 🟢 · ChemRxiv 🟡 · Zenodo 🟡 · HAL 🟡 |
| **Needs a key** | Semantic Scholar 🟡 · PubMed 🟡 — off by default; switches stay greyed out until a credential is saved |
| **RSS / Atom** | Nature ×3 · Science ×2 · PNAS · eLife · PLOS ONE · NBER 🟢 · Cell · Lancet · JAMA · BMJ · NEJM · bioRxiv/medRxiv · Wiley · T&F · SAGE · Springer 🟡 |
| **Custom** | Any RSS/Atom URL from the admin UI, with SSRF validation and automatic feed discovery |

Only key-free sources are enabled by default. Adding a source needs no code — one YAML block plus an existing adapter.

> ⚠️ arXiv's per-category RSS pages are gone (`info.arxiv.org/help/rss` → 404); use the API query string as a feed, at ≤1 request / 3 s.
> ⚠️ JournalTOCs stopped parsing on 2026-09-06 and Zetoc retired in 2022. No third-party TOC aggregator is used.
> Chinese-language sources: ChinaXiv is unstable (off by default); CNKI / Wanfang / VIP have no open APIs and are deliberately unsupported. Query OpenAlex / Crossref / DOAJ for their English metadata instead.

**Acknowledgment:** *Thank you to arXiv for use of its open access interoperability.*

## Configuration

`config/default.yaml` → `config/config.yaml` → environment (`PAPERPULSE_*`) → DB `system_settings`

```bash
PAPERPULSE_SECRET_KEY=...           # required in production
PAPERPULSE_ENCRYPTION_KEY=...       # required in production; rotating it invalidates stored secrets
PAPERPULSE_SOURCES__CONTACT_EMAIL=you@example.com   # joins the OpenAlex / Crossref polite pool
```

Secrets are encrypted at rest with Fernet. LLM providers ship as presets (DeepSeek, Qwen, Moonshot, Zhipu, SiliconFlow, OpenAI, Anthropic, Gemini, Ollama), and any OpenAI-compatible endpoint works. Users may bring their own key; the deployer's global credential is used when they don't.

## CLI

```bash
python -m app.cli setup                                    # 6-step terminal wizard
python -m app.cli setup --check                            # system check only
python -m app.cli setup --step 3                           # resume from a step
python -m app.cli init-db                                  # create tables + sync sources
python -m app.cli create-admin --email you@example.com     # create or promote an admin
python -m app.cli verify-sources                            # connectivity check per source
python -m app.cli test-email --to you@example.com           # send a test email
python -m app.cli run-once fetch|dispatch|digest|purge|revise [id]
```

## Operations

The admin dashboard shows user count, active subscriptions, paper-pool size, pending/failed deliveries and recent jobs. **System check** covers egress, SMTP ports, LLM, mail channel, sources, disk, memory, clock drift and secrets in one pass. A guard job then runs every 30 minutes:

| Condition | Action |
|---|---|
| Disk ≥ 80% | Purge now, retention temporarily tightened to 7 days |
| Disk ≥ 90% | Stop collecting; keep delivery and purge only, and alert |
| RSS ≥ 450 MB | Force GC and log |
| RSS ≥ 650 MB | Suspend collection, keep delivery |

Self-healing is built in: tasks stuck past their 600 s soft timeout are reset (and failed after 3 tries), `running` tasks return to `pending` on restart, and systemd restarts the process on crash with a 60 s watchdog.

## Documentation

| | |
|---|---|
| [Architecture](docs/architecture.md) | Process model, memory budget, task state machine |
| [Deployment](docs/deployment.md) | Bare-metal prerequisites, both wizards, upgrade & rollback |
| [Operations](docs/operations.md) | Inspection checklist, troubleshooting table |
| [Data sources](docs/data-sources.md) | URL templates, dedup keys, enrichment, RSS notes |
| [LLM providers](docs/llm-providers.md) | Provider matrix, three-tier degradation, BYOK |
| [Email delivery](docs/email-delivery.md) | Free-tier comparison, quota governance, anti-spam checklist |
| **Setup guides** | [Getting an LLM API key](docs/guides/llm-api-key.md) · [Configuring email delivery & DNS](docs/guides/email-delivery-setup.md) — step-by-step for deployers |

## In-site "Today's Picks"

Email is constrained by channel content policies and size; the web feed is not.
Signed-in users see **all** recommendations at `/feed` (or the home page), sorted by
relevance score, each with title, DOI, abstract excerpt and the LLM's reason, and
scorable inline.

If your mail channel rejects messages on content (common with CN providers), set
`email.content_filter_patterns` in `config/config.yaml`:

```yaml
email:
  content_filter_patterns:
    - "transgender|gender dysphoria"
```

Matching papers stay out of the email body and appear in the web feed only; the
email then states how many were withheld and links to the full list. See
[Email delivery setup](docs/guides/email-delivery-setup.md).

## Known limitations

- **Chinese input without an LLM.** The local tokenizer emits Chinese keywords while the pool holds English metadata, and FTS5's `unicode61` tokenizer cannot match across languages. Configure an LLM and bilingual keywords are produced, which removes the limitation.
- **Brevo's free tier adds a "Sent with Brevo" footer.** Switch to Resend (100/day, no footer) if that matters.
- **SES goes through its SMTP endpoint** to avoid pulling in boto3; set your own sending quota in the AWS console.
- **Bounce webhooks are not implemented.** Hard-bounce auto-pause needs a provider-side webhook or IMAP polling.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Adding a data source needs no code; the checklist is there. Security reports: [SECURITY.md](SECURITY.md).

## License

Apache-2.0 © [yuyanfeixue123](https://github.com/yuyanfeixue123) and contributors — see [LICENSE](LICENSE).
