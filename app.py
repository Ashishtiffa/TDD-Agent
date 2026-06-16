from flask import Flask, render_template, request, jsonify, send_file
from openai import OpenAI
import json
import io
import os
import time
import threading
import re
import difflib
from dotenv import load_dotenv, set_key
from docx_generator import generate_tdd_docx
from table_xml_parser import enrich_table_result
from ax_xml_parser import parse_object as _xml_parse_object
from azure_devops_service import AzureDevOpsService
from changeset_analyzer_service import ChangesetAnalyzerService
from workitem_analyzer_service import WorkItemAnalyzerService

load_dotenv()

# ---------------------------------------------------------------------------
# Sliding-window TPM rate limiter
# Tracks every AI call's token count in a 60-second window.  A new call is
# allowed immediately if (used_in_window + est_tokens) <= TPM_LIMIT.
# If the window is full it waits only until the oldest entry expires, then
# re-checks — never over-waiting or blocking other workers unnecessarily.
# ---------------------------------------------------------------------------
_TPM_LIMIT  = 20_000
_TPM_WINDOW = 60.0
_ai_lock    = threading.Lock()
_token_window: list = []   # [(monotonic_timestamp, token_count)]

_session_usage = {"prompt": 0, "completion": 0, "total": 0}
_usage_lock = threading.Lock()

def _acquire_ai_slot(est_tokens: int):
    """Block until the TPM sliding window has room, then reserve est_tokens."""
    global _token_window
    while True:
        with _ai_lock:
            now = time.monotonic()
            # Drop entries older than the window
            _token_window = [(t, n) for t, n in _token_window if now - t < _TPM_WINDOW]
            used = sum(n for _, n in _token_window)
            if used + est_tokens <= _TPM_LIMIT:
                _token_window.append((now, est_tokens))
                return          # slot acquired — caller may proceed
            # Window is full: wait until the oldest entry expires
            oldest = min(t for t, _ in _token_window)
            wait = _TPM_WINDOW - (now - oldest) + 0.5
        # Sleep OUTSIDE the lock so other threads can still check/acquire
        print(f"  TPM window {used}/{_TPM_LIMIT} tokens used — waiting {wait:.0f}s")
        time.sleep(min(wait, 10.0))   # re-check every 10 s in case window clears sooner

app = Flask(__name__)

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

# Initialize Services
ado_org = os.environ.get("AZURE_DEVOPS_ORG")
ado_project = os.environ.get("AZURE_DEVOPS_PROJECT")
ado_pat = os.environ.get("AZURE_DEVOPS_PAT")
azure_service = AzureDevOpsService(ado_org, ado_pat, ado_project) if ado_org and ado_pat else None

ENDPOINT = "https://apim-sj-foundry-eastus2.azure-api.net/sj-foundry-eastus2-resource/openai/v1"
DEPLOYMENT = "Kimi-K2.6-1"

def _extract_name_from_xml(xml_content: str) -> str:
    """Extract the object name from the root <Name> element of any AX XML file.
    Matches the <Name> that is a direct child of the root AX element so nested
    DataSource/Table/Relation names are never picked up by mistake."""
    if not xml_content:
        return ''
    # Match <Name> immediately after the root AX opening tag
    match = re.search(
        r'<Ax[A-Za-z]+(?:\s[^>]*)?>[\s\S]{0,200}?<Name>([^<]+)</Name>',
        xml_content
    )
    if match:
        return match.group(1).strip()
    return ''


def _make_diff(old_code: str, new_code: str, context: int = 6) -> str:
    """Compute a unified diff between old and new code (6 lines of context)."""
    old_lines = old_code.splitlines(keepends=True)
    new_lines = new_code.splitlines(keepends=True)
    return ''.join(difflib.unified_diff(
        old_lines, new_lines,
        fromfile='baseline', tofile='latest',
        n=context
    ))


def _find_enclosing_method(source_lines: list, target_line_1based: int) -> str:
    """Walk backward from target_line in X++ source to find the enclosing method name.

    Looks for a line that starts with a recognised X++ access modifier followed by
    a return-type word and then the method name before '('.  This mirrors what
    'git diff' prints after the @@ marker as function context.
    """
    method_re = re.compile(
        r'^\s*(?:public|private|protected|internal)\s+'  # access modifier
        r'(?:(?:static|final|abstract|display|edit|server|client)\s+)*'  # optional qualifiers
        r'\w[\w<>\[\]]*\s+'                               # return type
        r'(\w+)\s*\(',                                    # method name (
    )
    idx = min(target_line_1based - 1, len(source_lines) - 1)
    for i in range(idx, -1, -1):
        m = method_re.match(source_lines[i])
        if m:
            return m.group(1)
    return ''


def _annotate_diff_with_methods(diff: str, source_code: str) -> str:
    """Append the enclosing method name to each @@ hunk header that lacks one.

    This lets the AI identify which method a hunk belongs to even when the
    method signature line falls outside the context window.
    """
    if not diff or not source_code:
        return diff
    source_lines = source_code.splitlines()
    hunk_re = re.compile(r'^(@@ -(\d+),?\d* \+\d+,?\d* @@)([ \t]*)$')
    result = []
    for line in diff.splitlines(keepends=True):
        m = hunk_re.match(line.rstrip('\n').rstrip('\r'))
        if m:
            old_start = int(m.group(2))
            method = _find_enclosing_method(source_lines, old_start)
            if method:
                line = line.rstrip('\n').rstrip('\r') + f' {method}\n'
        result.append(line)
    return ''.join(result)


def perform_ai_analysis(object_type, is_new, old_code, new_code, object_name):
    # Diff compression: for modified objects, replace old+new with a unified diff
    # when the diff is smaller. Unchanged methods are excluded, slashing token usage
    # for large classes (e.g. 28K tokens → 6K tokens).
    effective_old = old_code
    effective_new = new_code
    diff_mode = False

    if not is_new and old_code and new_code:
        diff = _make_diff(old_code, new_code)
        # Annotate @@ hunk headers with the enclosing method name so the AI can
        # identify the method even when its signature is outside the context window.
        diff = _annotate_diff_with_methods(diff, old_code)
        combined_len = len(old_code) + len(new_code)
        # Use diff if it saves any tokens (threshold: diff < 95% of combined).
        # Views/forms/queries can be very large XMLs — even a 10% saving avoids TPM overflows.
        if diff and len(diff) < combined_len * 0.95:
            effective_old = diff
            effective_new = ''
            diff_mode = True
            saving_pct = 100 - (100 * len(diff) // combined_len)
            print(f"  [{object_name}] diff compression: {combined_len:,} → {len(diff):,} chars (saved {saving_pct}%)")

    # HYBRID PATH: XML parser extracts structure; LLM writes descriptions only.
    # For report and unknown types, fall through to the original full-XML LLM path.
    use_hybrid = object_type in _HYBRID_TYPES

    if use_hybrid:
        # Classes: diff tells us exactly which methods changed
        # XML types: prefer diff when available (only changed hunks → only changed items)
        #            fall back to full old_code vs new_code when no diff
        if object_type in ('class', 'class_extension'):
            parsed_facts = _xml_parse_object(object_type, effective_old, effective_new, is_new, diff_mode, object_name=object_name)
        elif diff_mode:
            # effective_old = unified diff; parser reconstructs before/after states internally
            parsed_facts = _xml_parse_object(object_type, effective_old, '', is_new, diff_mode=True, object_name=object_name)
        else:
            parsed_facts = _xml_parse_object(object_type, old_code, new_code, is_new, diff_mode=False, object_name=object_name)

        if parsed_facts is None:
            use_hybrid = False  # unknown type — fall back
        else:
            messages = _build_desc_messages(object_type, object_name, parsed_facts, is_new)
            print(f"  [{object_name}] hybrid mode — LLM asked for descriptions only")

    if not use_hybrid:
        # Original path: send full XML/diff to LLM and ask for full JSON
        messages = build_messages(object_type, is_new, effective_old, effective_new, object_name, diff_mode=diff_mode)

    api_key = os.environ.get("KIMI_API_KEY")
    if not api_key:
        raise Exception("KIMI_API_KEY not found in .env")

    client = OpenAI(
        base_url=ENDPOINT,
        api_key="unused",  # real auth is the api-key header below
        default_headers={"api-key": api_key},
        timeout=120.0,  # 2-minute hard timeout per request — prevents infinite hang
    )

    # Wait for a safe slot before calling the AI — serializes concurrent workers
    # so their combined token usage never bursts past the 20K TPM window.
    est_tokens = sum(len(m.get('content', '')) for m in messages) // 4
    print(f"  [{object_name}] ~{est_tokens} tokens — acquiring AI slot")
    _acquire_ai_slot(est_tokens)

    # On 429, wait 60s per retry. Azure uses a sliding TPM window.
    max_retries = 5
    raw = None
    for attempt in range(max_retries):
        try:
            completion = client.chat.completions.create(
                model=DEPLOYMENT,
                messages=messages,
            )
            raw = completion.choices[0].message.content
            usage = completion.usage
            if usage:
                pt = usage.prompt_tokens or 0
                ct = usage.completion_tokens or 0
                tt = usage.total_tokens or (pt + ct)
                print(f"  [{object_name}] tokens — prompt:{pt} completion:{ct} total:{tt}")
                with _ai_lock:
                    for i in range(len(_token_window) - 1, -1, -1):
                        if _token_window[i][1] == est_tokens:
                            _token_window[i] = (_token_window[i][0], tt)
                            break
                with _usage_lock:
                    _session_usage["prompt"]     += pt
                    _session_usage["completion"] += ct
                    _session_usage["total"]      += tt
            break
        except Exception as e:
            err = str(e)
            if '429' in err or 'RateLimitReached' in err:
                if attempt < max_retries - 1:
                    print(f"Rate limit hit for '{object_name}' — waiting 60s (attempt {attempt + 1}/{max_retries})...")
                    time.sleep(60)
                else:
                    raise Exception(
                        f"AI analysis failed after {max_retries} retries due to rate limit. "
                        "The object may be too large for the 20K TPM quota in a single call."
                    )
            elif 'timeout' in err.lower() or 'timed out' in err.lower():
                if attempt < max_retries - 1:
                    print(f"Request timed out for '{object_name}' — retrying (attempt {attempt + 1}/{max_retries})...")
                else:
                    raise Exception(f"AI analysis timed out after {max_retries} attempts for '{object_name}'.")
            else:
                raise Exception(f"AI analysis failed: {err}")

    if raw is None:
        raise Exception("AI analysis failed: no response received.")

    raw_clean = raw.replace('```json', '').replace('```', '').strip()

    try:
        llm_json = json.loads(raw_clean)
    except json.JSONDecodeError:
        raise Exception(f"AI model returned an improperly formatted JSON response. Please try again. Raw response: {raw[:200]}...")

    if use_hybrid:
        # Merge XML-parsed structure with LLM-generated descriptions
        result = _merge_parsed_and_llm(object_type, object_name, parsed_facts, llm_json, is_new)
        # Normalize table structure (strip empty arrays, fix type/edt aliases)
        if object_type in ('table', 'table_extension'):
            result = normalize_table_result(result)
    else:
        result = llm_json
        if result.get('type') in ('table', 'table_extension'):
            result = enrich_table_result(result, old_code, new_code, is_new)
            result = normalize_table_result(result)
        # Extension types: AI uses the base-type prompt so returns the base type name.
        # Restore the real detected type so the rendering layers use the right section.
        if object_type in _EXTENSION_TO_BASE and result.get('type') == _EXTENSION_TO_BASE[object_type]:
            result['type'] = object_type

    # Override AI-detected name with the actual root <Name> from the XML.
    # The AI can pick up wrong names from nested DataSources/Tables/Relations.
    # Prefer new_code; fall back to old_code for deleted objects.
    xml_name = _extract_name_from_xml(new_code or old_code)
    if xml_name:
        result['name'] = xml_name

    return result

@app.route('/token-usage')
def token_usage():
    with _usage_lock:
        return jsonify(dict(_session_usage))

@app.route('/reset-token-usage', methods=['POST'])
def reset_token_usage():
    with _usage_lock:
        _session_usage["prompt"] = 0
        _session_usage["completion"] = 0
        _session_usage["total"] = 0
    return jsonify({"reset": True})

@app.route('/ado-status')
def ado_status():
    if azure_service:
        try:
            result = azure_service.test_connection()
            return jsonify(result)
        except Exception:
            pass
    return jsonify({"connected": False, "org": ado_org or "", "project": ado_project or ""})


@app.route('/configure-ado', methods=['POST'])
def configure_ado():
    global azure_service, ado_org, ado_project, ado_pat
    data = request.json
    org     = (data.get('org') or '').strip()
    project = (data.get('project') or '').strip()
    pat     = (data.get('pat') or '').strip()

    if not org or not project or not pat:
        return jsonify({"error": "Organization, Project and PAT are all required."}), 400

    try:
        svc = AzureDevOpsService(org, pat, project)
        result = svc.test_connection()

        # Persist to .env so credentials survive server restarts
        set_key(ENV_PATH, 'AZURE_DEVOPS_ORG', org)
        set_key(ENV_PATH, 'AZURE_DEVOPS_PROJECT', project)
        set_key(ENV_PATH, 'AZURE_DEVOPS_PAT', pat)

        # Update runtime globals
        azure_service = svc
        ado_org     = org
        ado_project = project
        ado_pat     = pat

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/work-item/<int:work_item_id>')
def work_item_details(work_item_id: int):
    if not azure_service:
        return jsonify({"error": "Azure DevOps credentials not configured in .env"}), 500
    try:
        data = azure_service.get_work_item_details(work_item_id)
        fields = data.get('fields', {}) or {}
        wi_type = fields.get('System.WorkItemType', '')
        title = fields.get('System.Title', '')
        return jsonify({
            "work_item": str(work_item_id),
            "work_item_type": wi_type,
            "title": title,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        data = request.json
        object_type = data.get('object_type')
        is_new = data.get('is_new', False)
        old_code = data.get('old_code', '')
        new_code = data.get('new_code', '')
        object_name = data.get('object_name', '')

        result = perform_ai_analysis(object_type, is_new, old_code, new_code, object_name)
        return jsonify(result)
    except Exception as e:
        print(f"Error during analysis: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/analyze-changeset', methods=['POST'])
def analyze_changeset():
    if not azure_service:
        return jsonify({"error": "Azure DevOps credentials not configured in .env"}), 500

    try:
        data = request.json
        changeset_id = data.get('changeset_id')
        if not changeset_id:
            return jsonify({"error": "Changeset ID is required"}), 400

        analyzer = ChangesetAnalyzerService(azure_service, perform_ai_analysis)
        result = analyzer.analyze_changeset(int(changeset_id))
        return jsonify(result)
    except Exception as e:
        print(f"Error analyzing changeset: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/analyze-workitem', methods=['POST'])
def analyze_workitem():
    if not azure_service:
        return jsonify({"error": "Azure DevOps credentials not configured in .env"}), 500

    try:
        data = request.json
        work_item_id = data.get('work_item_id')
        if not work_item_id:
            return jsonify({"error": "Work Item ID is required"}), 400

        analyzer = WorkItemAnalyzerService(azure_service, perform_ai_analysis)
        result = analyzer.analyze_work_item(int(work_item_id))
        return jsonify(result)
    except Exception as e:
        print(f"Error analyzing work item: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/generate-docx', methods=['POST'])
def generate_docx():
    data = request.json
    header = data.get('header', {})
    objects = []
    for o in data.get('objects', []):
        if o.get('type') == 'table':
            o = normalize_table_result(o)
        objects.append(o)

    buf = generate_tdd_docx(header, objects)
    wi = header.get('work_item', '').strip()
    filename = f"TDD_Document_{wi}.docx" if wi else "TDD_Document.docx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )

def normalize_table_result(result):
    """Keep only non-empty table subsections for conditional TDD rendering."""
    list_keys = ('fields', 'field_groups', 'indexes', 'relations', 'methods')
    for key in list_keys:
        items = result.get(key)
        if not items:
            result.pop(key, None)
    for field in result.get('fields', []):
        if not field.get('type') and field.get('field_type'):
            field['type'] = field['field_type']
        field.setdefault('type', '')
        field.setdefault('edt', '')
    for group in result.get('field_groups', []):
        fields_val = group.get('fields', '')
        if isinstance(fields_val, list):
            group['fields'] = ', '.join(fields_val)
    for index in result.get('indexes', []):
        fields_val = index.get('fields', '')
        if isinstance(fields_val, list):
            index['fields'] = ', '.join(fields_val)
    return result


# ---------------------------------------------------------------------------
# Prompt caching: each object type has a static system message (instructions
# + JSON schema) that the API caches, and a dynamic user message that contains
# only the object name and code content — so only the short user portion is
# billed on repeated calls of the same type.
# ---------------------------------------------------------------------------

_CLASS_SYSTEM = """You are a D365 AX technical documentation expert. Analyze X++ class code and extract TDD documentation.

Return ONLY a JSON object in this exact format with no preamble or markdown:
{
  "type": "class",
  "name": "<class name extracted from code>",
  "description": "<one line description of what this class does. IF items were deleted, mention it here e.g. 'Methods X and Y deleted.'>",
  "methods": [
    {
      "name": "<method name>",
      "description": "<what this method does, 1 sentence>",
      "is_new": <true if method was added, false if modified>
    }
  ]
}

Rules:
- DO NOT include deleted methods in the 'methods' array. Only include methods that were added or modified.
- Mention any deleted methods ONLY in the 'description' field."""

_TABLE_SYSTEM = """D365 AX TDD expert. Analyze AX table XML. Return ONLY JSON for added/changed items, no preamble.
Schema (omit any key with no changes, no empty arrays):
{"type":"table","name":"<name>","description":"<one line; list deletions here>","fields":[{"name":"<Name>","type":"<String|Int64|Real|Enum|...>","edt":"<EDT or empty>"}],"field_groups":[{"name":"<group>","fields":"<added field names csv>"}],"indexes":[{"name":"<idx>","fields":"<fields csv>"}],"relations":[{"name":"<rel>","field":"<col>","related_table":"<tbl>","related_table_field":"<col>"}],"methods":[{"name":"<m>","description":"<one sentence>"}]}
Field rules: name=<Name> element only. type=from i:type attr (AxTableFieldString->String, AxTableFieldEnum->Enum). edt=ExtendedDataType element value, empty string if absent. field_groups: only changed groups, list only newly added field names."""

_EDT_SYSTEM = """You are a D365 AX technical documentation expert. Analyze AX EDT XML and extract TDD documentation.

Return ONLY a JSON object in this exact format with no preamble or markdown:
{
  "type": "edt",
  "name": "<EDT name>",
  "description": "<one line description>",
  "data_type": "<primitive type e.g. String, Int64, Real>",
  "extends": "<Base EDT name it extends>"
}"""

_FORM_SYSTEM = """D365 AX TDD expert. Analyze AX form XML. Return ONLY JSON for added/changed items, no preamble.
Schema (omit empty arrays):
{"type":"form","name":"<name>","description":"<one line; list deletions here>","properties":{"pattern":"","style":"","caption":"","data_source":""},"added_controls":[{"name":"<ctrl>","control_type":"<CheckBox|String|Button|...>","data_source":"<ds>","data_field":"<field>"}],"modified_controls":[{"name":"<ctrl>","control_type":"<type>","data_source":"<ds>","data_field":"<field>"}],"methods":[{"name":"<method only, no return type>","description":"<one sentence>"}]}
Rules: added_controls=new XML only. modified_controls=changed properties only. For new forms all controls go in added_controls."""

_VIEW_SYSTEM = """D365 AX TDD expert. Analyze AX view XML. Return ONLY JSON for added/changed items, no preamble.
Schema (omit empty arrays):
{"type":"view","name":"<name>","description":"<one line; list deletions here>","data_sources":[{"name":"<ds>","table":"<tbl>"}],"fields":[{"name":"<field>","data_source":"<ds>","edt":"<edt or empty>"}],"field_groups":[{"name":"<group>","fields":"<csv>"}],"methods":[{"name":"<m>","description":"<one sentence>"}]}
Rules: Only added/modified items. Omit empty arrays."""

_SERVICES_SYSTEM = """You are a D365 AX technical documentation expert. Analyze XML and determine if it is a Service Group or a Service.

Return ONLY a JSON object in this exact format:
{
  "type": "services",
  "subtype": "<'service_group' or 'service'>",
  "name": "<object name detected from XML or provided>",
  "description": "<one line description>",
  "details": [
    {
      "name": "<service name if group, or method name if service>",
      "description": "<description of this item>"
    }
  ]
}

Rules:
- If subtype is 'service_group', 'details' lists the services in that group.
- If subtype is 'service', 'details' lists the class methods exposed as service operations.
- IF items were deleted, mention it in the description.
- Only include added or modified items."""

_QUERY_SYSTEM = """You are a D365 AX technical documentation expert. Analyze AX query XML and extract TDD documentation for ONLY what was added or changed.

Return ONLY a JSON object in this exact format with no preamble or markdown:
{
  "type": "query",
  "name": "<query name>",
  "description": "<one line description of what this query retrieves. IF items were deleted, mention it here.>",
  "data_sources": [
    {
      "name": "<data source name>",
      "table": "<table name>",
      "join_type": "<Inner / Outer / Exists / NotExists — leave blank if root>"
    }
  ],
  "fields": [
    {
      "data_source": "<data source name>",
      "field": "<field name>"
    }
  ],
  "ranges": [
    {
      "data_source": "<data source name>",
      "field": "<field name>",
      "value": "<range value or expression, blank if dynamic>"
    }
  ],
  "methods": [
    {
      "name": "<method name>",
      "description": "<what this method does, one sentence>"
    }
  ]
}

Rules:
- Only include data_sources, fields, ranges, and methods that were added or modified.
- Omit any array key entirely if nothing was added or changed in that category.
- Mention deleted items ONLY in the description field.
- Do not return empty arrays."""

_ENUM_SYSTEM = """You are a D365 AX technical documentation expert. Analyze AX Base Enum XML and extract TDD documentation for ONLY what was added or changed.

Return ONLY a JSON object in this exact format with no preamble or markdown:
{
  "type": "enum",
  "name": "<enum name from XML>",
  "description": "<one line description of what this enum represents. IF values were deleted, mention it here.>",
  "values": [
    {
      "name": "<enum value name e.g. Approved>",
      "label": "<display label shown in UI e.g. Approved>"
    }
  ]
}

Rules:
- Only include enum values that were added or modified — not deleted ones.
- Mention deleted values ONLY in the description field.
- Omit the "values" key entirely if no values were added or changed.
- Do not return empty arrays."""

_DATA_ENTITY_SYSTEM = """D365 AX TDD expert. Analyze AX Data Entity XML. Return ONLY JSON for added/changed items, no preamble.
Schema (omit empty arrays):
{"type":"data_entity","name":"<name>","description":"<one line; list deletions here>","data_sources":[{"name":"<ds>","table":"<tbl>","join_type":"<Root|Inner|Outer|Exists>"}],"fields":[{"name":"<field>","data_source":"<ds>","edt":"<edt or empty>"}],"entity_keys":[{"name":"<key>","fields":"<csv>"}],"field_groups":[{"name":"<group>","fields":"<csv>"}],"methods":[{"name":"<m>","description":"<one sentence>"}]}
Rules: Only added/modified items. Omit empty arrays."""

_REPORT_SYSTEM = """You are a D365 AX technical documentation expert. Analyze AX SSRS report XML and extract TDD documentation for ONLY what was added or changed.

Return ONLY a JSON object in this exact format with no preamble or markdown:
{
  "type": "report",
  "name": "<report name>",
  "description": "<one line description of what this report shows. IF items were deleted, mention it here.>",
  "fields": [
    {
      "field_label": "<display label shown on the report>",
      "dataset_field": "<dataset field name>",
      "source_table": "<source table name>",
      "source_field": "<source field name>",
      "data_type": "<data type e.g. String, Int64, Date, Real>",
      "logic": "<any calculation, expression, or lookup logic — blank if straightforward>",
      "remarks": "<additional notes or remarks — blank if none>"
    }
  ]
}

Rules:
- Only include fields that were added or modified — not deleted ones.
- Mention deleted fields ONLY in the description field.
- Omit the "fields" key entirely if no fields were added or changed.
- Do not return empty arrays."""

_SECURITY_SYSTEM = """You are a D365 AX technical documentation expert. Analyze XML and determine if it is a Security Privilege, Duty, Role, or Policy.

Return ONLY a JSON object in this exact format:
{
  "type": "security",
  "subtype": "<'privilege', 'duty', 'role', or 'policy'>",
  "name": "<security object name>",
  "description": "<one line description>",
  "permissions": [
    {
      "object_name": "<name of the secured object e.g. a table, menu item, or class>",
      "access_level": "<the access level granted e.g. Read, Delete, Update, Invoke, etc>"
    }
  ]
}

Rules:
- For 'privilege', list the entry points or data permissions in the 'permissions' array.
- For others, provide a summary in the description and list associated elements in 'permissions' if applicable.
- Only include added or modified items.
- IF items were deleted, mention it in the description."""


# ---------------------------------------------------------------------------
# Hybrid approach: XML parser extracts structure, LLM writes descriptions only
# ---------------------------------------------------------------------------

_HYBRID_TYPES = {
    'class', 'class_extension',
    'table', 'table_extension',
    'form', 'form_extension',
    'view', 'view_extension',
    'query', 'query_extension',
    'enum', 'enum_extension',
    'edt', 'edt_extension',
    'data_entity', 'data_entity_extension',
    'security',
    'services',
    'menu_item',
    'menu_extension',
}

_DESC_ONLY_SYSTEM = """You are a D365 AX technical documentation expert.
Given a structural summary of a D365 AX object, write concise technical descriptions.

Return ONLY a JSON object with no preamble or markdown:
{
  "description": "<one sentence: what does this object do or represent? If items were deleted, mention them.>",
  "method_descriptions": {
    "<method_or_operation_name>": "<one sentence: what this method/operation does>"
  }
}

Omit "method_descriptions" entirely if no methods or operations are listed in the summary."""


def _build_desc_user_msg(object_type: str, object_name: str, parsed: dict, is_new: bool) -> str:
    base = _EXTENSION_TO_BASE.get(object_type, object_type)
    change = "new" if is_new else "modified"
    lines = [f"Object: {object_name} ({base}, {change})"]

    fields = parsed.get('fields', [])
    if fields:
        names = [f.get('name', '') or f.get('field_label', '') for f in fields if f.get('name') or f.get('field_label')]
        if names:
            lines.append(f"Fields ({len(names)}): {', '.join(names)}")

    ds_list = parsed.get('data_sources', [])
    if ds_list:
        lines.append(f"Data sources: {', '.join(d['name'] for d in ds_list if d.get('name'))}")

    controls = parsed.get('added_controls', []) + parsed.get('modified_controls', [])
    if controls:
        ctrl_names = [c['name'] for c in controls if c.get('name')]
        lines.append(f"Controls ({len(ctrl_names)}): {', '.join(ctrl_names)}")

    indexes = parsed.get('indexes', [])
    if indexes:
        lines.append(f"Indexes: {', '.join(i['name'] for i in indexes if i.get('name'))}")

    relations = parsed.get('relations', [])
    if relations:
        rel_info = [f"{r['name']}→{r.get('related_table','')}" for r in relations if r.get('name')]
        lines.append(f"Relations: {', '.join(rel_info)}")

    values = parsed.get('values', [])
    if values:
        lines.append(f"Enum values: {', '.join(v['name'] for v in values if v.get('name'))}")

    if parsed.get('data_type'):
        lines.append(f"Data type: {parsed['data_type']}")
    if parsed.get('extends'):
        lines.append(f"Extends: {parsed['extends']}")

    keys = parsed.get('entity_keys', [])
    if keys:
        lines.append(f"Entity keys: {', '.join(k['name'] for k in keys if k.get('name'))}")

    perms = parsed.get('permissions', [])
    if perms:
        perm_str = [f"{p['object_name']}({p.get('access_level','')})" for p in perms if p.get('object_name')]
        lines.append(f"Permissions: {', '.join(perm_str)}")

    # Menu item fields
    obj_type_ref = parsed.get('object_type')
    obj_name_ref = parsed.get('object_name_ref')
    mi_label = parsed.get('label')
    if obj_type_ref:
        lines.append(f"Object Type: {obj_type_ref}")
    if obj_name_ref:
        lines.append(f"Object: {obj_name_ref}")
    if mi_label:
        lines.append(f"Label: {mi_label}")

    # Menu extension items
    ext_items = parsed.get('items', [])
    if ext_items:
        item_names = [i.get('menu_item_name', '') for i in ext_items if i.get('menu_item_name')]
        lines.append(f"Menu items added: {', '.join(item_names)}")

    method_names = parsed.get('method_names', [])
    if method_names:
        lines.append(f"Methods/operations: {', '.join(method_names)}")

    deleted = parsed.get('deleted', [])
    if deleted:
        lines.append(f"DELETED in this change: {', '.join(deleted)}")

    lines.append('')
    lines.append("Write a 1-sentence description of what this object does.")
    if deleted:
        lines.append("The description MUST mention the deleted items by name.")
    if method_names:
        lines.append("Also write a 1-sentence description for each method/operation listed.")

    return '\n'.join(lines)


def _build_desc_messages(object_type: str, object_name: str, parsed: dict, is_new: bool) -> list:
    return [
        {"role": "system", "content": _DESC_ONLY_SYSTEM},
        {"role": "user", "content": _build_desc_user_msg(object_type, object_name, parsed, is_new)},
    ]


def _merge_parsed_and_llm(object_type: str, object_name: str, parsed: dict, llm_result: dict, is_new: bool) -> dict:
    base = _EXTENSION_TO_BASE.get(object_type, object_type)
    result = {
        'type': object_type,
        'name': object_name,
        'description': llm_result.get('description', ''),
    }
    
    # Preserve subtype if present (important for security objects)
    if 'subtype' in parsed:
        result['subtype'] = parsed['subtype']
    elif 'subtype' in llm_result:
        result['subtype'] = llm_result['subtype']

    method_descs = llm_result.get('method_descriptions', {})

    for key in ('fields', 'field_groups', 'indexes', 'relations', 'data_sources',
                'added_controls', 'modified_controls', 'values', 'entity_keys',
                'permissions', 'properties', 'data_type', 'extends',
                'object_type', 'object_name_ref', 'label', 'items'):
        if key in parsed:
            result[key] = parsed[key]

    method_names = parsed.get('method_names', [])

    if base == 'services':
        details = parsed.get('details', [])
        if details:
            result['details'] = [
                {'name': d['name'], 'description': method_descs.get(d['name'], '')}
                for d in details
            ]
    elif method_names:
        result['methods'] = [
            {'name': m, 'description': method_descs.get(m, ''), 'is_new': is_new}
            for m in method_names
        ]

    return result


_EXTENSION_TO_BASE = {
    'table_extension': 'table',
    'form_extension': 'form',
    'view_extension': 'view',
    'edt_extension': 'edt',
    'query_extension': 'query',
    'enum_extension': 'enum',
    'class_extension': 'class',
    'data_entity_extension': 'data_entity',
}


def build_messages(object_type, is_new, old_code, new_code, object_name, diff_mode=False):
    if not object_name:
        object_name = "(detect from code)"
    """Return [system, user] messages for prompt caching.

    The system message holds the static format instructions for the object type —
    this prefix is identical across all calls of the same type, so the API caches it
    and only bills for the dynamic user message (object name + code) on each call.

    diff_mode=True means old_code is already a unified diff string (lines prefixed
    with +/-) and new_code is empty. The AI understands unified diff format natively.
    """
    if diff_mode:
        change_label = "unified diff (lines starting with + are added, - are removed, context shown)"
    elif is_new:
        change_label = "new"
    else:
        change_label = "diff (old vs new)"

    # Extension types use their base type's prompt; the caller restores the real type after.
    effective_type = _EXTENSION_TO_BASE.get(object_type, object_type)

    if effective_type == 'class':
        system = _CLASS_SYSTEM
        if is_new:
            user = f"Object Name: {object_name}\nObject Type: Class\nChange Type: new class — analyze all methods\n\n{new_code}"
        elif diff_mode:
            user = f"Object Name: {object_name}\nObject Type: Class\nChange Type: {change_label}\n\n{old_code}"
        else:
            user = f"Object Name: {object_name}\nObject Type: Class\nChange Type: diff (old vs new)\n\nOld code:\n{old_code}\n\nNew code:\n{new_code}"

    elif effective_type == 'table':
        system = _TABLE_SYSTEM
        if is_new:
            user = f"Object Name: {object_name}\nObject Type: Table\nChange Type: new table\n\n{new_code}"
        elif diff_mode:
            user = f"Object Name: {object_name}\nObject Type: Table\nChange Type: {change_label}\n\n{old_code}"
        else:
            user = f"Object Name: {object_name}\nObject Type: Table\nChange Type: diff (old vs new)\n\nOld XML:\n{old_code}\n\nNew XML:\n{new_code}"

    elif effective_type == 'enum':
        system = _ENUM_SYSTEM
        if is_new:
            user = f"Object Name: {object_name}\nObject Type: Enum\nChange Type: new enum\n\n{new_code}"
        elif diff_mode:
            user = f"Object Name: {object_name}\nObject Type: Enum\nChange Type: {change_label}\n\n{old_code}"
        else:
            user = f"Object Name: {object_name}\nObject Type: Enum\nChange Type: diff (old vs new)\n\nOld XML:\n{old_code}\n\nNew XML:\n{new_code}"

    elif effective_type == 'edt':
        system = _EDT_SYSTEM
        if is_new:
            user = f"Object Name: {object_name}\nObject Type: EDT\nChange Type: new EDT\n\n{new_code}"
        elif diff_mode:
            user = f"Object Name: {object_name}\nObject Type: EDT\nChange Type: {change_label}\n\n{old_code}"
        else:
            user = f"Object Name: {object_name}\nObject Type: EDT\nChange Type: diff\n\nOld XML:\n{old_code}\n\nNew XML:\n{new_code}"

    elif effective_type == 'form':
        system = _FORM_SYSTEM
        if is_new:
            user = f"Object Name: {object_name}\nObject Type: Form\nChange Type: new form\n\n{new_code}"
        elif diff_mode:
            user = f"Object Name: {object_name}\nObject Type: Form\nChange Type: {change_label}\n\n{old_code}"
        else:
            user = f"Object Name: {object_name}\nObject Type: Form\nChange Type: diff\n\nOld XML:\n{old_code}\n\nNew XML:\n{new_code}"

    elif effective_type == 'query':
        system = _QUERY_SYSTEM
        if is_new:
            user = f"Object Name: {object_name}\nObject Type: Query\nChange Type: new query\n\n{new_code}"
        elif diff_mode:
            user = f"Object Name: {object_name}\nObject Type: Query\nChange Type: {change_label}\n\n{old_code}"
        else:
            user = f"Object Name: {object_name}\nObject Type: Query\nChange Type: diff (old vs new)\n\nOld XML:\n{old_code}\n\nNew XML:\n{new_code}"

    elif effective_type == 'view':
        system = _VIEW_SYSTEM
        if is_new:
            code = new_code
        elif diff_mode:
            code = old_code
        else:
            code = f"{old_code}\n{new_code}"
        user = f"Object Name: {object_name}\nObject Type: View\nChange Type: {change_label}\n\n{code}"

    elif object_type == 'services':
        system = _SERVICES_SYSTEM
        code = new_code if is_new else old_code if diff_mode else f"{old_code}\n{new_code}"
        user = f"Object Name: {object_name}\nChange Type: {change_label}\n\n{code}"

    elif object_type == 'security':
        system = _SECURITY_SYSTEM
        code = new_code if is_new else old_code if diff_mode else f"{old_code}\n{new_code}"
        user = f"Object Name: {object_name}\nChange Type: {change_label}\n\n{code}"

    elif effective_type == 'data_entity':
        system = _DATA_ENTITY_SYSTEM
        if is_new:
            user = f"Object Name: {object_name}\nObject Type: Data Entity\nChange Type: new entity\n\n{new_code}"
        elif diff_mode:
            user = f"Object Name: {object_name}\nObject Type: Data Entity\nChange Type: {change_label}\n\n{old_code}"
        else:
            user = f"Object Name: {object_name}\nObject Type: Data Entity\nChange Type: diff (old vs new)\n\nOld XML:\n{old_code}\n\nNew XML:\n{new_code}"

    elif object_type == 'report':
        system = _REPORT_SYSTEM
        if is_new:
            user = f"Object Name: {object_name}\nObject Type: Report\nChange Type: new report\n\n{new_code}"
        elif diff_mode:
            user = f"Object Name: {object_name}\nObject Type: Report\nChange Type: {change_label}\n\n{old_code}"
        else:
            user = f"Object Name: {object_name}\nObject Type: Report\nChange Type: diff (old vs new)\n\nOld XML:\n{old_code}\n\nNew XML:\n{new_code}"

    else:
        system = f'You are a D365 AX technical documentation expert. Analyze {object_type} code/XML and return a JSON summary.\n\nReturn ONLY: {{"type": "{object_type}", "name": "<name>", "description": "<description>"}}'
        user = f"Object Name: {object_name}\n\n{new_code}"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


if __name__ == '__main__':
    app.run(debug=True, port=5000, use_reloader=False, threaded=True)
