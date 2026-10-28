frappe.ui.form.on("Item Importer", {

    refresh(frm) {
        if (!frm.is_new()) {
            // ── Start Import ─────────────────────────────────────────────────
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

            // ── Export Failed Rows ───────────────────────────────────────────
            // Show the button only when there are known failures
            const has_failures = ["Partially Completed", "Failed"].includes(frm.doc.status)
                || (frm.doc.last_log); // also show whenever a log exists so user can always export

            if (has_failures) {
                frm.add_custom_button(__("Export Failed Rows"), function () {
                    frappe.show_alert({ message: __("Building failed-rows file…"), indicator: "blue" });
                    frappe.call({
                        method: "item_importer.item_importer.doctype.item_importer.item_importer.export_failed_rows",
                        args: { docname: frm.doc.name },
                        freeze: true,
                        freeze_message: __("Generating failed rows export..."),
                        callback: function (r) {
                            if (r.exc) return;
                            const file_url = r.message;
                            frappe.msgprint({
                                title: __("Export Ready"),
                                message: `Failed rows exported. <a href="${file_url}" target="_blank">Click here to download</a>`,
                                indicator: "green",
                            });
                        },
                    });
                }, __("Tools"));
            }
        }

        if (!frm._listeners_bound) {
            frm._listeners_bound = true;

            // ── Live progress updates ────────────────────────────────────────
            frappe.realtime.on("item_import_progress", function (data) {
                if (data.docname !== frm.doc.name) return;
                frm.set_value("progress", data.progress);
                frm.refresh_field("progress");
                // If the server already set the final status, reflect it immediately
                if (data.status && data.status !== frm.doc.status) {
                    frm.set_value("status", data.status);
                    frm.refresh_field("status");
                }
            });

            // ── Import complete ──────────────────────────────────────────────
            frappe.realtime.on("item_import_complete", function (data) {
                if (data.docname !== frm.doc.name) return;
                frm.reload_doc();
                frappe.msgprint({
                    title: __("Import Complete"),
                    message: `Total: ${data.total} | Success: ${data.success} | Failed: ${data.failed}`,
                    indicator: data.failed > 0 ? "orange" : "green",
                });
            });

            // ── Rename complete ──────────────────────────────────────────────
            frappe.realtime.on("rename_template_complete", function (data) {
                if (data.docname !== frm.doc.name) return;
                frm.reload_doc();
                frappe.msgprint({
                    title: __("Rename Complete"),
                    message: `Total: ${data.total} | Success: ${data.success} | Failed: ${data.failed}`,
                    indicator: data.failed > 0 ? "orange" : "green",
                });
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