
from ax_xml_parser import parse_security

def test_security_parsing_diff():
    diff = """
@@ -10,1 +10,6 @@
+		<AxSecurityTablePermission>
+			<Name>HSPlanningVarianceJournalBatch</Name>
+		</AxSecurityTablePermission>
    """
    
    result = parse_security(diff, "", is_new=False, diff_mode=True, object_name="HSPlanningVarianceJournalBatchMaintain")
    print("Result with object_name (expect Maintain):", result)

if __name__ == "__main__":
    test_security_parsing_diff()
