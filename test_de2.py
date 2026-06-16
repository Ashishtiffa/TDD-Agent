
from ax_xml_parser import parse_data_entity

def test_de_ext():
    diff = """
@@ -12,2 +12,8 @@
 	<Fields>
+		<AxDataEntityViewExtensionMappedField xmlns="" i:type="AxDataEntityViewExtensionMappedField">
+			<Name>MyCustomField</Name>
+			<DataField>MyCustomField</DataField>
+			<DataSource>DataSourceName</DataSource>
+		</AxDataEntityViewExtensionMappedField>
 	</Fields>
    """
    res = parse_data_entity(diff, "", is_new=False, diff_mode=True)
    print("Result:", res)

if __name__ == "__main__":
    test_de_ext()
