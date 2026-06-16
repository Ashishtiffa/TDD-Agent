import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from azure_devops_service import AzureDevOpsService
from object_type_detector import ObjectTypeDetector
from typing import List, Dict, Optional


class WorkItemAnalyzerService:
    def __init__(self, azure_service: AzureDevOpsService, ai_analyze_fn):
        self.azure_service = azure_service
        self.ai_analyze_fn = ai_analyze_fn

    def _extract_changeset_ids(self, work_item_data: dict) -> List[int]:
        """Parse 'Fixed in Changeset' artifact links from work item relations.
        URL format: vstfs:///VersionControl/Changeset/10284
        """
        relations = work_item_data.get('relations') or []
        ids = []
        for rel in relations:
            if rel.get('rel') != 'ArtifactLink':
                continue
            name = rel.get('attributes', {}).get('name', '')
            if 'fixed in changeset' not in name.lower():
                continue
            url = rel.get('url', '')
            m = re.search(r'/Changeset/(\d+)', url, re.IGNORECASE)
            if m:
                ids.append(int(m.group(1)))
        return sorted(ids)

    def _process_object(self, path, obj_name, obj_type, cs_type_map, sec_subtype, ser_subtype, menu_subtype, wi_cs_set):
        """Fetch history/XML for one object and run AI analysis.
        Returns (result_dict, None) on success, (None, failed_dict) on AI error, (None, None) to skip.
        """
        try:
            history = self.azure_service.get_item_history(path)
        except Exception as e:
            print(f"Warning: failed to get history for {path}: {e}")
            return None, None

        if not history:
            return None, None

        wi_in_history = [cs for cs in history if cs in wi_cs_set]
        if not wi_in_history:
            return None, None

        oldest_wi_cs = min(wi_in_history)
        latest_wi_cs = max(wi_in_history)

        is_new = False
        baseline_cs = None
        try:
            idx = history.index(oldest_wi_cs)
            if idx + 1 < len(history):
                baseline_cs = history[idx + 1]
            else:
                is_new = True
        except ValueError:
            is_new = True

        first_change_type = cs_type_map.get(oldest_wi_cs, '')
        if 'add' in first_change_type.lower():
            is_new = True
            baseline_cs = None

        latest_change_type = cs_type_map.get(latest_wi_cs, '')
        is_deleted = 'delete' in latest_change_type.lower()

        print(
            f"  {obj_name} ({obj_type}): "
            f"baseline={baseline_cs or 'N/A (new)'}, latest={latest_wi_cs}"
            + (" [DELETED]" if is_deleted else "")
            + (" [NEW]" if is_new else "")
        )

        baseline_xml = ''
        latest_xml = ''

        if not is_new and baseline_cs:
            try:
                baseline_xml = self.azure_service.get_item_content(path, baseline_cs)
            except Exception as e:
                print(f"  Warning: failed to download baseline for {obj_name} (cs {baseline_cs}): {e}")

        if not is_deleted:
            try:
                latest_xml = self.azure_service.get_item_content(path, latest_wi_cs)
            except Exception as e:
                print(f"  Warning: failed to download latest for {obj_name} (cs {latest_wi_cs}): {e}")

        if not latest_xml and not baseline_xml:
            return None, None

        try:
            analysis_result = self.ai_analyze_fn(
                object_type=obj_type,
                is_new=is_new,
                old_code=baseline_xml,
                new_code=latest_xml,
                object_name=obj_name
            )
        except Exception as e:
            err_msg = str(e)
            print(f"  AI analysis failed for {obj_name}: {err_msg}")
            return None, {"name": obj_name, "type": obj_type, "reason": err_msg}

        if is_deleted:
            analysis_result['status'] = 'Deleted'
            analysis_result['description'] = "Object deleted. " + analysis_result.get('description', '')
        elif is_new:
            analysis_result['status'] = 'New'

        analysis_result['baseline_changeset'] = baseline_cs
        analysis_result['latest_changeset'] = latest_wi_cs

        if sec_subtype:
            analysis_result['subtype'] = sec_subtype
        if ser_subtype:
            analysis_result['subtype'] = ser_subtype
        if menu_subtype:
            analysis_result['subtype'] = menu_subtype

        return analysis_result, None

    def analyze_work_item(self, work_item_id: int) -> dict:
        # ── Step 1: Work item metadata ──────────────────────────────────────
        wi_data = self.azure_service.get_work_item_details(work_item_id)
        fields = wi_data.get('fields', {}) or {}
        work_item_type = fields.get('System.WorkItemType', 'Enhancement')
        work_item_title = fields.get('System.Title', '')
        author_field = fields.get('System.ChangedBy', '')
        author = (
            author_field.get('displayName', '')
            if isinstance(author_field, dict)
            else str(author_field)
        )

        # ── Step 2: All changesets linked to this work item ──────────────────
        wi_changeset_ids = self._extract_changeset_ids(wi_data)
        if not wi_changeset_ids:
            raise Exception(
                f"No 'Fixed in Changeset' links found on Work Item {work_item_id}. "
                "Ensure changesets are linked to the work item in Azure DevOps."
            )

        wi_cs_set = set(wi_changeset_ids)
        print(f"Work Item {work_item_id}: found {len(wi_changeset_ids)} linked changeset(s): {wi_changeset_ids}")

        # ── Step 3: Collect changed objects across all WI changesets ─────────
        object_cs_map: Dict[str, Dict[int, str]] = {}
        project_solution = ''

        for cs_id in wi_changeset_ids:
            try:
                changes = self.azure_service.get_changeset_changes(cs_id)
                for change in changes:
                    path = change.get('item', {}).get('path', '')
                    if not path:
                        continue
                    if not project_solution and path.endswith('.rnrproj'):
                        project_solution = path.split('/')[-1]
                        print(f"  Found project solution: {project_solution}")
                        continue
                    if not path.endswith('.xml'):
                        continue
                    change_type = change.get('changeType', '')
                    if path not in object_cs_map:
                        object_cs_map[path] = {}
                    object_cs_map[path][cs_id] = change_type
            except Exception as e:
                print(f"Warning: failed to get changes for changeset {cs_id}: {e}")

        print(f"Work Item {work_item_id}: {len(object_cs_map)} unique XML path(s) found across all changesets.")

        # ── Step 3.5: Deduplicate by (obj_name, obj_type) ───────────────────
        path_meta = {}
        for path in object_cs_map:
            obj_type, obj_name, sec_subtype, ser_subtype, menu_subtype = ObjectTypeDetector.detect(path)
            if obj_type != 'unknown':
                path_meta[path] = (obj_type, obj_name, sec_subtype, ser_subtype, menu_subtype)

        groups: Dict = defaultdict(list)
        for path, (obj_type, obj_name, sec_subtype, ser_subtype, menu_subtype) in path_meta.items():
            groups[(obj_name, obj_type)].append((path, object_cs_map[path], sec_subtype, ser_subtype, menu_subtype))

        deduplicated = []
        for (obj_name, obj_type), entries in groups.items():
            primary_path, primary_cs_map, sec_subtype, ser_subtype, menu_subtype = max(entries, key=lambda e: len(e[1]))
            if len(entries) > 1:
                print(f"  Dedup: {obj_name} ({obj_type}) found in {len(entries)} branch path(s) — using primary: {primary_path}")
            deduplicated.append((primary_path, obj_name, obj_type, primary_cs_map, sec_subtype, ser_subtype, menu_subtype))

        print(f"Work Item {work_item_id}: {len(deduplicated)} unique logical object(s) after deduplication.")

        date = ''
        try:
            cs_details = self.azure_service.get_changeset_details(max(wi_changeset_ids))
            date = cs_details.get('createdDate', '')
        except Exception:
            pass

        results = {
            "workItemId": work_item_id,
            "workItemType": work_item_type,
            "workItemTitle": work_item_title,
            "author": author,
            "date": date,
            "changesets": wi_changeset_ids,
            "project_solution": project_solution,
            "objects": [],
            "failed_objects": []
        }

        # ── Step 4: Process all unique objects in parallel ───────────────────
        print(f"Work Item {work_item_id}: analysing {len(deduplicated)} object(s) with 3 parallel workers...")
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_obj = {
                executor.submit(
                    self._process_object,
                    path, obj_name, obj_type, cs_type_map, sec_subtype, ser_subtype, menu_subtype, wi_cs_set
                ): (obj_name, obj_type)
                for path, obj_name, obj_type, cs_type_map, sec_subtype, ser_subtype, menu_subtype in deduplicated
            }
            for future in as_completed(future_to_obj):
                try:
                    analysis_result, failed = future.result()
                except Exception as e:
                    obj_name, obj_type = future_to_obj[future]
                    print(f"  Unexpected error for {obj_name}: {e}")
                    results['failed_objects'].append({"name": obj_name, "type": obj_type, "reason": str(e)})
                    continue
                if failed:
                    results['failed_objects'].append(failed)
                elif analysis_result:
                    results['objects'].append(analysis_result)

        return results
