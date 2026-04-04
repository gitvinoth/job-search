# Daily Job Collector — Make.com + RSS + Airtable + Gemini

End-to-end blueprint for a scheduled job-ingestion scenario with **configurable job role and location**.

Copy values from `config.example.json` into your Make.com Data store, Airtable Settings, or scenario parameters.

---

## 0. Configuration (change role/location in one place)

Pick **one** pattern; downstream modules only reference mapped fields—never hard-code role or location in multiple modules.

### Option A — Make Data store (recommended)

1. Create a data store, e.g. `job_search_config`.
2. Store: `job_role`, `location`, `rss_feed_url`, optionally `resume_summary`.
3. **First module after schedule:** Data store → get your config record.
4. Map: `{{config.job_role}}`, `{{config.location}}`, `{{config.rss_feed_url}}`.

**To change search:** edit the data store entry only.

### Option B — Airtable Settings table

- Single-row table `Settings` with fields: `Job Role`, `Location`, `RSS Feed URL`, optional `Resume Summary`.
- First module: Search records (e.g. one fixed record id).
- Map fields into RSS URL and prompts.

### Option C — Scenario parameters

- If your Make plan supports blueprint/scenario parameters, define `job_role`, `location`, `rss_feed_url` there.

### RSS URL and search text

- If RSS.app gives a **parameterized** URL, build it with **Set variable** / **Text aggregator** from `job_role` + `location`.
- If the feed URL is **opaque**, store the full `rss_feed_url` in config; when the saved search changes, **paste the new XML link** in config only.
- **Search optimization:** keep the combined query you care about (e.g. role + location) under **32 words** for Google-style indexing limits.

---

## 1. High-level flow

```
Schedule (every 3h) → Get config → RSS → Airtable Search (by Job Link)
  → Filter (bundles = 0) → Sleep 30s → HTTP GET job page
  → Gemini Summarize → Gemini Match % → Airtable Create
```

- **Duplicates:** filter so HTTP/Gemini run only when Search returns **0** bundles.
- **Rate limits:** **Sleep 30s** before HTTP when processing many new items; optional retry + sleep on 429.

---

## 2. Module mapping

| Step | Module | Notes |
|------|--------|--------|
| 0 | Schedule | Every 3 hours |
| 1 | Data store / Airtable Settings | Outputs `job_role`, `location`, `rss_feed_url` |
| 2 | RSS — Watch feed | URL = `rss_feed_url` or built from role + location |
| 3 | Airtable — Search records | Formula: `{Job Link} = "{{RSS.link}}"` |
| 4 | Filter | **Total bundles from step 3 = 0** |
| 5 | Tools — Sleep | 30 seconds (optional but recommended for batches) |
| 6 | HTTP — Make a request | GET `{{RSS.link}}` |
| 7 | Gemini — Summarizer | See §4 |
| 8 | Gemini — Matcher | See §4 |
| 9 | Airtable — Create record | Include Search Role / Search Location from config |

---

## 3. Airtable schema (suggested)

| Field | Source |
|-------|--------|
| Job Link | RSS `link` (unique; duplicate check on this) |
| Title | RSS `title` |
| Search Role | Config `job_role` |
| Search Location | Config `location` |
| Summary | Gemini summarizer output |
| Match % | Parsed integer 0–100 from matcher |

**Duplicate formula example:** `{Job Link} = "<mapped link from current RSS item>"`

---

## 4. Production prompts

**Mapping (same as §0):** Each `{{…}}` below is the **canonical field name** you insert from your **config module** (`job_role`, `location`, `resume_summary`) or from the **HTTP** / **prior Gemini** step (`HTTP_Response_Data`, `Gemini_Summary_Output`). Do not mix in a `config.` prefix unless you have a real Make item named that way.

### Module A — Summarizer (max 215 characters)

```
Target role context: {{job_role}} | {{location}}

Given the following HTML from a job posting: {{HTTP_Response_Data}}

Task: Output only a concise summary (max 215 characters) with core technologies and primary responsibilities. If the HTML is unusable, summarize any readable plain text in the input.
```

Insert **`job_role`** and **`location`** from the **same config bundle** as in §0 (UI may show e.g. `{{1.job_role}}`). Insert **`HTTP_Response_Data`** from the HTTP module’s response body.

### Module B — Job matcher (numeric output only)

```
Job summary: {{Gemini_Summary_Output}}
Advertised context: role "{{job_role}}", location "{{location}}".

Compare against this resume summary:
{{resume_summary}}

Task: Job Match Percentage 0-100 for how well the profile fits this role (skills, level, domain).
Return ONLY the integer (e.g. 85). No other text.
```

Insert **`job_role`** and **`location`** from the **same config bundle** as Module A. Insert **`Gemini_Summary_Output`** from the summarizer step. Insert **`resume_summary`** from that same config bundle (or a dedicated field mapped to `resume_summary`).

---

## 5. Gemini API (conceptual)

- **REST:** `POST` to Google Generative Language API `generateContent` for your **Gemini Pro** (or current Pro model) endpoint.
- **Body:** `contents` with user `parts[].text` = full prompt string.
- **Parse:** `candidates[0].content.parts[0].text`; for Module B, strip to digits 0–100.

**Cost note:** Two calls per job doubles usage vs one call; optional optimization: single prompt returning JSON `{"summary":"...","match":85}` and one parse step.

---

## 6. LinkedIn / career-site scraping caveat

Many sites return login walls, 403, or thin HTML to simple GET requests. If the body is empty or useless, branch to summarize RSS `description` or skip with Ignore and optional error logging.

---

## 7. Error handling

- **HTTP errors:** Router on status / body length → Sleep → one retry → else Ignore.
- **Gemini 429:** Sleep 30–60s, retry once, then Ignore.
- **Duplicates:** no HTTP/Gemini; end branch or Ignore.

---

## 8. When you change search

1. Update config: `job_role`, `location`, and `rss_feed_url` if the feed URL changed.
2. Confirm combined search text ≤ 32 words if it affects feed indexing.
3. Run once and verify new rows show **Search Role** and **Search Location**.

---

## Local files

| File | Purpose |
|------|---------|
| `config.example.json` | Copy fields into Make Data store or Airtable; rename to `config.json` locally if you use scripts (do not commit secrets). |