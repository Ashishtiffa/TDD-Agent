import os
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
            # History is descending. Find current_version and take the one after it.
            idx = history.index(current_version)
            if idx + 1 < len(history):
                return history[idx + 1]
        except ValueError:
            # current_version not found in history? Unexpected, but fallback to one before or None
            pass
            
        return None

    def analyze_changeset(self, changeset_id: int) -> Dict:
        # Step 1: Get Changeset Details
        details = self.azure_service.get_changeset_details(changeset_id)
        
        # Step 2: Get Changed Objects
        changes = self.azure_service.get_changeset_changes(changeset_id)
        
        results = {
            "changesetId": details.get('changesetId'),
            "author": details.get('author', {}).get('displayName'),
            "date": details.get('createdDate'),
            "comment": details.get('comment'),
            "objects": []
        }

        for change in changes:
            path = change.get('item', {}).get('path')
            if not path or not path.endswith('.xml'):
                continue
            
            # Step 3: Detect Object Type
            obj_type, obj_name, sec_subtype, ser_subtype = ObjectTypeDetector.detect(path)
            if obj_type == 'unknown':
                continue

            current_version = change.get('item', {}).get('version')
            change_type = change.get('changeType', '').lower()
            
            # Step 5: Determine Versions
            old_version = self._resolve_old_version(path, current_version, change_type)
            
            is_new = 'add' in change_type
            is_deleted = 'delete' in change_type
            
            # Step 6 & 7: Download XMLs
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

            # If both are empty, nothing to analyze
            if not new_xml and not old_xml:
                continue

            # Step 8 & 9: Compare & Analyze (using AI function)
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
            
            results['objects'].append(analysis_result)

        return results
