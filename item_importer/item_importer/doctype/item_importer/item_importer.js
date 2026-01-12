// item_importer/item_importer/doctype/item_importer/item_importer.js
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
                        frappe.show_alert({ message: __("Import started in background"), indicator: "green" });
                        frm.reload_doc();
                    }
                });
            });
        }

        frappe.realtime.on("item_import_progress", function (data) {
            if (data.docname === frm.doc.name) {
                frm.set_value("progress", data.progress);
                frm.refresh_field("progress");
            }
        });
    }
});