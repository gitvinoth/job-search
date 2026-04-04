# job-search

Make.com blueprint and config template for a **Daily Job Collector** (RSS → Airtable → HTTP scrape → Gemini) with **configurable job role and location**.

## Files

| File | Description |
|------|-------------|
| [MAKE_JOB_AGENT_BLUEPRINT.md](./MAKE_JOB_AGENT_BLUEPRINT.md) | Scenario design, module mapping, prompts, and Make.com field naming |
| [config.example.json](./config.example.json) | Example `job_role`, `location`, `rss_feed_url`, `resume_summary` |

Copy `config.example.json` into your Make Data store or Airtable Settings; do not commit real API keys or private resume text.

Repository: [github.com/gitvinoth/job-search](https://github.com/gitvinoth/job-search)
