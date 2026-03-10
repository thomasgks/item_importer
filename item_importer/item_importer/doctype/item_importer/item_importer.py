# item_importer/item_importer/doctype/item_importer/item_importer.py
import frappe
from frappe.model.document import Document
from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file
from frappe.utils import now_datetime


class ItemImporter(Document):
    pass


@frappe.whitelist()
def start_import(docname):
    frappe.logger().info("✅ importing started")
    doc = frappe.get_doc("Item Importer", docname)
    if not doc.import_file:
        frappe.throw("Please attach an Excel file before starting import.")

    doc.status = "In Progress"
    doc.progress = 0
    doc.save(ignore_permissions=True)
    frappe.logger().info("✅ Importing job added to enqueue...")
    frappe.enqueue(
        "item_importer.item_importer.doctype.item_importer.item_importer.run_import",
        queue="long",
        timeout=3600,
        docname=docname,
    )


def run_import(docname):
    doc = frappe.get_doc("Item Importer", docname)
    frappe.logger().info("✅ Background job started")
    try:
        file_url = doc.import_file
        if not file_url:
            frappe.throw("No file attached.")

        rows = read_xlsx_file_from_attached_file(file_url=file_url) or []
    except Exception:
        doc.status = "Failed"
        doc.save(ignore_permissions=True)
        frappe.logger().error(f"❌ Item Importer File Error")
        frappe.log_error(
            title="Item Importer File Error", message=frappe.get_traceback()
        )
        return

    if len(rows) < 3:
        doc.status = "Failed"
        doc.save(ignore_permissions=True)
        frappe.logger().error(
            f"❌ Excel must have at least 3 rows (fieldnames, descriptions, data)"
        )
        frappe.throw(
            "Excel must have at least 3 rows (fieldnames, descriptions, data)."
        )

    header = rows[0]
    data_rows = rows[2:]  # skip row 1 (fieldnames) and row 2 (descriptions)

    col_index = {str(h).strip(): idx for idx, h in enumerate(header)}

    mandatory_fields = [
        "custom_brand_code",
        "brand",
        "Group-Level1",
        "Group-Level2",
        "Group-Level3",
        "Group-Level4",
        "Group-Level5",
        "item_code",
        "item_name",
        "supplier_items.supplier",
        "MRP",
        "RSP",
        "is_stock_item",
        "is_sales_item",
    ]

    variant_mandatory = ["variant_of", "Color", "Size", "Year", "Season", "Color Name"]

    log_doc = frappe.get_doc(
        {
            "doctype": "Item Import Log",
            "item_importer": doc.name,
            "status": "In Progress",
        }
    )
    log_doc.insert(ignore_permissions=True)
    doc.last_log = log_doc.name
    doc.save(ignore_permissions=True)

    total = len(data_rows)
    frappe.logger().info(f"✅ total rows={total}")
    processed = 0
    commit_interval = 50
    success_count = 0
    failure_count = 0

    for row_idx, row in enumerate(data_rows, start=3):
        row_data = _row_to_dict(row, col_index)
        row_data = sanitize_row(row_data)
        row_status = "Success"
        failure_reason = ""
        frappe.logger().info(
            f"✅ Importing  row={row_idx}, item={row_data.get("item_code")}"
        )

        try:
            _validate_mandatory(row_data, mandatory_fields, row_idx)
            is_variant = bool(row_data.get("variant_of"))
            if is_variant:
                _validate_mandatory(row_data, variant_mandatory, row_idx)

            item_group_name = _ensure_item_group_hierarchy(row_data)
            brand_name = _ensure_brand(row_data)
            supplier_name = _ensure_supplier(row_data)
            attributes = _ensure_attributes(row_data)
            item_doc = _ensure_item(
                row_data, item_group_name, brand_name, supplier_name, attributes
            )
            _ensure_barcodes(item_doc, row_data)
            _ensure_item_prices(item_doc, row_data)

            success_count += 1
            frappe.logger().info(
                f"✅ Imported successfully  row={row_idx}, item={row_data.get("item_code")}"
            )

        except Exception:
            row_status = "Failed"
            failure_reason = frappe.get_traceback()
            failure_count += 1
            frappe.logger().error(f"❌Importer Error {failure_reason}")

        log_doc.append(
            "entries",
            {
                "row_no": row_idx,
                "row_data": frappe.as_json(row_data),
                "status": row_status,
                "failure_reason": failure_reason,
            },
        )

        processed += 1

        if processed % commit_interval == 0 or processed == total:
            log_doc.save(ignore_permissions=True)
            frappe.db.commit()

        progress = int(processed * 100 / total)
        doc.progress = progress
        doc.save(ignore_permissions=True)
        frappe.publish_realtime(
            event="item_import_progress",
            message={"progress": progress, "docname": doc.name},
            user=doc.owner,
        )

    log_doc.status = "Completed"
    log_doc.success_count = success_count
    log_doc.failure_count = failure_count
    log_doc.save(ignore_permissions=True)

    doc.status = "Completed" if failure_count == 0 else "Failed"
    doc.save(ignore_permissions=True)
    # Create Purchase Order if enabled
    frappe.logger().error(f"❌ Creating PO")
    doc.reload()
    if doc.create_purchase_order:
        try:
            _create_purchase_order(log_doc)
        except Exception:
            frappe.log_error(
                title="Item Importer PO Error", message=frappe.get_traceback()
            )

    frappe.db.commit()


def _create_purchase_order(log_doc):
    """
    Create Purchase Orders grouped by supplier.
    Each supplier gets one PO with all items belonging to them.
    Supports multi-currency with currency and exchange_rate fields.
    """
    from frappe.utils import nowdate

    supplier_map = {}  # {supplier: {currency: {rate: rate, items: []}}}

    for entry in log_doc.entries:
        if entry.status != "Success":
            continue

        row = frappe.parse_json(entry.row_data)

        supplier = row.get("supplier_items.supplier")
        item_code = str(row.get("item_code")).strip()
        qty = float(row.get("po_qty") or 0)
        price_list_rate = float(row.get("po_price") or 0)
        rate = float(row.get("po_price") or 0)
        currency = (
            row.get("currency")
            or frappe.defaults.get_global_default("currency")
            or "SAR"
        )
        exchange_rate = float(row.get("exchange_rate") or 1.0)
        set_warehouse = "Main Warehouse - MAATC"

        if not supplier or not item_code or qty <= 0:
            continue

        # Group by supplier and currency
        key = (supplier, currency)
        if key not in supplier_map:
            supplier_map[key] = {"exchange_rate": exchange_rate, "items": []}

        supplier_map[key]["items"].append(
            {
                "item_code": item_code,
                "qty": qty,
                "rate": rate,
                "price_list_rate": price_list_rate,
            }
        )

    frappe.logger().error(f"❌ Inserting PO")

    # Create PO for each supplier-currency combination
    for (supplier, currency), data in supplier_map.items():
        po = frappe.get_doc(
            {
                "doctype": "Purchase Order",
                "supplier": supplier,
                "transaction_date": nowdate(),
                "schedule_date": nowdate(),
                "currency": currency,
                "conversion_rate": data["exchange_rate"],
                "set_warehouse": set_warehouse,
                "items": [],
            }
        )

        for it in data["items"]:
            po.append(
                "items",
                {
                    "item_code": it["item_code"],
                    "qty": it["qty"],
                    "price_list_rate": it["price_list_rate"],
                    "rate": it["rate"],
                    "schedule_date": nowdate(),
                },
            )

        frappe.logger().error(f"❌ Creating PO {po.supplier} with currency {currency}")
        po.insert(ignore_permissions=True)
        # po.submit()


def _row_to_dict(row, col_index):
    data = {}
    for fieldname, idx in col_index.items():
        data[fieldname] = row[idx] if idx < len(row) else None
    return data


def sanitize_row(row_data):
    for key, value in row_data.items():
        if isinstance(value, int) or isinstance(value, float):
            row_data[key] = str(value)
        elif value is None:
            row_data[key] = ""
        else:
            row_data[key] = str(value).strip()
    return row_data


def _validate_mandatory(row_data, fields, row_idx):
    missing = [f for f in fields if not row_data.get(f)]
    if missing:
        frappe.throw(f"Row {row_idx}: Missing mandatory fields: {', '.join(missing)}")


def _ensure_item_group_hierarchy(row_data):
    lvl1 = row_data.get("Group-Level1")
    lvl2 = row_data.get("Group-Level2")
    lvl3 = row_data.get("Group-Level3")
    lvl4 = row_data.get("Group-Level4")
    lvl5 = row_data.get("Group-Level5")

    if not all([lvl1, lvl2, lvl3, lvl4, lvl5]):
        frappe.throw("Item Group levels 1-5 are required.")

    def get_or_create_group(name, parent_item_group, is_group):
        existing = frappe.db.get_value("Item Group", {"item_group_name": name})
        if existing:
            return existing
        doc = frappe.get_doc(
            {
                "doctype": "Item Group",
                "item_group_name": name,
                "parent_item_group": (
                    "All Item Groups" if not parent_item_group else parent_item_group
                ),
                "is_group": 1 if is_group else 0,
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name

    name1 = lvl1
    ig1 = get_or_create_group(name1, None, True)

    name2 = f"{lvl1}.{lvl2}"
    ig2 = get_or_create_group(name2, ig1, True)

    name3 = f"{lvl1}.{lvl2}.{lvl3}"
    ig3 = get_or_create_group(name3, ig2, True)

    name4 = f"{lvl1}.{lvl2}.{lvl3}.{lvl4}"
    ig4 = get_or_create_group(name4, ig3, True)

    name5 = f"{lvl1}.{lvl2}.{lvl3}.{lvl4}.{lvl5}"
    ig5 = get_or_create_group(name5, ig4, False)

    return ig5


def _ensure_brand(row_data):
    brand_code = row_data.get("custom_brand_code")
    brand_name = row_data.get("brand")
    if not brand_name:
        frappe.throw("Brand is mandatory.")

    existing = frappe.db.get_value("Brand", {"brand": brand_name})
    if existing:
        if brand_code:
            frappe.db.set_value("Brand", existing, "custom_brand_code", brand_code)
        return existing

    doc = frappe.get_doc(
        {"doctype": "Brand", "brand": brand_name, "custom_brand_code": brand_code}
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_supplier(row_data):
    supplier_name = row_data.get("supplier_items.supplier")
    if not supplier_name:
        frappe.throw("Supplier is mandatory.")

    existing = frappe.db.get_value("Supplier", {"name": supplier_name})
    if existing:
        return existing

    doc = frappe.get_doc(
        {
            "doctype": "Supplier",
            "supplier_name": supplier_name,
            "supplier_group": "All Supplier Groups",
            "supplier_type": "Company",
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_attributes(row_data):
    attr_map = {
        "Color": "Color",
        "Size": "Size",
        "Year": "Year",
        "Season": "Season",
        "Color Name": "Color Name",
    }
    result = {}
    for field, attr_name in attr_map.items():
        value = row_data.get(field)
        if not value:
            continue
        attr = _get_or_create_attribute(attr_name)
        _get_or_create_attribute_value(attr, value)
        result[field] = value
    return result


def _get_or_create_attribute(attribute_name):
    existing = frappe.db.get_value("Item Attribute", {"attribute_name": attribute_name})
    if existing:
        return existing
    doc = frappe.get_doc(
        {
            "doctype": "Item Attribute",
            "attribute_name": attribute_name,
            "item_attribute_values": [],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _get_or_create_attribute_value(attribute_name, value):
    exists = frappe.db.get_value(
        "Item Attribute Value", {"parent": attribute_name, "attribute_value": value}
    )
    if exists:
        return exists
    attr_doc = frappe.get_doc("Item Attribute", attribute_name)
    attr_doc.append(
        "item_attribute_values", {"attribute_value": value, "abbr": str(value)[:10]}
    )
    attr_doc.save(ignore_permissions=True)
    return value


def _ensure_item(row_data, item_group_name, brand_name, supplier_name, attributes):
    item_code = row_data.get("item_code")
    variant_of = row_data.get("variant_of")
    item_name = row_data.get("item_name")

    # existing = frappe.db.get_value("Item", {"item_code": item_code})
    existing = frappe.db.get_value(
        "Item", filters={"item_code": ["=", item_code]}, fieldname="name"
    )
    if existing:
        return frappe.get_doc("Item", existing)

    is_variant = bool(variant_of)

    common_fields = {
        "doctype": "Item",
        "item_code": item_code,
        "item_name": item_name,
        "item_group": item_group_name,
        "brand": brand_name,
        "is_stock_item": int(row_data.get("is_stock_item") or 0),
        "disabled": int(row_data.get("disabled") or 0),
        "is_sales_item": int(row_data.get("is_sales_item") or 1),
        "description": row_data.get("description"),
        "custom_item_name_arabic": row_data.get("custom_item_name_arabic"),
        "custom_model_no": row_data.get("custom_model_no"),
        "custom_new_item_code": row_data.get("custom_new_item_code"),
        "custom_main_brand": row_data.get("custom_main_brand"),
        "custom_product_orgin": row_data.get("custom_product_orgin"),
        "custom_supplier_account": row_data.get("custom_supplier_account"),
        "custom_product_type": row_data.get("custom_product_type"),
        "custom_material": row_data.get("custom_material"),
        "custom_season_type": row_data.get("custom_season_type"),
        "custom_shipment": row_data.get("custom_shipment"),
        "custom_purchase_date": row_data.get("custom_purchase_date"),
        "custom_year": row_data.get("custom_year"),
        "custom_dcs": row_data.get("custom_dcs"),
        "custom_dcs_name": row_data.get("custom_dcs_name"),
        "custom_packing": row_data.get("custom_packing"),
        "custom_fit": row_data.get("custom_fit"),
        "custom_fit_description": row_data.get("custom_fit_description"),
        "custom_theme": row_data.get("custom_theme"),
        "custom_theme_description": row_data.get("custom_theme_description"),
        "valuation_rate": row_data.get("valuation_rate"),
        "custom_last_synced": now_datetime(),
        "supplier_items": [{"supplier": supplier_name}],
    }

    if is_variant:
        template_code = variant_of
        # template = frappe.db.get_value("Item", {"item_code": template_code})
        # Force exact match to avoid MariaDB auto-conversion
        template = frappe.db.get_value(
            "Item", filters={"item_code": ["=", template_code]}, fieldname="name"
        )
        frappe.logger().info(
            f"✅ Template item={row_data.get("item_code")} = {template}"
        )
        if not template:
            template_doc = frappe.get_doc(common_fields.copy())
            template_doc.item_code = template_code
            template_doc.item_name = template_code
            template_doc.has_variants = 1
            template_doc.variant_based_on = "Item Attribute"
            template_doc.attributes = []
            for field, value in attributes.items():
                template_doc.append(
                    "attributes", {"attribute": field, "numeric_values": 0}
                )
            template_doc.insert(ignore_permissions=True)
        else:
            template_doc = frappe.get_doc("Item", template)

        item_doc = frappe.get_doc(common_fields.copy())
        item_doc.variant_of = template_doc.name
        item_doc.has_variants = 0
        item_doc.attributes = []
        for field, value in attributes.items():
            item_doc.append(
                "attributes", {"attribute": field, "attribute_value": value}
            )
        item_doc.insert(ignore_permissions=True)
        return item_doc

    item_doc = frappe.get_doc(common_fields)
    item_doc.insert(ignore_permissions=True)
    return item_doc


def _ensure_barcodes(item_doc, row_data):
    barcode = row_data.get("barcodes.barcode") or row_data.get("item_code")
    if not barcode:
        return
    for b in item_doc.get("barcodes", []):
        if b.barcode == barcode:
            return
    item_doc.append("barcodes", {"barcode": barcode})
    item_doc.save(ignore_permissions=True)


def _ensure_item_prices(item_doc, row_data):
    price_map = {"MRP": "MRP", "RSP": "RSP", "WSP": "WSP", "STAFF": "STAFF"}
    currency = frappe.defaults.get_global_default("currency") or "SAR"
    for field, price_list in price_map.items():
        rate = row_data.get(field)
        if not rate:
            continue
        existing = frappe.db.get_value(
            "Item Price", {"item_code": item_doc.item_code, "price_list": price_list}
        )
        if existing:
            frappe.db.set_value("Item Price", existing, "price_list_rate", rate)
            continue
        ip = frappe.get_doc(
            {
                "doctype": "Item Price",
                "item_code": item_doc.item_code,
                "price_list": price_list,
                "price_list_rate": rate,
                "currency": currency,
            }
        )
        ip.insert(ignore_permissions=True)
