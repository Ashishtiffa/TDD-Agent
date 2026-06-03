from flask import Flask, render_template, request, jsonify, send_file
from openai import OpenAI
import json
import io
import os
import time
import re
from dotenv import load_dotenv
from docx_generator import generate_tdd_docx
from table_xml_parser import enrich_table_result
from azure_devops_service import AzureDevOpsService
from changeset_analyzer_service import ChangesetAnalyzerService

load_dotenv()

app = Flask(__name__)

# Initialize Services
ado_org = os.environ.get("AZURE_DEVOPS_ORG")
ado_project = os.environ.get("AZURE_DEVOPS_PROJECT")
ado_pat = os.environ.get("AZURE_DEVOPS_PAT")
azure_service = AzureDevOpsService(ado_org, ado_pat, ado_project) if ado_org and ado_pat else None

ENDPOINT = "https://apim-sj-foundry-eastus2.azure-api.net/sj-foundry-eastus2-resource/openai/v1"
DEPLOYMENT = "Kimi-K2.6-1"

def perform_ai_analysis(object_type, is_new, old_code, new_code, object_name):
    prompt = build_prompt(object_type, is_new, old_code, new_code, object_name)
    
    api_key = os.environ.get("KIMI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise Exception("API key not found in .env")

    client = OpenAI(
        base_url=ENDPOINT,
        api_key="unused",  # real auth is the api-key header below
        default_headers={"api-key": api_key},
    )
    
    try:
        completion = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[
                {"role": "user", "content": prompt},
            ],
        )
        raw = completion.choices[0].message.content
    except Exception as e:
        raise Exception(f"AI analysis failed: {str(e)}")

    clean = raw.replace('```json', '').replace('```', '').strip()
    
    try:
        result = json.loads(clean)
    except json.JSONDecodeError:
        raise Exception(f"AI model returned an improperly formatted JSON response. Please try again. Raw response: {raw[:200]}...")
    
    if result.get('type') == 'table':
        result = enrich_table_result(result, old_code, new_code, is_new)
        result = normalize_table_result(result)
    
    return result

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
    return send_file(
        buf,
        as_attachment=True,
        download_name='TDD_Document.docx',
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


def build_prompt(object_type, is_new, old_code, new_code, object_name):
    if object_type == 'class':
        return f"""You are a D365 AX technical documentation expert. Analyze the following X++ class code {'(new class)' if is_new else 'diff (old vs new)'} and extract TDD documentation.

Object Name: {object_name}
Object Type: Class
{'New class - analyze all methods:' if is_new else 'Old code (left side of diff):'}
{new_code if is_new else old_code}

{'New code (right side of diff):' if not is_new else ''}
{'' if is_new else new_code}

Return ONLY a JSON object in this exact format with no preamble or markdown:
{{
  "type": "class",
  "name": "<class name extracted from code>",
  "description": "<one line description of what this class does. IF items were deleted, mention it here e.g. 'Methods X and Y deleted.'>",
  "methods": [
    {{
      "name": "<method name>",
      "description": "<what this method does, 1 sentence>",
      "is_new": <true if method was added, false if modified>
    }}
  ]
}}

Rules:
- DO NOT include deleted methods in the 'methods' array. Only include methods that were added or modified.
- Mention any deleted methods ONLY in the 'description' field."""

    elif object_type == 'table':
        return f"""You are a D365 AX technical documentation expert. Analyze the following AX table XML {'(new table)' if is_new else 'diff (old vs new)'} and extract TDD documentation for ONLY what was added or changed.

Object Name: {object_name}
Object Type: Table
{'New table XML:' if is_new else 'Old XML:'}
{new_code if is_new else old_code}

{'New XML:' if not is_new else ''}
{'' if is_new else new_code}

Return ONLY a JSON object in this exact format with no preamble or markdown:
{{
  "type": "table",
  "name": "<table name from XML>",
  "description": "<one line description. IF fields/methods/indexes were deleted, mention it here e.g. 'Field X and Method Y deleted.'>",
  "fields": [
    {{
      "name": "<field name>",
      "type": "<primitive type e.g. String, Int64, Real — from XML or EDT>",
      "edt": "<EDT name>"
    }}
  ],
  "field_groups": [
    {{
      "name": "<field group name>",
      "fields": "<comma-separated field names e.g. Name, Age>"
    }}
  ],
  "indexes": [
    {{
      "name": "<index name>",
      "fields": "<comma-separated indexed field names>"
    }}
  ],
  "relations": [
    {{
      "name": "<relation name>",
      "field": "<field on this table>",
      "related_table": "<related table name>",
      "related_table_field": "<field on related table>"
    }}
  ],
  "methods": [
    {{
      "name": "<method name>",
      "description": "<what this method does, one sentence>"
    }}
  ]
}}

Rules:
- DO NOT include deleted fields, methods, indexes, or relations in the arrays. Only include added or modified items.
- Mention any deleted items ONLY in the 'description' field.
- Omit any array entirely (do not include the key) if nothing was added or changed in that category.
- For a diff: compare old vs new XML carefully.
- "fields": Include new fields AND existing fields changed in the diff (e.g. AllowEdit on ApproversName).
- Each field row — three columns from XML, never swapped:
  - "name" = <Name> only (e.g. ApproversName)
  - "type" = from i:type (AxTableFieldString → String, AxTableFieldEnum → Enum)
  - "edt" = <ExtendedDataType> only (e.g. HSOCRApproversName) — leave "" if missing; do NOT put field name or EnumType in edt for string fields
- "field_groups": ONLY groups that changed; "fields" column lists ONLY field names newly added to that group.
- Do not return empty arrays."""

    elif object_type == 'edt':
        return f"""You are a D365 AX technical documentation expert. Analyze the following AX EDT XML {'(new EDT)' if is_new else 'diff'} and extract TDD documentation.

Object Name: {object_name}
Object Type: EDT
{'New EDT XML:' if is_new else 'Old XML:'}
{new_code if is_new else old_code}

{'New XML:' if not is_new else ''}
{'' if is_new else new_code}

Return ONLY a JSON object in this exact format with no preamble or markdown:
{{
  "type": "edt",
  "name": "<EDT name>",
  "description": "<one line description>",
  "data_type": "<primitive type e.g. String, Int64, Real>",
  "extends": "<Base EDT name it extends>"
}}
"""

    elif object_type == 'form':
        return f"""You are a D365 AX technical documentation expert. Analyze the following AX form XML {'(new form)' if is_new else 'diff'} and extract TDD documentation.

Object Name: {object_name}
Object Type: Form
{'New form XML:' if is_new else 'Old XML:'}
{new_code if is_new else old_code}

{'New XML:' if not is_new else ''}
{'' if is_new else new_code}

Return ONLY a JSON object in this exact format with no preamble or markdown:
{{
  "type": "form",
  "name": "<form name>",
  "description": "<one line description. IF controls/methods were deleted, mention it here e.g. 'Control X deleted.'>",
  "properties": {{
    "pattern": "<pattern if visible>",
    "style": "<style if visible>",
    "caption": "<caption if visible>",
    "data_source": "<data source if visible>"
  }},
  "added_controls": [
    {{
      "name": "<control name>",
      "control_type": "<CheckBox/String/Button etc>",
      "data_source": "<data source>",
      "data_field": "<data field>"
    }}
  ],
  "modified_controls": [
    {{
      "name": "<control name>",
      "control_type": "<CheckBox/String/Button etc>",
      "data_source": "<data source>",
      "data_field": "<data field>"
    }}
  ],
  "methods": [
    {{
      "name": "<method name ONLY, e.g. 'init', no return type or params>",
      "description": "<what this method does>"
    }}
  ]
}}

Rules:
- DO NOT include deleted controls or methods in the arrays. Only include added or modified items.
- Mention any deleted items ONLY in the 'description' field.
- 'added_controls': Controls that exist in the new XML but NOT in the old XML.
- 'modified_controls': Controls that exist in both but have property changes (e.g. different label, data source, etc).
- For new forms, all controls should be in 'added_controls'."""

    elif object_type == 'view':
        return f"""You are a D365 AX technical documentation expert. Analyze the following AX view XML {'(new view)' if is_new else 'diff'} and extract TDD documentation.

Object Name: {object_name}
Object Type: View
Code: {new_code if is_new else old_code + chr(10) + new_code}

Return ONLY a JSON object in this exact format with no preamble or markdown:
{{
  "type": "view",
  "name": "<view name>",
  "description": "<what this view does. IF items were deleted, mention it here.>"
}}"""

    elif object_type == 'services':
        return f"""You are a D365 AX technical documentation expert. Analyze the following XML and determine if it is a Service Group or a Service.

Object Name: {object_name}
Code:
{new_code if is_new else old_code + chr(10) + new_code}

Return ONLY a JSON object in this exact format:
{{
  "type": "services",
  "subtype": "<'service_group' or 'service'>",
  "name": "<object name detected from XML or provided>",
  "description": "<one line description>",
  "details": [
    {{
      "name": "<service name if group, or method name if service>",
      "description": "<description of this item>"
    }}
  ]
}}

Rules:
- If subtype is 'service_group', 'details' lists the services in that group.
- If subtype is 'service', 'details' lists the class methods exposed as service operations.
- IF items were deleted, mention it in the description.
- Only include added or modified items."""

    elif object_type == 'security':
        return f"""You are a D365 AX technical documentation expert. Analyze the following XML and determine if it is a Security Privilege, Duty, Role, or Policy.

Object Name: {object_name}
Code:
{new_code if is_new else old_code + chr(10) + new_code}

Return ONLY a JSON object in this exact format:
{{
  "type": "security",
  "subtype": "<'privilege', 'duty', 'role', or 'policy'>",
  "name": "<security object name>",
  "description": "<one line description>",
  "permissions": [
    {{
      "object_name": "<name of the secured object e.g. a table, menu item, or class>",
      "access_level": "<the access level granted e.g. Read, Delete, Update, Invoke, etc>"
    }}
  ]
}}

Rules:
- For 'privilege', list the entry points or data permissions in the 'permissions' array.
- For others, provide a summary in the description and list associated elements in 'permissions' if applicable.
- Only include added or modified items.
- IF items were deleted, mention it in the description."""

    else:
        return f"""Analyze this D365 AX {object_type} code/XML and return a JSON summary.
Code: {new_code}
Return ONLY: {{"type": "{object_type}", "name": "<name>", "description": "<description>"}}"""


if __name__ == '__main__':
    app.run(debug=True, port=5000)
