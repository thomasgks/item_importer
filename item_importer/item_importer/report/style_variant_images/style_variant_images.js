frappe.query_reports["Style Variant Images"] = {
	filters: [
		{
			fieldname: "brand",
			label: __("Brand"),
			fieldtype: "MultiSelectList",
			get_data: function (txt) {
				return frappe.db.get_link_options("Brand", txt);
			},
		},
		{
			fieldname: "season",
			label: __("Season"),
			fieldtype: "MultiSelectList",
			get_data: function (txt) {
				return frappe.xcall(
					"item_importer.item_importer.report.style_variant_images.style_variant_images.get_seasons",
					{ txt: txt || "" }
				);
			},
		},
		{ fieldname: "style", label: __("Style"), fieldtype: "Data" },
		{ fieldname: "item_code", label: __("Item Code"), fieldtype: "Data" },
		{ fieldname: "attribute_value", label: __("Colour Code / Name"), fieldtype: "Data" },
		{ fieldname: "item_group", label: __("Item Group"), fieldtype: "Link", options: "Item Group" },
		{ fieldname: "published", label: __("Published"), fieldtype: "Select", options: "\nYes\nNo" },
		{ fieldname: "missing_images", label: __("Missing Images Only"), fieldtype: "Check" },
	],
};
