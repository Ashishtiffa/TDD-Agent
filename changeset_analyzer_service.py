import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from azure_devops_service import AzureDevOpsService
from object_type_detector import ObjectTypeDetector
from typing import List, Dict, Optional

class ChangesetAnalyzerService:
    def __init__(self, azure_service: AzureDevOpsService, ai_analyze_fn):
        self.azure_service = azure_service
        self.ai_analyze_fn = ai_analyze_fn

    def _resolve_old_version(self, path: str, current_version: int, change_type: str) -> Optional[int]:
        if 'add' in change_type.lower():
            return None

        history = self.azure_service.get_item_history(path)
        try:
            idx = history.index(current_version)
            if idx + 1 < len(history):
                return history[idx + 1]
        except ValueError:
            pass

        return None

    def _process_change(self, change, changeset_id):
        """Download XMLs and run AI analysis for a single changed file.
        Returns analysis_result dict, or None to skip.
        """
        path = change.get('item', {}).get('path')
        if not path or not path.endswith('.xml'):
            return None

        obj_type, obj_name, sec_subtype, ser_subtype, menu_subtype = ObjectTypeDetector.detect(path)
        if obj_type == 'unknown':
            return None

        current_version = change.get('item', {}).get('version')
        change_type = change.get('changeType', '').lower()

        old_version = self._resolve_old_version(path, current_version, change_type)

        is_new = 'add' in change_type
        is_deleted = 'delete' in change_type

        new_xml = ""
        old_xml = ""

        if not is_deleted:
            try:
                new_xml = self.azure_service.get_item_content(path, current_version)
            except Exception as e:
                print(f"Error downloading new version of {obj_name}: {e}")

        if old_version:
            try:
                old_xml = self.azure_service.get_item_content(path, old_version)
            except Exception as e:
                print(f"Error downloading old version of {obj_name}: {e}")

        if not new_xml and not old_xml:
            return None

        analysis_result = self.ai_analyze_fn(
            object_type=obj_type,
            is_new=is_new,
            old_code=old_xml,
            new_code=new_xml,
            object_name=obj_name
        )

        if is_deleted:
            analysis_result['status'] = 'Deleted'
            analysis_result['description'] = f"Object deleted in changeset {current_version}. " + analysis_result.get('description', '')
        elif is_new:
            analysis_result['status'] = 'New'

        if sec_subtype: analysis_result['subtype'] = sec_subtype
        if ser_subtype: analysis_result['subtype'] = ser_subtype
        if menu_subtype: analysis_result['subtype'] = menu_subtype

        return analysis_result

    def analyze_changeset(self, changeset_id: int) -> Dict:
        # Step 1: Get Changeset Details
        details = self.azure_service.get_changeset_details(changeset_id)

        comment = details.get('comment', '')
        wi_match = re.search(r'WI\s*(\d+)', comment, re.IGNORECASE)

        work_item_id = None
        work_item_type = "Enhancement"
        work_item_title = comment

        if wi_match:
            work_item_id = int(wi_match.group(1))
            try:
                wi_details = self.azure_service.get_work_item_details(work_item_id)
                fields = wi_details.get('fields', {})
                work_item_type = fields.get('System.WorkItemType', 'Enhancement')
                work_item_title = fields.get('System.Title', comment)
            except Exception as e:
                print(f"Failed to fetch work item details for {work_item_id}: {e}")

        # Step 2: Get Changed Objects
        changes = self.azure_service.get_changeset_changes(changeset_id)

        results = {
            "changesetId": details.get('changesetId'),
            "author": details.get('author', {}).get('displayName'),
            "date": details.get('createdDate'),
            "comment": comment,
            "workItemId": work_item_id,
            "workItemType": work_item_type,
            "workItemTitle": work_item_title,
            "objects": []
        }

        xml_changes = [c for c in changes if c.get('item', {}).get('path', '').endswith('.xml')]
        print(f"Changeset {changeset_id}: analysing {len(xml_changes)} XML file(s) with 3 parallel workers...")

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(self._process_change, change, changeset_id) for change in xml_changes]
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        results['objects'].append(result)
                except Exception as e:
                    print(f"Error processing change: {e}")

        return results
