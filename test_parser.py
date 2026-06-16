
from ax_xml_parser import parse_security

def test_security_parsing():
    xml = """
<AxSecurityPrivilege xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
	<Name>HSPlanningVarianceJournalBatchMaintain</Name>
	<Label>@HSP:PlanningVarianceJournalBatchMaintain</Label>
	<Description>@HSP:PlanningVarianceJournalBatchMaintainDescription</Description>
	<EntryPointPermissions />
	<TablePermissions>
		<AxSecurityTablePermission>
			<Name>HSPlanningVarianceJournalBatch</Name>
			<Access>7</Access>
		</AxSecurityTablePermission>
	</TablePermissions>
</AxSecurityPrivilege>
    """
    result = parse_security("", xml, is_new=True)
    print("Result with numeric Access 7:", result)

    xml2 = """
<AxSecurityPrivilege>
	<Name>HSPlanningVarianceJournalBatchView</Name>
	<TablePermissions>
		<AxSecurityTablePermission>
			<Name>HSPlanningVarianceJournalBatch</Name>
			<Access>Read</Access>
		</AxSecurityTablePermission>
	</TablePermissions>
</AxSecurityPrivilege>
    """
    result2 = parse_security("", xml2, is_new=True)
    print("Result with string Access Read:", result2)

    xml3 = """
<AxSecurityPrivilege>
	<Name>HSPlanningVarianceJournalBatchMaintain</Name>
	<EntryPointPermissions>
		<AxSecurityEntryPointReference>
			<Name>HSPlanningVarianceJournalBatch</Name>
			<Grant>
				<Read>Allow</Read>
			</Grant>
		</AxSecurityEntryPointReference>
	</EntryPointPermissions>
</AxSecurityPrivilege>
    """
    result3 = parse_security("", xml3, is_new=True)
    print("Result with EntryPoint Grant/Read:", result3)

    # Test case where Access tag is missing but name implies it
    xml4 = """
<AxSecurityPrivilege>
	<Name>HSPlanningVarianceJournalBatchMaintain</Name>
	<TablePermissions>
		<AxSecurityTablePermission>
			<Name>HSPlanningVarianceJournalBatch</Name>
		</AxSecurityTablePermission>
	</TablePermissions>
</AxSecurityPrivilege>
    """
    result4 = parse_security("", xml4, is_new=True)
    print("Result with missing Access, relying on name hint:", result4)

if __name__ == "__main__":
    test_security_parsing()
