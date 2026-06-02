import requests
import base64
import os
from typing import Dict, List, Optional

class AzureDevOpsService:
    def __init__(self, org: str, pat: str, project: Optional[str] = None):
        self.org = org
        self.pat = pat
        self.project = project
        self.global_base_url = f"https://dev.azure.com/{org}/_apis/tfvc"
        if project:
            self.project_base_url = f"https://dev.azure.com/{org}/{project}/_apis/tfvc"
        else:
            self.project_base_url = self.global_base_url
            
        self.auth_header = self._get_auth_header()

    def _get_auth_header(self) -> Dict[str, str]:
        auth_str = f":{self.pat}"
        encoded_auth = base64.b64encode(auth_str.encode()).decode()
        return {"Authorization": f"Basic {encoded_auth}"}

    def _handle_response(self, response: requests.Response) -> Dict:
        if response.status_code == 203:
            raise Exception("Authentication Failed: Azure DevOps returned a login page (203 status). Please verify your Personal Access Token (PAT).")
        
        response.raise_for_status()
        
        try:
            return response.json()
        except requests.exceptions.JSONDecodeError:
            raise Exception("Azure DevOps API returned an invalid JSON response. Please verify your Organization and Project configurations.")

    def get_changeset_details(self, changeset_id: int) -> Dict:
        url = f"{self.global_base_url}/changesets/{changeset_id}?api-version=7.1"
        response = requests.get(url, headers=self.auth_header)
        return self._handle_response(response)

    def get_changeset_changes(self, changeset_id: int) -> List[Dict]:
        url = f"{self.global_base_url}/changesets/{changeset_id}/changes?api-version=7.1"
        response = requests.get(url, headers=self.auth_header)
        data = self._handle_response(response)
        return data.get('value', [])

    def get_item_history(self, path: str) -> List[int]:
        url = f"{self.project_base_url}/changesets?searchCriteria.itemPath={path}&api-version=7.1"
        response = requests.get(url, headers=self.auth_header)
        data = self._handle_response(response)
        return [item.get('changesetId') for item in data.get('value', []) if item.get('changesetId')]

    def get_item_content(self, path: str, version: Optional[int] = None) -> str:
        url = f"{self.project_base_url}/items?path={path}&api-version=7.1&download=true"
        if version:
            url += f"&versionDescriptor.versionType=changeset&versionDescriptor.version={version}"
        
        response = requests.get(url, headers=self.auth_header)
        
        if response.status_code == 203:
            raise Exception("Authentication Failed: Azure DevOps returned a login page (203 status). Please verify your Personal Access Token (PAT).")
        
        response.raise_for_status()
        return response.text

    def get_work_item_details(self, work_item_id: int) -> Dict:
        url = f"https://dev.azure.com/{self.org}/{self.project}/_apis/wit/workitems/{work_item_id}?$expand=relations&api-version=7.1"
        response = requests.get(url, headers=self.auth_header)
        return self._handle_response(response)
