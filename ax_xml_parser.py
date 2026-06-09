"""
Hybrid XML parser for all AX object types.
Extracts structural facts so the LLM only writes descriptions.

Change-detection strategy
──────────────────────────
diff_mode=True  → old_xml is a unified diff string.
                  We reconstruct "before" and "after" XML from the diff,
                  then compare — only changed hunks are visible, so only
                  changed items come out.
diff_mode=False → old_xml / new_xml are plain XML strings.
                  We compare them directly. If old_xml is empty we
                  treat everything in new_xml as new.
is_new=True     → everything in new_xml is new regardless of mode.
"""
import re
from typing import Dict, List, Optional, Set, Tuple

from table_xml_parser import (
    parse_table_fields, parse_field_groups, diff_table_xml,
    clean_pasted_xml, _rows_from_names,
)

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _tag(xml: str, tag: str) -> str:
    m = re.search(rf'<{tag}>\s*([^<]+?)\s*</{tag}>', xml, re.IGNORECASE)
    return m.group(1).strip() if m else ''

def _all_tags(xml: str, tag: str) -> List[str]:
    return [v.strip() for v in re.findall(rf'<{tag}>\s*([^<]+?)\s*</{tag}>', xml, re.IGNORECASE)]

def _blocks(xml: str, tag: str) -> List[str]:
    return re.findall(rf'<{tag}\b[^>]*>.*?</{tag}>', xml, re.DOTALL | re.IGNORECASE)

def _has_diff_markers(xml: str) -> bool:
    return bool(re.search(r'^\s*[+-](?![+-])', xml or '', re.MULTILINE))

# ---------------------------------------------------------------------------
# Diff reconstruction helpers
# ---------------------------------------------------------------------------

def _diff_to_xml(diff: str) -> str:
    """New state: keep context lines + added (+) lines; drop removed (-) lines."""
    lines = []
    for line in (diff or '').splitlines():
        s = line.rstrip('\r\n')
        if re.match(r'^\s*(@@|\+\+\+|---)', s):
            continue
        if re.match(r'^\s*-(?!-)', s):
            continue
        if re.match(r'^\s*\+(?!\+)', s):
            lines.append(re.sub(r'^\s*\+', '', s, count=1))
        else:
            lines.append(s)
    return '\n'.join(lines)

def _diff_old_state(diff: str) -> str:
    """Old state: keep context lines + removed (-) lines; drop added (+) lines."""
    lines = []
    for line in (diff or '').splitlines():
        s = line.rstrip('\r\n')
        if re.match(r'^\s*(@@|\+\+\+|---)', s):
            continue
        if re.match(r'^\s*\+(?!\+)', s):
            continue
        if re.match(r'^\s*-(?!-)', s):
            lines.append(re.sub(r'^\s*-', '', s, count=1))
        else:
            lines.append(s)
    return '\n'.join(lines)

def _parse_states(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool) -> Tuple[str, str]:
    """
    Return (old_clean, new_clean) ready for comparison.

    - diff_mode : old_xml is a diff string → reconstruct before/after
    - is_new    : nothing existed before → old_clean = ''
    - otherwise : both are plain XML strings
    """
    if diff_mode:
        diff = old_xml or ''
        return clean_pasted_xml(_diff_old_state(diff)), clean_pasted_xml(_diff_to_xml(diff))
    if is_new:
        return '', clean_pasted_xml(new_xml or old_xml or '')
    return clean_pasted_xml(old_xml or ''), clean_pasted_xml(new_xml or old_xml or '')

def _name_set(xml: str, block_tag: str) -> Set[str]:
    return {_tag(b, 'Name') for b in _blocks(xml, block_tag) if _tag(b, 'Name')}

def _find_deleted(old_state: str, new_state: str, block_tag: str,
                  name_fn=None) -> List[str]:
    """Return names present in old_state but absent from new_state."""
    if not old_state:
        return []
    get_name = name_fn or (lambda b: _tag(b, 'Name'))
    old_names = {get_name(b) for b in _blocks(old_state, block_tag) if get_name(b)}
    new_names = {get_name(b) for b in _blocks(new_state, block_tag) if get_name(b)}
    return sorted(old_names - new_names)

# ---------------------------------------------------------------------------
# Method name helpers
# ---------------------------------------------------------------------------

def _xml_method_names(old_state: str, new_state: str, is_new: bool) -> List[str]:
    """Return method names that are new OR whose Source changed."""
    def method_map(xml: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for b in _blocks(xml, 'Method'):
            name = _tag(b, 'Name')
            if name:
                src_m = re.search(r'<Source>(.*?)</Source>', b, re.DOTALL | re.IGNORECASE)
                out[name] = src_m.group(1).strip() if src_m else ''
        return out

    if is_new or not old_state:
        return [_tag(b, 'Name') for b in _blocks(new_state, 'Method') if _tag(b, 'Name')]
    old_map = method_map(old_state)
    new_map = method_map(new_state)
    return [n for n, src in new_map.items() if n not in old_map or src != old_map[n]]

# ---------------------------------------------------------------------------
# Class / Class Extension  (X++ source, not XML)
# ---------------------------------------------------------------------------

_METHOD_SIG_RE = re.compile(
    r'^\s*(?:public|private|protected|internal)\s+'
    r'(?:(?:static|final|abstract|display|edit|server|client)\s+)*'
    r'\w[\w<>\[\]]*\s+(\w+)\s*\(',
)

# Matches an annotated @@ hunk header: "@@ -N,N +N,N @@ methodName"
_HUNK_METHOD_RE = re.compile(r'^@@ [^@]+ @@ (\w+)\s*$')


def _class_methods_from_diff(diff: str) -> List[str]:
    """Return changed/added method names from a unified diff string.

    Two sources are checked in order:
    1. Annotated @@ hunk headers added by _annotate_diff_with_methods in app.py
       — these capture methods whose BODY changed (signature on a context line).
    2. + lines whose content matches a method signature
       — these capture newly ADDED methods.
    """
    names, seen = [], set()
    for line in (diff or '').splitlines():
        if line.startswith('@@'):
            m = _HUNK_METHOD_RE.match(line.strip())
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                names.append(m.group(1))
            continue
        if not (line.startswith('+') and not line.startswith('+++')):
            continue
        m = _METHOD_SIG_RE.match(line[1:])
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            names.append(m.group(1))
    return names


def _class_methods_from_source(source: str) -> List[str]:
    names, seen = [], set()
    for line in (source or '').splitlines():
        m = _METHOD_SIG_RE.match(line)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            names.append(m.group(1))
    return names


def _extract_method_bodies(source: str) -> Dict[str, str]:
    """Return a map of method_name → full source block (signature + body)."""
    result: Dict[str, str] = {}
    lines = source.splitlines()
    i = 0
    while i < len(lines):
        sig_m = _METHOD_SIG_RE.match(lines[i])
        if sig_m:
            name = sig_m.group(1)
            body: List[str] = [lines[i]]
            depth = lines[i].count('{') - lines[i].count('}')
            j = i + 1
            while j < len(lines):
                body.append(lines[j])
                depth += lines[j].count('{') - lines[j].count('}')
                j += 1
                if depth <= 0:
                    break
            result[name] = '\n'.join(body)
            i = j
        else:
            i += 1
    return result


def parse_class(old_code: str, new_code: str, is_new: bool, diff_mode: bool = False) -> Dict:
    result: Dict = {}
    if diff_mode:
        method_names = _class_methods_from_diff(old_code or '')
        # Deleted: methods in old state of diff that are gone in new state
        old_state_src = _diff_old_state(old_code or '')
        new_state_src = _diff_to_xml(old_code or '')
        old_m = set(_class_methods_from_source(old_state_src))
        new_m = set(_class_methods_from_source(new_state_src))
        deleted = sorted(old_m - new_m)
    elif is_new:
        method_names = _class_methods_from_source(new_code or '')
        deleted = []
    else:
        old_m = set(_class_methods_from_source(old_code or ''))
        new_m = set(_class_methods_from_source(new_code or ''))
        # Truly new methods (signature added)
        method_names = [m for m in new_m if m not in old_m]
        deleted = sorted(old_m - new_m)
        # No new method signatures found — check for methods whose body changed
        if not method_names and old_code and new_code:
            old_bodies = _extract_method_bodies(old_code)
            new_bodies = _extract_method_bodies(new_code)
            method_names = [
                m for m in new_bodies
                if m in old_bodies and new_bodies[m].strip() != old_bodies[m].strip()
            ]
    if method_names:
        result['method_names'] = method_names
    if deleted:
        result['deleted'] = deleted
    return result

# ---------------------------------------------------------------------------
# Table / Table Extension
# ---------------------------------------------------------------------------

def parse_table(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool = False) -> Dict:
    old_state, new_state = _parse_states(old_xml, new_xml, is_new, diff_mode)
    result: Dict = {}

    # Fields + field_groups via existing battle-tested logic
    if is_new or not old_state:
        field_map = parse_table_fields(new_state)
        if field_map:
            result['fields'] = list(field_map.values())
        groups = parse_field_groups(new_state)
        if groups:
            result['field_groups'] = [{'name': g, 'fields': ', '.join(m)} for g, m in sorted(groups.items())]
    else:
        # diff_table_xml handles both diff-markers and plain old+new XML
        if diff_mode:
            dt = diff_table_xml('', new_state)   # treat new_state as "all new" within the hunk
            # but also capture fields that appear only in old_state hunk (added in diff context)
            dt2 = diff_table_xml(old_state, new_state)
            fields = dt2['fields'] or dt['fields']
            fg = dt2['added_field_groups'] or dt['added_field_groups']
        else:
            dt = diff_table_xml(old_xml or '', new_xml or '')
            fields = dt['fields']
            fg = dt['added_field_groups']
        if fields:
            result['fields'] = fields
        if fg:
            result['field_groups'] = fg

    # Indexes — only new ones
    old_idx = _name_set(old_state, 'AxTableIndex')
    for block in _blocks(new_state, 'AxTableIndex'):
        name = _tag(block, 'Name')
        if name and name not in old_idx:
            result.setdefault('indexes', []).append(
                {'name': name, 'fields': ', '.join(_all_tags(block, 'DataField'))})

    # Relations — only new ones
    old_rel = _name_set(old_state, 'AxTableRelation')
    for block in _blocks(new_state, 'AxTableRelation'):
        rel_name = _tag(block, 'Name')
        if not rel_name or rel_name in old_rel:
            continue
        related_table = _tag(block, 'Table') or _tag(block, 'RelatedTable')
        for con in _blocks(block, 'AxTableRelationConstraint'):
            field = _tag(con, 'Field') or _tag(con, 'SourceField')
            rel_field = _tag(con, 'RelatedField') or _tag(con, 'TargetField')
            if field:
                result.setdefault('relations', []).append(
                    {'name': rel_name, 'field': field,
                     'related_table': related_table, 'related_table_field': rel_field})

    # Methods
    methods = _xml_method_names(old_state, new_state, is_new or not old_state)
    if methods:
        result['method_names'] = methods

    # Deletions (fields, indexes, relations, methods)
    if old_state:
        deleted = (
            _find_deleted(old_state, new_state, 'AxTableField') +
            _find_deleted(old_state, new_state, 'AxTableIndex') +
            _find_deleted(old_state, new_state, 'AxTableRelation') +
            _find_deleted(old_state, new_state, 'Method')
        )
        if deleted:
            result['deleted'] = sorted(set(deleted))

    return result

# ---------------------------------------------------------------------------
# Form / Form Extension
# ---------------------------------------------------------------------------

_AX_CTRL_MAP = {
    'AxFormStringControl': 'String', 'AxFormIntegerControl': 'Integer',
    'AxFormRealControl': 'Real', 'AxFormDateControl': 'Date',
    'AxFormCheckBoxControl': 'CheckBox', 'AxFormButtonControl': 'Button',
    'AxFormCommandButtonControl': 'CommandButton',
    'AxFormMenuButtonControl': 'MenuButton',
    'AxFormButtonGroupControl': 'ButtonGroup', 'AxFormGroupControl': 'Group',
    'AxFormGridControl': 'Grid', 'AxFormTabControl': 'Tab',
    'AxFormTabPageControl': 'TabPage', 'AxFormStaticTextControl': 'StaticText',
    'AxFormComboBoxControl': 'ComboBox', 'AxFormListBoxControl': 'ListBox',
    'AxFormRadioControl': 'Radio', 'AxFormSegmentedEntryControl': 'SegmentedEntry',
    'AxFormReferenceControl': 'Reference', 'AxFormDateTimeControl': 'DateTime',
    'AxFormInt64Control': 'Int64', 'AxFormListControl': 'List',
}
_CTRL_TRACKED = ('DataSource', 'DataField', 'DataMethod', 'Label', 'Visible',
                 'Skip', 'AllowEdit', 'Mandatory', 'ExtendedDataType', 'HelpText')

def _ctrl_type(i_type: str) -> str:
    return _AX_CTRL_MAP.get(i_type, re.sub(r'^AxForm|Control$', '', i_type) if i_type else '')

def _ctrl_block_map(xml: str) -> Dict[str, str]:
    """name → raw block for every AxFormControl in the XML."""
    result: Dict[str, str] = {}
    for block in re.findall(r'<AxFormControl\b[^>]*>.*?</AxFormControl>',
                            xml, re.DOTALL | re.IGNORECASE):
        name_m = re.search(r'<Name>\s*([^<]+?)\s*</Name>', block, re.IGNORECASE)
        if name_m:
            result[name_m.group(1).strip()] = block
    return result

def _ctrl_key_props(block: str) -> Dict:
    props = {t: _tag(block, t) for t in _CTRL_TRACKED}
    itype_m = re.search(r'i:type="([^"]+)"', block, re.IGNORECASE)
    props['i_type'] = itype_m.group(1) if itype_m else ''
    return props

def _parse_ctrl(block: str) -> Optional[Dict]:
    name_m = re.search(r'<Name>\s*([^<]+?)\s*</Name>', block, re.IGNORECASE)
    if not name_m:
        return None
    itype_m = re.search(r'i:type="([^"]+)"', block, re.IGNORECASE)
    entry: Dict = {'name': name_m.group(1).strip(),
                   'control_type': _ctrl_type(itype_m.group(1)) if itype_m else ''}
    ds = _tag(block, 'DataSource')
    df = _tag(block, 'DataField') or _tag(block, 'DataMethod')
    if ds: entry['data_source'] = ds
    if df: entry['data_field'] = df
    return entry

def _diff_controls(old_map: Dict[str, str], new_map: Dict[str, str]) -> Tuple[List, List]:
    """Compare two control maps; return (added, modified)."""
    added, modified = [], []
    for name, block in new_map.items():
        ctrl = _parse_ctrl(block)
        if not ctrl:
            continue
        if name not in old_map:
            added.append(ctrl)
        elif _ctrl_key_props(block) != _ctrl_key_props(old_map[name]):
            modified.append(ctrl)
    return added, modified

def parse_form(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool = False) -> Dict:
    old_state, new_state = _parse_states(old_xml, new_xml, is_new, diff_mode)
    result: Dict = {}

    # Data sources — always from new state (structural context)
    ds_list = [{'name': _tag(b, 'Name'), 'table': _tag(b, 'Table')}
               for b in _blocks(new_state, 'AxFormDataSource') if _tag(b, 'Name')]
    if ds_list:
        result['data_sources'] = ds_list

    # Controls
    new_map = _ctrl_block_map(new_state)
    if is_new or not old_state:
        # All controls are new (or can't compare without old version)
        controls = [c for c in (_parse_ctrl(b) for b in new_map.values()) if c]
        if controls:
            result['added_controls'] = controls
    else:
        old_map = _ctrl_block_map(old_state)
        added, modified = _diff_controls(old_map, new_map)
        if added: result['added_controls'] = added
        if modified: result['modified_controls'] = modified

    # Methods
    methods = _xml_method_names(old_state, new_state, is_new or not old_state)
    if methods:
        result['method_names'] = methods

    # Deletions
    if old_state:
        old_ctrl_names = set(_ctrl_block_map(old_state).keys())
        new_ctrl_names = set(_ctrl_block_map(new_state).keys())
        deleted = sorted((old_ctrl_names - new_ctrl_names) |
                         set(_find_deleted(old_state, new_state, 'Method')))
        if deleted:
            result['deleted'] = deleted

    return result

# ---------------------------------------------------------------------------
# View / View Extension
# ---------------------------------------------------------------------------

def parse_view(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool = False) -> Dict:
    old_state, new_state = _parse_states(old_xml, new_xml, is_new, diff_mode)
    result: Dict = {}

    # Data sources — only new ones
    old_ds: Set[str] = set()
    for tag in ('AxViewDataSource', 'AxQueryDataSource', 'AxDataEntityViewDataSource'):
        old_ds |= _name_set(old_state, tag)
    ds_list = []
    for tag in ('AxViewDataSource', 'AxQueryDataSource', 'AxDataEntityViewDataSource'):
        for b in _blocks(new_state, tag):
            name = _tag(b, 'Name')
            if name and name not in old_ds:
                ds_list.append({'name': name, 'table': _tag(b, 'Table'),
                                'join_type': _tag(b, 'JoinMode') or _tag(b, 'FetchMode')})
    if ds_list:
        result['data_sources'] = ds_list

    # Fields — only new ones
    old_fields = _name_set(old_state, 'AxViewField')
    fields = [{'name': _tag(b, 'Name'), 'data_source': _tag(b, 'DataSource'), 'edt': _tag(b, 'ExtendedDataType')}
              for b in _blocks(new_state, 'AxViewField')
              if _tag(b, 'Name') and _tag(b, 'Name') not in old_fields]
    if fields:
        result['fields'] = fields

    # Field groups — only new ones
    old_fg = _name_set(old_state, 'AxTableFieldGroup')
    groups = {g: m for g, m in parse_field_groups(new_state).items() if g not in old_fg}
    if groups:
        result['field_groups'] = [{'name': g, 'fields': ', '.join(m)} for g, m in sorted(groups.items())]

    methods = _xml_method_names(old_state, new_state, is_new or not old_state)
    if methods:
        result['method_names'] = methods

    if old_state:
        deleted = (
            _find_deleted(old_state, new_state, 'AxViewField') +
            _find_deleted(old_state, new_state, 'Method')
        )
        if deleted:
            result['deleted'] = sorted(set(deleted))

    return result

# ---------------------------------------------------------------------------
# Query / Query Extension
# ---------------------------------------------------------------------------

def parse_query(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool = False) -> Dict:
    old_state, new_state = _parse_states(old_xml, new_xml, is_new, diff_mode)
    result: Dict = {}

    old_ds: Set[str] = set()
    for tag in ('AxQueryDataSource', 'AxQuerySimpleDataSource'):
        old_ds |= _name_set(old_state, tag)
    ds_list = []
    for tag in ('AxQueryDataSource', 'AxQuerySimpleDataSource'):
        for b in _blocks(new_state, tag):
            name = _tag(b, 'Name')
            if name and name not in old_ds:
                ds_list.append({'name': name, 'table': _tag(b, 'Table'),
                                'join_type': _tag(b, 'JoinMode') or _tag(b, 'FetchMode')})
    if ds_list:
        result['data_sources'] = ds_list

    old_qf = {_tag(b, 'Field') or _tag(b, 'Name')
              for b in _blocks(old_state, 'AxQueryDataSourceField')
              if _tag(b, 'Field') or _tag(b, 'Name')}
    fields = [{'data_source': _tag(b, 'DataSource'), 'field': _tag(b, 'Field') or _tag(b, 'Name')}
              for b in _blocks(new_state, 'AxQueryDataSourceField')
              if (_tag(b, 'Field') or _tag(b, 'Name')) and (_tag(b, 'Field') or _tag(b, 'Name')) not in old_qf]
    if fields:
        result['fields'] = fields

    old_ranges = {(_tag(b, 'DataSource'), _tag(b, 'Field') or _tag(b, 'Name'))
                  for b in _blocks(old_state, 'AxQueryDataSourceRange')}
    ranges = []
    for b in _blocks(new_state, 'AxQueryDataSourceRange'):
        f = _tag(b, 'Field') or _tag(b, 'Name')
        ds = _tag(b, 'DataSource')
        if f and (ds, f) not in old_ranges:
            ranges.append({'data_source': ds, 'field': f, 'value': _tag(b, 'Value')})
    if ranges:
        result['ranges'] = ranges

    methods = _xml_method_names(old_state, new_state, is_new or not old_state)
    if methods:
        result['method_names'] = methods

    if old_state:
        old_ds_names: Set[str] = set()
        for tag in ('AxQueryDataSource', 'AxQuerySimpleDataSource'):
            old_ds_names |= _name_set(old_state, tag)
        new_ds_names: Set[str] = set()
        for tag in ('AxQueryDataSource', 'AxQuerySimpleDataSource'):
            new_ds_names |= _name_set(new_state, tag)
        deleted = sorted((old_ds_names - new_ds_names) |
                         set(_find_deleted(old_state, new_state, 'Method')))
        if deleted:
            result['deleted'] = deleted

    return result

# ---------------------------------------------------------------------------
# Enum / Enum Extension
# ---------------------------------------------------------------------------

def parse_enum(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool = False) -> Dict:
    old_state, new_state = _parse_states(old_xml, new_xml, is_new, diff_mode)
    old_vals = _name_set(old_state, 'AxEnumValue')
    values = [{'name': _tag(b, 'Name'), 'label': _tag(b, 'Label') or _tag(b, 'Name')}
              for b in _blocks(new_state, 'AxEnumValue')
              if _tag(b, 'Name') and _tag(b, 'Name') not in old_vals]
    result: Dict = {}
    if values:
        result['values'] = values
    deleted = _find_deleted(old_state, new_state, 'AxEnumValue')
    if deleted:
        result['deleted'] = deleted
    return result

# ---------------------------------------------------------------------------
# EDT / EDT Extension
# ---------------------------------------------------------------------------

def parse_edt(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool = False) -> Dict:
    _, new_state = _parse_states(old_xml, new_xml, is_new, diff_mode)
    result: Dict = {}
    itype_m = re.search(r'i:type="([^"]+)"', new_state, re.IGNORECASE)
    if itype_m:
        result['data_type'] = re.sub(r'^AxEdt', '', itype_m.group(1))
    extends = _tag(new_state, 'Extends')
    if extends:
        result['extends'] = extends
    return result

# ---------------------------------------------------------------------------
# Data Entity
# ---------------------------------------------------------------------------

def parse_data_entity(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool = False) -> Dict:
    old_state, new_state = _parse_states(old_xml, new_xml, is_new, diff_mode)
    result: Dict = {}

    old_ds: Set[str] = set()
    for tag in ('AxDataEntityViewDataSource', 'AxDataEntityViewExtensionDataSource'):
        old_ds |= _name_set(old_state, tag)
    ds_list = []
    for tag in ('AxDataEntityViewDataSource', 'AxDataEntityViewExtensionDataSource'):
        for b in _blocks(new_state, tag):
            name = _tag(b, 'Name')
            if name and name not in old_ds:
                ds_list.append({'name': name, 'table': _tag(b, 'Table'),
                                'join_type': _tag(b, 'JoinMode') or _tag(b, 'FetchMode')})
    if ds_list:
        result['data_sources'] = ds_list

    old_fields: Set[str] = set()
    for tag in ('AxDataEntityViewField', 'AxViewField', 'AxDataEntityViewExtensionField', 'AxDataEntityViewExtensionMappedField'):
        old_fields |= _name_set(old_state, tag)
    fields = []
    for tag in ('AxDataEntityViewField', 'AxViewField', 'AxDataEntityViewExtensionField', 'AxDataEntityViewExtensionMappedField'):
        for b in _blocks(new_state, tag):
            name = _tag(b, 'Name')
            if name and name not in old_fields:
                fields.append({'name': name, 'data_source': _tag(b, 'DataSource'),
                               'edt': _tag(b, 'ExtendedDataType')})
    if fields:
        result['fields'] = fields

    old_keys: Set[str] = set()
    for tag in ('AxDataEntityViewKey', 'AxEntityKey'):
        old_keys |= _name_set(old_state, tag)
    keys = []
    for tag in ('AxDataEntityViewKey', 'AxEntityKey'):
        for b in _blocks(new_state, tag):
            name = _tag(b, 'Name')
            if name and name not in old_keys:
                kfields = ', '.join(_all_tags(b, 'DataField') + _all_tags(b, 'Field'))
                keys.append({'name': name, 'fields': kfields})
    if keys:
        result['entity_keys'] = keys

    old_fg = _name_set(old_state, 'AxTableFieldGroup')
    groups = {g: m for g, m in parse_field_groups(new_state).items() if g not in old_fg}
    if groups:
        result['field_groups'] = [{'name': g, 'fields': ', '.join(m)} for g, m in sorted(groups.items())]

    methods = _xml_method_names(old_state, new_state, is_new or not old_state)
    if methods:
        result['method_names'] = methods

    if old_state:
        all_old_fields: Set[str] = set()
        for tag in ('AxDataEntityViewField', 'AxViewField', 'AxDataEntityViewExtensionField', 'AxDataEntityViewExtensionMappedField'):
            all_old_fields |= _name_set(old_state, tag)
        all_new_fields: Set[str] = set()
        for tag in ('AxDataEntityViewField', 'AxViewField', 'AxDataEntityViewExtensionField', 'AxDataEntityViewExtensionMappedField'):
            all_new_fields |= _name_set(new_state, tag)
        deleted = sorted((all_old_fields - all_new_fields) |
                         set(_find_deleted(old_state, new_state, 'Method')))
        if deleted:
            result['deleted'] = deleted

    return result

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

_ACCESS_LEVEL_NUM_MAP = {
    '0': 'NoAccess', '1': 'NoAccess', '2': 'Read', '3': 'Update',
    '4': 'Create', '5': 'Correct', '6': 'Delete', '7': 'Maintain',
}


def _security_access_level(block: str) -> str:
    """
    Extract the effective access level from any AX security permission block.

    AxSecurityEntryPointReference  → ObjectAccessLevel / EffectiveAccess
    AxSecurityTablePermission      → Access
    AxSecurityServiceOperationPermission → Access / EffectiveAccess
    Fallback: derive from individual Read/Update/Create/DeletePermissions flags.
    Numeric values (0–7) are mapped to their string equivalents.
    """
    direct = (
        _tag(block, 'ObjectAccessLevel') or
        _tag(block, 'EffectiveAccess') or
        _tag(block, 'Access') or
        _tag(block, 'AccessLevel') or
        _tag(block, 'EffectiveAccessLevel') or
        _tag(block, 'Level') or
        _tag(block, 'GrantedPermission') or
        _tag(block, 'PermissionValue')
    )
    if direct:
        mapped = _ACCESS_LEVEL_NUM_MAP.get(direct.strip())
        return mapped if mapped else direct
    # Individual permission flags — supports both old-style <DeletePermissions>Allow</DeletePermissions>
    # and new D365 FO <Grant><Delete>Allow</Delete>...</Grant> format.
    # Delete=Allow is the highest flag → maps to "Maintain" (D365 convention for full access).
    for level, flags in (
        ('Maintain', ('DeletePermissions', 'Delete', 'FullControl')),
        ('Correct',  ('CorrectPermissions', 'Correct')),
        ('Update',   ('UpdatePermissions', 'Update')),
        ('Create',   ('CreatePermissions', 'Create')),
        ('Read',     ('ReadPermissions',   'Read')),
    ):
        for flag in flags:
            val = _tag(block, flag)
            if val.lower() in ('allow', 'yes', 'true', '1'):
                return level
    return ''


_NAME_SUFFIX_ACCESS = [
    ('maintain',  'Maintain'),
    ('fullaccess','Maintain'),
    ('delete',    'Delete'),
    ('correct',   'Correct'),
    ('update',    'Update'),
    ('edit',      'Update'),
    ('create',    'Create'),
    ('add',       'Create'),
    ('view',      'Read'),
    ('read',      'Read'),
    ('display',   'Read'),
    ('approve',   'Update'),
    ('post',      'Update'),
    ('process',   'Update'),
    ('invoke',    'Invoke'),
]


def _access_from_name(name: str) -> str:
    """Derive access level from privilege/object name suffix (naming convention fallback)."""
    n = name.lower()
    for suffix, level in _NAME_SUFFIX_ACCESS:
        if n.endswith(suffix):
            return level
    return ''


def parse_security(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool = False, object_name: str = '') -> Dict:
    _, new_state = _parse_states(old_xml, new_xml, is_new, diff_mode)

    # Fallback: derive access level from the root privilege / duty name
    # Prioritize object_name (passed from file path) over _tag('Name') because in diff_mode
    # the root <Name> tag might be missing, and _tag would incorrectly find the first child's name.
    root_name = object_name or _tag(new_state, 'Name')
    name_hint = _access_from_name(root_name)

    permissions = []
    for tag in ('AxSecurityEntryPointReference', 'AxSecurityTablePermission',
                'AxSecurityServiceOperationPermission'):
        for b in _blocks(new_state, tag):
            obj_name = _tag(b, 'Name') or _tag(b, 'ObjectName')
            if not obj_name:
                continue
            access = (
                _security_access_level(b) or
                _access_from_name(obj_name) or
                name_hint
            )
            permissions.append({'object_name': obj_name, 'access_level': access})

    if not permissions:
        return {}
    
    res = {'permissions': permissions}
    subtype = _tag(new_state, 'Subtype')
    if subtype:
        res['subtype'] = subtype.lower()
    return res

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def parse_services(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool = False) -> Dict:
    _, new_state = _parse_states(old_xml, new_xml, is_new, diff_mode)
    names: List[str] = []
    seen: Set[str] = set()
    for tag in ('AxServiceGroupService', 'AxServiceOperation'):
        for b in _blocks(new_state, tag):
            name = _tag(b, 'Name') or _tag(b, 'Service')
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    if not names:
        return {}
    return {'details': [{'name': n} for n in names], 'method_names': names}

# ---------------------------------------------------------------------------
# Menu Item (Action / Display / Output)
# ---------------------------------------------------------------------------

def parse_menu_item(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool = False) -> Dict:
    _, new_state = _parse_states(old_xml, new_xml, is_new, diff_mode)
    result: Dict = {}
    obj_type = _tag(new_state, 'ObjectType')
    obj_name_ref = _tag(new_state, 'Object') or _tag(new_state, 'ObjectName')
    label = _tag(new_state, 'Label')
    if obj_type:
        result['object_type'] = obj_type
    if obj_name_ref:
        result['object_name_ref'] = obj_name_ref
    if label:
        result['label'] = label
    return result


# ---------------------------------------------------------------------------
# Menu Extension
# ---------------------------------------------------------------------------

def parse_menu_extension(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool = False) -> Dict:
    old_state, new_state = _parse_states(old_xml, new_xml, is_new, diff_mode)
    result: Dict = {}

    old_items: Set[str] = set()
    for b in _blocks(old_state, 'AxMenuItemReference'):
        name = _tag(b, 'MenuItemName') or _tag(b, 'Name')
        if name:
            old_items.add(name)

    items = []
    for b in _blocks(new_state, 'AxMenuItemReference'):
        item_name = _tag(b, 'MenuItemName') or _tag(b, 'Name')
        if not item_name or item_name in old_items:
            continue
        parent = (
            _tag(b, 'ParentMenuItemName') or
            _tag(b, 'SubMenuName') or
            _tag(b, 'ParentName') or ''
        )
        item_type = _tag(b, 'MenuItemType') or ''
        items.append({'parent': parent, 'menu_item_name': item_name, 'item_type': item_type})

    if items:
        result['items'] = items
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def parse_report(old_xml: str, new_xml: str, is_new: bool, diff_mode: bool = False) -> Dict:
    _, new_state = _parse_states(old_xml, new_xml, is_new, diff_mode)
    fields = []
    for tag in ('AxReportDataSetField', 'Field'):
        for b in _blocks(new_state, tag):
            fname = _tag(b, 'Name')
            if fname:
                fields.append({
                    'field_label': _tag(b, 'Label') or fname,
                    'dataset_field': fname,
                    'source_table': _tag(b, 'SourceTable') or _tag(b, 'Table'),
                    'source_field': _tag(b, 'SourceField') or _tag(b, 'Field'),
                    'data_type': _tag(b, 'DataType') or _tag(b, 'Type'),
                    'logic': '', 'remarks': '',
                })
    return {'fields': fields} if fields else {}

# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_PARSERS = {
    'class': parse_class, 'class_extension': parse_class,
    'table': parse_table, 'table_extension': parse_table,
    'form': parse_form, 'form_extension': parse_form,
    'view': parse_view, 'view_extension': parse_view,
    'query': parse_query, 'query_extension': parse_query,
    'enum': parse_enum, 'enum_extension': parse_enum,
    'edt': parse_edt, 'edt_extension': parse_edt,
    'data_entity': parse_data_entity,
    'data_entity_extension': parse_data_entity,
    'security': parse_security,
    'services': parse_services,
    'report': parse_report,
    'menu_item': parse_menu_item,
    'menu_extension': parse_menu_extension,
}

def parse_object(
    object_type: str,
    old_xml: str,
    new_xml: str,
    is_new: bool,
    diff_mode: bool = False,
    object_name: str = '',
) -> Optional[Dict]:
    fn = _PARSERS.get(object_type)
    if fn is None:
        return None
    # Pass object_name only if the parser function accepts it
    import inspect
    sig = inspect.signature(fn)
    if 'object_name' in sig.parameters:
        return fn(old_xml, new_xml, is_new, diff_mode, object_name=object_name)
    return fn(old_xml, new_xml, is_new, diff_mode)
