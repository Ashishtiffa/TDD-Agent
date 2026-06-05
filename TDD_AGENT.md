# TDD Agent — D365 Technical Design Document Automation

## What Is TDD Agent?

TDD Agent is an AI-powered automation tool built for **Microsoft Dynamics 365 Finance & Operations (AX)** technical consultants and developers. It eliminates the manual effort of writing Technical Design Documents (TDDs) by automatically analyzing code changes in Azure DevOps TFVC and generating structured, formatted Word documents ready for delivery.

---

## The Problem It Solves

In D365/AX projects, every development work item requires a Technical Design Document that describes:
- What objects were created or modified (tables, classes, forms, EDTs, security objects, etc.)
- What fields, methods, and logic changed
- Why the change was made (business requirement)

**Before TDD Agent**, a developer or consultant had to:

1. Manually open Azure DevOps and find all changesets linked to a work item
2. Open each changeset, identify every changed XML file
3. Download the old and new versions of each file
4. Compare them side by side
5. Write up the changes in a Word document following the company TDD template
6. Repeat for every object (could be 10–50+ objects per work item)

For a work item with 30 objects, this process took **4–8 hours per document**. For a large project with dozens of work items, this became a significant bottleneck.

---

## How It Works

### Work Item Level Processing (Primary Flow)

```
User enters Work Item ID
        │
        ▼
Fetch Work Item from Azure DevOps API
(title, type, author, linked changesets)
        │
        ▼
Extract all "Fixed in Changeset" links
(can be 5–50 changesets per work item)
        │
        ▼
For each changeset → fetch all changed XML objects
Build: object → changeset mapping
        │
        ▼
Deduplicate objects by (name + type)
(same file can appear in Dev/UAT branch changesets)
Pick primary path = branch with most WI activity
        │
        ▼
For each unique object:
  ├── Get full object history from TFVC
  ├── Find oldest WI changeset → look 1 step back = baseline
  ├── Find latest WI changeset = final state
  └── Download baseline XML + latest XML
        │
        ▼
Send to Kimi-K2.6 AI model (via Azure APIM)
  ├── System message: static format instructions (cached)
  └── User message: baseline XML vs latest XML (billed per call)
        │
        ▼
AI extracts: new fields, modified methods, UI changes,
security changes, business logic, descriptions
        │
        ▼
Aggregate all object results
        │
        ▼
Render live preview in browser (Word-style)
        │
        ▼
Generate and download .docx file
(Hitachi Solutions branded TDD template)
```

### Object Types Supported

| AX Object Type | What Gets Extracted |
|---|---|
| **Class** | New/modified methods with descriptions |
| **Table** | Fields, field groups, indexes, relations, methods |
| **Form** | Added/modified controls, form properties, methods |
| **EDT** | Data type, base EDT it extends |
| **View** | Description of the view purpose |
| **Security** | Privileges, duties, roles with permissions |
| **Services** | Service groups and service operations |

---

## Key Technical Design Decisions

### 1. Work Item-Level vs Changeset-Level Comparison
The agent does not just compare "last changeset vs previous changeset." It finds the **true baseline** — the version of each object that existed *before the work item started* — and compares it against the **final version after all work item changes**. This gives an accurate picture of the full change, even when a single object was modified across 10 different changesets.

### 2. Multi-Branch Deduplication
D365 projects in TFVC often have Dev, UAT, and Production branches. A single work item can contain merge changesets that copy the same file across branches, causing the same logical object to appear 2–3 times in the changeset list. TDD Agent detects this, groups duplicates by `(object name + type)`, and processes each object exactly once — using the branch with the most development activity.

### 3. Prompt Caching for Cost Efficiency
Each AI call is split into two messages:
- **System message** — the format instructions and JSON schema for the object type. This is identical for every call of the same type and gets cached by the API.
- **User message** — only the actual XML code content, which changes each call.

This means the large instruction block is billed once (cached), and only the dynamic code content is billed on subsequent calls of the same type.

### 4. Rate Limit Handling
The Azure APIM gateway enforces a **20,000 token per minute** quota on the Kimi-K2.6 deployment. TDD Agent handles this with:
- **12-second delay** between every AI call (keeps throughput safely under ~4 calls/min)
- **5 automatic retries** on 429 errors, each waiting **90 seconds** (covers Azure's sliding token window — not just a hard 60s reset)
- Failed objects after all retries are **excluded from the document** (clean output) and shown in a warning panel so the user knows exactly what to re-run

---

## Efficiency Gains

| Task | Manual | TDD Agent |
|---|---|---|
| Find all changesets for a work item | 5–15 min | Automatic |
| Download and compare XML versions | 30–90 min | Automatic |
| Write fields/methods/changes | 2–5 hours | Automatic |
| Format to TDD Word template | 30–60 min | Automatic |
| **Total per work item** | **4–8 hours** | **5–20 minutes** |

The 5–20 minutes is primarily AI processing time (API calls with rate limit delays). Human effort is reduced to entering the Work Item ID and reviewing the output.

For a project with 50 work items, this saves approximately **200–400 hours** of documentation effort.

---

## Architecture

```
┌─────────────────────────────────────────┐
│              Browser (UI)               │
│         Flask + Jinja2 frontend         │
│    Live Word-style document preview     │
└────────────────┬────────────────────────┘
                 │ HTTP
┌────────────────▼────────────────────────┐
│           Flask Backend (app.py)        │
│                                         │
│  /analyze-workitem                      │
│  /analyze-changeset (fallback)          │
│  /analyze (manual object entry)         │
│  /generate-docx                         │
└──────┬──────────────────────┬───────────┘
       │                      │
┌──────▼──────┐     ┌─────────▼──────────┐
│  Azure      │     │  Kimi-K2.6 AI      │
│  DevOps     │     │  via Azure APIM    │
│  TFVC API   │     │  (OpenAI SDK)      │
│             │     │                    │
│  Work items │     │  Prompt caching    │
│  Changesets │     │  Rate limit retry  │
│  XML files  │     │  5 retries × 90s   │
└─────────────┘     └────────────────────┘
       │
┌──────▼──────────────────────────────────┐
│         Support Services                │
│                                         │
│  WorkItemAnalyzerService                │
│    - Changeset extraction               │
│    - Multi-branch deduplication         │
│    - Baseline/latest resolution         │
│                                         │
│  ChangesetAnalyzerService               │
│    - Single changeset fallback          │
│                                         │
│  ObjectTypeDetector                     │
│    - TFVC path → AX object type         │
│                                         │
│  DocxGenerator                          │
│    - Hitachi Solutions branded .docx    │
└─────────────────────────────────────────┘
```

---

## Input / Output

**Input:**
- Work Item ID (e.g. `98204`)
- Azure DevOps credentials in `.env` (PAT, org, project)
- Kimi-K2.6 API key in `.env`

**Output:**
- Live preview of the TDD document in the browser
- Downloadable `.docx` file with:
  - Cover page (work item title, author, date, revision table)
  - Table of contents (auto-generated)
  - Section 1: Technical Design Planning
  - Section 2: Functional Requirement
  - Section 3: Visual Studio Project
  - Section 4: User Interface (Forms)
  - Section 5: Data Dictionary (Tables, Views, EDTs)
  - Section 6: Application Components (Classes)
  - Section 7: Services
  - Section 8: Security

---

## Running the Agent

```bash
# Install dependencies
pip install flask openai python-docx python-dotenv requests

# Configure credentials
# Edit .env and set:
# KIMI_API_KEY=your_key
# AZURE_DEVOPS_ORG=your_org
# AZURE_DEVOPS_PROJECT=your_project
# AZURE_DEVOPS_PAT=your_pat

# Start the server
python app.py

# Open in browser
http://localhost:5000
```

---

## Limitations

- Processes only `.xml` files (AX metadata format). Source code files (`.xpp`) are not analyzed.
- Rate limited to ~4 objects per minute by the Azure APIM 20K TPM quota. A work item with 40 objects takes approximately 10–15 minutes to fully process.
- Very large XML files (>15,000 tokens) may exhaust the per-call quota and fail even after retries. These are flagged in a warning panel.
- Requires Azure DevOps TFVC. Git-based repositories are not supported.
