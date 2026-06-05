import os
import re

class ObjectTypeDetector:
    TYPE_MAP = {
        'AxTable': 'table',
        'AxClass': 'class',
        'AxForm': 'form',
        'AxView': 'view',
        'AxEdt': 'edt',
        'AxEnum': 'enum',
        'AxQuery': 'query',
        'AxDataEntityView': 'data_entity',
        'AxTableExtension': 'table_extension',
        'AxFormExtension': 'form_extension',
        'AxViewExtension': 'view_extension',
        'AxEdtExtension': 'edt_extension',
        'AxQueryExtension': 'query_extension',
        'AxEnumExtension': 'enum_extension',
        'AxClassExtension': 'class_extension',
        'AxSecurityPrivilege': 'security',
        'AxSecurityDuty': 'security',
        'AxSecurityRole': 'security',
        'AxSecurityPolicy': 'security',
        'AxService': 'services',
        'AxServiceGroup': 'services',
        'AxReport': 'report'
    }

    @staticmethod
    def detect(path: str):
        # Example path: $/HTS-D365/Trunk/UAT/Metadata/HISOL/HISOL/AxTable/HSOCRInvoiceTemplate.xml
        parts = path.split('/')
        if not parts:
            return None, None

        filename = parts[-1]
        object_name = os.path.splitext(filename)[0]
        
        object_type = 'unknown'
        for folder_name, type_key in ObjectTypeDetector.TYPE_MAP.items():
            if folder_name in parts:
                object_type = type_key
                break
        
        # Refine security subtype
        security_subtype = None
        if object_type == 'security':
            if 'AxSecurityPrivilege' in parts: security_subtype = 'privilege'
            elif 'AxSecurityDuty' in parts: security_subtype = 'duty'
            elif 'AxSecurityRole' in parts: security_subtype = 'role'
            elif 'AxSecurityPolicy' in parts: security_subtype = 'policy'

        # Refine services subtype
        services_subtype = None
        if object_type == 'services':
            if 'AxService' in parts: services_subtype = 'service'
            elif 'AxServiceGroup' in parts: services_subtype = 'service_group'

        return object_type, object_name, security_subtype, services_subtype
