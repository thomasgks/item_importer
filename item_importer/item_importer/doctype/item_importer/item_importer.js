frappe.ui.form.on("Item Importer", {

    refresh(frm) {
        if (!frm.is_new()) {
            frm.add_custom_button(__("Start Import"), function () {
                frappe.call({
                    method: "item_importer.item_importer.doctype.item_importer.item_importer.start_import",
                    args: { docname: frm.doc.name },
                    freeze: true,
                    freeze_message: __("Queuing import..."),
                    callback: function () {
                        frappe.show_alert({
                            message: __("Import started in background"),
                            indicator: "green",
                        });
                        frm.reload_doc();
                    },
                });
            });
        }

        if (!frm._listeners_bound) {
            frm._listeners_bound = true;

            frappe.realtime.on("item_import_progress", function (data) {
                if (data.docname === frm.doc.name) {
                    frm.set_value("progress", data.progress);
                    frm.refresh_field("progress");
                }
            });

            frappe.realtime.on("rename_template_complete", function (data) {
                if (data.docname === frm.doc.name) {
                    frm.reload_doc();
                    frappe.msgprint({
                        title: __("Rename Complete"),
                        message: `Total: ${data.total} | Success: ${data.success} | Failed: ${data.failed}`,
                        indicator: data.failed > 0 ? "orange" : "green",
                    });
                }
            });
        }
    },

    custom_rename(frm) {
        if (frm._confirming) return;
        if (!frm.doc.custom_select_file) {
            frappe.msgprint(__("Please attach a CSV/Excel file first."));
            return;
        }
        frm._confirming = true;
        frappe.confirm(
            __("Are you sure you want to rename the templates in the attached file?"),
            function () {
                frm._confirming = false;
                frappe.call({
                    method: "item_importer.item_importer.doctype.item_importer.rename_template.start_rename",
                    args: { docname: frm.doc.name },
                    freeze: true,
                    freeze_message: __("Queuing rename job..."),
                    callback(r) {
                        if (!r.exc) {
                            frappe.show_alert({
                                message: __("Rename job started in background."),
                                indicator: "green",
                            });
                        }
                    },
                });
            },
            function () {
                frm._confirming = false;
            }
        );
    },
});