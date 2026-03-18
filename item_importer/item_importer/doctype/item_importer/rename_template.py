import frappe


@frappe.whitelist()
def start_rename(docname):
    doc = frappe.get_doc("Item Importer", docname)
    if not doc.custom_select_file:
        frappe.throw("Please attach a CSV or Excel file before renaming.")

    doc.save(ignore_permissions=True)
    frappe.enqueue(
        "item_importer.item_importer.doctype.item_importer.rename_template.run_rename",
        queue="long",
        timeout=3600,
        docname=docname,
    )
    return "Rename job queued successfully."

def run_rename(docname):
    import time
    from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

    doc = frappe.get_doc("Item Importer", docname)
    
    # ── Set status to In Progress ──────────────────────────────────────────
    doc.status = "In Progress"
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    file_url = doc.custom_select_file

    try:
        if file_url.endswith(".csv"):
            import csv
            file_path = frappe.get_site_path() + "/public" + file_url
            with open(file_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                rows = [r for r in reader]
        else:
            raw_rows = read_xlsx_file_from_attached_file(file_url=file_url) or []
            if len(raw_rows) < 2:
                frappe.throw("File must have a header row and at least one data row.")
            header = [str(h).strip() for h in raw_rows[0]]
            rows = []
            for raw in raw_rows[1:]:
                row = {header[i]: (str(raw[i]).strip() if i < len(raw) and raw[i] is not None else "") for i in range(len(header))}
                rows.append(row)
    except Exception:
        doc.reload()
        doc.status = "Failed"
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.log_error(title="Rename Template File Error", message=frappe.get_traceback())
        return

    results = []
    success = 0
    failed = 0

    for row in rows:
        old_id = str(row.get("old_id") or "").strip()
        new_id = str(row.get("new_id") or "").strip()

        if not old_id or not new_id:
            results.append({"old_id": old_id, "new_id": new_id, "status": "Failed", "reason": "Missing old_id or new_id"})
            failed += 1
            continue

        if old_id == new_id:
            results.append({"old_id": old_id, "new_id": new_id, "status": "Skipped", "reason": "old_id and new_id are the same"})
            continue

        try:
            if not frappe.db.exists("Item", old_id):
                results.append({"old_id": old_id, "new_id": new_id, "status": "Failed", "reason": f"Item {old_id} does not exist"})
                failed += 1
                continue

            is_template = frappe.db.get_value("Item", old_id, "has_variants")
            if not is_template:
                results.append({"old_id": old_id, "new_id": new_id, "status": "Failed", "reason": f"{old_id} is not a template (has_variants=0)"})
                failed += 1
                continue

            if frappe.db.exists("Item", new_id):
                results.append({"old_id": old_id, "new_id": new_id, "status": "Failed", "reason": f"Item {new_id} already exists"})
                failed += 1
                continue

            frappe.rename_doc("Item", old_id, new_id, force=True)
            frappe.db.commit()

            variants = frappe.get_all("Item", filters={"variant_of": old_id}, pluck="name")
            for variant in variants:
                frappe.db.set_value("Item", variant, "variant_of", new_id, update_modified=False)

            frappe.db.sql("""
                UPDATE `tabItem Variant Attribute`
                SET parent = %s
                WHERE parent = %s
            """, (new_id, old_id))

            frappe.db.commit()

            results.append({
                "old_id": old_id,
                "new_id": new_id,
                "status": "Success",
                "reason": f"Renamed. {len(variants)} variant(s) updated.",
            })
            success += 1

        except Exception:
            frappe.db.rollback()
            tb = frappe.get_traceback()
            results.append({"old_id": old_id, "new_id": new_id, "status": "Failed", "reason": tb[:500]})
            failed += 1
            frappe.log_error(title=f"Rename Template Error: {old_id}", message=tb)

    # ── Save results and set final status ─────────────────────────────────
    doc.reload()
    doc.custom_rename_log = frappe.as_json(results)
    doc.status = "Completed" if failed == 0 else "Partially Completed"
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    frappe.publish_realtime(
        event="rename_template_complete",
        message={
            "docname": docname,
            "success": success,
            "failed": failed,
            "total": len(rows),
        },
        user=doc.owner,
    )
    frappe.logger().info(f"✅ Rename complete. Success: {success}, Failed: {failed}")
    
    
    