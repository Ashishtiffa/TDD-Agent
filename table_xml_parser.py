"""Parse AX table XML to enrich / correct AI-extracted TDD table metadata."""
import re
from typing import Dict, List, Set


AX_FIELD_TYPE_MAP = {
    'AxTableFieldString': 'String',
    'AxTableFieldEnum': 'Enum',
    'AxTableFieldInt': 'Int',
    'AxTableFieldInt64': 'Int64',
    'AxTableFieldReal': 'Real',
    'AxTableFieldDate': 'Date',
    'AxTableFieldUtcDateTime': 'UtcDateTime',
    'AxTableFieldGuid': 'Guid',
}

TRACKED_FIELD_PROPS = (
    'AllowEdit', 'ExtendedDataType', 'EnumType', 'Mandatory',
    'Visible', 'Label', 'HelpText', 'ConfigurationKey',
)


def _has_diff_markers(xml: str) -> bool:
    return bool(re.search(r'^\s*[+-](?![+-])', xml or '', re.MULTILINE))


def clean_pasted_xml(xml: str) -> str:
    """Strip git/ADO diff +/- prefixes so XML can be parsed."""
    if not xml:
        return ''
    lines = []
    for line in xml.splitlines():
        if re.match(r'^\s*@@', line) or re.match(r'^\s*(\+\+\+|---)', line):
            continue
        stripped = line
        if re.match(r'^\s*\+(?![+])', line):
            stripped = re.sub(r'^\s*\+', '', line, count=1)
        elif re.match(r'^\s*-(?![-])', line):
            continue
        lines.append(stripped)
    return '\n'.join(lines)


def _field_blocks(xml: str) -> List[str]:
    if not xml:
        return []
    blocks = re.findall(
        r'<AxTableField\b[^>]*>.*?</AxTableField>',
        xml,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if blocks:
        return blocks
    return re.findall(
        r'<FIELD\b[^>]*>.*?</FIELD>',
        xml,
        flags=re.DOTALL | re.IGNORECASE,
    )


def _split_raw_field_segments(xml: str) -> List[str]:
    if not xml:
        return []
    parts = re.split(r'(?=<AxTableField\b)', xml, flags=re.IGNORECASE)
    segments = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not part.lower().startswith('<axtablefield'):
            continue
        end = re.search(r'</AxTableField>', part, re.IGNORECASE)
        if end:
            part = part[: end.end()]
        segments.append(part)
    return segments


def _segment_has_plus_line(raw_segment: str) -> bool:
    for line in raw_segment.splitlines():
        s = line.strip()
        if s.startswith('+') and not s.startswith('++'):
            return True
    return False


def _type_from_ax_tag(block: str, edt: str) -> str:
    itype_m = re.search(r'i:type="([^"]+)"', block, re.IGNORECASE)
    if itype_m:
        ax_type = itype_m.group(1)
        if ax_type in AX_FIELD_TYPE_MAP:
            return AX_FIELD_TYPE_MAP[ax_type]
        if 'String' in ax_type:
            return 'String'
        if 'Enum' in ax_type:
            return 'Enum'
        if 'Int64' in ax_type:
            return 'Int64'
        if 'Real' in ax_type:
            return 'Real'
    if re.search(r'<EnumType>', block, re.IGNORECASE):
        return 'Enum'
    return _type_from_edt(edt)


def _type_from_edt(edt: str) -> str:
    if not edt:
        return ''
    lower = edt.lower()
    if lower.startswith('str') or 'string' in lower:
        return 'String'
    if lower.startswith('int') or 'integer' in lower:
        return 'Int64'
    if 'enum' in lower or lower.startswith('noyes'):
        return 'Enum'
    if 'real' in lower or 'amount' in lower:
        return 'Real'
    return ''


def parse_field_block(block: str) -> Dict[str, str]:
    """
    Field Name  <- <Name>
    Type        <- i:type (AxTableFieldString -> String)
    EDT         <- <ExtendedDataType> only (not EnumType, not field name)
    """
    name_m = re.search(r'<Name>\s*([^<]+?)\s*</Name>', block, re.IGNORECASE)
    if not name_m:
        return {}
    name = name_m.group(1).strip()

    edt_m = re.search(
        r'<ExtendedDataType>\s*([^<]+?)\s*</ExtendedDataType>',
        block,
        re.IGNORECASE,
    )
    edt = edt_m.group(1).strip() if edt_m else ''

    field_type = _type_from_ax_tag(block, edt)
    return {'name': name, 'type': field_type, 'edt': edt}


def parse_table_fields(xml: str) -> Dict[str, Dict[str, str]]:
    result: Dict[str, Dict[str, str]] = {}
    for block in _field_blocks(clean_pasted_xml(xml)):
        row = parse_field_block(block)
        if row.get('name'):
            result[row['name']] = row
    return result


def _field_props(block: str) -> Dict[str, str]:
    props: Dict[str, str] = {}
    for tag in TRACKED_FIELD_PROPS:
        m = re.search(rf'<{tag}>\s*([^<]*?)\s*</{tag}>', block, re.IGNORECASE)
        if m:
            props[tag] = m.group(1).strip()
    return props


def _field_block_map(clean_xml: str) -> Dict[str, str]:
    blocks: Dict[str, str] = {}
    for block in _field_blocks(clean_xml):
        row = parse_field_block(block)
        name = row.get('name')
        if name:
            blocks[name] = block
    return blocks


def _touched_field_names_from_diff(new_xml: str, old_field_map: Dict) -> Set[str]:
    """
    Fields with + lines in diff paste: new fields OR modified (e.g. AllowEdit on ApproversName).
    """
    names: Set[str] = set()
    for segment in _split_raw_field_segments(new_xml):
        if not _segment_has_plus_line(segment):
            continue
        row = parse_field_block(clean_pasted_xml(segment))
        name = row.get('name')
        if name:
            names.add(name)
    return names


def _changed_field_names(old_clean: str, new_clean: str) -> Set[str]:
    old_blocks = _field_block_map(old_clean)
    new_blocks = _field_block_map(new_clean)
    changed: Set[str] = set()
    for name, new_block in new_blocks.items():
        old_block = old_blocks.get(name)
        if old_block and _field_props(old_block) != _field_props(new_block):
            changed.add(name)
    return changed


def _rows_from_names(names: Set[str], field_map: Dict[str, Dict[str, str]]) -> List[Dict[str, str]]:
    rows = []
    for name in sorted(names):
        if name in field_map:
            rows.append(dict(field_map[name]))
    return rows


def _field_group_blocks(xml: str) -> List[str]:
    cleaned = clean_pasted_xml(xml)
    if not cleaned:
        return []
    section = cleaned
    fg_m = re.search(r'<FieldGroups>(.*?)</FieldGroups>', cleaned, re.DOTALL | re.IGNORECASE)
    if fg_m:
        section = fg_m.group(1)
    blocks = re.findall(
        r'<AxTableFieldGroup\b[^>]*>.*?</AxTableFieldGroup>',
        section,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if blocks:
        return blocks
    return re.findall(
        r'<FieldGroup\b[^>]*>.*?</FieldGroup>',
        section,
        flags=re.DOTALL | re.IGNORECASE,
    )


def parse_field_groups(xml: str) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    for block in _field_group_blocks(xml):
        name_m = re.search(r'<Name>\s*([^<]+?)\s*</Name>', block, re.IGNORECASE)
        if not name_m:
            continue
        group_name = name_m.group(1).strip()
        fields: List[str] = []
        for df in re.findall(
            r'<DataField>\s*([^<]+?)\s*</DataField>',
            block,
            re.IGNORECASE,
        ):
            fields.append(df.strip())
        if fields:
            groups[group_name] = fields
    return groups


def diff_table_xml(old_xml: str, new_xml: str) -> Dict:
    """
    Fields table: new fields + modified fields (from diff + lines or property diff).
    Columns always from new XML: Name, Type, ExtendedDataType.
    """
    old_clean = clean_pasted_xml(old_xml or '')
    new_clean = clean_pasted_xml(new_xml or '')
    old_fields = parse_table_fields(old_clean)
    new_fields = parse_table_fields(new_clean)

    added_names = set(new_fields) - set(old_fields)
    changed_names = _changed_field_names(old_clean, new_clean)

    if _has_diff_markers(new_xml or ''):
        touched = _touched_field_names_from_diff(new_xml, old_fields)
        field_names = touched if touched else (added_names | changed_names)
    elif old_fields:
        field_names = added_names | changed_names
    else:
        field_names = added_names

    old_groups = parse_field_groups(old_clean)
    new_groups = parse_field_groups(new_clean)
    added_group_entries: List[Dict[str, str]] = []
    for group_name, new_members in new_groups.items():
        old_members = set(old_groups.get(group_name, []))
        added = [f for f in new_members if f not in old_members]
        if added:
            added_group_entries.append({
                'name': group_name,
                'fields': ', '.join(added),
            })

    return {
        'fields': _rows_from_names(field_names, new_fields),
        'added_field_groups': added_group_entries,
    }


def enrich_table_result(result: dict, old_code: str, new_code: str, is_new: bool) -> dict:
    """Build Fields from XML only — correct columns, new + modified fields."""
    new_field_map = parse_table_fields(new_code or '')

    if is_new:
        result['fields'] = _rows_from_names(set(new_field_map), new_field_map)
        groups = parse_field_groups(new_code or '')
        if groups:
            result['field_groups'] = [
                {'name': g, 'fields': ', '.join(members)}
                for g, members in sorted(groups.items())
            ]
        return result

    if old_code and new_code:
        diff = diff_table_xml(old_code, new_code)
        if diff['fields']:
            result['fields'] = diff['fields']
        else:
            result.pop('fields', None)

        if diff['added_field_groups']:
            result['field_groups'] = diff['added_field_groups']
        else:
            result.pop('field_groups', None)

    return result
